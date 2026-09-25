# Release notes: `bridge-v1.1.0` (proposed, not yet tagged)

This release makes bridge-created YouTube sources land as proper TV shows in a
Plex **TV Shows** library that uses the native "Plex NFO Series" agent
(PMS 1.43.1+). Each channel or playlist becomes a show, each episode-date
year a season, and each video an episode with a stable date-based number. Sidecar
NFO and JPG files carry the metadata. MediaNest supersedes its DECISIONS
#40/#49 ("Other Videos" library) with DECISIONS #55.

Tagging and image publication are human-gated actions for the repository
owner. This file records what the tag would contain and what was verified.

## What ships

1. **Stable date-based episode numbering** (`sync/models/media.py`,
   `sync/models/source.py`, which are upstream-owned).
   - New `media_format` keys:
     - `{episode_yyyy}` and `{episode_mmddnn}`, both derived from one date
       source: `published`, then `upload_date`, then `created`. `NN` is the
       same-day index, computed with a single COUNT query.
     - `{title_full_bounded}`, the title capped at 150 UTF-8 bytes.
   - Channel NFOs now emit `<season>YYYY</season>` and
     `<episode>MMDDNN</episode>`. Past 99 videos on one day, `<episode>`
     moves to a separate 10,000,000+ range so numbers never collide.
     Playlists filed by this scheme (every bridge-created playlist) get the
     same values. Other playlists keep season `1` and playlist order.
   - Items are grouped by the same date they encode, including unpublished
     items whose date comes from metadata, so two videos never share a
     number.
   - Backfilling an older video never renumbers other days.
2. **`tvshow.nfo` per source** (new `sync/tvshow_nfo.py`, with hooks in the
   upstream-owned `sync/tasks.py`).
   - The file is written after indexing, after channel-image download, and
     after each video's metadata is saved (so the first real channel name
     reaches it without waiting for the next index). It contains the real
     channel or playlist title, the description when known, and
     `<uniqueid type="youtube">`.
   - Writing it is best-effort: a missing source directory is skipped and
     any other error is logged, never failing or retrying the task. A path
     that already holds a video's own NFO (a `media_format` rendering to
     `tvshow`) is left alone with a warning.
   - The episode `<showtitle>` uses the same resolved title.
   - Writes are escaped via ElementTree and happen only when the content
     changed.
3. **`MEDIANEST_BRIDGE_SOURCE_DEFAULTS`** (bridge-only).
   - Bridge-created sources now default to `write_nfo`, `copy_thumbnails` and
     `copy_channel_images` on, `index_streams` off, and
     `media_format = Season {episode_yyyy}/s{episode_yyyy}e{episode_mmddnn} - {title_full_bounded} [{key}].{ext}`.
   - The profile is configurable as JSON per source type. Invalid config
     makes the new optional readiness component `sourceDefaults` report
     `unavailable`, and `POST /sources` returns 503 `PROVIDER_UNAVAILABLE`.
     Nothing falls back silently. Error details name fields and error codes,
     never configured values.
   - Overlays may not set `source_type`, `key`, `name`, `directory` or
     `target_schedule`. Boolean fields must be JSON `true`/`false`, and a
     `media_format` with a `..` segment or an invalid `filter_text` regex is
     rejected.
4. **`manage.py medianest_backfill_plex_sidecars`** (new command, no upstream
   edits).
   - It applies the profile to existing `acq-src-*` sources, renames
     downloaded files into `Season YYYY/`, rewrites episode NFOs, writes
     `tvshow.nfo`, and enqueues channel images.
   - Dry-run is the default and validates exactly what `--apply` would. It
     never deletes files. A re-run is safe: it only saves a source whose
     fields actually change.
   - `--apply` must run as the user that owns `DOWNLOAD_ROOT` (`docker exec
     -u app ...`), otherwise new `Season YYYY/` directories would be
     root-owned and unwritable by TubeSync. It refuses otherwise.
   - Each video's database record is saved as soon as the file moves, before
     its NFO and thumbnail are written, so a later failure never leaves the
     database behind the file. A missing file, or an occupied target for the
     video or any of its sidecars, is an error and nothing moves. The command
     exits non-zero when anything errored or was skipped as locked, so the
     operator re-runs it.

## Contract

The contract gains one additive, optional component, `HealthReady.components.sourceDefaults`, which is not in `required` (MediaNest DECISIONS #54). `POST /sources` now also declares its 503 `ProviderUnavailable` response. `info.version` stays `1.0.0`. The vendored copy was re-synced from the canonical MediaNest branch commit `f84aa1853cf8b3ba2cd4c68be6dca8b997e64731` (#2404), and `contract_fixtures.json` `source_sha256` was re-locked.

## Before tagging

- Merge the canonical contract PR in MediaNest. Then re-sync the vendored header SHA to the merged commit and re-lock `source_sha256`. The body stays byte-identical.
- Bump `medianest_bridge/config.py::BRIDGE_VERSION` to `1.1.0`.
- Update the "Fork delta" count in the README and `docs/upstream-sync.md` if an upstream sync lands in between. This release adds upstream touch points in `sync/models/media.py`, `sync/models/source.py` and `sync/tasks.py`.
- Follow MediaNest `docs/deployment/youtube-plex-tv-library-migration.md` for rollout. It covers the ZFS snapshot, backfill dry-run, pilot, new Plex library, and `PLEX_LIBRARY_KEY` switch.

## Rollback

- Bridge-created sources (new ones, and every source the backfill touched) store a `media_format` that uses the new `{episode_yyyy}`, `{episode_mmddnn}` and `{title_full_bounded}` keys. Unsetting `MEDIANEST_BRIDGE_SOURCE_DEFAULTS` does not change stored sources. An image older than this release does not know those keys, so every media save for those sources would fail. Do not roll the image back below this release until each affected source's previous `media_format` has been restored.
- The backfill moves files and updates the TubeSync database together. Snapshot and restore the TubeSync `/config` database together with the downloads dataset. Restoring only the files leaves `media_file` and `media_format` pointing at `Season YYYY/` paths that no longer exist, and TubeSync then marks that media skipped.

## Verification (2026-09-25, stack tip T4, after the review sweep)

- `manage.py test sync medianest_bridge`: 451 tests OK. They ran inside `ghcr.io/kinginyellows/tubesync:bridge-v1.0.0` with the worktree mounted and `local_settings.py` copied from `.example`, as CI does.
- `ruff check` with the CI rule set: clean. `makemigrations --check`: no changes.
- Manual end-to-end smoke: a throwaway SQLite DB and scratch `DOWNLOAD_ROOT`, with fixture metadata and no network. `--all-bridge-sources --apply` produced `video/acq-src-*/tvshow.nfo` and `Season 2017/s2017e091101 - <title> [<key>].mkv|.nfo` for a channel and a playlist source. A non-`acq-src-` source was untouched. Every `.nfo` parsed with ElementTree (`xmllint` is not in the image).
- Not verifiable offline: Plex's actual NFO-agent parsing, which should be confirmed on the pilot source during the migration runbook.
