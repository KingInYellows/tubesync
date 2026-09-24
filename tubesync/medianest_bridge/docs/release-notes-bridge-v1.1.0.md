# Release notes: `bridge-v1.1.0` (proposed, not yet tagged)

This release makes bridge-created YouTube sources land as proper TV shows in a
Plex **TV Shows** library that uses the native "Plex NFO Series" agent
(PMS 1.43.1+). Each channel or playlist becomes a show, each upload year a
season, and each video an episode with a stable date-based number. Sidecar
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
     `<episode>MMDDNN</episode>`. Playlist NFOs are unchanged.
   - Backfilling an older video never renumbers other days.
2. **`tvshow.nfo` per source** (new `sync/tvshow_nfo.py`, with hooks in the
   upstream-owned `sync/tasks.py`).
   - The file is written after indexing and after channel-image download. It
     contains the real channel or playlist title, the description when
     known, and `<uniqueid type="youtube">`.
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
     Nothing falls back silently.
4. **`manage.py medianest_backfill_plex_sidecars`** (new command, no upstream
   edits).
   - It applies the profile to existing `acq-src-*` sources, renames
     downloaded files into `Season YYYY/`, rewrites episode NFOs, writes
     `tvshow.nfo`, and enqueues channel images.
   - Dry-run is the default. It never deletes files and is idempotent.

## Contract

The contract gains one additive, optional component: `HealthReady.components.sourceDefaults`, which is not in `required` (MediaNest DECISIONS #54). `info.version` stays `1.0.0`. The vendored copy was re-synced from the canonical MediaNest branch commit `a7689cdc7a87f93f0ddc8a5c8efd9d9ec7c88eda`, and `contract_fixtures.json` `source_sha256` was re-locked.

## Before tagging

- Merge the canonical contract PR in MediaNest. Then re-sync the vendored header SHA to the merged commit and re-lock `source_sha256`. The body stays byte-identical.
- Bump `medianest_bridge/config.py::BRIDGE_VERSION` to `1.1.0`.
- Update the "Fork delta" count in the README and `docs/upstream-sync.md` if an upstream sync lands in between. This release adds upstream touch points in `sync/models/media.py`, `sync/models/source.py` and `sync/tasks.py`.
- Follow MediaNest `docs/deployment/youtube-plex-tv-library-migration.md` for rollout. It covers the ZFS snapshot, backfill dry-run, pilot, new Plex library, and `PLEX_LIBRARY_KEY` switch.

## Verification (2026-09-24, stack tip T4)

- `manage.py test sync medianest_bridge`: 416 tests OK. They ran inside `ghcr.io/kinginyellows/tubesync:bridge-v1.0.0` with the worktree mounted and `local_settings.py` copied from `.example`, as CI does.
- `ruff check` with the CI rule set: clean. `makemigrations --check`: no changes.
- Manual end-to-end smoke: a throwaway SQLite DB and scratch `DOWNLOAD_ROOT`, with fixture metadata and no network. `--all-bridge-sources --apply` produced `video/acq-src-*/tvshow.nfo` and `Season 2017/s2017e091101 - <title> [<key>].mkv|.nfo` for a channel and a playlist source. A non-`acq-src-` source was untouched. Every `.nfo` parsed with ElementTree (`xmllint` is not in the image).
- Not verifiable offline: Plex's actual NFO-agent parsing, which should be confirmed on the pilot source during the migration runbook.
