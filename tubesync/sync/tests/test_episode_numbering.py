'''
    T1: stable date-based episode numbering.

    Covers `Media.episode_date`, `Media.episode_yyyy`/`episode_mmddnn`
    (and their shared `_same_day_index` helper), `Media.title_full_bounded`,
    the new `nfoxml` season/episode behaviour for non-playlist sources, and
    that playlists keep the legacy `calculate_episode_number` numbering.

    Uses the same fixed Source configuration as `test_media.py` /
    `test_filepath.py` (1080p/VP9/OPUS, `prefer_60fps=False`,
    `prefer_hdr=False`) so the checked-in metadata fixtures format
    successfully -- deterministic, no network, no `/downloads` dependence.
'''
import logging
from datetime import datetime, timedelta
from unittest.mock import patch
from xml.etree import ElementTree

from django.conf import settings
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
    return timezone.make_aware(datetime(*args, **kwargs))


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

    def test_published_none_falls_back_to_upload_date_but_groups_by_created_day(self):
        # Two items with published=NULL, different metadata (different
        # upload_date), forced onto the same `created` UTC calendar day.
        first = Media.objects.create(
            key='nullpub-1', source=self.source, metadata=metadata,
        )
        second = Media.objects.create(
            key='nullpub-2', source=self.source, metadata=metadata_hdr,
        )
        shared_day = aware(2026, 4, 1, 6, 0, 0)
        Media.objects.filter(pk=first.pk).update(created=shared_day)
        Media.objects.filter(pk=second.pk).update(created=shared_day + timedelta(hours=6))
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertIsNone(first.published)
        self.assertIsNone(second.published)
        # episode_date/episode_yyyy fall back to each item's own upload_date.
        self.assertEqual(first.episode_yyyy, '2017')
        self.assertEqual(second.episode_yyyy, '2016')
        # The same-day INDEX groups both by `created`'s shared calendar day,
        # not by their (different) upload_date days -- MMDD still comes
        # from each item's own upload_date, only the counter is shared.
        self.assertEqual(first.episode_mmddnn, '091101')
        self.assertEqual(second.episode_mmddnn, '110902')

    def test_more_than_99_same_day_items_logs_a_warning_and_uses_3_digits(self):
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
        self.assertEqual(mmddnn, '0801100')
        mock_log.warning.assert_called_once()
        warning_args = mock_log.warning.call_args[0][0]
        self.assertIn('filler-099', warning_args)
        self.assertIn(str(self.source), warning_args)

    def test_nfo_episode_numbers_never_collide_past_99_same_day_items(self):
        # '0101' + '110' and '1011' + '10' would both be int() 101110.
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
        self.assertEqual(jan_110.episode_mmddnn, '0101110')
        self.assertEqual(oct_10.episode_mmddnn, '101110')
        self.assertEqual(oct_10.nfo_episode_number, 101110)
        self.assertEqual(jan_110.nfo_episode_number, 10_000_000 + 101 * 10_000 + 110)
        self.assertNotEqual(jan_110.nfo_episode_number, oct_10.nfo_episode_number)

    def test_title_full_bounded_respects_byte_limit_and_strips_slash(self):
        title = 'café🎉/' * 40
        media = Media(source=self.source, key='longtitle', metadata=metadata, title=title)
        bounded = media.title_full_bounded
        self.assertNotIn('/', bounded)
        self.assertLessEqual(len(bounded.encode('utf-8')), 150)
        # No dangling partial multibyte sequence left by the truncation.
        self.assertEqual(bounded, bounded.encode('utf-8').decode('utf-8'))
        self.assertEqual(bounded, bounded.strip())

    def test_title_full_bounded_short_title_is_unchanged_but_stripped(self):
        media = Media(source=self.source, key='shorttitle', metadata=metadata, title='  Some Title  ')
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
        self.assertEqual(unsaved.episode_mmddnn, episode_date.strftime('%m%d') + '01')

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

    def test_playlist_nfo_numbering_is_unchanged(self):
        playlist_source = Source.objects.create(
            source_type=Val(YouTube_SourceType.PLAYLIST),
            key='playlistkey',
            name='playlistname',
            directory='playlistdirectory',
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
