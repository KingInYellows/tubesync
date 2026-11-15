'''
    MED-663: TubeSync's database grew ~0.6 GB a day because full yt-dlp
    JSON (expiring format URLs, fragments, every auto-caption URL) was
    stored, and every refresh rewrote the metadata row and every format
    row, several times per refresh.

    Covers:
      - Metadata.ingest_metadata trims data when SHRINK_NEW_MEDIA_METADATA
        is on, and stores it untouched when it is off.
      - Ingesting the same data again writes nothing.
      - save_dict_to_metadata changes several keys with one ingest.
      - the shrink-metadata command trims rows already stored.
'''
import json
import logging
from copy import deepcopy
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from django.core.management import call_command
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from sync.choices import Val, YouTube_SourceType
from sync.models import Media, Metadata, MetadataFormat, Source


TESTDATA = Path(__file__).resolve().parent.parent / 'testdata'


def _load(name):
    with open(TESTDATA / name) as f:
        return json.load(f)


def _writes(queries):
    return [
        q['sql'] for q in queries
        if q['sql'].lstrip().upper().startswith(('INSERT', 'UPDATE', 'DELETE'))
    ]


class MetadataStorageTestCase(TestCase):

    def setUp(self):
        logging.disable(logging.CRITICAL)
        self.source = Source.objects.create(
            source_type=Val(YouTube_SourceType.CHANNEL),
            key='UC_md_storage', name='Metadata Storage',
            directory='/tmp/md_storage',
        )
        self.media = Media.objects.create(
            source=self.source, key='vid_md', title='Vid',
            published=timezone.now(),
        )
        # 2023 capture: full auto-caption URLs, fragments, heatmap
        self.full = _load('metadata_2023-06-29.json')

    def tearDown(self):
        logging.disable(logging.NOTSET)

    def _stored_size(self):
        md = Metadata.objects.get(media=self.media)
        size = len(json.dumps(md.value))
        for mdf in MetadataFormat.objects.filter(metadata=md):
            size += len(json.dumps(mdf.value))
        return size

    @override_settings(SHRINK_NEW_MEDIA_METADATA=True)
    def test_shrink_drops_urls_fragments_and_heatmap(self):
        self.media.ingest_metadata(deepcopy(self.full))
        md = Metadata.objects.get(media=self.media)
        self.assertNotIn('heatmap', md.value)
        for mdf in MetadataFormat.objects.filter(metadata=md):
            self.assertNotIn('fragments', mdf.value)
            self.assertNotIn('http_headers', mdf.value)
            url = mdf.value.get('url', '')
            self.assertFalse('expire=' in url or '/expire/' in url, url)
        # Format choice still has what it needs
        formats = self.media.loaded_metadata['formats']
        self.assertEqual(len(formats), len(self.full['formats']))
        self.assertTrue(all('format_id' in f and 'vcodec' in f for f in formats))
        # and the stored JSON is far smaller than the capture
        self.assertLess(self._stored_size(), len(json.dumps(self.full)) // 4)

    @override_settings(SHRINK_NEW_MEDIA_METADATA=False)
    def test_shrink_off_stores_everything(self):
        self.media.ingest_metadata(deepcopy(self.full))
        md = Metadata.objects.get(media=self.media)
        self.assertIn('heatmap', md.value)
        self.assertTrue(any(
            'fragments' in mdf.value
            for mdf in MetadataFormat.objects.filter(metadata=md)
        ))

    @override_settings(SHRINK_NEW_MEDIA_METADATA=True)
    def test_same_data_again_writes_nothing(self):
        self.media.ingest_metadata(deepcopy(self.full))
        media = Media.objects.get(pk=self.media.pk)
        with CaptureQueriesContext(connection) as ctx:
            media.ingest_metadata(deepcopy(self.full))
        self.assertEqual(_writes(ctx.captured_queries), [])

    @override_settings(SHRINK_NEW_MEDIA_METADATA=True)
    def test_one_changed_format_updates_one_row(self):
        self.media.ingest_metadata(deepcopy(self.full))
        changed = deepcopy(self.full)
        changed['formats'][3]['filesize'] = 123456789
        media = Media.objects.get(pk=self.media.pk)
        with CaptureQueriesContext(connection) as ctx:
            media.ingest_metadata(changed)
        writes = _writes(ctx.captured_queries)
        self.assertEqual(len(writes), 1, writes)
        self.assertIn('sync_media_metadata_format', writes[0])
        md = Metadata.objects.get(media=self.media)
        self.assertEqual(md.format.get(number=4).value['filesize'], 123456789)

    @override_settings(SHRINK_NEW_MEDIA_METADATA=True)
    def test_fewer_formats_deletes_the_rest(self):
        self.media.ingest_metadata(deepcopy(self.full))
        fewer = deepcopy(self.full)
        fewer['formats'] = fewer['formats'][:5]
        Media.objects.get(pk=self.media.pk).ingest_metadata(fewer)
        md = Metadata.objects.get(media=self.media)
        self.assertEqual(
            list(md.format.order_by('number').values_list('number', flat=True)),
            [1, 2, 3, 4, 5],
        )

    @override_settings(SHRINK_NEW_MEDIA_METADATA=True)
    def test_empty_formats_keeps_stored_formats(self):
        self.media.ingest_metadata(deepcopy(self.full))
        no_formats = deepcopy(self.full)
        no_formats['formats'] = []
        Media.objects.get(pk=self.media.pk).ingest_metadata(no_formats)
        md = Metadata.objects.get(media=self.media)
        self.assertEqual(md.format.count(), len(self.full['formats']))

    @override_settings(SHRINK_NEW_MEDIA_METADATA=True)
    def test_save_dict_to_metadata_ingests_once(self):
        self.media.ingest_metadata(deepcopy(self.full))
        media = Media.objects.get(pk=self.media.pk)
        with patch.object(Media, 'ingest_metadata', autospec=True,
                          side_effect=Media.ingest_metadata) as spy:
            media.save_dict_to_metadata({'availability': 'unlisted', 'formats_epoch': 1})
        self.assertEqual(spy.call_count, 1)
        data = Media.objects.get(pk=self.media.pk).loaded_metadata
        self.assertEqual(data['availability'], 'unlisted')
        self.assertEqual(data['formats_epoch'], 1)

    def test_shrink_metadata_command_trims_stored_rows(self):
        with override_settings(SHRINK_NEW_MEDIA_METADATA=False):
            self.media.ingest_metadata(deepcopy(self.full))
        before = self._stored_size()

        out = StringIO()
        call_command('shrink-metadata', '--dry-run', '--pause', '0', stdout=out)
        self.assertIn('would change 1 metadata rows', out.getvalue())
        self.assertEqual(self._stored_size(), before)

        out = StringIO()
        call_command('shrink-metadata', '--pause', '0', stdout=out)
        self.assertIn('changed 1 metadata rows', out.getvalue())
        after = self._stored_size()
        self.assertLess(after, before // 4)
        md = Metadata.objects.get(media=self.media)
        self.assertNotIn('heatmap', md.value)

        # running it again finds nothing left to trim
        out = StringIO()
        call_command('shrink-metadata', '--pause', '0', stdout=out)
        self.assertIn('changed 0 metadata rows and 0 format rows', out.getvalue())
