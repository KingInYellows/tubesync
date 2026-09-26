'''
    T1: stable date-based episode numbering.

    Covers `Media.episode_date`, `Media.episode_yyyy`/`episode_mmddnn`
    (and their shared `_same_day_index` helper), the numbers a downloaded
    file keeps, `Media.title_full_bounded`,
    the new `nfoxml` season/episode behaviour for non-playlist sources and
    for playlists filed by the date scheme, and that other playlists keep
    the legacy `calculate_episode_number` numbering.

    Uses the same fixed Source configuration as `test_media.py` /
    `test_filepath.py` (1080p/VP9/OPUS, `prefer_60fps=False`,
    `prefer_hdr=False`) so the checked-in metadata fixtures format
    successfully -- deterministic, no network, no `/downloads` dependence.
'''
import json
import logging
import tempfile
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone as dt_timezone
from pathlib import Path
from unittest.mock import patch
from xml.etree import ElementTree

from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist
from django.db.models.functions import Coalesce
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from medianest_bridge.source_forms import default_form_data, run_edit_source_checks
from sync.choices import (
    Val, Fallback, SourceResolution,
    YouTube_AudioCodec, YouTube_VideoCodec,
    YouTube_SourceType,
)
from sync.forms import SourceForm
from sync.models import Media, Metadata, Source
from sync.models._migrations import media_file_storage
from sync.models.media import (
    _episode_day_index_from_name, _parse_episode_token,
)

from .fixtures import all_test_metadata

metadata = all_test_metadata['boring']  # upload_date 2017-09-11
metadata_hdr = all_test_metadata['hdr']  # upload_date 2016-11-09


def aware(*args, **kwargs):
    '''
        Builds an aware UTC datetime explicitly, rather than via
        `timezone.make_aware(datetime(...))` (which uses Django's active
        or `settings.TIME_ZONE` timezone) -- these tests assert on UTC
        calendar days and MMDD strings, so they must not depend on the
        environment's `TZ`.
    '''
    return datetime(*args, **kwargs, tzinfo=dt_timezone.utc)


