'''
    Contract 1.6.0 (MED-809, MediaNest ADR-0036): pausing a source.

    A paused source is an ordinary TubeSync source with index_schedule
    NEVER and download_media off: TubeSync's own index task and
    schedule_indexing() skip a source that is not is_active, and
    Media.download_checklist() turns a queued download into a no-op when
    its source has download_media off. Nothing in TubeSync itself changes.

    Telling a paused source apart, with no new model (the fork adds no
    migrations, see docs/migration-upgrade-proof.md):

    - a source being deleted (deletion.py) also has index_schedule NEVER,
      but deletion turns index_videos off too, so a source that still
      indexes videos or streams is never the hidden row a deletion
      leaves;
    - a video list (videolist.py) also has index_schedule NEVER, but it
      indexes nothing, and its reserved key tells it apart anyway;
    - a source whose download and index flags are all off is TubeSync's
      own "disabled", and stays `disabled`.

    So `paused` is: not a video list, index_schedule NEVER, and index
    videos or streams still on. That includes a source set to "never" in
    TubeSync's own UI, which is honestly paused too.

    What resume brings back (the schedule and download setting the source
    had) is kept in a small JSON record per source under the config
    directory, next to TubeSync's database, so it is backed up and
    restored with it. The record is written before the source changes and
    removed after resume, so a crash in between leaves at most a record
    for a source that is not paused, which nothing reads: pausing again
    overwrites it. A missing or unreadable record falls back to the
    bridge's default schedule and downloads on.
'''
import json
import os
import tempfile
from datetime import datetime, timezone as dt_timezone
from pathlib import Path

from django.conf import settings
from django.db import transaction

from common.logger import log
from sync.choices import IndexSchedule, Val
from sync.models import Source

from . import config
from .sync_dedup import schedule_sync_now_index
from .videolist import is_videolist

# The schedules a caller may set (contract UpdateSourceRequest.indexSchedule):
# every TubeSync IndexSchedule except NEVER, which is what pause is for.
ALLOWED_SCHEDULES = tuple(
    value for value in IndexSchedule.values if value != Val(IndexSchedule.NEVER)
)
DEFAULT_SCHEDULE = Val(IndexSchedule.EVERY_24_HOURS)

RECORD_DIR_PARTS = ('medianest-bridge', 'paused')


def record_dir():
    return Path(settings.CONFIG_BASE_DIR).joinpath(*RECORD_DIR_PARTS)


def _record_path(source):
    return record_dir() / f'{source.pk}.json'


def is_paused(source):
    if is_videolist(source):
        return False
    if source.index_schedule != Val(IndexSchedule.NEVER):
        return False
    return bool(source.index_videos or source.index_streams)


def read_record(source):
    '''The stored pre-pause settings, or None when there is no usable record.'''
    path = _record_path(source)
    try:
        raw = json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        log.warning('medianest_bridge: unreadable pause record for source %s', source.pk)
        return None
    if not isinstance(raw, dict):
        return None
    record = {}
    schedule = raw.get('indexSchedule')
    if isinstance(schedule, int) and not isinstance(schedule, bool) and schedule in ALLOWED_SCHEDULES:
        record['indexSchedule'] = schedule
    if isinstance(raw.get('downloadMedia'), bool):
        record['downloadMedia'] = raw['downloadMedia']
    paused_at = raw.get('pausedAt')
    if isinstance(paused_at, str):
        record['pausedAt'] = paused_at
    return record


def _write_record(source, record):
    '''Atomic write: a reader sees the old record or the new one, never half.'''
    directory = record_dir()
    directory.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=directory, prefix='.tmp-', suffix='.json')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            json.dump(record, handle)
        os.replace(tmp_name, _record_path(source))
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _remove_record(source):
    try:
        _record_path(source).unlink()
    except FileNotFoundError:
        pass
    except OSError:
        # A leftover record for a source that is not paused is never read
        # (see the module docstring), so this is not worth failing resume.
        log.warning('medianest_bridge: could not remove pause record for source %s', source.pk)


