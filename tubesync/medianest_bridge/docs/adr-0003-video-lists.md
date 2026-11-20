# ADR-0003: Video lists for single videos (bridge contract 1.5.0)

Status: proposed (2026-11-20). MediaNest MED-730, part of MED-729 and
MediaNest ADR-0030.

## Context

MediaNest members want to add one YouTube video from a link. TubeSync only
has channel and playlist sources, and every source is indexed by crawling a
YouTube URL. In Plex every video must belong to a show, so MediaNest gives
each of its groups one show of single videos ("Cooking: Single videos").

The fork adds no migrations (`migration-upgrade-proof.md`,
`tests/test_migrations.py`). A new `YouTube_SourceType` value or a new model
would need one.

## Decision

- A video list is an ordinary playlist-type source (`'p'`) whose key starts
  with `medianest-videolist-`. No YouTube playlist ID contains that text,
  and `POST /sources` refuses it, so only `POST /videolists` makes one.
- It never indexes: `index_videos`, `index_streams` off and
  `index_schedule` NEVER, so `is_active` is false and neither
  `schedule_indexing()` nor `index_source()` touches it. Nothing builds a
  URL from its key.
- It downloads: `download_media` on. `POST /sources/{uuid}/videos` creates
  the Media row and schedules the thumbnail and metadata tasks
  `index_source()` schedules for a newly found video. The metadata save sets
  `published`, which un-skips the row and schedules the download.
- Channel images, removed-media cleanup, old-media deletion, the download
  cap and text/duration filters are forced off. Everything else (media
  format, NFO, thumbnails, quality) comes from the playlist entry of
  `MEDIANEST_BRIDGE_SOURCE_DEFAULTS`.
- `DELETE /sources/{uuid}/videos/{videoId}` deletes the Media row, marking
  it skipped first so `media_post_delete` leaves no placeholder row. Files
  stay unless `deleteFiles=true`, which sets `delete_files_on_disk` on the
  deleted row's in-memory source only.
- The bridge reports a video list as `sourceType: videolist`,
  `canonicalUrl: null`, `normalizedState: active`. The show title Plex sees
  is the source `name` (no channel or playlist metadata exists to beat it).
- `Capabilities.oneOffVideoDownload` is now true.

## Consequences

- No upstream file changes and no migrations.
- A video list's `name` is its show title, so renaming a MediaNest group
  needs a later rename endpoint; until then the old title stays.
- Sync-now on a video list is accepted and does nothing (it is not active).