class EpisodeNumberingTestCase(TestCase):

    def setUp(self):
        # Disable general logging for test case
        logging.disable(logging.CRITICAL)
        self.source = Source.objects.create(
            source_type=Val(YouTube_SourceType.CHANNEL),
            key='testkey',
            name='testname',
            directory='testdirectory',
            media_format=settings.MEDIA_FORMATSTR_DEFAULT,
            index_schedule=3600,
            delete_old_media=False,
            days_to_keep=14,
            source_resolution=Val(SourceResolution.VIDEO_1080P),
            source_vcodec=Val(YouTube_VideoCodec.VP9),
            source_acodec=Val(YouTube_AudioCodec.OPUS),
            prefer_60fps=False,
            prefer_hdr=False,
            fallback=Val(Fallback.FAIL),
        )

    def test_same_day_uploads_are_numbered_in_published_order(self):
        first = Media.objects.create(
            key='same-day-1', source=self.source, metadata=metadata,
            published=aware(2026, 3, 5, 8, 0, 0),
        )
        second = Media.objects.create(
            key='same-day-2', source=self.source, metadata=metadata,
            published=aware(2026, 3, 5, 20, 0, 0),
        )
        self.assertEqual(first.episode_mmddnn, '030501')
        self.assertEqual(second.episode_mmddnn, '030502')
        self.assertEqual(first.episode_yyyy, '2026')

    def test_backfill_on_an_earlier_day_does_not_change_a_later_days_numbers(self):
        day_two_first = Media.objects.create(
            key='day-two-1', source=self.source, metadata=metadata,
            published=aware(2026, 1, 15, 8, 0, 0),
        )
        day_two_second = Media.objects.create(
            key='day-two-2', source=self.source, metadata=metadata,
            published=aware(2026, 1, 15, 20, 0, 0),
        )
        self.assertEqual(day_two_first.episode_mmddnn, '011501')
        self.assertEqual(day_two_second.episode_mmddnn, '011502')
        # Backfill an older video published on an earlier day entirely.
        Media.objects.create(
            key='backfilled-earlier-day', source=self.source, metadata=metadata,
            published=aware(2026, 1, 10, 12, 0, 0),
        )
        # Day 2026-01-15's numbers are untouched by a backfill on 2026-01-10.
        self.assertEqual(day_two_first.episode_mmddnn, '011501')
        self.assertEqual(day_two_second.episode_mmddnn, '011502')

    def test_backfill_on_the_same_day_shifts_that_days_later_numbers(self):
        '''
            Documents expected behaviour, not a bug: because the index is a
            live position (not a number frozen at creation time), inserting
            an earlier-published item into an *already-numbered* day shifts
            every later same-day item's number. Only cross-day isolation
            (the previous test) is guaranteed. A downloaded file of a
            source filed by `{episode_mmddnn}` keeps its number instead
            (`FrozenEpisodeNumberTestCase`).
        '''
        morning = Media.objects.create(
            key='same-day-morning', source=self.source, metadata=metadata,
            published=aware(2026, 2, 1, 8, 0, 0),
        )
        evening = Media.objects.create(
            key='same-day-evening', source=self.source, metadata=metadata,
            published=aware(2026, 2, 1, 20, 0, 0),
        )
        self.assertEqual(morning.episode_mmddnn, '020101')
        self.assertEqual(evening.episode_mmddnn, '020102')
        # Backfill an earlier item on the SAME day.
        Media.objects.create(
            key='same-day-earliest', source=self.source, metadata=metadata,
            published=aware(2026, 2, 1, 5, 0, 0),
        )
        self.assertEqual(morning.episode_mmddnn, '020102')
        self.assertEqual(evening.episode_mmddnn, '020103')

    def test_published_none_groups_by_the_upload_date_it_encodes(self):
        # Two items with published=NULL, different metadata (different
        # upload_date), forced onto the same `created` UTC calendar day.
        # Metadata is set on the legacy `Media.metadata` column directly
        # (no `ingest_metadata` call, so no `new_metadata` row), which is
        # exactly the narrow case `episode_date`'s `upload_date` fallback
        # step and `_same_day_index`'s residual Python group still exist
        # for -- see both docstrings in sync/models/media.py.
        first = Media.objects.create(
            key='nullpub-1', source=self.source, metadata=metadata,
        )
        second = Media.objects.create(
            key='nullpub-2', source=self.source, metadata=metadata_hdr,
        )
        shared_day = aware(2026, 4, 1, 6, 0, 0)
        Media.objects.filter(pk=first.pk).update(created=shared_day)
        Media.objects.filter(pk=second.pk).update(
            created=shared_day + timedelta(hours=6),
        )
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertIsNone(first.published)
        self.assertIsNone(second.published)
        # episode_date/episode_yyyy fall back to each item's own upload_date.
        self.assertEqual(first.episode_yyyy, '2017')
        self.assertEqual(second.episode_yyyy, '2016')
        # The same-day index groups by that same upload_date, not by the
        # shared `created` day, so each is first on its own day.
        self.assertEqual(first.episode_mmddnn, '091101')
        self.assertEqual(second.episode_mmddnn, '110901')

    def test_same_encoded_date_never_shares_an_index(self):
        # Same upload_date (2017-09-11), published=NULL, created on
        # different days: must not both be '01'. Metadata is set on the
        # legacy column directly (see comment in the previous test).
        early = Media.objects.create(
            key='samedate-1', source=self.source, metadata=metadata,
        )
        late = Media.objects.create(
            key='samedate-2', source=self.source, metadata=metadata,
        )
        Media.objects.filter(pk=early.pk).update(created=aware(2026, 4, 1))
        Media.objects.filter(pk=late.pk).update(created=aware(2026, 5, 1))
        # A published item on the same UTC day shares the same counter.
        published = Media.objects.create(
            key='samedate-3', source=self.source,
            published=aware(2017, 9, 11, 12, 0, 0),
        )
        early.refresh_from_db()
        late.refresh_from_db()
        self.assertEqual(
            sorted([
                early.episode_mmddnn,
                late.episode_mmddnn,
                published.episode_mmddnn,
            ]),
            ['091101', '091102', '091103'],
        )
        # Unpublished items are dated midnight UTC, so they sort first.
        self.assertEqual(early.episode_mmddnn, '091101')
        self.assertEqual(late.episode_mmddnn, '091102')
        self.assertEqual(published.episode_mmddnn, '091103')

    def test_metadata_only_in_the_related_table_is_grouped_by_upload_date(self):
        # import-existing-media style: Media.metadata stays NULL and the
        # metadata lives only in the related `new_metadata` row.
        early = Media.objects.create(key='related-1', source=self.source)
        late = Media.objects.create(key='related-2', source=self.source)
        for item in (early, late):
            item.ingest_metadata(json.loads(metadata))
        Media.objects.filter(pk=early.pk).update(created=aware(2026, 4, 1))
        Media.objects.filter(pk=late.pk).update(created=aware(2026, 5, 1))
        early.refresh_from_db()
        late.refresh_from_db()
        self.assertIsNone(early.metadata)
        self.assertIsNone(early.published)
        self.assertEqual(early.episode_mmddnn, '091101')
        self.assertEqual(late.episode_mmddnn, '091102')

    def test_reindex_published_change_keeps_numbers_once_metadata_ingested(self):
        '''
            sync.tasks.index_source rewrites `Media.published` on every
            re-index (its `db_fields_media` bulk `save_db_batch` includes
            `'published'`) from yt-dlp's forced
            `youtubetab:approximate_date=true` tab listing, which can
            change between crawls (yt-dlp derives it from relative text
            like "3 weeks ago"). Once `new_metadata.published` is set --
            which happens for essentially every indexed item, via
            sync.tasks.migrate_to_metadata / download_media_metadata --
            `episode_date` must keep using that stable value instead of
            drifting with `published`. This directly simulates that
            re-index bulk update.
        '''
        media = Media.objects.create(key='reindex-stable', source=self.source)
        media.ingest_metadata(json.loads(metadata))  # upload_date 2017-09-11
        before_mmddnn = media.episode_mmddnn
        before_nfo = media.nfo_episode_number
        self.assertEqual(before_mmddnn, '091101')
        # Simulate index_source's re-index bulk update overwriting
        # Media.published with a fresh (and here, deliberately different
        # and wrong) approximate date.
        Media.objects.filter(pk=media.pk).update(
            published=aware(2030, 1, 1, 0, 0, 0),
        )
        media.refresh_from_db()
        self.assertEqual(media.published, aware(2030, 1, 1, 0, 0, 0))
        self.assertEqual(media.episode_mmddnn, before_mmddnn)
        self.assertEqual(media.nfo_episode_number, before_nfo)

    def test_no_published_no_metadata_same_day_numbered_by_created_then_key(self):
        '''
            The one `_same_day_index` group `test_metadata_only_in_...`
            and `test_identical_published_ties_break_...` don't cover:
            no `published`, no metadata at all (so `episode_date` falls
            all the way to `created`), two items on the same UTC day.
        '''
        day = aware(2026, 11, 3, 0, 0, 0)
        b_item = Media.objects.create(key='nometa-b', source=self.source)
        a_item = Media.objects.create(key='nometa-a', source=self.source)
        Media.objects.filter(pk=b_item.pk).update(created=day + timedelta(hours=2))
        Media.objects.filter(pk=a_item.pk).update(created=day + timedelta(hours=10))
        b_item.refresh_from_db()
        a_item.refresh_from_db()
        self.assertIsNone(b_item.published)
        self.assertIsNone(a_item.published)
        with self.assertRaises(ObjectDoesNotExist):
            b_item.new_metadata
        # Earlier `created` wins.
        self.assertEqual(b_item.episode_mmddnn, '110301')
        self.assertEqual(a_item.episode_mmddnn, '110302')
        # Same `created` too: `key` decides.
        Media.objects.filter(pk=a_item.pk).update(created=b_item.created)
        a_item.refresh_from_db()
        b_item.refresh_from_db()
        self.assertEqual(a_item.episode_mmddnn, '110301')
        self.assertEqual(b_item.episode_mmddnn, '110302')

    def test_sql_coalesce_matches_python_episode_date_for_mixed_sources(self):
        '''
            `_same_day_index`'s SQL `COUNT` and `episode_date`'s Python
            computation must use the exact same precedence
            (`_episode_date_coalesce()` / `episode_date`'s docstring), or
            the two can disagree about which UTC day, and therefore which
            same-day index, an item belongs to. Directly compares the two
            for one item of each kind on the same day: `published` set,
            metadata-only (`new_metadata.published` set), and neither.
        '''
        day = aware(2026, 12, 5, 0, 0, 0)
        published_item = Media.objects.create(
            key='mixed-published', source=self.source,
            published=day + timedelta(hours=8),
        )
        metadata_item = Media.objects.create(key='mixed-metadata', source=self.source)
        metadata_item.ingest_metadata(json.loads(metadata))
        metadata_item.new_metadata.published = day + timedelta(hours=4)
        metadata_item.new_metadata.save(update_fields=['published'])
        neither_item = Media.objects.create(key='mixed-neither', source=self.source)
        Media.objects.filter(pk=neither_item.pk).update(
            created=day + timedelta(hours=12),
        )

        items = (published_item, metadata_item, neither_item)
        for item in items:
            item.refresh_from_db()
        annotated_by_key = {
            m.key: m
            for m in Media.objects.filter(source=self.source).annotate(
                episode_date_sql=Coalesce(
                    'new_metadata__published', 'published', 'created',
                ),
            )
        }
        for item in items:
            with self.subTest(item=item.key):
                self.assertEqual(
                    annotated_by_key[item.key].episode_date_sql,
                    item.episode_date,
                )

        # And the resulting same-day ordering matches the manually forced
        # dates: metadata (4h) < published (8h) < neither (12h).
        self.assertEqual(metadata_item.episode_mmddnn, '120501')
        self.assertEqual(published_item.episode_mmddnn, '120502')
        self.assertEqual(neither_item.episode_mmddnn, '120503')

    def test_identical_published_ties_break_by_created_then_key(self):
        when = aware(2026, 6, 2, 12, 0, 0)
        b_item = Media.objects.create(
            key='tie-b', source=self.source, published=when,
        )
        a_item = Media.objects.create(
            key='tie-a', source=self.source, published=when,
        )
        Media.objects.filter(pk=b_item.pk).update(created=aware(2026, 6, 1))
        Media.objects.filter(pk=a_item.pk).update(created=aware(2026, 6, 2))
        a_item.refresh_from_db()
        b_item.refresh_from_db()
        # Same published: the earlier `created` wins.
        self.assertEqual(b_item.episode_mmddnn, '060201')
        self.assertEqual(a_item.episode_mmddnn, '060202')
        # Same published and created: `key` decides.
        Media.objects.filter(pk=a_item.pk).update(created=b_item.created)
        a_item.refresh_from_db()
        b_item.refresh_from_db()
        self.assertEqual(a_item.episode_mmddnn, '060201')
        self.assertEqual(b_item.episode_mmddnn, '060202')

    def test_same_day_index_is_per_source(self):
        other_source = Source.objects.create(
            source_type=Val(YouTube_SourceType.CHANNEL),
            key='otherkey',
            name='othername',
            directory='otherdirectory',
            media_format=settings.MEDIA_FORMATSTR_DEFAULT,
            index_schedule=3600,
        )
        when = aware(2026, 7, 3, 9, 0, 0)
        mine = Media.objects.create(
            key='per-source-1', source=self.source,
            published=when + timedelta(hours=1),
        )
        Media.objects.create(
            key='per-source-2', source=other_source, published=when,
        )
        self.assertEqual(mine.episode_mmddnn, '070301')

    def test_more_than_99_same_day_items_logs_a_warning_and_matches_nfo_number(self):
        base = aware(2026, 8, 1, 0, 0, 0)
        fillers = [
            Media(
                key=f'filler-{i:03}', source=self.source,
                published=base + timedelta(minutes=i),
            )
            for i in range(99)
        ]
        Media.objects.bulk_create(fillers)
        hundredth = Media.objects.create(
            key='filler-099', source=self.source, metadata=metadata,
            published=base + timedelta(minutes=99),
        )
        with patch('sync.models.media.log') as mock_log:
            mmddnn = hundredth.episode_mmddnn
        # Past 99 same-day items, episode_mmddnn is nfo_episode_number's
        # string (not "mmdd" + the unpadded index concatenated), so the
        # two never disagree once parsed back with int() -- see
        # test_nfo_episode_numbers_never_collide_past_99_same_day_items
        # for why the naive concatenation was unsafe.
        self.assertEqual(mmddnn, str(hundredth.nfo_episode_number))
        self.assertEqual(int(mmddnn), hundredth.nfo_episode_number)
        mock_log.warning.assert_called_once()
        warning_args = mock_log.warning.call_args[0][0]
        self.assertIn('filler-099', warning_args)
        self.assertIn(str(self.source), warning_args)

    def test_nfo_episode_numbers_never_collide_past_99_same_day_items(self):
        # '0101' + '110' and '1011' + '10' would both int() to 101110.
        # episode_mmddnn now returns nfo_episode_number's overflow string
        # once the same-day index exceeds 99, so int(episode_mmddnn) and
        # nfo_episode_number can never collide with a different day's
        # <=99-index value either.
        jan_first = aware(2026, 1, 1, 0, 0, 0)
        Media.objects.bulk_create([
            Media(
                key=f'jan-{i:03}', source=self.source,
                published=jan_first + timedelta(minutes=i),
            )
            for i in range(110)
        ])
        oct_eleventh = aware(2026, 10, 11, 0, 0, 0)
        Media.objects.bulk_create([
            Media(
                key=f'oct-{i:03}', source=self.source,
                published=oct_eleventh + timedelta(minutes=i),
            )
            for i in range(10)
        ])
        jan_110 = Media.objects.get(key='jan-109')
        oct_10 = Media.objects.get(key='oct-009')
        self.assertEqual(oct_10.episode_mmddnn, '101110')
        self.assertEqual(oct_10.nfo_episode_number, 101110)
        self.assertEqual(jan_110.nfo_episode_number, 10_000_000 + 101 * 10_000 + 110)
        self.assertEqual(jan_110.episode_mmddnn, str(jan_110.nfo_episode_number))
        self.assertNotEqual(jan_110.nfo_episode_number, oct_10.nfo_episode_number)
        self.assertEqual(int(jan_110.episode_mmddnn), jan_110.nfo_episode_number)
        self.assertNotEqual(int(jan_110.episode_mmddnn), int(oct_10.episode_mmddnn))

    def test_int_episode_mmddnn_matches_nfo_episode_number_across_the_99_boundary(self):
        '''
            Exercises the exact boundary `_episode_mmdd_and_index`/
            `episode_mmddnn` switch on: day_index 99 (still the plain
            2-digit form), 100 and 110 (both overflowed). At every one of
            them, `int(episode_mmddnn) == nfo_episode_number`, and every
            overflowed value sits above the highest possible <=99-index
            value (mmdd=1231, index=99 -> 123199), so it can never be
            confused with one.
        '''
        day = aware(2027, 2, 2, 0, 0, 0)
        Media.objects.bulk_create([
            Media(
                key=f'boundary-{i:03}', source=self.source,
                published=day + timedelta(minutes=i),
            )
            for i in range(110)
        ])
        ninety_nine = Media.objects.get(key='boundary-098')  # 99th item
        one_hundred = Media.objects.get(key='boundary-099')  # 100th item
        one_hundred_ten = Media.objects.get(key='boundary-109')  # 110th item
        self.assertEqual(ninety_nine.episode_mmddnn, '020299')
        self.assertEqual(ninety_nine.nfo_episode_number, 202 * 100 + 99)
        self.assertEqual(
            int(ninety_nine.episode_mmddnn), ninety_nine.nfo_episode_number,
        )
        for item, day_index in ((one_hundred, 100), (one_hundred_ten, 110)):
            with self.subTest(day_index=day_index):
                expected_nfo_number = 10_000_000 + 202 * 10_000 + day_index
                self.assertEqual(item.nfo_episode_number, expected_nfo_number)
                self.assertEqual(item.episode_mmddnn, str(expected_nfo_number))
                self.assertEqual(int(item.episode_mmddnn), item.nfo_episode_number)
                # Above the highest possible <=99-index value from any day.
                self.assertGreater(item.nfo_episode_number, 123199)

    def test_title_full_bounded_respects_byte_limit_and_strips_slash(self):
        title = 'café🎉/' * 40
        media = Media(
            source=self.source, key='longtitle', metadata=metadata, title=title,
        )
        bounded = media.title_full_bounded
        self.assertNotIn('/', bounded)
        self.assertLessEqual(len(bounded.encode('utf-8')), 150)
        # No dangling partial multibyte sequence left by the truncation.
        self.assertEqual(bounded, bounded.encode('utf-8').decode('utf-8'))
        self.assertEqual(bounded, bounded.strip())

    def test_title_full_bounded_short_title_is_unchanged_but_stripped(self):
        media = Media(
            source=self.source, key='shorttitle', metadata=metadata,
            title='  Some Title  ',
        )
        self.assertEqual(media.title_full_bounded, 'Some Title')

    def test_unsaved_media_does_not_crash_on_missing_created(self):
        '''
            `created` (auto_now_add) is only None before the first save.
            episode_date/episode_mmddnn must fall back to "now" for that
            case instead of passing None into a `created__lt` query lookup
            (which Django raises on) or `timezone.make_aware(None)` (which
            raises AttributeError).
        '''
        unsaved = Media(source=self.source, key='unsaved-media')
        self.assertIsNone(unsaved.created)
        self.assertIsNone(unsaved.published)
        before = timezone.now()
        episode_date = unsaved.episode_date
        after = timezone.now()
        self.assertTrue(before <= episode_date <= after)
        # Must not raise, and must use "now"'s MMDD with a same-day index.
        self.assertEqual(
            unsaved.episode_mmddnn, episode_date.strftime('%m%d') + '01',
        )

    def test_new_format_keys_pass_bridge_media_format_validation(self):
        media_format = (
            'Season {episode_yyyy}/s{episode_yyyy}e{episode_mmddnn} - '
            '{title_full_bounded} [{key}].{ext}'
        )
        data = default_form_data()
        data.update(
            source_type=Val(YouTube_SourceType.CHANNEL),
            key='newchannelkey',
            name='New Channel',
            directory='newchanneldir',
            media_format=media_format,
        )
        form = SourceForm(data=data)
        self.assertTrue(form.is_valid(), form.errors)
        run_edit_source_checks(form)
        self.assertNotIn('media_format', form.errors)
        saved_source = form.save(commit=False)
        self.assertNotEqual('', saved_source.get_example_media_format())

    def test_media_filename_uses_new_format_keys(self):
        self.source.media_format = (
            'Season {episode_yyyy}/s{episode_yyyy}e{episode_mmddnn} - '
            '{title_full_bounded} [{key}].{ext}'
        )
        episode_media = Media.objects.create(
            key='key', source=self.source, metadata=metadata,
            published=aware(2026, 9, 14, 10, 0, 0),
        )
        # Media.save() derives `title` from metadata when metadata is set,
        # so override it in-memory afterward rather than via the constructor.
        episode_media.title = 'Title'
        self.assertEqual(
            episode_media.filename,
            'Season 2026/s2026e091401 - Title [key].mkv',
        )

    def test_nfo_season_and_episode_for_a_channel(self):
        media = Media.objects.create(
            key='nfochannel', source=self.source, metadata=metadata,
            published=aware(2026, 9, 14, 10, 0, 0),
        )
        nfo_tree = ElementTree.fromstring(media.nfoxml)
        self.assertEqual(nfo_tree.find('season').text, '2026')
        self.assertEqual(nfo_tree.find('episode').text, '91401')

    def make_playlist_source(self, media_format):
        return Source.objects.create(
            source_type=Val(YouTube_SourceType.PLAYLIST),
            key='playlistkey',
            name='playlistname',
            directory='playlistdirectory',
            media_format=media_format,
            index_schedule=3600,
            delete_old_media=False,
            days_to_keep=14,
            source_resolution=Val(SourceResolution.VIDEO_1080P),
            source_vcodec=Val(YouTube_VideoCodec.VP9),
            source_acodec=Val(YouTube_AudioCodec.OPUS),
            prefer_60fps=False,
            prefer_hdr=False,
            fallback=Val(Fallback.FAIL),
        )

    def test_playlist_nfo_numbering_is_unchanged(self):
        playlist_source = self.make_playlist_source(
            settings.MEDIA_FORMATSTR_DEFAULT,
        )
        first = Media.objects.create(
            key='playlist-1', source=playlist_source, metadata=metadata,
            published=aware(2026, 1, 1, 0, 0, 0),
        )
        second = Media.objects.create(
            key='playlist-2', source=playlist_source, metadata=metadata,
            published=aware(2026, 1, 2, 0, 0, 0),
        )
        first_nfo = ElementTree.fromstring(first.nfoxml)
        second_nfo = ElementTree.fromstring(second.nfoxml)
        self.assertEqual(first_nfo.find('season').text, '1')
        self.assertEqual(first_nfo.find('episode').text, '1')
        self.assertEqual(second_nfo.find('season').text, '1')
        self.assertEqual(second_nfo.find('episode').text, '2')

    def test_date_filed_playlist_nfo_matches_its_filename(self):
        playlist_source = self.make_playlist_source(
            'Season {episode_yyyy}/s{episode_yyyy}e{episode_mmddnn} - '
            '{title_full_bounded} [{key}].{ext}',
        )
        media = Media.objects.create(
            key='playlist-1', source=playlist_source, metadata=metadata,
            published=aware(2026, 9, 14, 10, 0, 0),
        )
        nfo_tree = ElementTree.fromstring(media.nfoxml)
        self.assertEqual(nfo_tree.find('season').text, '2026')
        self.assertEqual(nfo_tree.find('episode').text, '91401')

    def test_playlist_date_scheme_detection_parses_format_fields(self):
        playlist_source = self.make_playlist_source(settings.MEDIA_FORMATSTR_DEFAULT)
        media = Media.objects.create(
            key='playlist-1', source=playlist_source, metadata=metadata,
            published=aware(2026, 9, 14, 10, 0, 0),
        )
        for media_format, expected_season in (
            ('Season {episode_yyyy}/s{episode_mmddnn:>8} [{key}].{ext}', '2026'),
            ('Season {episode_yyyy}/s{episode_mmddnn!s} [{key}].{ext}', '2026'),
            ('{{episode_mmddnn}} {key}.{ext}', '1'),
        ):
            with self.subTest(media_format=media_format):
                media.source.media_format = media_format
                nfo_tree = ElementTree.fromstring(media.nfoxml)
                self.assertEqual(nfo_tree.find('season').text, expected_season)


