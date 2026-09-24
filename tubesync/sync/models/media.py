import os
import uuid
import json
import re
from collections import OrderedDict
from copy import deepcopy
from itertools import chain
from datetime import datetime, timedelta, timezone as tz
from pathlib import Path
from string import Formatter
from xml.etree import ElementTree
from django.conf import settings
from django.db import models
from django.db.models.functions import Coalesce
from django.core.exceptions import ObjectDoesNotExist
from django.db.transaction import atomic
from django.utils.text import slugify
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from common.logger import log
from common.errors import NoFormatException
from common.json_encoder import JSONEncoder
from common.utils import (
    clean_filename, clean_emoji, directory_and_stem,
    glob_quote, mkdir_p, seconds_to_timestr,
)
from ..youtube import (
    get_media_info as get_youtube_media_info,
    download_media as download_youtube_media,
)
from ..utils import (
    filter_response, parse_media_format, write_text_file,
)
from ..matching import (
    get_best_combined_format,
    get_best_audio_format, get_best_video_format,
)
from ..choices import (
    Val, Fallback, MediaState, SourceResolution,
    YouTube_AudioCodec, YouTube_VideoCodec,
)
from ._migrations import (
    media_file_storage, get_media_thumb_path, get_media_file_path,
)
from ._private import _srctype_dict, _nfo_element
from .media__tasks import (
    copy_thumbnail, download_checklist, download_finished,
    failed_format, refresh_formats, wait_for_premiere, write_nfo_file,
)
from .source import Source


# UTF-8 byte budget for `Media.title_full_bounded`.
TITLE_FULL_BOUNDED_MAX_BYTES = 150
# `episode_mmddnn` is 'MMDD' plus a two-digit same-day index up to this.
EPISODE_DAY_INDEX_TWO_DIGIT_MAX = 99
# Base of the disjoint episode-number range for a same-day index past
# EPISODE_DAY_INDEX_TWO_DIGIT_MAX: base + MMDD * 10_000 + index (8 digits).
EPISODE_OVERFLOW_BASE = 10_000_000
EPISODE_OVERFLOW_MMDD_FACTOR = 10_000


def _format_field_names(format_str):
    '''
        The top-level field names `format_str` substitutes, so
        `{episode_mmddnn:>8}` and `{episode_mmddnn!s}` count, a field nested
        in another field's format spec (`{title:.{episode_yyyy}}`) counts
        too, and an escaped `{{episode_mmddnn}}` does not. Empty for an
        unparseable format.
    '''
    try:
        return _parsed_field_names(format_str)
    except ValueError as e:
        log.warning(f'Unparseable media_format {format_str!r}: {e}')
        return set()


def _parsed_field_names(format_str):
    names = set()
    for _literal, field, spec, _conversion in Formatter().parse(format_str):
        if field:
            names.add(field.split('.', 1)[0].split('[', 1)[0])
        if spec:
            # str.format allows one level of nesting inside a spec.
            names |= _parsed_field_names(spec)
    return names


def _episode_day_index_from_name(name, media_format, mmdd):
    '''
        The same-day index a file name already carries in its
        `{episode_mmddnn}` field, or `None`.

        Anchors on the literal text around each `{episode_mmddnn}` in
        `media_format` (for the built-in Plex profile, `e` before and
        ` - ` after), not on the whole rendered name, so a later title,
        format or extension change does not lose the number. The token
        must encode `mmdd` (the item's current `episode_date`), and every
        occurrence must agree. A field with a format spec or a conversion
        other than `!s` is not parsed, and neither is one with no literal
        text on either side (only digit boundaries would anchor it, so an
        unrelated digit run elsewhere in the path could match): such a
        format numbers every item by its live same-day order.
    '''
    try:
        parsed = list(Formatter().parse(media_format))
    except ValueError:
        return None
    patterns = []
    for position, (before, field, spec, conversion) in enumerate(parsed):
        if field != 'episode_mmddnn':
            continue
        if spec or conversion not in (None, 's'):
            return None
        after = ''
        if position + 1 < len(parsed):
            after = parsed[position + 1][0]
        if not (before or after):
            return None
        patterns.append(
            (re.escape(before) if before else r'(?<!\d)')
            + r'(\d{8}|\d{6})'
            + (re.escape(after) if after else r'(?!\d)')
        )
    indexes = set()
    for pattern in patterns:
        for token in re.findall(pattern, name):
            index = _parse_episode_token(token, mmdd)
            if index is None:
                return None
            indexes.add(index)
    if len(indexes) != 1:
        return None
    return indexes.pop()


def _parse_episode_token(token, mmdd):
    '''
        The same-day index an `episode_mmddnn` value encodes for `mmdd`,
        or `None` when it encodes another day or is not such a value.
    '''
    if len(token) == 6:
        index = int(token[4:])
        if token[:4] == mmdd and 1 <= index <= EPISODE_DAY_INDEX_TWO_DIGIT_MAX:
            return index
        return None
    rest = int(token) - EPISODE_OVERFLOW_BASE
    if rest < 0:
        return None
    token_mmdd, index = divmod(rest, EPISODE_OVERFLOW_MMDD_FACTOR)
    if f'{token_mmdd:04}' == mmdd and index > EPISODE_DAY_INDEX_TWO_DIGIT_MAX:
        return index
    return None


def _aware_utc(value):
    '''Returns `value` as a timezone-aware datetime in UTC (naive = UTC).'''
    if timezone.is_naive(value):
        value = timezone.make_aware(value, tz.utc)
    return value.astimezone(tz.utc)


def _episode_date_coalesce():
    '''
        The SQL-side mirror of `Media.episode_date`'s precedence, used to
        annotate every other row in `Media._same_day_index` with the same
        value `episode_date` would compute for each of them in Python:
        `new_metadata__published` (stable once ingested -- see
        `sync.models.metadata.Metadata.ingest_metadata`), else
        `published` (can be rewritten with approximate data on every
        `index_source` re-index -- see sync/tasks.py `db_fields_media`
        and sync/youtube.py's forced `youtubetab:approximate_date=true`),
        else `created` (always set once a row is saved).

        `upload_date` never needs its own branch in this SQL expression,
        unlike in `episode_date`'s full Python order (`new_metadata.
        published` -> `published` -> `upload_date` -> `created`): it
        isn't a database column -- it's derived from JSON metadata in
        Python -- so it can't appear in a SQL expression at all.
        `Metadata.ingest_metadata` now folds it into `new_metadata.
        published` at ingest time whenever no real release/upload
        timestamp exists, so its value is reachable through the first
        branch for every row that has a `new_metadata` row at all. The
        one remaining case -- a `Media` with a legacy `metadata` column
        and no `new_metadata.published` (no related row, or one written
        without going through `ingest_metadata`) -- still can't be
        expressed here; `_same_day_index` filters those out of the query
        that uses this function and evaluates them separately in Python.

        Keep this and `episode_date` in exact agreement -- if they
        disagree about which UTC day an item falls on, `_same_day_index`
        (SQL, via this function) and `episode_mmddnn`/`nfo_episode_number`
        (Python, via `episode_date`) can disagree about that item's
        same-day index.
    '''
    return Coalesce('new_metadata__published', 'published', 'created')


