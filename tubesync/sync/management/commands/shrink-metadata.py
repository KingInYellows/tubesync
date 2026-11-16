import json
import time

from django.core.management.base import BaseCommand
from django.db import connection, transaction

from common.logger import log
from sync.models import Metadata, MetadataFormat
from sync.models.metadata import json_normalise, shrink_formats
from sync.utils import filter_response


def _size(value, /):
    return len(json.dumps(value, separators=(',', ':')))


class Command(BaseCommand):

    help = (
        'Trims metadata that is already stored: drops expiring format URLs, '
        'fragments, caption URLs and the heatmap, as TUBESYNC_SHRINK_NEW does '
        'for new metadata (MED-663). Works in small batches so other writers '
        'are not held up. The database file only gets smaller after VACUUM; '
        'without it the freed space is reused for new data.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Report what would change without writing anything',
        )
        parser.add_argument(
            '--batch-size', type=int, default=20,
            help='Metadata rows per write transaction (default 20)',
        )
        parser.add_argument(
            '--pause', type=float, default=0.5,
            help='Seconds to wait between batches (default 0.5)',
        )

    def _shrink_batch(self, uuids, dry_run):
        stats = dict(rows=0, formats=0, before=0, after=0)
        metadata_updates = list()
        format_updates = list()
        # select_for_update locks the rows on PostgreSQL / MySQL, so a
        # refresh cannot commit between this read and the bulk_update and
        # then be overwritten with the older snapshot. SQLite ignores it;
        # there the IMMEDIATE transaction already holds the write lock.
        # Lock metadata before formats, the same order ingest_metadata
        # writes them, so the two cannot deadlock.
        metadata_rows = list(
            Metadata.objects.select_for_update().filter(uuid__in=uuids).order_by('uuid')
        )
        formats_by_md = dict()
        for mdf in MetadataFormat.objects.select_for_update().filter(
            metadata_id__in=uuids,
        ).order_by('metadata_id', 'number'):
            formats_by_md.setdefault(mdf.metadata_id, list()).append(mdf)
        for md in metadata_rows:
            new_value = json_normalise(filter_response(md.value, True))
            stats['before'] += _size(md.value)
            stats['after'] += _size(new_value)
            if new_value != md.value:
                md.value = new_value
                metadata_updates.append(md)
            for mdf in formats_by_md.get(md.uuid, ()):
                new_format = json_normalise(shrink_formats([mdf.value])[0])
                stats['before'] += _size(mdf.value)
                stats['after'] += _size(new_format)
                if new_format != mdf.value:
                    mdf.value = new_format
                    format_updates.append(mdf)
        stats['rows'] = len(metadata_updates)
        stats['formats'] = len(format_updates)
        if not dry_run:
            if metadata_updates:
                Metadata.objects.bulk_update(metadata_updates, ['value'])
            if format_updates:
                MetadataFormat.objects.bulk_update(format_updates, ['value'], batch_size=200)
        return stats

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        batch_size = max(1, options['batch_size'])
        pause = max(0.0, options['pause'])
        uuids = list(Metadata.objects.order_by('uuid').values_list('uuid', flat=True))
        total = dict(rows=0, formats=0, before=0, after=0)
        log.info(f'shrink-metadata: {len(uuids):,} metadata rows to check{" (dry run)" if dry_run else ""}')
        for start in range(0, len(uuids), batch_size):
            batch = uuids[start:start + batch_size]
            # One short write transaction per batch. Reading the rows inside
            # it means a refresh that lands between batches is never
            # overwritten with older data.
            with transaction.atomic():
                stats = self._shrink_batch(batch, dry_run)
            for key, value in stats.items():
                total[key] += value
            done = min(start + batch_size, len(uuids))
            if 0 == (done // batch_size) % 50 or done == len(uuids):
                log.info(f'shrink-metadata: {done:,}/{len(uuids):,} rows checked')
            if pause and done < len(uuids):
                time.sleep(pause)
        verb = 'would change' if dry_run else 'changed'
        message = (
            f'shrink-metadata: {verb} {total["rows"]:,} metadata rows and '
            f'{total["formats"]:,} format rows; stored JSON '
            f'{total["before"]:,} -> {total["after"]:,} characters'
        )
        log.info(message)
        self.stdout.write(message)
        if 'sqlite' == connection.vendor and not dry_run:
            with connection.cursor() as cursor:
                cursor.execute('PRAGMA freelist_count')
                free_pages = cursor.fetchone()[0]
                cursor.execute('PRAGMA page_size')
                page_size = cursor.fetchone()[0]
            self.stdout.write(
                f'SQLite free space now {free_pages * page_size:,} bytes. '
                'New data reuses it; run VACUUM while TubeSync is stopped '
                'to give it back to the disk.'
            )
