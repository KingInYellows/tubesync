'''
    Source deletion, moved out of DeleteSourceView.post (sync/views/sources.py)
    so the MediaNest bridge (medianest_bridge, DELETE /sources/{uuid}) runs
    TubeSync's own delete instead of a copy of it (MED-657, MediaNest
    ADR-0022). Fork delta: the body of prepare_source_for_deletion() is the
    upstream view body moved verbatim, plus the `keep_files` branch, which
    upstream's view never takes. See medianest_bridge/README.md "Fork delta".

    How TubeSync deletes a source: it clones the source into a hidden "media
    source" (no schedule, nothing downloads, filter_text = the original's
    uuid), moves every Media row to the clone, renames the original out of
    the way and deletes it. Deleting the original fires source_pre_delete
    (sync/signals.py), which schedules delete_all_media_for_source for the
    clone. That task deletes the media rows (each one firing
    media_post_delete), deletes the clone, and removes the download folder
    when the clone has delete_removed_media set or the folder holds a
    `.to_be_removed` marker.
'''
import pathlib

from django.db import transaction
from django.utils import timezone

from common.utils import mkdir_p

from .choices import IndexSchedule
from .models import Media, Source


def _with_affix(value, *, prefix='', suffix='', max_length=100):
    '''`prefix + value + suffix`, shortening `value` to fit max_length.'''
    room = max_length - len(prefix) - len(suffix)
    return f'{prefix}{value[:room]}{suffix}'


def prepare_source_for_deletion(source, *, delete_files, keep_files=False, bridge=False):
    '''
        Everything DeleteSourceView.post did before deleting the source.
        Returns the hidden media source. The caller deletes `source`.

        bridge=True (the MediaNest bridge's delete) differs from upstream's
        view so that no file moves and nothing is written into the folder:

        - the original is renamed without firing source_pre_save, which
          moves a source's folder whenever its directory changes (upstream
          moves every file into `<dir>/deleted` and leaves each
          Media.media_file path stale);
        - the temporary name, key and directory are shortened to fit their
          100-character columns (a MediaNest folder name can be 100
          characters, and upstream's `/deleted` suffix would overflow it on
          a database that enforces the limit);
        - the clone's copy_channel_images is off, so source_post_save does
          not schedule download_source_images into the folder.

        keep_files=True (the bridge's keep-files delete) also forces the
        clone's delete_files_on_disk and delete_removed_media off
        (media_post_delete removes each downloaded video when its source has
        delete_files_on_disk set, and the clone copies that flag from the
        original) and drops a stale `.to_be_removed` marker.

        Upstream's UI keeps its own behaviour (both False).
    '''
    if keep_files and delete_files:
        raise ValueError('keep_files and delete_files are mutually exclusive')
    if keep_files and not bridge:
        raise ValueError('keep_files requires bridge=True')
    media_source = dict(
        uuid=None,
        index_schedule=IndexSchedule.NEVER,
        download_media=False,
        index_videos=False,
        index_streams=False,
        filter_text=str(source.pk),
        target_schedule=source.target_schedule or timezone.now(),
    )
    # ruff: ignore[C401]
    copy_fields = set(f.name for f in source._meta.fields) - set(media_source.keys())
    for k, v in source.__dict__.items():
        if k in copy_fields:
            media_source[k] = v
    media_source = Source(**media_source)
    delete_media = bool(delete_files)
    # overload this boolean for our own use
    media_source.delete_removed_media = delete_media
    if keep_files:
        media_source.delete_files_on_disk = False
    if bridge:
        media_source.copy_channel_images = False
    # adjust the directory and key on the source to be deleted
    if bridge:
        source.directory = _with_affix(source.directory, suffix='/deleted')
        source.key = _with_affix(source.key, suffix='/deleted')
        source.name = _with_affix(source.name, prefix='[Deleting] ')
    else:
        source.directory = source.directory + '/deleted'
        source.key = source.key + '/deleted'
        source.name = f'[Deleting] {source.name}'
    if bridge:
        # A queryset update skips source_pre_save, which would otherwise
        # move the whole download folder into its new `<dir>/deleted`
        # subfolder (and leave every Media.media_file path stale).
        Source.objects.filter(pk=source.pk).update(
            directory=source.directory, key=source.key, name=source.name,
        )
    else:
        source.save(update_fields={'directory', 'key', 'name'})
    source.refresh_from_db()
    # save the new media source now that it is not a duplicate
    media_source.uuid = None
    media_source.save()
    media_source.refresh_from_db()
    # switch the media to the new source instance
    Media.objects.filter(source=source).update(source=media_source)
    directory_path = pathlib.Path(media_source.directory_path)
    if delete_media:
        mkdir_p(directory_path)
        (directory_path / '.to_be_removed').touch(exist_ok=True)
    elif keep_files:
        (directory_path / '.to_be_removed').unlink(missing_ok=True)
    return media_source


def delete_source(source, *, delete_files):
    '''
        Deletes `source` the way TubeSync's UI does, in one transaction.
        delete_files=False keeps every file, whatever the source's own
        "delete files on disk" setting says. Returns the hidden media
        source that TubeSync's background task removes later.
    '''
    keep_files = not delete_files
    with transaction.atomic():
        media_source = prepare_source_for_deletion(
            source, delete_files=delete_files, keep_files=keep_files, bridge=True,
        )
        renamed_path = pathlib.Path(source.directory_path)
        source.delete()
    # Deleting the renamed original saves it once more (deactivate()), and
    # source_pre_save then creates its missing folder (`<dir>/deleted`, or a
    # shortened sibling for a 93-100 character directory). Remove that empty
    # folder again, and its parent when that is an empty leftover too.
    keep = {
        pathlib.Path(media_source.directory_path),
        pathlib.Path(media_source.directory_path).parent,
    }
    for path in (renamed_path, renamed_path.parent):
        if path in keep:
            break
        try:
            path.rmdir()
        except OSError:
            break
    return media_source
