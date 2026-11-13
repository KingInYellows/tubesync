'''
    Contract 1.4.0 (MED-657, MediaNest ADR-0022) helpers for removing a
    source and, later, its folder.

    A deleted source leaves a hidden "media source" behind until TubeSync's
    delete_all_media_for_source task finishes (see sync/source_delete.py).
    That row keeps the original's key, name and directory, never indexes or
    downloads, and has filter_text = the deleted source's uuid. The helpers
    below recognise it so the bridge can report the deletion as running
    (state `deleting`) instead of handing the hidden row out as if it were
    the live source.
'''
import os
import re
import shutil
import threading
import uuid as uuid_module
from pathlib import Path

from django.conf import settings
from django.db.models import Q

from common.logger import log
from sync.choices import IndexSchedule
from sync.models import Source

# MediaNest names every source it creates `acq-src-<canonicalKey>` (its
# deriveInjectiveName(); see sync/management/commands/
# medianest_backfill_plex_sidecars.py). DELETE /folders/{sourceKey} only
# ever deletes a folder with exactly this name.
FOLDER_PREFIX = 'acq-src-'
# Source.directory is max_length=100, so a key longer than this can never
# have had a MediaNest folder.
MAX_KEY_LENGTH = 100 - len(FOLDER_PREFIX)
_KEY_RE = re.compile(r'^[A-Za-z0-9_-]+$')
# Folders being removed in the background are first moved into a directory
# of this name next to them (same filesystem, so the move is an atomic
# rename). The leading dot keeps media servers from scanning it.
TRASH_DIR_NAME = '.medianest-trash'


def parse_uuid(value):
    try:
        return uuid_module.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        return None


def _hidden_candidates():
    return Source.objects.filter(
        index_schedule=IndexSchedule.NEVER,
        download_media=False,
        index_videos=False,
    )


def _is_pending_deletion(source):
    '''
        True when `source` is the hidden row TubeSync's delete leaves behind:
        never scheduled, filter_text names a source uuid, and that source is
        gone (its rename-and-delete already committed).
    '''
    if source.index_schedule != IndexSchedule.NEVER or source.download_media:
        return False
    if source.index_videos:
        return False
    original = parse_uuid(source.filter_text)
    if original is None or original == source.pk:
        return False
    return not Source.objects.filter(pk=original).exists()


def pending_deletion_for_uuid(source_uuid):
    '''The hidden row left by deleting source `source_uuid`, or None.'''
    parsed = parse_uuid(source_uuid)
    if parsed is None:
        return None
    for candidate in _hidden_candidates().filter(filter_text=str(parsed)):
        if _is_pending_deletion(candidate):
            return candidate
    return None


def pending_deletion_for_key(key):
    '''The hidden row of a source with this key that is being deleted, or None.'''
    candidate = Source.objects.filter(key=key).first()
    if candidate is not None and _is_pending_deletion(candidate):
        return candidate
    return None


def is_pending_deletion(source):
    return _is_pending_deletion(source)


def deletion_will_remove_files(media_source):
    '''Whether TubeSync's task will delete the folder of this hidden row.'''
    if media_source.delete_removed_media:
        return True
    try:
        return (Path(media_source.directory_path) / '.to_be_removed').is_file()
    except OSError:
        return False


def serialize_deletion(source_uuid, media_source):
    return {
        'uuid': str(source_uuid),
        'canonicalKey': media_source.key,
        'directory': media_source.directory,
        'deleteFiles': deletion_will_remove_files(media_source),
        'normalizedState': 'deleting',
    }


def valid_folder_key(key):
    return bool(key) and len(key) <= MAX_KEY_LENGTH and bool(_KEY_RE.match(key))


def folder_name(key):
    return FOLDER_PREFIX + key


def sources_using_folder(key):
    '''Every Source row (live or hidden) that uses this key or its folder.'''
    name = folder_name(key)
    return Source.objects.filter(
        Q(key=key)
        | Q(key=f'{key}/deleted')
        | Q(directory=name)
        | Q(directory__startswith=f'{name}/')
    )


def _resolved_source_path(source):
    try:
        return Path(source.directory_path).resolve()
    except (OSError, RuntimeError, ValueError):
        return None