PLEX_FORMAT = (
    'Season {episode_yyyy}/s{episode_yyyy}e{episode_mmddnn} - '
    '{title_full_bounded} [{key}].{ext}'
)


@contextmanager
def temp_download_root():
    '''
        Points both the media storage and `settings.DOWNLOAD_ROOT` (which
        `write_text_file`'s callers are checked against) at one temporary
        directory, never the real downloads directory.
    '''
    with tempfile.TemporaryDirectory() as tmp_dir:
        with (
            override_settings(DOWNLOAD_ROOT=tmp_dir),
            patch.object(media_file_storage, 'location', tmp_dir),
        ):
            yield tmp_dir


class FrozenEpisodeNumberTestCase(TestCase):
    '''
        A downloaded file of a source filed by `{episode_mmddnn}` keeps the
        number its name carries; other same-day items number around it.
    '''

    def setUp(self):
        logging.disable(logging.CRITICAL)
        self.source = Source.objects.create(
            source_type=Val(YouTube_SourceType.CHANNEL),
            key='frozenkey',
            name='frozenname',
            directory='frozendirectory',
            media_format=PLEX_FORMAT,
            index_schedule=3600,
            delete_old_media=False,
            days_to_keep=14,
            source_resolution=Val(SourceResolution.VIDEO_1080P),
            source_vcodec=Val(YouTube_VideoCodec.VP9),
            source_acodec=Val(YouTube_AudioCodec.OPUS),
            prefer_60fps=False,
            prefer_hdr=False,
            fallback=Val(Fallback.FAIL),
        )

    def make_media(self, key, published):
        return Media.objects.create(
            key=key, source=self.source, metadata=metadata,
            published=published,
        )

    def mark_downloaded(self, media, name=None):
        '''
            Marks `media` downloaded at `name` (default: its current
            filename) without saving through signals, and reloads it.
        '''
        if name is None:
            name = str(Path(self.source.directory) / media.filename)
        Media.objects.filter(pk=media.pk).update(
            downloaded=True, media_file=name,
        )
        return Media.objects.get(pk=media.pk)

    def test_downloaded_file_keeps_its_number_when_an_earlier_item_arrives(self):
        later = self.mark_downloaded(
            self.make_media('later', aware(2026, 3, 5, 10, 0, 0)),
        )
        self.assertEqual(later.episode_mmddnn, '030501')
        earlier = self.make_media('earlier', aware(2026, 3, 5, 9, 0, 0))
        # Live order would put `earlier` first and give it 030501, the
        # number `later`'s file already has.
        self.assertEqual(earlier._same_day_index(), 1)
        self.assertEqual(later.episode_mmddnn, '030501')
        self.assertEqual(earlier.episode_mmddnn, '030502')
        self.assertNotEqual(earlier.filepath, later.filepath)
        self.assertEqual(later.nfo_episode_number, 30501)
        self.assertEqual(earlier.nfo_episode_number, 30502)

    def test_other_items_take_the_free_numbers_in_same_day_order(self):
        noon = self.mark_downloaded(
            self.make_media('noon', aware(2026, 3, 5, 12, 0, 0)),
        )
        morning = self.make_media('morning', aware(2026, 3, 5, 9, 0, 0))
        afternoon = self.make_media('afternoon', aware(2026, 3, 5, 13, 0, 0))
        self.assertEqual(noon.episode_mmddnn, '030501')
        self.assertEqual(morning.episode_mmddnn, '030502')
        self.assertEqual(afternoon.episode_mmddnn, '030503')
        # Downloading in any order keeps every number where it was.
        afternoon = self.mark_downloaded(afternoon)
        self.assertEqual(morning.episode_mmddnn, '030502')
        morning = self.mark_downloaded(morning)
        self.assertEqual(
            [noon.episode_mmddnn, morning.episode_mmddnn, afternoon.episode_mmddnn],
            ['030501', '030502', '030503'],
        )
        dawn = self.make_media('dawn', aware(2026, 3, 5, 6, 0, 0))
        self.assertEqual(dawn.episode_mmddnn, '030504')

    def test_title_change_keeps_the_frozen_number(self):
        media = self.make_media('retitled', aware(2026, 3, 5, 10, 0, 0))
        media = self.mark_downloaded(
            media,
            'frozendirectory/Season 2026/s2026e030507 - Old Title [retitled].webm',
        )
        media.title = 'New Title'
        self.assertEqual(media.episode_mmddnn, '030507')
        self.assertEqual(
            media.filename, 'Season 2026/s2026e030507 - New Title [retitled].mkv',
        )

    def test_legacy_named_download_is_numbered_live(self):
        media = self.mark_downloaded(
            self.make_media('legacy', aware(2026, 3, 5, 10, 0, 0)),
            'frozendirectory/20260305_frozenname_legacy_title_legacy.mkv',
        )
        earlier = self.make_media('legacy-earlier', aware(2026, 3, 5, 9, 0, 0))
        self.assertEqual(earlier.episode_mmddnn, '030501')
        self.assertEqual(media.episode_mmddnn, '030502')

    def test_file_numbered_for_another_day_is_renumbered(self):
        media = self.mark_downloaded(
            self.make_media('moved-day', aware(2026, 3, 5, 10, 0, 0)),
            'frozendirectory/Season 2026/s2026e030403 - T [moved-day].mkv',
        )
        self.assertIsNone(
            media._frozen_day_index(PLEX_FORMAT, '0305'),
        )
        self.assertEqual(media.episode_mmddnn, '030501')

    def test_overflow_number_is_read_back(self):
        overflow = str(Media._nfo_episode_number_for('0305', 100))
        media = self.mark_downloaded(
            self.make_media('overflow', aware(2026, 3, 5, 10, 0, 0)),
            f'frozendirectory/Season 2026/s2026e{overflow} - T [overflow].mkv',
        )
        self.assertEqual(media.episode_mmddnn, overflow)
        self.assertEqual(media.nfo_episode_number, int(overflow))

    def test_a_downloaded_item_without_a_frozen_number_skips_taken_ones(self):
        self.mark_downloaded(
            self.make_media('taken', aware(2026, 3, 5, 12, 0, 0)),
            'frozendirectory/Season 2026/s2026e030501 - T [taken].mkv',
        )
        legacy = self.mark_downloaded(
            self.make_media('legacy-first', aware(2026, 3, 5, 9, 0, 0)),
            'frozendirectory/legacy-first.mkv',
        )
        self.assertEqual(legacy.episode_mmddnn, '030502')

    def test_query_count_does_not_grow_with_the_days_downloads(self):
        def queries_for_a_new_item(downloads):
            for number in range(downloads):
                self.mark_downloaded(self.make_media(
                    f'count-{downloads}-{number}',
                    aware(2026, 4, downloads, 10, number, 0),
                ))
            item = self.make_media(
                f'count-{downloads}-new', aware(2026, 4, downloads, 9, 0, 0),
            )
            with CaptureQueriesContext(connection) as queries:
                item.episode_mmddnn
            # One pass over the day's rows, not a COUNT plus a fetch.
            self.assertFalse(any(
                'COUNT(' in query['sql'] for query in queries.captured_queries
            ))
            return len(queries)

        self.assertEqual(queries_for_a_new_item(1), queries_for_a_new_item(6))

    def test_format_without_episode_mmddnn_stays_live(self):
        self.source.media_format = settings.MEDIA_FORMATSTR_DEFAULT
        self.source.save()
        later = self.mark_downloaded(
            self.make_media('plain-later', aware(2026, 3, 5, 10, 0, 0)),
            'frozendirectory/Season 2026/s2026e030501 - T [plain-later].mkv',
        )
        self.make_media('plain-earlier', aware(2026, 3, 5, 9, 0, 0))
        self.assertEqual(later.episode_mmddnn, '030502')

    def delete_for_good(self, media):
        '''
            Deletes `media`, then the skipped placeholder row upstream's
            media_post_delete re-creates with the same key (a second
            delete, as a later cleanup_removed_media pass does, leaves
            none).
        '''
        media.delete()
        placeholder = Media.objects.get(source=self.source, key=media.key)
        placeholder.delete()
        self.assertFalse(
            Media.objects.filter(source=self.source, key=media.key).exists()
        )

    def test_a_deleted_siblings_placeholder_keeps_a_legacy_format_number(self):
        self.source.media_format = settings.MEDIA_FORMATSTR_DEFAULT
        self.source.save()
        first = self.mark_downloaded(
            self.make_media('hold-1', aware(2026, 3, 5, 8, 0, 0)),
        )
        self.mark_downloaded(self.make_media('hold-2', aware(2026, 3, 5, 9, 0, 0)))
        third = self.mark_downloaded(
            self.make_media('hold-3', aware(2026, 3, 5, 10, 0, 0)),
        )
        first.delete()
        third = Media.objects.get(pk=third.pk)
        self.assertEqual(third.nfo_episode_number, 30503)

    def test_removing_an_earlier_sibling_for_good_shifts_a_legacy_format_number(self):
        '''
            Documents accepted behaviour (see `_episode_day_index`): with a
            `media_format` without `{episode_mmddnn}` nothing freezes the
            index, so once an earlier same-day row is gone for good a
            later item's NFO `<episode>` moves down by one. Upstream's
            `calculate_episode_number()` drifts the same way.
        '''
        self.source.media_format = settings.MEDIA_FORMATSTR_DEFAULT
        self.source.save()
        first = self.mark_downloaded(
            self.make_media('drift-1', aware(2026, 3, 5, 8, 0, 0)),
        )
        self.mark_downloaded(self.make_media('drift-2', aware(2026, 3, 5, 9, 0, 0)))
        third = self.mark_downloaded(
            self.make_media('drift-3', aware(2026, 3, 5, 10, 0, 0)),
        )
        self.assertEqual(third.nfo_episode_number, 30503)
        self.delete_for_good(first)
        third = Media.objects.get(pk=third.pk)
        self.assertEqual(third.nfo_episode_number, 30502)

    def test_removing_an_earlier_sibling_for_good_keeps_a_frozen_number(self):
        first = self.mark_downloaded(
            self.make_media('keep-1', aware(2026, 3, 5, 8, 0, 0)),
        )
        self.mark_downloaded(self.make_media('keep-2', aware(2026, 3, 5, 9, 0, 0)))
        third = self.mark_downloaded(
            self.make_media('keep-3', aware(2026, 3, 5, 10, 0, 0)),
        )
        self.assertEqual(third.nfo_episode_number, 30503)
        self.delete_for_good(first)
        third = Media.objects.get(pk=third.pk)
        self.assertEqual(third.nfo_episode_number, 30503)


