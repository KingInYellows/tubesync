'''
    MED-702: applies the Plex naming changes to files already on disk,
    without downloading anything.

      --show-title TEXT  writes the source's tvshow.nfo with this <title>
                         as a hand-owned file (no TubeSync checksum), which
                         TubeSync then leaves alone and every episode NFO's
                         <showtitle> follows (sync/tvshow_nfo.py). Delete
                         tvshow.nfo to go back to the automatic title.
      --artist-titles    switches the source's media_format from
                         {title_full_bounded}/{title_full} to
                         {artist_title_bounded}/{artist_title}, so file
                         names and episode NFO <title>s start with the
                         artist (sync/plex_naming.py).

    Both act on the one --source. When either changes anything, every
    downloaded media of that source is then renamed to its new name and its NFO rewritten, one media at a time:
    each in its own short transaction under the same huey locks as
    rename_all_media_for_source, so no database lock is held across the
    run (MED-695). Media busy elsewhere are skipped and reported; re-run
    the command to finish them. The media_format is saved with a queryset
    update, so Source post_save (and its save_all_media_for_source /
    rename_all_media_for_source cascade) does not fire.

    Dry-run by default: prints what would change, with old and new paths.
    --apply writes. Run it as the app user (`docker exec -u app ...`).
'''
import copy
import uuid
from pathlib import Path
from xml.etree import ElementTree

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q
from django.db.transaction import atomic
from django_huey import lock_task as huey_lock_task
from huey.exceptions import TaskLockedException

from sync.choices import Val, TaskQueue
from sync.models import Media, Source
from sync.models.media import _format_field_names
from sync.plex_naming import ARTIST_TITLE_FIELDS
from sync.tvshow_nfo import (
    _invalidate_show_title_cache, build_tvshow_nfo, resolve_show_title,
)
from sync.utils import write_text_file


_ARTIST_SWAPS = (
    ('{title_full_bounded}', '{artist_title_bounded}'),
    ('{title_full}', '{artist_title}'),
)


def artist_media_format(media_format):
    '''
        `media_format` with its full-title key swapped for the matching
        artist-title key, or None when it has neither (nothing to swap).
        Unchanged when it already uses an artist-title key.
    '''
    if _format_field_names(media_format) & ARTIST_TITLE_FIELDS:
        return media_format
    for old, new in _ARTIST_SWAPS:
        if old in media_format:
            return media_format.replace(old, new)
    return None


def hand_owned_tvshow_nfo(source, title):
    '''
        The tvshow.nfo text for `source` with `title` as <title> and
        <sorttitle>, and no TubeSync checksum, so tvshow_nfo.py treats it
        as hand-owned (never rewritten, its <title> used for episodes).
        Built from the file on disk when that is a <tvshow>, else from
        build_tvshow_nfo(); the <uniqueid>s are kept, so Plex sees the
        same show.
    '''
    nfo_path = Path(source.directory_path) / 'tvshow.nfo'
    root = None
    if nfo_path.is_file() and not nfo_path.is_symlink():
        try:
            root = ElementTree.fromstring(nfo_path.read_bytes())
        except ElementTree.ParseError:
            root = None
        if root is not None and root.tag != 'tvshow':
            root = None
    if root is None:
        root = ElementTree.fromstring(build_tvshow_nfo(source).encode('utf-8'))
    for tag in ('title', 'sorttitle'):
        element = root.find(tag)
        if element is None:
            element = ElementTree.SubElement(root, tag)
        element.text = title
    for uniqueid in root.iter('uniqueid'):
        uniqueid.attrib.pop('checksum', None)
    return ElementTree.tostring(root, encoding='utf8', method='xml').decode('utf8')


