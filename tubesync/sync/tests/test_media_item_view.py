'''
    Covers the media-item page's manual "Fetch Metadata and Download"
    action, added for index-only media that has no metadata yet -- see
    settings.INDEX_ONLY_SKIP_METADATA and MediaRedownloadView's
    `elif not media.has_metadata` branch. Without this action the page
    never exposed any way to trigger MediaRedownloadView's new branch,
    since the pre-existing "Begin Downloading" link only ever appears
    when media.can_download is already true.
'''
import logging
from django.test import TestCase, Client, override_settings
from django.urls import reverse
from django.utils import timezone
from sync.choices import Val, YouTube_SourceType
from sync.models import Source, Media


def make_source(**overrides):
    defaults = dict(
        source_type=Val(YouTube_SourceType.CHANNEL),
        key='UC_media_item_view',
        name='Media Item View Source',
        directory='/tmp/media_item_view',
    )
    defaults.update(overrides)
    return Source.objects.create(**defaults)


def make_media(source, **overrides):
    defaults = dict(
        source=source,
        key='video1',
        title='Test Video',
        published=timezone.now(),
    )
    defaults.update(overrides)
    return Media.objects.create(**defaults)


class MediaItemFetchMetadataActionTestCase(TestCase):

    def setUp(self):
        logging.disable(logging.CRITICAL)

    def tearDown(self):
        logging.disable(logging.NOTSET)

    def _get(self, media):
        return Client().get(reverse('sync:media-item', kwargs={'pk': media.pk}))

    def _redownload_url(self, media):
        return reverse('sync:redownload-media', kwargs={'pk': media.pk})

    def test_shown_for_index_only_item_without_metadata(self):
        source = make_source(download_media=False)
        media = make_media(source)
        self.assertFalse(media.has_metadata)
        self.assertFalse(media.can_download)
        response = self._get(media)
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn('Fetch Metadata and Download', html)
        self.assertIn(self._redownload_url(media), html)
        # Not the "already downloadable" link/wording.
        self.assertNotIn('Begin Downloading', html)

    def test_not_shown_once_can_download(self):
        source = make_source(key='UC_can_download', directory='/tmp/can_download', download_media=True)
        media = make_media(source, key='video2', can_download=True)
        response = self._get(media)
        html = response.content.decode()
        self.assertIn('Begin Downloading', html)
        self.assertNotIn('Fetch Metadata and Download', html)

    def test_not_shown_once_downloaded(self):
        source = make_source(key='UC_downloaded', directory='/tmp/downloaded', download_media=True)
        media = make_media(
            source, key='video3', can_download=True, downloaded=True,
        )
        response = self._get(media)
        html = response.content.decode()
        self.assertNotIn('Fetch Metadata and Download', html)
        self.assertNotIn('Begin Downloading', html)

    def test_shown_even_when_skip_is_set(self):
        # Matches the pre-existing "Begin Downloading" link's own rule:
        # neither link checks media.skip/manual_skip, since can_download
        # (or, here, needing metadata) can legitimately outlive a later
        # skip -- MediaRedownloadView's form always clears both flags as
        # part of any redownload/fetch request.
        source = make_source(key='UC_skipped', directory='/tmp/skipped', download_media=False)
        media = make_media(source, key='video4', skip=True)
        response = self._get(media)
        html = response.content.decode()
        self.assertIn('Fetch Metadata and Download', html)

    def test_not_shown_for_normal_source_item_without_metadata(self):
        # A normal source's item without metadata yet is just in the
        # ordinary brief window before its automatic fetch runs --
        # offering a manual fetch there would schedule a second,
        # non-deduped metadata task alongside the automatic one (codex
        # P2 finding).
        source = make_source(key='UC_normal_no_meta', directory='/tmp/normal_no_meta', download_media=True)
        media = make_media(source, key='video6')
        self.assertFalse(media.has_metadata)
        response = self._get(media)
        html = response.content.decode()
        self.assertNotIn('Fetch Metadata and Download', html)
        self.assertNotIn('Begin Downloading', html)

    @override_settings(INDEX_ONLY_SKIP_METADATA=False)
    def test_not_shown_for_index_only_item_when_setting_disabled(self):
        # With the fork setting off, an index-only source's media is no
        # different from upstream's own behavior -- it gets metadata
        # fetched automatically like any other media, so the manual
        # action has nothing extra to offer.
        source = make_source(key='UC_legacy_no_meta', directory='/tmp/legacy_no_meta', download_media=False)
        media = make_media(source, key='video7')
        response = self._get(media)
        html = response.content.decode()
        self.assertNotIn('Fetch Metadata and Download', html)

    def test_no_formats_error_hidden_until_metadata_exists(self):
        # media.has_metadata is False here, so it is not yet known
        # whether any format matches the source requirements -- the
        # "no formats match" error would be premature/inaccurate.
        source = make_source(key='UC_no_meta_yet', directory='/tmp/no_meta_yet', download_media=False)
        media = make_media(source, key='video5')
        response = self._get(media)
        html = response.content.decode()
        self.assertNotIn('no formats which match', html)
