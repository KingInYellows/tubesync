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


def prepare_source_for_deletion(source, *, delete_files, keep_files=False):
    '''
        Everything DeleteSourceView.post did before deleting the source.
        Returns the hidden media source. The caller deletes `source`.

        keep_files=True (the bridge's keep-files delete) differs from
        upstream's view in three ways, so no file is deleted or moved:

        - the clone's delete_files_on_disk and delete_removed_media are
          forced off: media_post_delete removes each downloaded video when
          its source has delete_files_on_disk set, and the clone copies that
          flag from the original;
        - a stale `.to_be_removed` marker is dropped;
        - the original is renamed without firing source_pre_save, which
          moves a source's folder whenever its directory changes (upstream
          moves every file into `<dir>/deleted`).

        Upstream's UI keeps its own behaviour (keep_files=False).
    '''
    if keep_files and delete_files:
        raise ValueError('keep_files and delete_files are mutually exclusive')
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
    # adjust the directory and key on the source to be deleted
    source.directory = source.directory + '/deleted'
    source.key = source.key + '/deleted'
    source.name = f'[Deleting] {source.name}'
    if keep_files:
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
            source, delete_files=delete_files, keep_files=keep_files,
        )
        source.delete()
    if keep_files:
        # Deleting the renamed original saves it once more (deactivate()),
        # and source_pre_save then creates its missing `<dir>/deleted`
        # folder. Remove it again when it is empty.
        try:
            (pathlib.Path(media_source.directory_path) / 'deleted').rmdir()
        except OSError:
            pass
    return media_source