class EpisodeTokenParsingTestCase(TestCase):

    def test_parse_episode_token(self):
        for token, mmdd, expected in (
            ('030501', '0305', 1),
            ('030599', '0305', 99),
            ('030500', '0305', None),
            ('030401', '0305', None),
            (str(Media._nfo_episode_number_for('0305', 100)), '0305', 100),
            (str(Media._nfo_episode_number_for('0304', 100)), '0305', None),
            ('09999999', '0305', None),
        ):
            with self.subTest(token=token, mmdd=mmdd):
                self.assertEqual(_parse_episode_token(token, mmdd), expected)

    def test_name_parsing_rules(self):
        for name, media_format, expected in (
            ('S/s2026e030502 - T [k].mkv', PLEX_FORMAT, 2),
            # Every occurrence must agree.
            ('030502-030503.mkv', '{episode_mmddnn}-{episode_mmddnn}.{ext}', None),
            ('030502-030502.mkv', '{episode_mmddnn}-{episode_mmddnn}.{ext}', 2),
            # No literal next to the field: digit boundaries anchor it.
            ('k1030502.mkv', '{key}{episode_mmddnn}.{ext}', None),
            ('key-030502.mkv', '{key}-{episode_mmddnn}.{ext}', 2),
            # No literal on either side: not parsed at all.
            ('ab030502cd.mkv', '{key}{episode_mmddnn}{title}.{ext}', None),
            # A format spec changes the rendering, so it is not parsed.
            ('s  030502.mkv', 's{episode_mmddnn:>8}.{ext}', None),
            ('s030502.mkv', 's{episode_mmddnn!s}.{ext}', 2),
            ('s030502.mkv', 's{episode_mmddnn!r}.{ext}', None),
            ('s030502.mkv', 's{episode_mmddnn.{ext}', None),
        ):
            with self.subTest(name=name, media_format=media_format):
                self.assertEqual(
                    _episode_day_index_from_name(name, media_format, '0305'),
                    expected,
                )


