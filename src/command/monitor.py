import asyncio
from email.utils import format_datetime
from json import loads, dumps
from typing import Union, MutableMapping, Final
from telethon.errors.rpcerrorlist import UserIsBlockedError, ChatWriteForbiddenError, UserIdInvalidError

from . import inner
from .utils import escape_html
from .inner.utils import get_hash, update_interval, deactivate_feed
from src import log, db, env
from src.i18n import i18n
from src.parsing.post import get_post_from_entry, Post
from src.web import feed_get

logger = log.getLogger('RSStT.monitor')

NOT_UPDATED: Final = 'not_updated'
CACHED: Final = 'cached'
EMPTY: Final = 'empty'
FAILED: Final = 'failed'
UPDATED: Final = 'updated'


class MonitoringLogs:
    monitoring_counts = 0
    not_updated = 0
    cached = 0
    empty = 0
    failed = 0
    updated = 0

    @classmethod
    def log(cls, not_updated: int, cached: int, empty: int, failed: int, updated: int):
        cls.not_updated += not_updated
        cls.cached += cached
        cls.empty += empty
        cls.failed += failed
        cls.updated += updated
        logger.debug(f'Finished feeds monitoring task: '
                     f'updated({updated}), '
                     f'not updated({not_updated}, including {cached} cached and {empty} empty), '
                     f'fetch failed({failed})')
        cls.monitoring_counts += 1
        if cls.monitoring_counts == 10:
            cls.print_summary()

    @classmethod
    def print_summary(cls):
        logger.info(
            f'Monitoring tasks summary in last 10 minutes: '
            f'updated({cls.updated}), '
            f'not updated({cls.not_updated}, including {cls.cached} cached and {cls.empty} empty), '
            f'fetch failed({cls.failed})'
        )
        cls.not_updated = cls.cached = cls.empty = cls.failed = cls.updated = 0
        cls.monitoring_counts = 0


async def run_monitor_task():
    feed_id_to_monitor = db.effective_utils.EffectiveTasks.get_tasks()
    if not feed_id_to_monitor:
        return

    feeds = await db.Feed.filter(id__in=feed_id_to_monitor)

    logger.debug('Started feeds monitoring task.')

    result = await asyncio.gather(*(__monitor(feed) for feed in feeds))

    not_updated = 0
    cached = 0
    empty = 0
    failed = 0
    updated = 0

    for r in result:
        if r is NOT_UPDATED:
            not_updated += 1
        elif r is CACHED:
            not_updated += 1
            cached += 1
        elif r is EMPTY:
            not_updated += 1
            empty += 1
        elif r is UPDATED:
            updated += 1
        elif r is FAILED:
            failed += 1

    MonitoringLogs.log(not_updated, cached, empty, failed, updated)


async def __monitor(feed: db.Feed) -> str:
    """
    Monitor the update of a feed.

    :param feed: the feed object to be monitored
    :return: monitoring result
    """
    headers = {
        'If-Modified-Since': format_datetime(feed.last_modified or feed.updated_at)
    }
    if feed.etag:
        headers['If-None-Match'] = feed.etag

    d = await feed_get(feed.link, headers=headers)
    rss_d = d['rss_d']

    if (rss_d is not None or d['status'] == 304) and feed.error_count > 0:
        feed.error_count = 0
        await feed.save()

    if d['status'] == 304:  # cached
        logger.debug(f'Fetched (not updated, cached): {feed.link}')
        return CACHED

    if rss_d is None:  # error occurred
        if feed.error_count >= 100:
            logger.error(f'Deactivated feed due to too many errors: {feed.link}')
            await __deactivate_feed_and_notify_all(feed)
            return FAILED
        feed.error_count += 1
        await feed.save()
        return FAILED

    if not rss_d.entries:  # empty
        if d['url'] != feed.link:
            await inner.sub.migrate_to_new_url(feed, d['url'])
        logger.debug(f'Fetched (empty): {feed.link}')
        return EMPTY

    # sequence matters so we cannot use a set
    old_hashes = loads(feed.entry_hashes) if feed.entry_hashes else []
    updated_hashes = []
    updated_entries = []
    for entry in rss_d.entries:
        h = get_hash(entry.get('guid', entry['link']))
        if h in old_hashes:
            continue
        updated_hashes.append(h)
        updated_entries.append(entry)

    if not updated_hashes:  # not updated
        logger.debug(f'Fetched (not updated): {feed.link}')
        return NOT_UPDATED

    logger.info(f'Updated: {feed.link}')
    length = max(len(rss_d.entries) * 2, 20)
    new_hashes = updated_hashes + old_hashes[:length - len(updated_hashes)]
    feed.entry_hashes = dumps(new_hashes)
    http_caching_d = inner.utils.get_http_caching_headers(d['headers'])
    feed.etag = http_caching_d['ETag']
    feed.last_modified = http_caching_d['Last-Modified']
    await feed.save()

    if d['url'] != feed.link:
        new_url_feed = await inner.sub.migrate_to_new_url(feed, d['url'])
        feed = new_url_feed if isinstance(new_url_feed, db.Feed) else feed

    await asyncio.gather(*(__notify_all(feed, entry) for entry in updated_entries))

    return UPDATED


async def __notify_all(feed: db.Feed, entry: MutableMapping):
    subs = await db.Sub.filter(feed=feed, state=1)
    if not subs:  # nobody has sub it
        await update_interval(feed)
    post = get_post_from_entry(entry, feed.title, feed.link)
    await post.generate_message()
    await asyncio.gather(
        *(__send(sub, post) for sub in subs)
    )


async def __send(sub: db.Sub, post: Union[str, Post]):
    # TODO: customized format
    try:
        if isinstance(post, str):
            await env.bot.send_message(sub.user_id, post, parse_mode='html')
            return
        await post.send_message(sub.user_id)
    except (UserIsBlockedError, UserIdInvalidError, ChatWriteForbiddenError):
        logger.warning(f'User blocked: {sub.user_id}')
        await inner.sub.unsub_all(sub.user_id)


async def __deactivate_feed_and_notify_all(feed: db.Feed):
    await deactivate_feed(feed)

    subs = await db.Sub.filter(feed=feed, state=1).prefetch_related('user')
    if not subs:  # nobody has sub it
        return

    await asyncio.gather(
        *(
            __send(
                sub=sub,
                post=(
                        f'<a href="{feed.link}">{escape_html(sub.title or feed.title)}</a>\n'
                        + i18n[sub.user.lang]['feed_deactivated_warn']
                )
            )
            for sub in subs)
    )