class Media(models.Model):
    '''
        Media is a single piece of media, such as a single YouTube video linked to a
        Source.
    '''

    # Used to convert seconds to datetime
    posix_epoch = datetime(1970, 1, 1, tzinfo=tz.utc)

    # Format to use to display a URL for the media
    URLS = _srctype_dict('https://www.youtube.com/watch?v={key}')

    # Callback functions to get a list of media from the source
    INDEXERS = _srctype_dict(get_youtube_media_info)

    # Maps standardised names to names used in source metdata
    _same_name = lambda n, k=None: {k or n: _srctype_dict(n) }
    METADATA_FIELDS = {
        **(_same_name('upload_date')),
        **(_same_name('timestamp')),
        **(_same_name('title')),
        **(_same_name('fulltitle')),
        **(_same_name('description')),
        **(_same_name('duration')),
        **(_same_name('formats')),
        **(_same_name('categories')),
        **(_same_name('average_rating', 'rating')),
        **(_same_name('age_limit')),
        **(_same_name('uploader')),
        **(_same_name('like_count', 'upvotes')),
        **(_same_name('dislike_count', 'downvotes')),
        **(_same_name('playlist_title')),
    }

    STATE_ICONS = dict(zip(
        MediaState.values,
        (
            '<i class="far fa-question-circle" title="Unknown download state"></i>',
            '<i class="far fa-clock" title="Scheduled to download"></i>',
            '<i class="fas fa-download" title="Downloading now"></i>',
            '<i class="far fa-check-circle" title="Downloaded"></i>',
            '<i class="fas fa-exclamation-circle" title="Skipped"></i>',
            '<i class="fas fa-stop-circle" title="Media downloading disabled at source"></i>',
            '<i class="fas fa-exclamation-triangle" title="Error downloading"></i>',
        )
    ))

    uuid = models.UUIDField(
        _('uuid'),
        primary_key=True,
        editable=False,
        default=uuid.uuid4,
        help_text=_('UUID of the media')
    )
    created = models.DateTimeField(
        _('created'),
        auto_now_add=True,
        db_index=True,
        help_text=_('Date and time the media was created')
    )
    source = models.ForeignKey(
        Source,
        on_delete=models.CASCADE,
        related_name='media_source',
        help_text=_('Source the media belongs to'),
    )
    published = models.DateTimeField(
        _('published'),
        db_index=True,
        null=True,
        blank=True,
        help_text=_('Date and time the media was published on the source')
    )
    key = models.CharField(
        _('key'),
        max_length=100,
        db_index=True,
        help_text=_('Media key, such as exact YouTube video ID')
    )
    thumb = models.ImageField(
        _('thumb'),
        upload_to=get_media_thumb_path,
        max_length=200,
        blank=True,
        null=True,
        width_field='thumb_width',
        height_field='thumb_height',
        help_text=_('Thumbnail')
    )
    thumb_width = models.PositiveSmallIntegerField(
        _('thumb width'),
        blank=True,
        null=True,
        help_text=_('Width (X) of the thumbnail')
    )
    thumb_height = models.PositiveSmallIntegerField(
        _('thumb height'),
        blank=True,
        null=True,
        help_text=_('Height (Y) of the thumbnail')
    )
    metadata = models.TextField(
        _('metadata'),
        blank=True,
        null=True,
        help_text=_('JSON encoded metadata for the media')
    )
    can_download = models.BooleanField(
        _('can download'),
        db_index=True,
        default=False,
        help_text=_('Media has a matching format and can be downloaded')
    )
    media_file = models.FileField(
        _('media file'),
        upload_to=get_media_file_path,
        max_length=255,
        blank=True,
        null=True,
        storage=media_file_storage,
        help_text=_('Media file')
    )
    skip = models.BooleanField(
        _('skip'),
        db_index=True,
        default=False,
        help_text=_('INTERNAL FLAG - Media will be skipped and not downloaded')
    )
    manual_skip = models.BooleanField(
        _('manual_skip'),
        db_index=True,
        default=False,
        help_text=_('Media marked as "skipped", won\'t be downloaded')
    )
    downloaded = models.BooleanField(
        _('downloaded'),
        db_index=True,
        default=False,
        help_text=_('Media has been downloaded')
    )
    download_date = models.DateTimeField(
        _('download date'),
        db_index=True,
        blank=True,
        null=True,
        help_text=_('Date and time the download completed')
    )
    downloaded_format = models.CharField(
        _('downloaded format'),
        max_length=30,
        blank=True,
        null=True,
        help_text=_('Video format (resolution) of the downloaded media')
    )
    downloaded_height = models.PositiveIntegerField(
        _('downloaded height'),
        blank=True,
        null=True,
        help_text=_('Height in pixels of the downloaded media')
    )
    downloaded_width = models.PositiveIntegerField(
        _('downloaded width'),
        blank=True,
        null=True,
        help_text=_('Width in pixels of the downloaded media')
    )
    downloaded_audio_codec = models.CharField(
        _('downloaded audio codec'),
        max_length=30,
        blank=True,
        null=True,
        help_text=_('Audio codec of the downloaded media')
    )
    downloaded_video_codec = models.CharField(
        _('downloaded video codec'),
        max_length=30,
        blank=True,
        null=True,
        help_text=_('Video codec of the downloaded media')
    )
    downloaded_container = models.CharField(
        _('downloaded container format'),
        max_length=30,
        blank=True,
        null=True,
        help_text=_('Container format of the downloaded media')
    )
    downloaded_fps = models.PositiveSmallIntegerField(
        _('downloaded fps'),
        blank=True,
        null=True,
        help_text=_('FPS of the downloaded media')
    )
    downloaded_hdr = models.BooleanField(
        _('downloaded hdr'),
        default=False,
        help_text=_('Downloaded media has HDR')
    )
    downloaded_filesize = models.PositiveBigIntegerField(
        _('downloaded filesize'),
        db_index=True,
        blank=True,
        null=True,
        help_text=_('Size of the downloaded media in bytes')
    )
    duration = models.PositiveIntegerField(
        _('duration'),
        blank=True,
        null=True,
        help_text=_('Duration of media in seconds')
    )
    title = models.CharField(
        _('title'),
        max_length=200,
        blank=True,
        null=False,
        default='',
        help_text=_('Video title')
    )

    def __str__(self):
        return self.key

    class Meta:
        verbose_name = _('Media')
        verbose_name_plural = _('Media')
        unique_together = (
            ('source', 'key'),
        )

    def save(self, force_insert=False, force_update=False, using=None, update_fields=None):
        setattr(self, '_cached_metadata_dict', None)
        # Correct the path after a source is renamed
        if self.created and self.downloaded and not self.media_file_exists:
            fp_list = list((self.filepath,))
            if self.media_file:
                # Try the new computed directory + the file base name from the database
                fp_list.append(self.filepath.parent / Path(self.media_file.path).name)
            for filepath in fp_list:
                if filepath.exists():
                    self.media_file.name = str(
                        filepath.relative_to(
                            self.media_file.storage.location
                        )
                    )
                    self.skip = False
                    if update_fields is not None:
                        update_fields = {'media_file', 'skip'}.union(update_fields)

        # Trigger an update of derived fields from metadata
        update_md = (
            self.has_metadata and
            (
                update_fields is None or
                'metadata' in update_fields
            )
        )
        if update_md:
            self.title = self.metadata_title[:200] or self.title
            self.duration = self.metadata_duration or self.duration
            setattr(self, '_cached_metadata_dict', None)
            if update_fields is not None:
                # If only some fields are being updated, make sure we update title and duration if metadata changes
                update_fields = {"title", "duration"}.union(update_fields)

        super().save(
            force_insert=force_insert,
            force_update=force_update,
            using=using,
            update_fields=update_fields,)

    def get_metadata_field(self, field):
        fields = self.METADATA_FIELDS.get(field, {})
        return fields.get(self.source.source_type, field)

    def get_metadata_first_value(self, iterable, default=None, /, *, arg_dict=None):
        '''
            fetch the first key with a value from metadata
        '''

        if arg_dict is None:
            arg_dict = self.loaded_metadata
        assert isinstance(arg_dict, dict), type(arg_dict)
        # str is an iterable of characters
        # we do not want to look for each character!
        if isinstance(iterable, str):
            iterable = (iterable,)
        for key in tuple(iterable):
            # reminder: unmapped fields return the key itself
            field = self.get_metadata_field(key)
            value = arg_dict.get(field)
            # value can be None because:
            #   - None was stored at the key
            #   - the key was not in the dictionary
            # either way, we don't want those values
            if value is None:
                continue
            if isinstance(value, str):
                return value.strip()
            return value
        return default

    def iter_formats(self):
        for fmt in self.formats:
            yield parse_media_format(fmt)

    def get_best_combined_format(self):
        return get_best_combined_format(self)

    def get_best_audio_format(self):
        return get_best_audio_format(self)

    def get_best_video_format(self):
        return get_best_video_format(self)

    def get_format_str(self):
        '''
            Returns a youtube-dl compatible format string for the best matches
            combination of source requirements and available audio and video formats.
            Returns boolean False if there is no valid downloadable combo.
        '''
        if self.source.is_audio:
            audio_match, audio_format = self.get_best_audio_format()
            if audio_format:
                return str(audio_format)
            else:
                return False
        else:
            combined_match, combined_format = self.get_best_combined_format()
            if combined_format:
                return str(combined_format)
            else:
                audio_match, audio_format = self.get_best_audio_format()
                video_match, video_format = self.get_best_video_format()
                if audio_format and video_format:
                    return f'{video_format}+{audio_format}'
                else:
                    # last resort: any combined format
                    fallback_hd_cutoff = getattr(settings, 'VIDEO_HEIGHT_IS_HD', 500)

                    for fmt in reversed(list(self.iter_formats())):
                        select_fmt = (
                            fmt.get('id') and
                            fmt.get('acodec') and
                            fmt.get('vcodec') and
                            self.source.can_fallback and
                            (
                                (self.source.fallback == Val(Fallback.NEXT_BEST_RESOLUTION)) or
                                (
                                    self.source.fallback == Val(Fallback.REQUIRE_CODEC) and
                                    self.source.source_vcodec == fmt.get('vcodec')
                                ) or
                                (
                                    self.source.fallback == Val(Fallback.REQUIRE_HD) and
                                    (fmt.get('height') or 0) >= fallback_hd_cutoff
                                )
                            )
                        )
                        if select_fmt:
                            return str(fmt.get('id'))
                    return False
        return False

    def get_display_format(self, format_str):
        '''
            Returns a tuple used in the format component of the output filename. This
            is the format(s) found by matching. Examples:
                # Audio and video streams
                ('1080p', 'vp9', 'opus')
                # Audio only stream
                ('opus',)
                # Audio and video streams with additional flags
                ('720p', 'avc1', 'mp4a', '60fps', 'hdr')  
        '''
        fmt = []
        resolution = ''
        vcodec = ''
        acodec = ''
        height = '0'
        width = '0'
        fps = ''
        hdr = ''
        # If the download has completed use existing values
        if self.downloaded:
            # Check if there's any stored meta data at all
            if (not self.downloaded_video_codec and \
                not self.downloaded_audio_codec):
                # Marked as downloaded but no metadata, imported?
                return {
                    'resolution': resolution,
                    'height': height,
                    'width': width,
                    'vcodec': vcodec,
                    'acodec': acodec,
                    'fps': fps,
                    'hdr': hdr,
                    'format': tuple(fmt),
                }
            is_audio_download = (
                (self.downloaded_format or '').lower() == Val(SourceResolution.AUDIO)
            )
            if is_audio_download:
                resolution = Val(SourceResolution.AUDIO)
            elif self.downloaded_height and self.downloaded_height > 0:
                resolution = f'{self.downloaded_height}p'
            elif self.downloaded_format:
                resolution = self.downloaded_format.lower()
            if resolution:
                fmt.append(resolution)
            vcodec = self.downloaded_video_codec
            if not is_audio_download and vcodec:
                vcodec = vcodec.lower()
                fmt.append(vcodec)
            else:
                vcodec = ''
            acodec = self.downloaded_audio_codec
            if acodec:
                acodec = acodec.lower()
                fmt.append(acodec)
            else:
                acodec = ''
            if not is_audio_download:
                fps = str(self.downloaded_fps)
                if fps:
                    fmt.append(f'{fps}fps')
                if self.downloaded_hdr:
                    hdr = 'hdr'
                    fmt.append(hdr)
                height = str(self.downloaded_height)
                width = str(self.downloaded_width)
            return {
                'resolution': resolution,
                'height': height,
                'width': width,
                'vcodec': vcodec,
                'acodec': acodec,
                'fps': fps,
                'hdr': hdr,
                'format': tuple(fmt),
            }
        # Otherwise, calculate from matched format codes
        vformat = None
        aformat = None
        if format_str and '+' in format_str:
            # Seperate audio and video streams
            vformat_code, aformat_code = format_str.split('+')
            vformat = self.get_format_by_code(vformat_code)
            aformat = self.get_format_by_code(aformat_code)
        else:
            # Combined stream or audio only
            cformat = self.get_format_by_code(format_str)
            aformat = cformat
            if cformat and cformat['vcodec']:
                # Combined
                vformat = cformat
        if vformat:
            if vformat.get('height', 0) > 0:
                resolution = f"{vformat['height']}p"
            elif vformat.get('format'):
                resolution = str(vformat['format']).lower()
            if resolution:
                fmt.append(resolution)
            vcodec = vformat['vcodec'].lower()
            if vcodec:
                fmt.append(vcodec)
        if aformat:
            acodec = aformat['acodec'].lower()
            if acodec:
                fmt.append(acodec)
        if vformat:
            if vformat['is_60fps']:
                fps = '60fps'
                fmt.append(fps)
            if vformat['is_hdr']:
                hdr = 'hdr'
                fmt.append(hdr)
            height = str(vformat['height'])
            width = str(vformat['width'])
        return {
            'resolution': resolution,
            'height': height,
            'width': width,
            'vcodec': vcodec,
            'acodec': acodec,
            'fps': fps,
            'hdr': hdr,
            'format': tuple(fmt),
        }

    def get_format_by_code(self, format_code):
        '''
            Matches a format code, such as '22', to a processed format dict.
        '''
        for fmt in self.iter_formats():
            if format_code == fmt['id']:
                return fmt
        return False

    @property
    def format_dict(self):
        '''
            Returns a dict matching the media_format key requirements for this item
            of media.
        '''
        format_str = self.get_format_str()
        display_format = self.get_display_format(format_str)
        dateobj = self.upload_date if self.upload_date else self.created
        # The episode_* keys cost a query each (episode_mmddnn a COUNT
        # too), so only a media_format that uses them gets them.
        # {yyyy}/{mm}/{dd} above stay on upload_date for compatibility and
        # can differ from {episode_yyyy} -- see Media.episode_date.
        used_fields = _format_field_names(str(self.source.media_format))
        episode_keys = {
            key: getattr(self, key)
            for key in ('episode_yyyy', 'episode_mmddnn')
            if key in used_fields
        }
        return episode_keys | {
            'yyyymmdd': dateobj.strftime('%Y%m%d'),
            'yyyy_mm_dd': dateobj.strftime('%Y-%m-%d'),
            'yyyy_0mm_dd': dateobj.strftime('%Y-0%m-%d'),
            'yyyy': dateobj.strftime('%Y'),
            'mm': dateobj.strftime('%m'),
            'dd': dateobj.strftime('%d'),
            'source': self.source.slugname,
            'source_full': clean_filename(self.source.name),
            'title': self.slugtitle,
            'title_full': clean_filename(self.title),
            'title_full_bounded': self.title_full_bounded,
            'key': self.key,
            'format': '-'.join(display_format['format']),
            'playlist_title': self.playlist_title,
            'video_order': self.get_episode_str(True),
            'ext': self.source.extension,
            'resolution': display_format['resolution'],
            'height': display_format['height'],
            'width': display_format['width'],
            'vcodec': display_format['vcodec'],
            'acodec': display_format['acodec'],
            'fps': display_format['fps'],
            'hdr': display_format['hdr'],
            'uploader': self.uploader,
        }


    @property
    def has_metadata(self):
        result = self.metadata is not None
        if not result:
            return False
        value = self.get_metadata_first_value(('id', 'display_id', 'channel_id', 'uploader_id',))
        return value is not None


    def metadata_clear(self, /, *, save=False):
        self.metadata = None
        setattr(self, '_cached_metadata_dict', None)
        if save:
            self.save()


    def metadata_dumps(self, arg_dict=dict()):
        fallback = dict()
        try:
            fallback.update(self.new_metadata.with_formats)
        except ObjectDoesNotExist:
            pass
        data = arg_dict or fallback
        return json.dumps(data, separators=(',', ':'), cls=JSONEncoder)


    def metadata_loads(self, arg_str='{}'):
        data = json.loads(arg_str) or self.loaded_metadata
        return data


    @atomic(durable=False)
    def ingest_metadata(self, data):
        assert isinstance(data, dict), type(data)
        site = self.get_metadata_first_value(
            'extractor_key',
            'Youtube',
            arg_dict=data,
        )
        md_model = self._meta.fields_map.get('new_metadata').related_model
        md, created = md_model.objects.filter(
            source__isnull=True,
        ).get_or_create(
            media=self,
            site=site,
            key=self.key,
        )
        setattr(self, '_cached_metadata_dict', None)
        return md.ingest_metadata(data)


    def save_to_metadata(self, key, value, /):
        data = self.loaded_metadata
        using_new_metadata = self.get_metadata_first_value(
            ('migrated', '_using_table',),
            False,
            arg_dict=data,
        )
        data[key] = value
        self.ingest_metadata(data)
        if not using_new_metadata:
            epoch = self.get_metadata_first_value('epoch', arg_dict=data)
            migrated = dict(migrated=True, epoch=epoch)
            migrated['_using_table'] = True
            self.metadata = self.metadata_dumps(arg_dict=migrated)
            self.save()
        from common.logger import log
        log.debug(f'Saved to metadata: {self.key} / {self.uuid}: {key=}: {value}')


    @property
    def reduce_data(self):
        now = timezone.now()
        using_table = False
        try:
            data = json.loads(self.metadata or "{}")
            old_mdl = len(self.metadata or "")
            if data.get('_using_table', False):
                try:
                    data.update(self.new_metadata.with_formats)
                except ObjectDoesNotExist:
                    pass
                else:
                    using_table = True
                    old_mdl = len(str(data))
            if '_reduce_data_ran_at' in data.keys():
                total_seconds = data['_reduce_data_ran_at']
                assert isinstance(total_seconds, int), type(total_seconds)
                ran_at = self.ts_to_dt(total_seconds)
                if (now - ran_at) < timedelta(hours=1):
                    return data

            compact_json = self.metadata_dumps(arg_dict=data)

            filtered_data = filter_response(data, True)
            filtered_data['_reduce_data_ran_at'] = round((now - self.posix_epoch).total_seconds())
            filtered_json = self.metadata_dumps(arg_dict=filtered_data)
        except Exception as e:
            from common.logger import log
            log.exception('reduce_data: %s', e)
        else:
            from common.logger import log
            # log the results of filtering / compacting on metadata size
            new_mdl = len(compact_json)
            if old_mdl > new_mdl:
                delta = old_mdl - new_mdl
                log.info(f'{self.key}: metadata compacted by {delta:,} characters ({old_mdl:,} -> {new_mdl:,})')
            new_mdl = len(filtered_json)
            if old_mdl > new_mdl:
                delta = old_mdl - new_mdl
                log.info(f'{self.key}: metadata reduced by {delta:,} characters ({old_mdl:,} -> {new_mdl:,})')
                if getattr(settings, 'SHRINK_OLD_MEDIA_METADATA', False):
                    if using_table:
                        self.ingest_metadata(filtered_data)
                    else:
                        self.metadata = filtered_json
                    return filtered_data
            return data


    @property
    def loaded_metadata(self):
        cached = getattr(self, '_cached_metadata_dict', None)
        if cached:
            return deepcopy(cached)
        data = None
        if getattr(settings, 'SHRINK_OLD_MEDIA_METADATA', False):
            data = self.reduce_data
        try:
            if not data:
                data = json.loads(self.metadata or "{}")
            if not isinstance(data, dict):
                return {}
            # if hasattr(self, 'new_metadata'):
            try:
                data.update(self.new_metadata.with_formats)
            except ObjectDoesNotExist:
                pass
            setattr(self, '_cached_metadata_dict', data)
            return data
        except Exception:
            return {}


    @property
    def url(self):
        url = self.URLS.get(self.source.source_type, '')
        return url.format(key=self.key)

    @property
    def description(self):
        return self.get_metadata_first_value('description', '')

    @property
    def metadata_title(self):
        return self.get_metadata_first_value(('fulltitle', 'title',), '')

    def ts_to_dt(self, /, timestamp):
        try:
            timestamp_float = float(timestamp)
        except (TypeError, ValueError,) as e:
            log.warn(f'Could not compute published from timestamp for: {self.source} / {self} with "{e}"')
            pass
        else:
            return self.posix_epoch + timedelta(seconds=timestamp_float)
        return None

    @property
    def slugtitle(self):
        transtab = str.maketrans({
            '&': 'and', '+': 'and',
        })
        slugified = slugify(
            self.title.translate(transtab),
            allow_unicode=True,
        )
        encoding = os.sys.getfilesystemencoding()
        decoded = slugified.encode(
            encoding=encoding,
            errors='ignore',
        ).decode(encoding=encoding)
        return decoded[:80]

    @property
    def title_full_bounded(self):
        '''
            Like `title_full` (`clean_filename(self.title)`), but bounded to
            at most `TITLE_FULL_BOUNDED_MAX_BYTES` (150) UTF-8 bytes,
            truncated at a character boundary so a
            multibyte character is never split, then stripped. Keeps a long
            multibyte/emoji title from pushing a filename's directory
            component past common filesystem length limits when
            combined with the rest of `media_format`.
        '''
        cleaned = clean_filename(self.title)
        encoded = cleaned.encode('utf-8')
        if len(encoded) <= TITLE_FULL_BOUNDED_MAX_BYTES:
            return cleaned.strip()
        # Decoding with errors='ignore' drops any incomplete trailing
        # multibyte sequence left by the raw byte-offset truncation.
        return encoded[:TITLE_FULL_BOUNDED_MAX_BYTES].decode(
            'utf-8', errors='ignore',
        ).strip()

    @property
    def thumbnail(self):
        default = f'https://i.ytimg.com/vi/{self.key}/maxresdefault.jpg'
        return self.get_metadata_first_value('thumbnail', default)

    @property
    def name(self):
        title = self.title
        return title if title else self.key

    @property
    def upload_date(self):
        upload_date_str = self.get_metadata_first_value('upload_date')
        if not upload_date_str:
            return None
        try:
            return datetime.strptime(upload_date_str, '%Y%m%d')
        except (AttributeError, ValueError) as e:
            log.debug(f'Media.upload_date: {self.source} / {self}: strptime: {e}')
            pass
        return None

    @property
    def episode_date(self):
        '''
            The single date source for date-based episode numbering
            (`episode_yyyy`, `episode_mmddnn`, and the non-playlist NFO
            <season>/<episode>): `new_metadata.published` when set, else
            `published`, else `upload_date`, else `created` -- always
            returned as an aware UTC datetime.

            `new_metadata.published` (the reverse `OneToOne` from
            `sync.models.metadata.Metadata.media`, `related_name=
            'new_metadata'`) is checked first because it is *stable*,
            unlike `published`: `Metadata.ingest_metadata` sets it once,
            from the full metadata's `release_timestamp`/`timestamp`
            (falling back to `upload_date`, then to `media.published` or
            `retrieved` -- see sync/models/metadata.py), and nothing
            afterwards rewrites it with approximate data --
            `sync.tasks.migrate_to_metadata` only ever merges the
            `epoch`/`availability`/`extractor_key` keys into it, never
            `timestamp`. `Media.published`, by contrast, is rewritten on
            every `index_source` re-index (sync/tasks.py's
            `db_fields_media` includes `'published'`) from yt-dlp's
            `youtubetab:approximate_date=true` tab listing (forced on in
            sync/youtube.py), which yt-dlp can derive from relative text
            like "3 weeks ago" and so can drift call to call -- silently
            reshuffling MMDD/index/filenames/NFOs for already-downloaded
            media.

            The `upload_date` step only still matters for a `Media` whose
            legacy `metadata` column was set directly (bypassing
            `ingest_metadata`, so no `new_metadata.published` exists --
            not reachable through this codebase's own indexing/ingest
            code paths, but a supported direct field assignment, e.g. in
            tests): `_same_day_index`'s SQL `COUNT` can't see it either,
            since `upload_date` is derived from JSON metadata in Python,
            not a database column, so `_same_day_index` evaluates that
            narrow case in Python too (see its docstring). Every row
            produced by the normal indexing pipeline gets a
            `new_metadata` row (via `migrate_to_metadata`/
            `download_media_metadata`), whose `.published` already
            covers `upload_date` (see `Metadata.ingest_metadata`), so
            this step is not reached for those.

            This is deliberately its own date source rather than reusing
            `calculate_episode_number` (which only ever looks at
            `published`), the pre-existing NFO season (`upload_date.year`),
            or the `{yyyy}` format key (`upload_date` or `created`) --
            those three can disagree with each other and with this one, and
            adding a fourth ad-hoc mix would only make that worse.

            `created` is only `None` for an unsaved instance (it has
            `auto_now_add=True`, populated on save) -- that case falls back
            to the current time rather than `None`.
        '''
        try:
            new_metadata_published = self.new_metadata.published
        except ObjectDoesNotExist:
            new_metadata_published = None
        if new_metadata_published:
            return _aware_utc(new_metadata_published)
        if self.published:
            return _aware_utc(self.published)
        upload_date = self.upload_date
        if upload_date:
            return _aware_utc(upload_date)
        return _aware_utc(
            self.created if self.created is not None else timezone.now()
        )

    @property
    def metadata_duration(self):
        duration = self.get_metadata_first_value('duration', 0)
        try:
            duration = int(duration)
        except (TypeError, ValueError):
            duration = 0
        return duration

    @property
    def duration_formatted(self):
        duration = self.duration
        if duration and duration > 0:
            return seconds_to_timestr(duration)
        return '??:??:??'

    @property
    def categories(self):
        return self.get_metadata_first_value('categories', list())

    @property
    def rating(self):
        return self.get_metadata_first_value('rating', 0)

    @property
    def votes(self):
        upvotes = self.get_metadata_first_value('upvotes', 0)
        if not isinstance(upvotes, int):
            upvotes = 0
        downvotes = self.get_metadata_first_value('downvotes', 0)
        if not isinstance(downvotes, int):
            downvotes = 0
        return upvotes + downvotes

    @property
    def age_limit(self):
        return self.get_metadata_first_value('age_limit', 0)

    @property
    def uploader(self):
        return self.get_metadata_first_value('uploader', '')

    @property
    def formats(self):
        return self.get_metadata_first_value('formats', list())

    @property
    def playlist_title(self):
        return self.get_metadata_first_value('playlist_title', '')

    @property
    def filename(self):
        # Create a suitable filename from the source media_format
        media_format = str(self.source.media_format)
        media_details = self.format_dict
        result = media_format.format(**media_details)
        return '.' + result if '/' == result[0] else result

    @property
    def directory_path(self):
        return self.filepath.parent

    @property
    def filepath(self):
        return self.source.directory_path / self.filename

    def filename_prefix(self):
        if self.downloaded and self.media_file:
            filename = self.media_file.path
        else:
            filename = self.filename
        # The returned prefix should not contain any directories.
        # So, we do not care about the different directories
        # used for filename in the cases above.
        prefix, ext = os.path.splitext(os.path.basename(filename))
        return prefix

    @property
    def thumbname(self):
        prefix = self.filename_prefix()
        return f'{prefix}.jpg'

    @property
    def thumbpath(self):
        return self.directory_path / self.thumbname

    @property
    def nfoname(self):
        prefix = self.filename_prefix()
        return f'{prefix}.nfo'

    @property
    def nfopath(self):
        return self.directory_path / self.nfoname

    @property
    def jsonname(self):
        prefix = self.filename_prefix()
        return f'{prefix}.info.json'

    @property
    def jsonpath(self):
        return self.directory_path / self.jsonname

    @property
    def thumb_file_exists(self):
        if not self.thumb:
            return False
        return os.path.exists(self.thumb.path)

    @property
    def media_file_exists(self):
        if not self.media_file:
            return False
        return os.path.exists(self.media_file.path)

    @property
    def content_type(self):
        if not self.downloaded:
            return 'video/mp4'
        vcodec = self.downloaded_video_codec
        if vcodec is None:
            acodec = self.downloaded_audio_codec
            if acodec is None:
                raise TypeError() # nothing here.
            acodec = acodec.upper()
            if acodec == Val(YouTube_AudioCodec.MP4A):
                return "audio/mp4"
            elif acodec == Val(YouTube_AudioCodec.OPUS):
                return "audio/opus"
            else:
                # fall-fall-back.
                return 'audio/ogg'
        vcodec = vcodec.upper()
        if vcodec == Val(YouTube_VideoCodec.AVC1):
            return 'video/mp4'
        else:
            return 'video/matroska'

    @property
    def nfoxml(self):
        '''
            Returns an NFO formatted (prettified) XML string.
        '''
        nfo = ElementTree.Element('episodedetails')
        nfo.text = '\n  '
        # title = media metadata title
        nfo.append(_nfo_element(nfo,
            'title', clean_emoji(self.title),
        ))
        # showtitle = resolved show title (T2): the cached channel/playlist
        # Metadata (sync/tvshow_nfo.py), then the latest media's own
        # channel/uploader/playlist_title, then source.name -- same
        # resolution tvshow.nfo's <title> uses, so both agree.
        from ..tvshow_nfo import resolve_show_title
        nfo.append(_nfo_element(nfo,
            'showtitle', clean_emoji(str(resolve_show_title(self.source)).strip()),
        ))
        # season = episode_date year, episode = MMDD + same-day index. A
        # playlist keeps the legacy season '1' and published-order
        # calculate_episode_number() unless its media_format files videos
        # by the same date scheme (every bridge-created playlist), so the
        # NFO always agrees with the Season YYYY/sYYYYeMMDDNN filename.
        legacy_playlist = (
            self.source.is_playlist
            and 'episode_mmddnn'
            not in _format_field_names(str(self.source.media_format))
        )
        nfo.append(_nfo_element(nfo,
            'season',
            '1' if legacy_playlist else str(self.episode_date.year),
        ))
        nfo.append(_nfo_element(nfo,
            'episode',
            self.get_episode_str() if legacy_playlist
            else str(self.nfo_episode_number),
        ))
        # ratings = media metadata youtube rating
        value = _nfo_element(nfo, 'value', str(self.rating), indent=6)
        votes = _nfo_element(nfo, 'votes', str(self.votes), indent=4)
        rating_attrs = OrderedDict()
        rating_attrs['name'] = 'youtube'
        rating_attrs['max'] = '5'
        rating_attrs['default'] = 'true'
        rating = nfo.makeelement('rating', rating_attrs)
        rating.text = '\n      '
        rating.append(value)
        rating.append(votes)
        rating.tail = '\n  '
        ratings = nfo.makeelement('ratings', {})
        ratings.text = '\n    '
        if self.rating is not None:
            ratings.append(rating)
        ratings.tail = '\n  '
        nfo.append(ratings)
        # plot = media metadata description
        nfo.append(_nfo_element(nfo,
            'plot', clean_emoji(str(self.description).strip()),
        ))
        # thumb = local path to media thumbnail
        nfo.append(_nfo_element(nfo,
            'thumb', self.thumbname if self.source.copy_thumbnails else '',
        ))
        # mpaa = media metadata age requirement
        if self.age_limit and self.age_limit > 0:
            nfo.append(_nfo_element(nfo,
                'mpaa', str(self.age_limit),
            ))
        # runtime = media metadata duration in seconds
        nfo.append(_nfo_element(nfo,
            'runtime', str(self.duration),
        ))
        # id = media key
        nfo.append(_nfo_element(nfo,
            'id', str(self.key).strip(),
        ))
        # uniqueid = media key
        uniqueid_attrs = OrderedDict()
        uniqueid_attrs['type'] = 'youtube'
        uniqueid_attrs['default'] = 'True'
        nfo.append(_nfo_element(nfo,
            'uniqueid', str(self.key).strip(), attrs=uniqueid_attrs,
        ))
        # studio = media metadata uploader
        nfo.append(_nfo_element(nfo,
            'studio', clean_emoji(str(self.uploader).strip()),
        ))
        # aired = media metadata uploaded date
        upload_date = self.upload_date
        nfo.append(_nfo_element(nfo,
            'aired', upload_date.strftime('%Y-%m-%d') if upload_date else '',
        ))
        # dateadded = date and time media was created in tubesync
        nfo.append(_nfo_element(nfo,
            'dateadded', self.created.strftime('%Y-%m-%d %H:%M:%S'),
        ))
        # genre = any media metadata categories if they exist
        for category_str in self.categories:
            nfo.append(_nfo_element(nfo,
                'genre', str(category_str).strip(),
            ))
        nfo[-1].tail = '\n'
        # Return XML tree as a prettified string
        return ElementTree.tostring(nfo, encoding='utf8', method='xml').decode('utf8')

    def get_download_state(self, task=None):
        if self.downloaded:
            return Val(MediaState.DOWNLOADED)
        if task:
            def running(arg_task, /):
                if hasattr(arg_task, 'locked_by_pid_running'):
                    return arg_task.locked_by_pid_running()
                from ..tasks import get_media_download_task
                return get_media_download_task(str(self.pk))
            if running(task):
                return Val(MediaState.DOWNLOADING)
            elif task.has_error():
                return Val(MediaState.ERROR)
            else:
                return Val(MediaState.SCHEDULED)
        if self.skip:
            return Val(MediaState.SKIPPED)
        if not self.source.download_media:
            return Val(MediaState.DISABLED_AT_SOURCE)
        return Val(MediaState.UNKNOWN)

    def get_download_state_icon(self, task=None):
        state = self.get_download_state(task)
        return self.STATE_ICONS.get(state, self.STATE_ICONS[Val(MediaState.UNKNOWN)])

    def download_media(self):
        format_str = self.get_format_str()
        if not format_str:
            raise NoFormatException(f'Cannot download, media "{self.pk}" ({self}) has '
                                    f'no valid format available')
        # Download the media with yt-dlp
        download_youtube_media(self.url, format_str, self.source.extension,
                               str(self.filepath), self.source.write_json,
                               self.source.sponsorblock_categories.expand_choices, self.source.embed_thumbnail,
                               self.source.embed_metadata, self.source.enable_sponsorblock,
                              self.source.write_subtitles, self.source.auto_subtitles,self.source.sub_langs )
        # Return the download paramaters
        return format_str, self.source.extension

    def index_metadata(self):
        '''
            Index the media metadata returning a dict of info.
        '''
        indexer = self.INDEXERS.get(self.source.source_type, None)
        if not callable(indexer):
            raise Exception(f'Media with source type f"{self.source.source_type}" '
                            f'has no indexer')
        response = indexer(self.url)
        no_formats_available = (
            not response or
            "formats" not in response.keys() or
            0 == len(response["formats"])
        )
        if no_formats_available:
            self.can_download = False
            self.skip = True
        return response

    def _same_day_index(self):
        '''
            Returns the 1-based position of this Media among its source's
            other media whose `episode_date` falls on the same UTC calendar
            day, ordered by (`episode_date`, `created`, `key`) -- the same
            tie-break `calculate_episode_number` uses. Membership is never
            filtered by `skip` or download state.

            Membership and ordering use `episode_date` itself (mirrored in
            SQL by `_episode_date_coalesce()`), so the MMDD that
            `episode_mmddnn` encodes and the day an item is counted in can
            never disagree (two items with the same encoded date always
            get distinct indexes). The rows are counted in two parts:

            - Every other row *except* the one below: a single annotated
              `COUNT` -- one query, regardless of source size -- of the
              rows that sort strictly before this one (unique per source
              by `key`, so a strict "less than" on the full tuple never
              includes this item itself). This is what replaces
              `calculate_episode_number`'s approach of iterating every
              candidate row in Python, which is an O(n) query cost paid
              on every call -- `format_dict` calls the equivalent of this
              once per filename evaluation, so that cost was effectively
              O(n^2) per source rename.
            - `published` unset, no `new_metadata.published` (no related
              row, or one written without `ingest_metadata`), but metadata
              in the legacy `metadata` column or the related row, set
              directly (bypassing
              `ingest_metadata` -- not reachable through this codebase's
              own indexing/ingest code paths, but a supported direct
              field assignment, e.g. in tests or a not-yet-migrated
              import): `episode_date` falls back to `upload_date` for
              these, which -- unlike `new_metadata.published` -- is
              derived from JSON metadata in Python and so can't appear
              in the `COUNT` above (`_episode_date_coalesce()`'s
              docstring). Evaluated in Python; every row produced by the
              normal indexing pipeline gets a `new_metadata` row (via
              `sync.tasks.migrate_to_metadata`/`download_media_metadata`,
              whose `.published` already covers `upload_date` -- see
              `Metadata.ingest_metadata`), so this group is expected to
              be empty or tiny.

            `created` is only `None` for an unsaved instance (it has
            `auto_now_add=True`, populated on save); that case falls back
            to the current time rather than passing `None` into an `__lt`
            query lookup, which Django raises on.
        '''
        date, created, key = this_item = self._episode_sort_key()
        sql_day, legacy_day = self._same_day_others()
        before = sql_day.filter(
            models.Q(episode_date_sort__lt=date) |
            models.Q(episode_date_sort=date, created__lt=created) |
            models.Q(episode_date_sort=date, created=created, key__lt=key)
        ).count()
        before += sum(
            1 for other in legacy_day()
            if other._episode_sort_key() < this_item
        )
        return before + 1

    def _episode_sort_key(self):
        '''
            (`episode_date`, `created`, `key`): the same-day order. An
            unsaved instance has no `created` yet (`auto_now_add=True`) and
            uses the current time instead.
        '''
        return (
            self.episode_date,
            _aware_utc(
                self.created if self.created is not None else timezone.now()
            ),
            self.key,
        )

    def _same_day_others(self):
        '''
            This source's other media whose `episode_date` falls on this
            item's UTC day, split as `_same_day_index` describes:
            (`sql_day`, `legacy_day`). `sql_day` is a queryset annotated
            with `episode_date_sort`; `legacy_day()` evaluates the few rows
            SQL can't date and yields the same-day ones.
        '''
        day_start = self.episode_date.replace(
            hour=0, minute=0, second=0, microsecond=0,
        )
        day_end = day_start + timedelta(days=1)

        others = Media.objects.filter(source_id=self.source_id)
        if self.pk is not None:
            others = others.exclude(pk=self.pk)
        # new_metadata__published is NULL both without a related row and
        # for one written without going through ingest_metadata. Either
        # metadata store can supply upload_date (loaded_metadata merges
        # the related row in), so a row with either is dated in Python.
        legacy_metadata_only = models.Q(
            published__isnull=True,
            new_metadata__published__isnull=True,
        ) & (
            models.Q(metadata__isnull=False)
            | models.Q(new_metadata__isnull=False)
        )
        sql_day = others.exclude(legacy_metadata_only).annotate(
            episode_date_sort=_episode_date_coalesce(),
        ).filter(
            episode_date_sort__gte=day_start,
            episode_date_sort__lt=day_end,
        )

        def legacy_day():
            for other in others.filter(legacy_metadata_only):
                if day_start <= other.episode_date < day_end:
                    yield other

        return sql_day, legacy_day

    def _episode_day_index(self):
        '''
            The same-day index `episode_mmddnn` and `nfo_episode_number`
            use.

            For a source whose `media_format` does not use
            `{episode_mmddnn}` this is the live `_same_day_index`. For one
            that does, a downloaded file keeps the index its name already
            carries (`_episode_day_index_from_name`), and every other
            same-day item takes the free indexes, in same-day order,
            around the ones those files hold. Without this, an earlier
            same-day item indexed after a later one was downloaded would
            take that file's number: its download target would be the
            existing file, which yt-dlp (`overwrites: None`) reports as
            already downloaded, and it would be attached to that file.

            A downloaded file whose name carries no index for the current
            day (a legacy name, or `episode_date` moved to another day)
            is numbered like an item not yet downloaded, and a rename then
            moves it. Two files that already share an index (from before
            this rule) both keep it.

            Accepted limitation: without `{episode_mmddnn}` nothing
            freezes the index. Deleting a media keeps its place (upstream's
            media_post_delete re-creates a skipped placeholder with the
            same key and date), but once an earlier same-day row is gone
            for good (that placeholder deleted too, e.g. by a later
            cleanup_removed_media pass) a later item's `<episode>` moves
            down on its next NFO rewrite. Upstream's
            `calculate_episode_number()` counts across the whole year and
            drifts the same way. Bridge-created sources use
            `{episode_mmddnn}` and are unaffected.
        '''
        media_format = str(self.source.media_format)
        if 'episode_mmddnn' not in _format_field_names(media_format):
            return self._same_day_index()
        mmdd = self.episode_date.strftime('%m%d')
        own = self._frozen_day_index(media_format, mmdd)
        if own is not None:
            return own

        # One pass over the day's other rows (one query, plus the rare
        # rows SQL can't date) gives both this item's live position and
        # the indexes downloaded files keep; the cost is bounded by that
        # day's upload count, not the source's size.
        this_item = self._episode_sort_key()
        sql_day, legacy_day = self._same_day_others()
        sql_rows = (
            (
                (
                    _aware_utc(other.episode_date_sort),
                    _aware_utc(other.created),
                    other.key,
                ),
                other,
            )
            for other in sql_day.only(
                'pk', 'key', 'created', 'downloaded', 'media_file',
            )
        )
        legacy_rows = (
            (other._episode_sort_key(), other) for other in legacy_day()
        )
        before = 0
        frozen = []
        for sort_key, other in chain(sql_rows, legacy_rows):
            if sort_key < this_item:
                before += 1
            index = other._frozen_day_index(media_format, mmdd)
            if index is not None:
                frozen.append((sort_key, index))
        if not frozen:
            return before + 1

        rank = before + 1 - sum(
            1 for sort_key, _ in frozen if sort_key < this_item
        )
        taken = {index for _, index in frozen}
        index = 0
        while rank:
            index += 1
            if index not in taken:
                rank -= 1
        return index

    def _frozen_day_index(self, media_format, mmdd):
        '''
            The same-day index this media's downloaded file name carries
            for `mmdd`, or `None`.
        '''
        if not (self.downloaded and self.media_file):
            return None
        return _episode_day_index_from_name(
            str(self.media_file.name), media_format, mmdd,
        )

    @property
    def episode_yyyy(self):
        '''4-digit year of `episode_date`, for `media_format`.'''
        return self.episode_date.strftime('%Y')

    def _episode_mmdd_and_index(self):
        '''
            Returns ("MMDD" of `episode_date`, same-day index), logging a
            warning when the index exceeds the two digits
            `episode_mmddnn` normally uses.
        '''
        day_index = self._episode_day_index()
        if day_index > EPISODE_DAY_INDEX_TWO_DIGIT_MAX:
            log.warning(
                f'Media.episode_mmddnn: more than 99 same-day items for '
                f'source {self.source} on {self.episode_date.date()}: '
                f'{self.key} is number {day_index}'
            )
        return self.episode_date.strftime('%m%d'), day_index

    @staticmethod
    def _nfo_episode_number_for(mmdd, day_index):
        '''
            The single formula for the disjoint NFO/overflow episode
            number for a given (MMDD, day_index) pair -- shared by
            `nfo_episode_number` and, for `day_index > 99`,
            `episode_mmddnn`, so the two can never drift from each
            other. See `nfo_episode_number`'s docstring for the two
            ranges this produces.
        '''
        if day_index <= EPISODE_DAY_INDEX_TWO_DIGIT_MAX:
            return int(mmdd) * 100 + day_index
        return (
            EPISODE_OVERFLOW_BASE
            + int(mmdd) * EPISODE_OVERFLOW_MMDD_FACTOR
            + day_index
        )

    @property
    def episode_mmddnn(self):
        '''
            "MMDD" (from `episode_date`) plus the same-day index from
            `_same_day_index`, zero-padded to two digits, e.g. '091401' for
            the first item on September 14th.

            More than 99 same-day items logs a warning and, instead of
            naively concatenating the unpadded (3+ digit) index (which,
            for a variable-width MMDD + index string, can produce the
            same digits as a *different* (MMDD, index) pair once parsed
            back with `int()` -- e.g. '0101' + '110' and '1011' + '10'
            both give 101110), returns `str(nfo_episode_number)`: the
            same disjoint, overflow-range number `nfo_episode_number`
            uses for the NFO `<episode>` value (both go through
            `_nfo_episode_number_for`). This keeps
            `int(episode_mmddnn) == nfo_episode_number` true in every
            case, so the filename and the NFO always agree on this
            item's number.
        '''
        mmdd, day_index = self._episode_mmdd_and_index()
        if day_index > EPISODE_DAY_INDEX_TWO_DIGIT_MAX:
            return str(self._nfo_episode_number_for(mmdd, day_index))
        return f'{mmdd}{day_index:02}'

    @property
    def nfo_episode_number(self):
        '''
            The non-playlist NFO <episode> value, computed from the same
            (MMDD, day_index) pair as `episode_mmddnn` without ever giving
            two such pairs the same number:

            - index <= 99: `int(mmdd) * 100 + day_index`, e.g. 91401 for
              ('0914', 1) (at most 123199) -- equal to `int(episode_mmddnn)`
              for that range, since `episode_mmddnn` is `f'{mmdd}{day_index:02}'`.
            - index > 99: 10_000_000 + MMDD * 10_000 + index, a range that
              cannot overlap the first one. `int()` of a variable-width
              '0101' + '110' would otherwise equal '1011' + '10'. These
              items sort after the regular episodes of their season.
              `episode_mmddnn` returns `str()` of this same value for
              index > 99, so `int(episode_mmddnn) == nfo_episode_number`
              always holds.
        '''
        mmdd, day_index = self._episode_mmdd_and_index()
        return self._nfo_episode_number_for(mmdd, day_index)

    def calculate_episode_number(self):
        if self.source.is_playlist:
            sorted_media = Media.objects.filter(
                source=self.source,
                metadata__isnull=False,
            ).order_by(
                'published',
                'created',
                'key',
            )
        else:
            self_year = self.created.year # unlikely to be accurate
            if self.published:
                self_year = self.published.year
            elif self.has_metadata and self.upload_date:
                self_year = self.upload_date.year
            elif self.download_date:
                # also, unlikely to be accurate
                self_year = self.download_date.year
            sorted_media = Media.objects.filter(
                source=self.source,
                metadata__isnull=False,
                published__year=self_year,
            ).order_by(
                'published',
                'created',
                'key',
            )
        for counter, media in enumerate(sorted_media, start=1):
            if media == self:
                return counter

    def get_episode_str(self, use_padding=False):
        episode_number = self.calculate_episode_number()
        if not episode_number:
            return ''

        if use_padding:
            return f'{episode_number:02}'

        return str(episode_number)

    def rename_files(self):
        if self.downloaded and self.media_file:
            old_video_path = Path(self.media_file.path)
            new_video_path = Path(get_media_file_path(self, None))
            if old_video_path == new_video_path:
                return
            if old_video_path.exists() and not new_video_path.exists():
                old_video_path = old_video_path.resolve(strict=True)

                # move video to destination
                mkdir_p(new_video_path.parent)
                log.debug(f'{self!s}: {old_video_path!s} => {new_video_path!s}')
                old_video_path.rename(new_video_path)
                log.info(f'Renamed video file for: {self!s}')

                # collect the list of files to move
                # this should not include the video we just moved
                (old_prefix_path, old_stem) = directory_and_stem(old_video_path)
                other_paths = list(old_prefix_path.glob(glob_quote(old_stem) + '*'))
                log.info(f'Collected {len(other_paths)} other paths for: {self!s}')

                # adopt orphaned files, if possible
                fuzzy_paths = list()
                media_format = str(self.source.media_format)
                top_dir_path = Path(self.source.directory_path)
                if '{key}' in media_format:
                    fuzzy_paths = list(top_dir_path.rglob('*' + glob_quote(str(self.key)) + '*'))
                    log.info(f'Collected {len(fuzzy_paths)} fuzzy paths for: {self!s}')

                if new_video_path.exists():
                    new_video_path = new_video_path.resolve(strict=True)

                    # update the media_file in the db
                    self.media_file.name = str(new_video_path.relative_to(self.media_file.storage.location))
                    self.skip = False
                    self.save(update_fields=('media_file', 'skip'))
                    log.info(f'Updated "media_file" in the database for: {self!s}')

                    (new_prefix_path, new_stem) = directory_and_stem(new_video_path)

                    # move and change names to match stem
                    for other_path in other_paths:
                        # it should exist, but check anyway
                        if not other_path.exists():
                            continue

                        old_file_str = other_path.name
                        new_file_str = new_stem + old_file_str[len(old_stem):]
                        new_file_path = Path(new_prefix_path / new_file_str)
                        if new_file_path == other_path:
                            continue
                        log.debug(f'Considering replace for: {self!s}\n\t{other_path!s}\n\t{new_file_path!s}')
                        # do not move the file we just updated in the database
                        # doing that loses track of the `Media.media_file` entirely
                        if not new_video_path.samefile(other_path):
                            log.debug(f'{self!s}: {other_path!s} => {new_file_path!s}')
                            other_path.replace(new_file_path)

                    for fuzzy_path in fuzzy_paths:
                        if not fuzzy_path.exists():
                            continue
                        (fuzzy_prefix_path, fuzzy_stem) = directory_and_stem(fuzzy_path, True)
                        old_file_str = fuzzy_path.name
                        new_file_str = new_stem + old_file_str[len(fuzzy_stem):]
                        new_file_path = Path(new_prefix_path / new_file_str)
                        if new_file_path == fuzzy_path:
                            continue
                        log.debug(f'Considering rename for: {self!s}\n\t{fuzzy_path!s}\n\t{new_file_path!s}')
                        # it quite possibly was renamed already
                        if not (new_video_path.samefile(fuzzy_path) or new_file_path.exists()):
                            log.debug(f'{self!s}: {fuzzy_path!s} => {new_file_path!s}')
                            fuzzy_path.rename(new_file_path)

                    # The thumbpath, <season> or <episode> inside the .nfo
                    # file may have changed
                    if self.source.write_nfo:
                        write_text_file(new_prefix_path / self.nfopath.name, self.nfoxml)
                        log.info(f'Wrote new ".nfo" file for: {self!s}')

                    # try to remove empty dirs
                    parent_dir = old_video_path.parent
                    stop_dir = self.source.directory_path
                    try:
                        while parent_dir.is_relative_to(stop_dir):
                            parent_dir.rmdir()
                            log.info(f'Removed empty directory: {parent_dir!s}')
                            parent_dir = parent_dir.parent
                    except OSError:
                        pass


# add imported functions
Media.copy_thumbnail = copy_thumbnail
Media.download_checklist = download_checklist
Media.download_finished = download_finished
Media.failed_format = failed_format
Media.refresh_formats = refresh_formats
Media.wait_for_premiere = wait_for_premiere
Media.write_nfo_file = write_nfo_file