class LazyEpisodeFormatKeysTestCase(TestCase):

    def setUp(self):
        logging.disable(logging.CRITICAL)
        self.source = Source.objects.create(
            source_type=Val(YouTube_SourceType.CHANNEL),
            key='lazykey',
            name='lazyname',
            directory='lazydirectory',
            media_format=settings.MEDIA_FORMATSTR_DEFAULT,
            index_schedule=3600,
            delete_old_media=False,
            days_to_keep=14,
            source_resolution=Val(SourceResolution.VIDEO_1080P),
            source_vcodec=Val(YouTube_VideoCodec.VP9),
            source_acodec=Val(YouTube_AudioCodec.OPUS),
            prefer_60fps=False,
            prefer_hdr=False,
            fallback=Val(Fallback.FAIL),
        )
        self.media = Media.objects.create(
            key='lazy', source=self.source, metadata=metadata,
            published=aware(2026, 3, 5, 10, 0, 0),
        )

    def test_unused_episode_keys_are_not_computed(self):
        with patch.object(Media, '_episode_day_index') as day_index:
            format_dict = self.media.format_dict
            self.media.filename
        day_index.assert_not_called()
        self.assertNotIn('episode_mmddnn', format_dict)
        self.assertNotIn('episode_yyyy', format_dict)

    def test_a_key_nested_in_a_format_spec_is_computed(self):
        self.source.media_format = '{title_full:.{episode_yyyy}} [{key}].{ext}'
        self.assertIn('episode_yyyy', self.media.format_dict)
        self.assertTrue(self.media.filename.endswith(' [lazy].mkv'))

    def test_used_episode_keys_are_computed(self):
        self.source.media_format = 's{episode_mmddnn} [{key}].{ext}'
        format_dict = self.media.format_dict
        self.assertEqual(format_dict['episode_mmddnn'], '030501')
        self.assertNotIn('episode_yyyy', format_dict)
        self.assertEqual(self.media.filename, 's030501 [lazy].mkv')


