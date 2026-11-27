# Release notes: `bridge-v1.6.0`

This release lets MediaNest pause and resume a YouTube source, change how
often it syncs, and retry or skip one video. A failed video also says why
it failed (MED-809, MED-810, MED-813, MediaNest ADR-0036). Until now the
bridge had no way to change a source after creating it, so the only way to
stop one was to remove it, and a failed video showed "Failed" forever.

Tagging and image publication are human-gated actions for the repository
owner. This file records what the tag would contain and what was verified.

## What ships

1. **Pause and resume** (`PATCH /sources/{uuid}` with `paused`). Pause sets
   the TubeSync source to never index and turns its downloads off, so a
   queued download does nothing when it starts. A download already running
   finishes. Resume puts back the schedule and download setting the source
   had and asks for an index right away, so videos published meanwhile are
   found. What resume needs is kept in a small JSON file per source under
   `/config/medianest-bridge/paused/`, beside TubeSync's database, so there
   are no migrations. A paused source reports `normalizedState: paused`.
2. **Sync schedule** (`PATCH /sources/{uuid}` with `indexSchedule`, in
   seconds, hourly to weekly). On a paused source it sets the schedule
   resume brings back.
3. **`Source.settings`** on every source read: `paused`, `pausedAt`,
   `indexSchedule`, `downloadMedia`.
4. **Retry one video** (`POST /sources/{uuid}/media/{id}/retry`). Queues a
   fresh manual download, as TubeSync's redownload button does for a video
   with nothing on disk, and un-skips it. Refused for a downloaded video
   (`409 MEDIA_DOWNLOADED`) and on a paused source (`409 SOURCE_PAUSED`).
5. **Skip one video** (`POST /sources/{uuid}/media/{id}/skip`). Marks it
   skipped the way TubeSync's skip button does, so the next index keeps it
   skipped, and revokes its queued downloads. Refused for a downloaded
   video and while it downloads (`409 MEDIA_BUSY`, retryable). Neither
   route deletes a file.
6. **`MediaItem.failureKind`**: `unavailable`, `private`, `members_only`,
   `age_restricted`, `geo_blocked`, `upcoming`, `temporary`, `disk_full` or
   `unknown`, read from the stored yt-dlp error.
7. **`GET /capabilities`** reports `updateSource`, `retryMedia` and
   `skipMedia` as true.
8. **`BRIDGE_VERSION` is `1.6.0`**, so `GET /meta` and MediaNest's
   Connections tab show "bridge 1.6.0".

## Contract

`1.6.0`. Additive: no existing path, request or required field changed, so
MediaNest releases that speak 1.5.0 keep working. They show a paused source
as "unknown" (only possible once someone pauses one, which needs the newer
MediaNest or TubeSync's own UI), and ignore the new fields.

## Known limits

- A manual download that a retry queued before the source was paused still
  runs (TubeSync's manual downloads skip the "source downloads off" check).
  Pausing does not look for those.
- Skip refuses a downloaded video. Removing one downloaded video (and its
  files) is a later contract change.
- `failureKind` matches yt-dlp's English messages. A new wording reads
  `unknown` until a phrase is added to `failure_kind.py`; the error text
  still reaches MediaNest.

## Before tagging

- Merge the MediaNest PR that carries contract 1.6.0 first, then point the
  vendored contract header at its squash-merge SHA (the body is already
  byte-identical) and regenerate nothing else.
- Tag `bridge-v1.6.0` on the `main` commit that merges this PR. That
  triggers `medianest-bridge-release.yaml` to publish
  `ghcr.io/kinginyellows/tubesync:bridge-v1.6.0`. The workflow can also be
  run by hand (`workflow_dispatch`) with the tag name on that commit.
- A tag push does not move `bridge-stable`. After the release image is
  published, run "MediaNest Bridge Image Refresh" by hand with `force`
  checked.

## Upgrade and rollback

No migrations and no new settings. Recreate the container on the new
image. Before rolling back to `bridge-v1.5.1`, resume every paused source
(older bridges report a paused source as `disabled` and cannot resume it;
TubeSync's own source page can, by setting the schedule back). The
`/config/medianest-bridge/` folder can stay; nothing older reads it.
