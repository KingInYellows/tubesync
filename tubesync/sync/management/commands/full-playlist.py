import re
import sys
from common.logger import log
from common.utils import glob_quote
from django.core.management.base import BaseCommand
from sync.models import Source
from sync.youtube import playlist_metadata_directory


class Command(BaseCommand):
    help = 'Validates and cleans up temporary playlist postprocessor_*_temp.info.json files efficiently.'

    def add_arguments(self, parser):
        parser.add_argument('playlist_id', type=str, help='The ID of the playlist')
        parser.add_argument('total_entries', type=str, help='The total expected entries count')
        parser.add_argument('fallback_downloaded', type=str, nargs='?', default='NA', 
                            help='Optional default value for downloaded entries')

    def handle(self, *args, **options):
        playlist_id = options['playlist_id']
        total_entries = options['total_entries']
        downloaded_entries = options['fallback_downloaded']

        # We can safely not abort when:
        # 1) No Source instance is configured to delete missing Media instances; or
        # 2) We matched a specific Source instance, and it is not configured to delete.
        # In all other cases, we MUST presume this Source is affected.
        sources_configured = Source.objects.filter(delete_removed_media=True).exists()
        source_qs = Source.objects.filter(key__endswith=playlist_id, delete_removed_media=False)
        this_source_deletes = True
        if playlist_id and source_qs.exists():
            this_source_deletes = False

        exact_match_regex = re.compile(
            rf'postprocessor_\[{re.escape(playlist_id)}\]_(?P<idx>\d+)_{re.escape(total_entries)}_temp\.info\.json$'
        )

        cleanup_pattern = glob_quote(f'postprocessor_[{playlist_id}]_') + '*_temp.info.json'

        for file_path in playlist_metadata_directory().rglob(cleanup_pattern):
            match = exact_match_regex.match(file_path.name)
            if match:
                downloaded_entries = match.group('idx')

            file_path.unlink(missing_ok=True)

        if (
            'NA' != downloaded_entries
            and 'NA' != total_entries
            and downloaded_entries != total_entries
            and sources_configured
            and this_source_deletes
        ):
            log.error('Validation failed: the entire playlist was not available.')
            sys.exit(1)

        log.debug('Playlist validation was completed successfully.')
        sys.exit(0)