class RenameRewritesEpisodeNfoTestCase(TestCase):

    def setUp(self):
        logging.disable(logging.CRITICAL)

    def test_rename_rewrites_the_nfo_without_copy_thumbnails(self):
        '''
            A rename into a new episode number moves the old NFO; with
            `write_nfo` on it is rewritten even when `copy_thumbnails` is
            off, so `<episode>` matches the new file name.
        '''
        source = Source.objects.create(
            source_type=Val(YouTube_SourceType.CHANNEL),
            key='renamekey',
            name='renamename',
            directory='renamedirectory',
            media_format=PLEX_FORMAT,
            write_nfo=True,
            copy_thumbnails=False,
            index_schedule=3600,
            delete_old_media=False,
            days_to_keep=14,
            source_resolution=Val(SourceResolution.VIDEO_1080P),
            source_vcodec=Val(YouTube_VideoCodec.VP9),
            source_acodec=Val(YouTube_AudioCodec.OPUS),
            prefer_60fps=False,
            prefer_hdr=False,
            fallback=Val(Fallback.FAIL),
        )
        media = Media.objects.create(
            key='renamed', source=source, metadata=metadata,
            published=aware(2026, 3, 5, 10, 0, 0),
        )
        with temp_download_root() as tmp_dir:
            old_video = Path(tmp_dir) / 'renamedirectory' / 'old name [renamed].mkv'
            old_video.parent.mkdir(parents=True)
            old_video.write_bytes(b'video')
            old_nfo = old_video.with_suffix('.nfo')
            old_nfo.write_text('<episodedetails><episode>1</episode></episodedetails>')
            Media.objects.filter(pk=media.pk).update(
                downloaded=True,
                media_file='renamedirectory/old name [renamed].mkv',
            )
            media = Media.objects.get(pk=media.pk)

            media.rename_files()

            new_video = source.directory_path / media.filename
            self.assertTrue(new_video.exists())
            self.assertFalse(old_video.exists())
            new_nfo = new_video.with_suffix('.nfo')
            self.assertFalse(old_nfo.exists())
            nfo_tree = ElementTree.fromstring(new_nfo.read_text())
            self.assertEqual(nfo_tree.find('episode').text, '30501')


