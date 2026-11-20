'''
    Contract 1.5.0 (MED-730, MediaNest ADR-0030): video-list sources.

    A video list is a TubeSync source that holds only the single videos
    MediaNest puts on it, one Plex show per MediaNest group ("Cooking:
    Single videos"). The fork adds no migrations (see
    docs/migration-upgrade-proof.md), so a video list is an ordinary
    playlist-type Source ('p') told apart by its reserved key prefix:

    - its key is `medianest-videolist-<suffix>`. No YouTube playlist ID
      contains "medianest-", and POST /sources refuses that prefix, so a
      real playlist can never be mistaken for one;
    - it never indexes: index_videos, index_streams off and
      index_schedule NEVER. TubeSync's index task and schedule_indexing()
      both skip a source that is not is_active, so nothing ever builds a
      URL from the fake key;
    - it still downloads (download_media on). Adding a video creates its
      Media row and schedules the same thumbnail and metadata tasks
      index_source() schedules for a newly indexed item. The metadata
      task sets `published`, and the save after it un-skips the media and
      schedules its download, exactly as for an indexed video;
    - channel images, removed-media cleanup, the download cap and file
      deletion on media delete are forced off, so nothing reaches YouTube
      for the list itself and a removed video only loses its files when
      the caller asks for that.
'''
import re

from django.db import transaction
from django_huey import lock_task as huey_lock_task
from huey.exceptions import TaskLockedException

from common.logger import log
from common.models import TaskHistory
from sync.choices import IndexSchedule, TaskQueue, Val, YouTube_SourceType
from sync.models import Media, Source

KEY_PREFIX = 'medianest-videolist-'


class VideoBusy(Exception):
    '''The video is being downloaded right now; removing it must wait.'''

# Lowercase letters, digits and hyphens; 100 is Source.key's max_length.
KEY_PATTERN = re.compile(r'^medianest-videolist-[a-z0-9][a-z0-9-]{0,79}$')

# A YouTube video ID: 11 characters of the URL-safe base64 alphabet.
VIDEO_ID_PATTERN = re.compile(r'^[A-Za-z0-9_-]{11}$')

# Applied over the playlist defaults overlay (MEDIANEST_BRIDGE_SOURCE_DEFAULTS)
# for every video list, whatever that overlay says.
FORCED_FIELDS = {
    'index_videos': False,
    'index_streams': False,
    'index_schedule': Val(IndexSchedule.NEVER),
    'download_media': True,
    'download_cap': 0,
    'copy_channel_images': False,
    'delete_removed_media': False,
    'delete_old_media': False,
    'delete_files_on_disk': False,
    'filter_text': '',
    'filter_seconds': None,
}

_THUMBNAIL_URL = 'https://i.ytimg.com/vi/{}/{}default.jpg'


def is_videolist_key(key):
    return isinstance(key, str) and key.startswith(KEY_PREFIX)


def is_videolist(source):
    return (
        source.source_type == Val(YouTube_SourceType.PLAYLIST)
        and is_videolist_key(source.key)
    )


def add_video(source, video_id):
    '''
        Puts `video_id` on the video list `source`. Returns (media, created).
        Adding a video already on the list changes nothing. The row is
        locked so two concurrent adds of the same video make one Media row
        and one set of tasks.
    '''
    with transaction.atomic():
        Source.objects.select_for_update().filter(pk=source.pk).first()
        media = Media.objects.filter(source=source, key=video_id).first()
        if media is not None and not media.manual_skip:
            return media, False
        if media is not None:
            # A row left skipped by hand (in TubeSync's UI): adding the
            # video again is the request to download it.
            media.manual_skip = False
            media.skip = False
            media.save()
            revived = True
        else:
            # The save fires media_post_save, which marks the row skipped
            # until it has a published date (sync/filtering.py), so it
            # schedules nothing itself. The tasks below fetch what it needs.
            media = Media(source=source, key=video_id, title='')
            media.save()
            revived = False
    _schedule_fetches(media)
    log.info(
        'medianest_bridge: video added to video list source=%s key=%s media=%s revived=%s',
        source.pk, video_id, media.pk, revived,
    )
    return media, True


def _schedule_fetches(media):
    '''The thumbnail and metadata tasks index_source() schedules for new media.'''
    # Imported here: source_forms imports this module for KEY_PREFIX, and
    # sync.tasks is heavy and best kept out of that import chain.
    from sync.tasks import download_media_image, download_media_metadata

    for num, prefix in enumerate(reversed(('hq', 'sd', 'maxres'))):
        download_media_image(
            str(media.pk),
            _THUMBNAIL_URL.format(media.key, prefix),
            priority=10 + (5 * num),
            delay=max(0, 65 - (30 * num)),
        )
    TaskHistory.schedule(
        download_media_metadata,
        str(media.pk),
        priority=download_media_metadata.settings.get('default_priority', 50) + 5,
        remove_duplicates=True,
        vn_fmt='Downloading metadata for: "{}": {}',
        vn_args=(media.key, media.name),
    )


def remove_video(source, video_id, *, delete_files):
    '''
        Takes `video_id` off the video list `source`. Returns False when it
        was not on the list. With delete_files the video's file and its
        sidecars (NFO, thumbnail, subtitles) go too; without it every file
        stays. media_post_delete reads delete_files_on_disk from the
        in-memory source of the row being deleted, so the flag is set on
        that instance only and never saved.

        The delete takes the same `media:<uuid>` lock download_media_file()
        holds while it downloads, and raises VideoBusy when that lock is
        held: a download that finished after the delete would save the
        row again and leave its file on disk. download_media_file()
        re-reads the row once it holds the lock, so a download that
        started before the delete but locked after it stops there.
    '''
    media = Media.objects.filter(source=source, key=video_id).select_related('source').first()
    if media is None:
        return False
    media.source.delete_files_on_disk = bool(delete_files)
    # skip + manual_skip stop media_post_delete from recreating the row as
    # a skipped placeholder (it keeps one so an index does not download a
    # deleted video again); a video list is never indexed, and the video
    # must be addable again later.
    media.skip = True
    media.manual_skip = True
    try:
        with huey_lock_task(f'media:{media.uuid}', queue=Val(TaskQueue.DB)):
            media.delete()
    except TaskLockedException as exc:
        raise VideoBusy(video_id) from exc
    log.info(
        'medianest_bridge: video removed from video list source=%s key=%s delete_files=%s',
        source.pk, video_id, bool(delete_files),
    )
    return True