class Command(BaseCommand):

    help = (
        'MED-702: artist-first episode titles and a show title override, '
        'applied to files already downloaded. '
        'Dry-run unless --apply.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--source', required=True,
            help='Source key, uuid, name or directory',
        )
        parser.add_argument('--show-title', default=None)
        parser.add_argument('--artist-titles', action='store_true')
        parser.add_argument('--apply', action='store_true')
        parser.add_argument(
            '--examples', type=int, default=5,
            help='How many old -> new paths to print per source (default 5)',
        )

    def _source(self, value):
        q = Q(key=value) | Q(name=value) | Q(directory=value)
        try:
            q |= Q(pk=uuid.UUID(value))
        except ValueError:
            pass
        matches = list(Source.objects.filter(q))
        if len(matches) != 1:
            raise CommandError(
                f'--source {value!r} matches {len(matches)} sources',
            )
        return matches[0]

    def handle(self, *args, **options):
        apply_changes = options['apply']
        show_title = options['show_title']
        if show_title is not None:
            show_title = show_title.strip()
            if not show_title:
                raise CommandError('--show-title must not be empty')
        if show_title is None and not options['artist_titles']:
            raise CommandError(
                'Nothing to do: give --show-title and/or --artist-titles',
            )
        source = self._source(options['source'])
        mode = 'APPLY' if apply_changes else 'DRY-RUN'
        self.stdout.write(f'[{mode}]')
        problems = self._naming(
            source, show_title, options['artist_titles'],
            apply_changes, options['examples'],
        )
        if problems:
            raise CommandError(
                f'{problems} media skipped or failed; see above, then re-run',
            )

    def _naming(self, source, show_title, artist_titles, apply_changes, examples):
        old_format = str(source.media_format)
        new_format = old_format
        if artist_titles:
            new_format = artist_media_format(old_format)
            if new_format is None:
                raise CommandError(
                    f'{source.name}: media_format {old_format!r} has no '
                    '{title_full_bounded} or {title_full} to swap',
                )
        preview = copy.copy(source)
        preview.media_format = new_format
        try:
            new_format.format(**preview.example_media_format_dict)
        except (KeyError, IndexError, ValueError) as e:
            raise CommandError(f'New media_format does not render: {e}') from e

        old_title = resolve_show_title(source)
        self.stdout.write(f'{source.name} ({source.key})')
        self.stdout.write(f'  show title: {old_title!r} -> {show_title or old_title!r}')
        self.stdout.write(f'  media_format: {old_format!r}')
        if new_format != old_format:
            self.stdout.write(f'            -> {new_format!r}')

        media_qs = Media.objects.filter(source=source, downloaded=True)
        shown = 0
        for media in media_qs.order_by('pk'):
            if not media.media_file:
                continue
            old_path = Path(media.media_file.path)
            media.source = preview
            new_path = Path(media.filepath)
            if old_path != new_path and shown < examples:
                self.stdout.write(f'  {old_path}\n    -> {new_path}')
                shown += 1
        self.stdout.write(f'  downloaded media: {media_qs.count()}')
        if not apply_changes:
            return 0

        if show_title is not None and show_title != old_title:
            nfo_path = Path(source.directory_path) / 'tvshow.nfo'
            write_text_file(nfo_path, hand_owned_tvshow_nfo(source, show_title))
            _invalidate_show_title_cache(source)
            self.stdout.write(f'  wrote hand-owned {nfo_path}')
        if new_format != old_format:
            # update(), not save(): no post_save cascade (see module doc)
            Source.objects.filter(pk=source.pk).update(media_format=new_format)
            self.stdout.write('  saved media_format')
        if not getattr(settings, 'RENAME_ALL_SOURCES', False) and (
            source.directory not in (getattr(settings, 'RENAME_SOURCES', None) or ())
        ):
            self.stdout.write(
                '  note: renaming is off for this source in TubeSync settings; '
                'this command renames anyway, as asked',
            )
        return self._rename_all(source)

    def _rename_all(self, source):
        '''
            rename_all_media_for_source's per-item loop, plus an NFO
            rewrite for media whose path did not change (rename_files only
            rewrites the NFO after a move).
        '''
        mqs = Media.objects.filter(source_id=source.pk, downloaded=True)
        keys = list(mqs.order_by('pk').values_list('pk', flat=True))
        done = skipped = failed = 0
        for media_key in keys:
            try:
                with (
                    huey_lock_task(
                        f'index_media:{media_key}', queue=Val(TaskQueue.FS),
                    ),
                    huey_lock_task(
                        f'media:{media_key}', queue=Val(TaskQueue.DB),
                    ),
                    atomic(durable=False),
                ):
                    media = mqs.filter(pk=media_key).first()
                    if media is None or not media.media_file:
                        continue
                    expected = Path(media.filepath)
                    media.rename_files()
                    if Path(media.media_file.path) != expected:
                        failed += 1
                        self.stdout.write(
                            f'  NOT RENAMED {media.key}: {media.media_file.path} '
                            f'(target {expected} exists, or the file is missing)',
                        )
                        continue
                    media.write_nfo_file()
                done += 1
            except TaskLockedException:
                skipped += 1
                self.stdout.write(f'  busy, skipped: {media_key}')
            except OSError as e:
                failed += 1
                self.stdout.write(f'  FAILED {media_key}: {e}')
        self.stdout.write(
            f'  renamed/rewrote {done}, busy {skipped}, failed {failed}',
        )
        return skipped + failed