class NewMetadataWithoutPublishedTestCase(TestCase):
    '''
        A `new_metadata` row without `published` (written without going
        through `ingest_metadata`) is dated by `upload_date` in Python;
        `_same_day_index` must count it the same way.
    '''

    def setUp(self):
        logging.disable(logging.CRITICAL)
        self.source = Source.objects.create(
            source_type=Val(YouTube_SourceType.CHANNEL),
            key='nullpubkey',
            name='nullpubname',
            directory='nullpubdirectory',
            media_format=settings.MEDIA_FORMATSTR_DEFAULT,
            index_schedule=3600,
            delete_old_media=False,
            days_to_keep=14,
            source_resolution=Val(SourceResolution.VIDEO_1080P),
            source_vcodec=Val(YouTube_VideoCodec.VP9),
            source_acodec=Val(YouTube_AudioCodec.OPUS),
            prefer_60fps=False,
            prefer_hdr=False,
            fallback=Val(Fallback.FAIL),
        )

    def test_counted_on_its_upload_date(self):
        # `metadata` encodes upload_date 2017-09-11.
        bare = Media.objects.create(
            key='bare-new-metadata', source=self.source, metadata=metadata,
        )
        Media.objects.filter(pk=bare.pk).update(published=None)
        Metadata.objects.create(
            media=bare, site='Youtube', key=bare.key, published=None,
        )
        bare = Media.objects.get(pk=bare.pk)
        self.assertIsNone(bare.new_metadata.published)
        self.assertEqual(bare.episode_date, aware(2017, 9, 11))
        later = Media.objects.create(
            key='later-same-day', source=self.source, metadata=metadata,
            published=aware(2017, 9, 11, 12, 0, 0),
        )
        self.assertEqual(later._same_day_index(), 2)
        self.assertEqual(bare._same_day_index(), 1)


