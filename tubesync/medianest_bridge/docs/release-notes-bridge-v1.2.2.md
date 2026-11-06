# Release notes: `bridge-v1.2.2`

This release keeps TubeSync downloading when a new yt-dlp comes out, makes
the bridge's health report say why something is wrong, and fixes three
problems found on the homelab after `bridge-v1.2.0`.

`bridge-v1.2.1` was a rebuild of `bridge-v1.2.0` code with a newer yt-dlp
(published by `workflow_dispatch`, no git tag; it still reported
`bridgeVersion` `1.2.0`). This is the next tagged release.

Tagging and image publication are human-gated actions for the repository
owner. This file records what the tag would contain and what was verified.

## What ships

1. **A newer yt-dlp release no longer stops downloads** (MED-623, #29).
   Upstream's Docker HEALTHCHECK stopped the `huey-net-limited` (download)
   consumer as soon as the bundled yt-dlp was older than the newest
   release, and downloads stayed stopped until the image was rebuilt. The
   fork's `healthcheck.py` now only warns by default and records the state
   in `/dev/shm/.healthcheck/yt-dlp-release.json`.
   `TUBESYNC_STALE_YTDLP_ACTION=pause` restores upstream's stop. Versions
   are compared numerically, so a nightly ahead of the latest stable
   release is not called stale. See `adr-0001-stale-ytdlp-guard.md`.
2. **`/health/ready` reports a stopped consumer as down** (MED-624, #29).
   `s6-svstat` prints `-1` for a stopped service, which the `workers`
   check counted as running ("4/4" while downloads were stopped). Only a
   PID above 0 counts now.
3. **Health says why** (contract 1.2.0, #29). Every component carries two
   new optional, nullable fields, `reason` and `latestVersion`:
   - `ytDlp`: `degraded`, `reason: "stale_ytdlp"`, `version` and
     `latestVersion` when a newer yt-dlp release exists.
   - `queues`: `reason: "queue_paused"` for a paused consumer, or
     `"stale_ytdlp"` when the opt-in pause mode did it.
   - `workers`: `reason: "worker_down"`.
   MediaNest shows these on the admin page (MediaNest #3082).
4. **The image refreshes itself** (MED-623, #29). The new
   `medianest-bridge-refresh.yaml` rebuilds the newest `bridge-vX.Y.Z` tag
   daily when a yt-dlp release has no image yet
   (`bridge-vX.Y.Z-yt<version>`) and every Monday
   (`bridge-vX.Y.Z-yt<version>-r<YYYYMMDD>`). Each of those tags is
   immutable. Each refresh also moves the `bridge-stable` channel tag.
5. **New sources download the best available format** (#30). The
   built-in source-defaults profile now sets `fallback` to `n` (next best
   resolution in any codec). Before, bridge-created sources kept
   TubeSync's model default, `REQUIRE_HD`, which skips any video without
   an HD format, such as older uploads and live recordings. Existing
   sources keep their setting.
6. **Changing a source's quality settings re-checks its videos in every
   process** (MED-634, #29). TubeSync connected its model signal receivers
   only once the URLconf loaded, so a source saved from
   `manage.py shell` scheduled no "Checking all media" task, and skipped
   videos stayed skipped. `SyncConfig.ready()` now connects them. Web and
   worker processes already had them.
7. **Playlists get a show poster in Plex** (MED-632, #31). A playlist has
   no channel avatar, so no `poster.jpg` was written and Plex showed a
   placeholder for the show and its episodes. The playlist thumbnail is
   now also written as `poster.jpg` and `season-poster.jpg` when there is
   no avatar.
8. **`BRIDGE_VERSION` is `1.2.2`**, so `GET /meta` and MediaNest's
   Connections tab show "bridge 1.2.2".

## Contract

Contract `1.2.0`, additive only: the optional, nullable
`ComponentStatus.reason` and `ComponentStatus.latestVersion`. Neither joins
`required`, and no path or response changed. A MediaNest that does not know
the fields ignores them. The vendored copy is re-synced to the squash merge
of MediaNest #3082 to `main`. Its body is byte-identical to the branch copy
first vendored in #29; the header now names the merge commit and
`source_sha256` is re-locked.

## Before tagging

- ~~Merge MediaNest #3082 and re-sync the vendored header SHA.~~ Done in
  the release-prep PR that this tag sits on.
- ~~Bump `BRIDGE_VERSION` to `1.2.2`.~~ Done in the same PR.
- Tag the release-prep merge commit `bridge-v1.2.2`. That triggers
  `medianest-bridge-release.yaml` to publish
  `ghcr.io/kinginyellows/tubesync:bridge-v1.2.2`. The tag must exist
  before the refresh workflow's next run, or it rebuilds `bridge-v1.2.0`
  code, which still pauses downloads.
- Point the TubeSync deployment at `bridge-v1.2.2` (or at `bridge-stable`
  to follow the refreshes) and recreate the container.

## After deploying

- Existing playlist folders get `poster.jpg` on the next
  `download_source_images` run for that source (a source save, or
  `manage.py medianest_backfill_plex_sidecars`).
- Existing sources keep their fallback. Change it per source, or
  through MediaNest, to get best-available downloads for them too.

## Upgrade and rollback

No migrations and no new required settings. `TUBESYNC_STALE_YTDLP_ACTION`
is optional. Rolling back to `bridge-v1.2.0` brings back the
pause-on-new-yt-dlp behavior and drops the `reason` fields; MediaNest
treats missing fields as unknown. Sources created with fallback `n` keep it
after a rollback.

## Known limits

- In the opt-in pause mode, `queues` labels a pause as `stale_ytdlp`
  whenever the guard asked for one, even if an operator paused the queue
  for another reason afterwards.
- A deployment pinned to an exact tag does not pick up refreshed images;
  only `bridge-stable` moves.
