'''
    T1: stable date-based episode numbering.

    Covers `Media.episode_date`, `Media.episode_yyyy`/`episode_mmddnn`
    (and their shared `_same_day_index` helper), `Media.title_full_bounded`,
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
from datetime import datetime, timedelta, timezone as dt_timezone
from unittest.mock import patch
from xml.etree import ElementTree

from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist
from django.db.models.functions import Coalesce
from django.test import TestCase
from django.utils import timezone

from medianest_bridge.source_forms import default_form_data, run_edit_source_checks
from sync.choices import (
    Val, Fallback, SourceResolution,
    YouTube_AudioCodec, YouTube_VideoCodec,
    YouTube_SourceType,
)
from sync.forms import SourceForm
from sync.models import Media, Source

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
            (the previous test) is guaranteed.
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