def _now_iso():
    return datetime.now(dt_timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z'


def default_schedule(source):
    '''
        The schedule resume uses when no record says otherwise: the one
        MEDIANEST_BRIDGE_SOURCE_DEFAULTS gives new sources of this type,
        else every 24 hours (TubeSync's own model default).
    '''
    from .mapping import map_source_type
    defaults_by_type, errors = config.load_validated_source_defaults(
        source_types=(map_source_type(source.source_type),),
    )
    if defaults_by_type and not errors:
        overlay = defaults_by_type.get(map_source_type(source.source_type)) or {}
        try:
            value = int(overlay.get('index_schedule', DEFAULT_SCHEDULE))
        except (TypeError, ValueError):
            value = DEFAULT_SCHEDULE
        if value in ALLOWED_SCHEDULES:
            return value
    return DEFAULT_SCHEDULE


def settings_for(source):
    '''Contract 1.6.0 Source.settings.'''
    if is_videolist(source):
        return {
            'paused': False,
            'pausedAt': None,
            'indexSchedule': None,
            'downloadMedia': bool(source.download_media),
        }
    if is_paused(source):
        record = read_record(source) or {}
        return {
            'paused': True,
            'pausedAt': record.get('pausedAt'),
            'indexSchedule': record.get('indexSchedule') or default_schedule(source),
            'downloadMedia': bool(source.download_media),
        }
    schedule = source.index_schedule
    return {
        'paused': False,
        'pausedAt': None,
        'indexSchedule': schedule if schedule in ALLOWED_SCHEDULES else None,
        'downloadMedia': bool(source.download_media),
    }


def apply_update(source_pk, *, paused=None, index_schedule=None):
    '''
        Applies an UpdateSourceRequest to the source, under a row lock so
        two concurrent calls apply one after the other. Returns the saved
        source. Callers validate the fields first.
    '''
    resumed = False
    with transaction.atomic():
        source = Source.objects.select_for_update().get(pk=source_pk)
        currently_paused = is_paused(source)
        if paused is True and not currently_paused:
            _pause(source, index_schedule=index_schedule)
        elif paused is True:
            if index_schedule is not None:
                _set_resume_schedule(source, index_schedule)
        elif paused is False and currently_paused:
            _resume(source, index_schedule=index_schedule)
            resumed = True
        elif index_schedule is not None:
            if currently_paused:
                _set_resume_schedule(source, index_schedule)
            elif source.index_schedule != index_schedule:
                source.index_schedule = index_schedule
                source.save(update_fields={'index_schedule'})
        if resumed:
            # Videos published while the source was paused are found by
            # the next index; ask for it now rather than after a whole
            # schedule period. Same dedup as POST /sources/{uuid}/sync.
            schedule_sync_now_index(source)
    if resumed:
        _remove_record(source)
    return Source.objects.get(pk=source_pk)


def _pause(source, *, index_schedule=None):
    previous = source.index_schedule
    if index_schedule is None:
        index_schedule = previous if previous in ALLOWED_SCHEDULES else default_schedule(source)
    _write_record(source, {
        'indexSchedule': index_schedule,
        'downloadMedia': bool(source.download_media),
        'pausedAt': _now_iso(),
    })
    source.index_schedule = Val(IndexSchedule.NEVER)
    source.download_media = False
    source.save(update_fields={'index_schedule', 'download_media'})
    log.info('medianest_bridge: paused source %s (resume schedule %s)', source.pk, index_schedule)


def _set_resume_schedule(source, index_schedule):
    record = read_record(source) or {}
    record['indexSchedule'] = index_schedule
    record.setdefault('downloadMedia', True)
    record.setdefault('pausedAt', None)
    _write_record(source, record)


def _resume(source, *, index_schedule=None):
    record = read_record(source) or {}
    if index_schedule is None:
        index_schedule = record.get('indexSchedule') or default_schedule(source)
    # A MediaNest source always downloads; with no record, assume it did.
    source.download_media = record.get('downloadMedia', True)
    source.index_schedule = index_schedule
    source.save(update_fields={'index_schedule', 'download_media'})
    log.info('medianest_bridge: resumed source %s (schedule %s)', source.pk, index_schedule)
