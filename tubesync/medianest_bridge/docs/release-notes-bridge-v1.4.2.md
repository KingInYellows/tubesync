# Release notes: `bridge-v1.4.2`

This release stops TubeSync's database from growing every day when no
new videos arrive (MED-663).

Tagging and image publication are human-gated actions for the repository
owner. This file records what the tag would contain and what was verified.

## What ships

1. **Video metadata is stored trimmed** (MED-663). On dockerdownload
   `db.sqlite3` grew from 2.8 GB (2026-11-09) to 5.29 GB (2026-11-13),
   about 0.6 GB a day, with the same ~3,800 metadata rows being rewritten
   with larger yt-dlp JSON (average 629 KB, then 764 KB a row). Upstream's
   `TUBESYNC_SHRINK_NEW` filter existed but was off by default.
   - `TUBESYNC_SHRINK_NEW` now defaults to `true` in the container. Set it
     to `false` to keep upstream's full JSON.
   - The filter now runs inside `Metadata.ingest_metadata`, so every write
     path is covered: indexing, format refreshes, `save_to_metadata`, and
     the Merger post-processor hook (which stored raw `requested_formats`).
   - It drops expiring per-format URLs, `fragments`, `http_headers`,
     auto-caption URLs (keeping the language list) and the `heatmap`. The
     2023 capture in `sync/testdata` goes from 388 KB to 40 KB. Format
     choice, NFO files and the bridge read none of the dropped fields;
     yt-dlp fetches fresh URLs at download time.
2. **Refreshes only write what changed** (MED-663).
   - `ingest_formats` used to save every format row on every call. It now
     compares each format and writes only the changed ones in one bulk
     update; an unchanged ingest writes nothing at all.
   - `refresh_formats` saved thumbnails, thumbnail, formats,
     `formats_epoch` and availability with five separate
     `save_to_metadata` calls, each re-reading all formats and re-ingesting
     the whole dict. It now makes one `save_dict_to_metadata` call.
3. **New `manage.py shrink-metadata`** trims rows stored before this
   release. It works in batches of 20 rows (`--batch-size`) with a short
   pause between them (`--pause`), so the nightly crawl and MediaNest are
   not held up. `--dry-run` reports what it would change.
4. **`BRIDGE_VERSION` is `1.4.2`**, so `GET /meta` and MediaNest's
   Connections tab show "bridge 1.4.2".

## Contract

`1.4.0`, unchanged from `bridge-v1.4.0`. No path, request or response
changed.

## Before tagging

- `bridge-v1.4.1` (tagged on `c2799e5`) is separate and can ship first.
- Tag `bridge-v1.4.2` on the `main` commit that merges the MED-663 PR.
  That triggers `medianest-bridge-release.yaml` to publish
  `ghcr.io/kinginyellows/tubesync:bridge-v1.4.2`.
- A tag push does not move `bridge-stable`. After the release image is
  published, run "MediaNest Bridge Image Refresh" by hand with `force`
  checked.

## Upgrade and rollback

No migrations. After the new image is running:

1. Optional check: `docker exec -u app tubesync python3 /app/manage.py
   shrink-metadata --dry-run` reports how many rows would shrink. Run as
   `-u app` so files keep the right owner.
2. Trim stored rows: `docker exec -u app tubesync python3 /app/manage.py
   shrink-metadata`. Run it outside the nightly crawl. Freed space stays
   inside the file and is reused, so the file stops growing but does not
   shrink.
3. To give the space back to the disk, stop TubeSync and run `VACUUM` on
   `db.sqlite3`. VACUUM needs free disk about the size of the database and
   takes a while on a multi-GB file. This step is the deployer's choice.

Rolling back to `bridge-v1.4.1` needs no database step. Trimmed rows stay
trimmed; set `TUBESYNC_SHRINK_NEW=false` there if you want full JSON back
for new rows (`bridge-v1.4.1` already stores full JSON by default).

## Verified

- `manage.py test`: 877 tests pass, including eight new ones in
  `sync/tests/test_metadata_storage.py`: trimmed vs full storage, no
  writes when the same data is ingested again, exactly one write when one
  format changes, fewer formats deletes the rest, an empty list keeps the
  stored formats, one ingest per `save_dict_to_metadata`, and
  `shrink-metadata` (dry run, real run, second run finds nothing).
- `ruff check` with the CI rule set passes.

## Known limits

- Rows only shrink on disk once `shrink-metadata` has run, and the file
  only gets smaller after VACUUM.
- A refresh still writes the metadata row once, because yt-dlp's `epoch`
  changes on every fetch; that row is now tens of KB instead of hundreds.
- Whether growth has stopped is checked by comparing `db.sqlite3` size
  over a few days after deploy.
