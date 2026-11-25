from pathlib import Path
from stat import S_IMODE
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase

from sync import youtube
from sync.models import Source


class PlaylistMetadataTests(TestCase):
    def test_writer_and_validator_share_metadata_directory(self):
        playlist_id = "PLconcerts"
        Source.objects.bulk_create([Source(key=playlist_id, delete_removed_media=True)])

        with TemporaryDirectory() as tmp:
            config = Path(tmp)
            fallback = config / "cache" / "playlist-metadata"
            custom = config / "custom-cache"
            cases = [
                ({}, fallback),
                ({"cachedir": False}, fallback),
                ({"cachedir": None}, fallback),
                ({"cachedir": str(custom)}, custom),
            ]
            for options, root in cases:
                with (
                    self.subTest(options=options),
                    self.settings(
                        CONFIG_BASE_DIR=config, COOKIES_FILE=config / "cookies"
                    ),
                    patch.object(youtube, "_defaults", options),
                    patch.object(
                        youtube.yt_dlp.YoutubeDL,
                        "extract_info",
                        autospec=True,
                        return_value={"id": playlist_id},
                    ) as extract,
                ):
                    youtube.get_media_info("https://example.org/playlist")
                    opts = extract.call_args.args[0].params
                    self.assertEqual(opts.get("cachedir"), options.get("cachedir"))
                    template = opts["outtmpl"]["pl_infojson"]
                    folder = root / "infojson" / "playlist"
                    self.assertEqual(Path(template).parent, folder)
                    if root == fallback:
                        self.assertEqual(S_IMODE(fallback.stat().st_mode), 0o700)
                    folder.mkdir(parents=True, exist_ok=True)
                    for count, expected_exit in [(2, 1), (3, 0)]:
                        marker = Path(
                            template
                            % {
                                "id": playlist_id,
                                "n_entries": count,
                                "playlist_count": 3,
                                "ext": "info.json",
                            }
                        )
                        marker.touch()
                        with self.assertRaises(SystemExit) as result:
                            call_command("full-playlist", playlist_id, "3")
                        self.assertEqual(result.exception.code, expected_exit)
                        self.assertFalse(marker.exists())
