# Release notes: `bridge-v1.4.4`

This release lets a source put the artist first in its episode titles, in
both the file name and the NFO `<title>`, and adds a command that applies
the new naming to files already on disk (MED-702). The decision record is
`docs/adr-0002-plex-naming.md`.

Tagging and image publication are human-gated actions for the repository
owner. This file records what the tag would contain and what was verified.

## What ships

1. **Artist-first titles** (#78). Two new `media_format` keys,
   `{artist_title}` and `{artist_title_bounded}`. The artist comes from
   the video's YouTube music credit (`artists`, then `artist`). Without
   one, a title already written "Artist | Title" or "Artist – Title" gets
   " - " as its separator; any other title is left as it is. A source
   that uses either key also gets that text as its episode NFO `<title>`,
   so the file name and Plex agree. No other source changes.
2. **`manage.py medianest_plex_naming`** (#78). Dry-run unless `--apply`.
   `--artist-titles` switches a source's `media_format` to the artist
   keys, then renames each downloaded item and rewrites its NFO in its own
   short transaction under the same huey locks as
   `rename_all_media_for_source` (MED-695: no lock held across the run).
   Busy items are skipped and the command exits non-zero, so it can be run
   again; a re-run finishes an interrupted rename without overwriting
   anything. It never changes the show or season folders. `--show-title`
   can write a hand-owned `tvshow.nfo` title; it is not used for now.
3. **`BRIDGE_VERSION` is `1.4.4`**, so `GET /meta` and MediaNest's
   Connections tab show "bridge 1.4.4".

## Contract

`1.4.0`, unchanged from `bridge-v1.4.0`. No path, request or response
changed.

## Before tagging

- Tag `bridge-v1.4.4` on the `main` commit that merges this release-prep
  PR. That triggers `medianest-bridge-release.yaml` to publish
  `ghcr.io/kinginyellows/tubesync:bridge-v1.4.4`. The workflow can also
  be run by hand (`workflow_dispatch`) with the tag name on that commit.
- A tag push does not move `bridge-stable`. After the release image is
  published, run "MediaNest Bridge Image Refresh" by hand with `force`
  checked.

## Upgrade and rollback

No migrations and no new settings. Recreate the container on the new
image. Nothing changes on disk until `medianest_plex_naming --apply` is
run. Rolling the image back to `bridge-v1.4.3` needs no database step,
but a source switched to `{artist_title_bounded}` then fails to format
its file names; set its `media_format` back first (the dry-run prints
the old one).

## Verified

- `manage.py test sync.tests.test_plex_naming
  sync.tests.test_backfill_plex_sidecars`: 227 tests pass, covering the
  artist rules, the format swap (escaped braces, conversions, specs), the
  dry-run and apply paths, busy items, occupied targets, another item's
  files, re-run recovery and a swallowed NFO write error.
- GitHub CI on #78's head `397ccc7` (test 3.12, 3.13, 3.14) passed.

## Known limits

- `medianest_backfill_plex_sidecars --all-bridge-sources` applies the
  bridge profile's `media_format`, which switches an artist-title source
  back to plain titles. Leave that source out, or re-run
  `medianest_plex_naming --artist-titles` afterwards.
- Plex treats a renamed file as a delete plus an add, so watch state on
  renamed episodes can reset.
