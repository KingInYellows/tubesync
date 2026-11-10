# Release notes: `bridge-v1.3.0`

This release reports each source's channel or playlist title, so
MediaNest can show readable source names instead of `acq-src-<key>`.

Tagging and image publication are human-gated actions for the repository
owner. This file records what the tag would contain and what was verified.

## What ships

1. **`Source.title`** (MED-635, #38). Every source MediaNest creates is
   named `acq-src-<key>`, so MediaNest had no display name for it.
   - Each source in the bridge's responses now carries `title`: the
     title `sync/tvshow_nfo.py::resolve_show_title()` resolves, which is
     what the source's `tvshow.nfo` gives Plex.
   - `title` is null when nothing better than `name` is known, including
     when the resolved title is only the name with emoji removed.
   - Saving a source now drops its cached show title in the process that
     saved it, so that process reports the new name at once. The next
     episode NFO written in that process also uses the new name; existing NFOs are rewritten only by
     TubeSync's usual delayed save and rename tasks.
   - MediaNest with #3100 uses `title` as the source's name when the
     source has none of its own, and marks sources active once the bridge
     reports them active or syncing.
2. **Build log** (#37). The image build's FFmpeg download now prints its
   output, so a failed download shows its HTTP status or error. No
   runtime change.
3. **`BRIDGE_VERSION` is `1.3.0`**, so `GET /meta` and MediaNest's
   Connections tab show "bridge 1.3.0".

## Contract

`1.3.0`: adds the optional, nullable `Source.title`. It is not required,
and no path or other response changed. Vendored from MediaNest
`3e57252260c520ec34fa4237592d49e0f7653442`, the squash merge of #3100.
Older MediaNest ignores the field.

## Before tagging

- Tag the release-prep merge commit `bridge-v1.3.0`. That triggers
  `medianest-bridge-release.yaml` to publish
  `ghcr.io/kinginyellows/tubesync:bridge-v1.3.0`, and the next refresh run
  builds from it.

## Upgrade and rollback

No migrations and no new settings. The SQLite journal mode setting from
`bridge-v1.2.3` carries over unchanged: keep
`TUBESYNC_SQLITE_JOURNAL_MODE=WAL` when upgrading from a WAL deployment.

Rolling back to `bridge-v1.2.3` needs no database step; MediaNest then
receives no `title` and keeps whatever names it already stored. Rolling
back further than `bridge-v1.2.3` on a WAL database needs the
`PRAGMA journal_mode=DELETE` step in `release-notes-bridge-v1.2.3.md`.

## Verified

- `manage.py test` with `TUBESYNC_DEBUG=True`: 820 tests OK on the
  branch of #38, and the `medianest_bridge` suite (422 tests) after the
  contract re-vendor, including the fixture sha256 lock.

## Known limits

- The title cache is per process (60 s). A rename is seen at once only
  in the process that saved it. Another web worker (Gunicorn runs
  several) can still report the old title, including an old fallback
  name, as `title` for up to 60 s, and a huey worker can still use it for
  episode NFOs for as long.
- Names show in MediaNest only for sources whose local name is blank.
  A name typed in MediaNest always wins.