def sources_overlapping(folders):
    '''
        Every Source row (live or hidden) whose resolved download folder is
        one of `folders`, inside one, or contains one. Source.directory is
        stored as typed (the edit form only checks it stays inside the
        download root), so `./acq-src-x`, `x/../acq-src-x` or a symlinked
        path can point at the same place as `acq-src-x` without matching
        it as a string.
    '''
    targets = [Path(f) for f in folders]
    if not targets:
        return []
    overlapping = []
    for source in Source.objects.all():
        path = _resolved_source_path(source)
        if path is None:
            continue
        for target in targets:
            if path == target or target in path.parents or path in target.parents:
                overlapping.append(source)
                break
    return overlapping


def download_root():
    return Path(settings.DOWNLOAD_ROOT).resolve()


def folder_parents():
    '''
        The directories a source folder can live in, matching
        Source.type_directory_path: the video and audio directories when
        SOURCE_DOWNLOAD_DIRECTORY_PREFIX is on, the download root when not.
    '''
    root = download_root()
    if getattr(settings, 'SOURCE_DOWNLOAD_DIRECTORY_PREFIX', True):
        return [root / settings.DOWNLOAD_VIDEO_DIR, root / settings.DOWNLOAD_AUDIO_DIR]
    return [root]


class FolderRefused(Exception):
    '''A folder with the right name exists but is not safe to delete.'''


def existing_folders(key):
    '''
        The folders named acq-src-<key> that exist, as resolved paths. The
        key is already checked to be letters, digits, `_` and `-` only, so
        the name cannot climb out of its parent. Raises FolderRefused for a
        folder that is a symlink, rather than reporting it missing (a 404
        would tell MediaNest nothing is left on disk).
    '''
    name = folder_name(key)
    found = []
    for parent in folder_parents():
        candidate = parent / name
        if candidate.is_symlink():
            raise FolderRefused(f'{name} is a symbolic link')
        if not candidate.is_dir():
            continue
        resolved = candidate.resolve()
        if resolved.parent != parent.resolve():
            raise FolderRefused(f'{name} does not resolve inside its download directory')
        found.append(resolved)
    return found


def _relative_to_root(folder):
    root = download_root()
    for parent in folder_parents():
        if folder.parent == parent.resolve():
            base = parent.relative_to(root) if parent != root else Path()
            return (base / folder.name).as_posix()
    return folder.name


def _trash_dir(parent):
    path = parent / TRASH_DIR_NAME
    path.mkdir(exist_ok=True)
    return path


def _ignore_missing(function, path, exc):
    # Another worker's sweep may be removing the same leftover tree.
    if not isinstance(exc, FileNotFoundError):
        raise exc


def _remove_tree(path):
    try:
        shutil.rmtree(path, onexc=_ignore_missing)
    except OSError:
        log.exception('medianest_bridge: could not finish removing %s', path.name)


def _remove_in_background(paths):
    thread = threading.Thread(
        target=lambda: [_remove_tree(p) for p in paths],
        name='medianest-bridge-folder-delete',
        daemon=True,
    )
    thread.start()
    return thread


def leftover_trash():
    '''Entries a previous, interrupted background removal left behind.'''
    entries = []
    for parent in folder_parents():
        trash = parent.resolve() / TRASH_DIR_NAME
        if trash.is_dir() and not trash.is_symlink():
            entries.extend(e for e in trash.iterdir() if not e.is_symlink())
    return entries


def sweep_leftover_trash(*, background=True):
    '''
        Removes whatever an interrupted earlier removal left in the trash
        directories. Returns the number of entries found.
    '''
    pending = leftover_trash()
    if pending and background:
        _remove_in_background(pending)
    else:
        for path in pending:
            _remove_tree(path)
    return len(pending)


def delete_folders(folders, *, background=True):
    '''
        Moves each folder into the trash directory (an atomic rename, so the
        library no longer shows it), then removes the trash in a background
        thread, together with anything an interrupted earlier removal left
        there. Returns the folders' paths relative to the download root.
    '''
    removed = []
    moved = []
    for folder in folders:
        relative = _relative_to_root(folder)
        # The trash sits next to the folder, so the move never crosses a
        # filesystem boundary.
        target = _trash_dir(folder.parent) / f'{folder.name}-{uuid_module.uuid4().hex}'
        os.rename(folder, target)
        moved.append(target)
        removed.append(relative)
    pending = moved + [p for p in leftover_trash() if p not in moved]
    if pending and background:
        _remove_in_background(pending)
    else:
        for path in pending:
            _remove_tree(path)
    return removed