class NewMetadataOnlyTestCase(NewMetadataWithoutPublishedTestCase):
    '''
        The same, for a row whose metadata lives only in the related row
        (`Media.metadata` NULL): `loaded_metadata` still reads its
        `upload_date`, so SQL must not date it by `created`.
    '''

    def test_counted_on_its_upload_date(self):
        bare = Media.objects.create(key='only-new-metadata', source=self.source)
        Media.objects.filter(pk=bare.pk).update(published=None, metadata=None)
        Metadata.objects.create(
            media=bare, site='Youtube', key=bare.key, published=None,
            value=json.loads(metadata),
        )
        bare = Media.objects.get(pk=bare.pk)
        self.assertIsNone(bare.metadata)
        self.assertEqual(bare.episode_date, aware(2017, 9, 11))
        later = Media.objects.create(
            key='later-same-day', source=self.source, metadata=metadata,
            published=aware(2017, 9, 11, 12, 0, 0),
        )
        self.assertEqual(later._same_day_index(), 2)
        self.assertEqual(bare._same_day_index(), 1)


class UploadDateOnlyTimezoneTestCase(TestCase):
    '''
        `download_media_metadata` sets `Media.published` from a bare
        `upload_date` with `timezone.make_aware` (local time), while
        `Metadata.ingest_metadata` stores it as UTC midnight. `episode_date`
        reads the latter, so a timezone east of UTC (local midnight is
        the previous UTC day) does not move the episode to the day before.
    '''

    @override_settings(TIME_ZONE='Asia/Tokyo')
    def test_episode_day_is_the_upload_date_in_any_timezone(self):
        logging.disable(logging.CRITICAL)
        source = Source.objects.create(
            source_type=Val(YouTube_SourceType.CHANNEL),
            key='tzkey',
            name='tzname',
            directory='tzdirectory',
            media_format=PLEX_FORMAT,
            index_schedule=3600,
            delete_old_media=False,
            days_to_keep=14,
            source_resolution=Val(SourceResolution.VIDEO_1080P),
            source_vcodec=Val(YouTube_VideoCodec.VP9),
            source_acodec=Val(YouTube_AudioCodec.OPUS),
            prefer_60fps=False,
            prefer_hdr=False,
            fallback=Val(Fallback.FAIL),
        )
        media = Media.objects.create(key='tz', source=source)
        data = json.loads(metadata)
        data.pop('timestamp', None)
        data.pop('release_timestamp', None)
        media.ingest_metadata(data)
        # What download_media_metadata does next for a bare upload_date.
        media.metadata = media.metadata_dumps(arg_dict={'_using_table': True})
        media.published = timezone.make_aware(media.upload_date)
        media.save()
        media = Media.objects.get(pk=media.pk)
        self.assertEqual(media.published.date(), datetime(2017, 9, 10).date())
        self.assertEqual(media.episode_date, aware(2017, 9, 11))
        self.assertEqual(media.episode_mmddnn, '091101')
