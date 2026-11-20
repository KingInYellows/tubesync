# Release notes: `bridge-v1.5.0`

This release adds video lists: TubeSync sources that hold single YouTube
videos MediaNest adds one at a time, so a member can download one video by
link (MED-730, MediaNest ADR-0030). Each MediaNest group gets one video
list, which Plex shows as one show ("<Group>: Single videos"). The design
record is `docs/adr-0003-video-lists.md`.

Tagging and image publication are human-gated actions for the repository
owner. This file records what the tag would contain and what was verified.

## What ships

1. **Video lists** (#80). A video list is an ordinary playlist-type source
   with the reserved key prefix `medianest-videolist-`, so there are no
   migrations. It never indexes. Adding a video creates its row and queues
   the same thumbnail and metadata tasks an index would, and TubeSync's
   normal download, NFO and naming path does the rest.
2. **Contract 1.5.0 (additive).** `POST /videolists` creates one (or returns
   the existing one with 409 `SOURCE_EXISTS` to adopt).
   `POST /sources/{uuid}/videos` adds a video ID (idempotent).
   `DELETE /sources/{uuid}/videos/{videoId}` removes one, keeping its files
   unless `deleteFiles=true`. A video that is downloading right now answers
   409 `MEDIA_BUSY` (retryable). `POST /sources` refuses the reserved prefix.
   `GET /capabilities` reports `oneOffVideoDownload: true`.
3. **Race fixes found in review**, all with tests:
   - removal takes the same `media:<uuid>` lock as a download;
   - a download re-reads its row under that lock;
   - `Media.save()` on a row loaded from the database is always an UPDATE,
     so a task holding a stale copy cannot bring a removed video back;
   - the thumbnail task cleans up after a removed video, and copies its
     sidecar only under the lock;
   - a video removed with its files kept and then added again gets its
     file back (video lists only).
4. **`BRIDGE_VERSION` is `1.5.0`**, so `GET /meta` and MediaNest's
   Connections tab show "bridge 1.5.0".

## Contract

`1.5.0`. Additive: no existing path, request or required field changed, so
MediaNest releases that speak 1.4.0 keep working. MediaNest offers single
videos only on a bridge reporting 1.5.0 or later.

## Before tagging

- Tag `bridge-v1.5.0` on the `main` commit that merges this release-prep
  PR. That triggers `medianest-bridge-release.yaml` to publish
  `ghcr.io/kinginyellows/tubesync:bridge-v1.5.0`. The workflow can also
  be run by hand (`workflow_dispatch`) with the tag name on that commit.
- A tag push does not move `bridge-stable`. After the release image is
  published, run "MediaNest Bridge Image Refresh" by hand with `force`
  checked.

## Upgrade and rollback

No migrations and no new settings. Recreate the container on the new
image. Rolling back to `bridge-v1.4.4` needs no database step. Video lists
created on 1.5.0 then show as ordinary paused playlist sources that never
index; remove them in MediaNest first if a rollback is planned.

## Verified

- Full suite `manage.py test --parallel 4`: 947 tests pass, including 37
  in `medianest_bridge.tests.test_videolists`.
- ruff (CI's rule set) is clean.
- GitHub CI on #80's head `a612d2ae` (test 3.12, 3.13, 3.14) passed.

## Known limits

- Renaming a MediaNest group does not rename its existing "Single videos"
  show; the new name applies to lists created afterwards.
- If scheduling fails partway through an add and the video is added again
  before the first thumbnail task runs, two thumbnail tasks fetch the
  same image. That's harmless.
