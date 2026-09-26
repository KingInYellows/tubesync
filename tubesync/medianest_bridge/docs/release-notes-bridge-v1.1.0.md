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
   `sync/models/source.py`, `sync/models/metadata.py` and
   `sync/templates/sync/_mediaformatvars.html`, which are upstream-owned).
   - New `media_format` keys:
     - `{episode_yyyy}` and `{episode_mmddnn}`, both derived from one date
       source, `Media.episode_date`, in precedence order: the related
       `Metadata` row's `new_metadata.published` (stable -- set once by
       `ingest_metadata`, never rewritten by a re-index), then
       `Media.published` (which IS rewritten with approximate data on
       every re-index), then `Media.upload_date`, then `Media.created`.
       `NN` is the same-day index, computed with a single annotated
       COUNT query (`_episode_date_coalesce`) instead of an O(n) Python
       scan.
     - `{title_full_bounded}`, the title capped at 150 UTF-8 bytes.
   - Channel NFOs now emit `<season>YYYY</season>` and
     `<episode>MMDDNN</episode>`, sharing the exact same overflow scheme
     as the filename's `{episode_mmddnn}`: past 99 videos on one day,
     the number moves to a separate 10,000,000+ range so the filename and
     the NFO's `<episode>` never disagree once parsed as an int, and
     numbers never collide. Playlists filed by this scheme (every
     bridge-created playlist) get the same values. Other playlists keep
     season `1` and playlist order.
   - Items are grouped by the same date they encode, including unpublished
     items whose date comes from metadata, so two videos never share a
     number.
   - Backfilling an older video never renumbers other days.
   - For a source filed by `{episode_mmddnn}`, a downloaded file keeps the
     number its name already carries; other same-day items take the free
     numbers around it. An earlier video indexed after a later one was
     downloaded therefore never gets that file's name as its download
     target (yt-dlp would have reported it as already downloaded).
   - `format_dict` computes `{episode_yyyy}`/`{episode_mmddnn}` (a query
     each) only for a `media_format` that uses them, and `rename_files`
     rewrites the episode NFO whenever `write_nfo` is on, so a renumbered
     file's `<episode>` follows its new name.
   - The three keys are listed in the source form's media-format help.
2. **`tvshow.nfo` per source** (new `sync/tvshow_nfo.py`, with hooks in the
   upstream-owned `sync/tasks.py`).
   - The file is written after indexing, after channel-image download
     (even when an image fails, since the channel metadata is already
     cached by then), and after each video's metadata is saved (so the
     first real channel name
     reaches it without waiting for the next index). It contains the real
     channel or playlist title, the description when known, and two
     `<uniqueid>` elements (`type="youtube"`, the source's current `key`;
     `type="tubesync"`, its immutable UUID plus a checksum of the file, so
     the file stays recognised as this writer's own even after an
     operator edits the source's `key`).
   - Only a file carrying that `tubesync` id, and not edited since (its
     checksum still matches), is replaced. A hand-edited copy, a
     `create-tvshow-nfo` file, or any file with only a `youtube` id is
     kept, and episode NFOs then take their `<showtitle>` from it.
   - Writing it is best-effort: a missing source directory is skipped and
     any other error is logged, never failing or retrying the task. A
     path that already holds a video's own NFO (a `media_format`
     rendering to `tvshow`), any other `<tvshow>` this writer did not
     create, or a non-empty file that does not parse as XML at all (a
     Kodi URL-only NFO, or upstream `create-tvshow-nfo`'s own output when
     a channel name has a raw `&`) is left alone with a logged warning; a
     zero-byte file is still replaceable. A symlinked `tvshow.nfo`, dangling
     or not, is never replaced; a live one's `<title>` still names the show.
   - The show title is resolved from the cheapest real data available
     (the cached channel/playlist metadata -- its `channel` for a channel,
     not the tab-suffixed page title -- then the newest few media with a
     channel/uploader or playlist title, then `source.name`; newer media
     win after a channel rename) and cached process-locally for 60
     seconds per source, since the same resolver runs once per episode
     NFO too; a database error (any `django.db.Error`, run in a
     savepoint) while resolving is logged and falls back to `source.name`
     rather than failing the caller.
   - The episode `<showtitle>` uses the same resolved title, and keeps a
     title made only of emoji, as `tvshow.nfo`'s `<title>` does.
   - Writes are escaped via ElementTree and happen only when the content
     changed -- one `build_tvshow_nfo()` call per write, shared by the
     need-to-write check and the actual write, not two.
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
     `media_format` whose rendered path has a `..` segment or an invalid
     `filter_text` regex is rejected -- both checked on the value the
     source form would store.
   - `POST /sources/validate` and `POST /sources` check only the requested
     type's overlay, its field allowlist included; malformed JSON, an
     unknown top-level key, an uncovered type, a non-object block or a bad
     `"*"` field still fails every type. Readiness checks both, and a
     healthy `sourceDefaults`
     names any type whose explicit `{}` opts out of a non-empty `"*"`.
4. **`manage.py medianest_backfill_plex_sidecars`** (new command, no upstream
   edits).
   - It applies the profile to existing `acq-src-*` sources, renames
     downloaded files into `Season YYYY/`, rewrites episode NFOs, writes
     `tvshow.nfo`, and enqueues channel images.
   - Dry-run is the default and validates exactly what `--apply` would. It
     never deletes files. A re-run is safe: it only saves a source whose
     fields actually change.
   - **Rename-cascade gate.** Saving a source fires TubeSync's own
     `save_all_media_for_source` -> `rename_all_media_for_source` cascade,
     which calls upstream `Media.rename_files()` directly with none of
     this command's own refusal checks and silently overwrites a
     same-stem sidecar. That cascade only skips a source when BOTH
     `TUBESYNC_RENAME_ALL_SOURCES` is `false` (default `true`) AND the
     source's directory is not in `TUBESYNC_RENAME_SOURCES` -- so on a
     typical deployment it WOULD run a few minutes after this command
     saves a source and clobber exactly the media this command itself
     refused. `--apply` now runs the same per-media decision logic a
     dry-run would, before saving any source whose overlay actually
     changes a field: if the cascade is enabled for that source and any
     media would be refused, the source is not saved at all, one error is
     counted, and the operator is told to resolve the conflicts or
     disable the cascade before re-running. The same happens, counted as
     `in_flight`, when the overlay can change the rendered path and any of
     the source's media is downloading right now (it would finish under
     the old name, and the cascade would rename it later, unchecked). The
     dry-run stops the source the same way, so its summary matches
     `--apply`'s.
   - **Wider occupied-sidecar detection.** An occupied target for the
     video (on disk -- a dangling symlink counts -- or already claimed by
     another media earlier in the same run, as its video or as a sidecar
     destination of its rename, dry-run included), an occupied destination
     for any sidecar (the same claimed paths count)
     `rename_files()` would move, OR an already-occupied target-side
     `.nfo` that no move of this media's own would bring (which this
     command's own NFO write would otherwise silently clobber right after
     the video moves), is an error and nothing moves. A foreign target-side
     `.jpg` is left alone and does not block the rename. Adopting an
     earlier half-finished move that left a stray same-key sidecar behind,
     or whose target is a symlink or resolves outside `DOWNLOAD_ROOT`, is
     also an error -- nothing is adopted, moved, or deleted. A stray
     old-name sidecar in the target's own directory (a format that only
     changed the file name) counts too; only the target's own sidecars are
     excluded.
   - **Key-matched moves.** With `{key}` in the profile, `rename_files()`
     also moves every path under the source directory whose name contains
     the media's key. The dry-run lists each of those moves
     (`key_matched_moves`), and a match that is another media's video, a
     sidecar of one, or a directory (in either move set), or whose
     destination is already taken (`rename_files()` would leave it behind),
     makes the media an error instead. So does a current file that is a
     symlink, is not a regular file (a directory would be moved whole) or
     resolves outside `DOWNLOAD_ROOT`, or a target directory that resolves
     outside it. A media already at its target gets the same checks before
     its NFO and thumbnail are written.
   - **Foreign episode NFOs.** An existing `.nfo` at the target that is not
     this media's own (`<episodedetails>` whose `<id>`/`<uniqueid>` is its
     key), or is a symlink, is never overwritten -- for a rename, an
     already-in-place media
     or an adoption alike; it is reported as an error.
   - **Targeted source save.** Only the overlay fields that change are
     saved, onto a freshly read row, so concurrent edits and
     `target_schedule` are kept and other fields are not re-normalized.
   - **Downloads during the run.** Media that finish downloading while
     `--apply` runs are processed before it ends; media still busy (their
     `media:<uuid>` lock is held) after an overlay that can change the
     rendered path (`media_format`, `source_resolution`, `source_vcodec`,
     `source_acodec`, `prefer_60fps`, `prefer_hdr` or `fallback`) are
     counted as `in_flight` and fail the run so it is repeated.
   - Turning `copy_channel_images` on makes TubeSync's own signal queue an
     image download even when `poster.jpg` exists, and that download
     replaces the existing images; both modes count it and print a note.
   - Dry-run turns `TUBESYNC_SHRINK_OLD` off while reading metadata, so it
     writes nothing to the database. Apply counts the episode NFO
     `rename_files()` wrote as written, matching the dry-run.
   - `--apply` must run as the user that owns `DOWNLOAD_ROOT` (`docker exec
     -u app ...`), otherwise new `Season YYYY/` directories would be
     root-owned and unwritable by TubeSync. It refuses otherwise.
   - Each video's database record is saved as soon as the file moves, before
     its NFO and thumbnail are written, so a later failure never leaves the
     database behind the file. Every per-media and per-source failure (a
     rename problem, locked media, an adoption or tvshow.nfo failure) is
     both logged and printed to stdout, not just logged, so the "see the
     output above" the final error points to is accurate. The command
     exits non-zero when anything errored or was skipped as locked, so the
     operator re-runs it.
   - Channel-image enqueueing goes through `TaskHistory.schedule(...,
     remove_duplicates=True)` (the same mechanism TubeSync's own signals
     use for `save_all_media_for_source`/`index_source`), so a job already
     pending for a source is revoked rather than duplicated when a worker
     next picks one up, instead of calling the huey task directly.

## Contract

The contract gains one additive, optional component, `HealthReady.components.sourceDefaults`, which is not in `required` (MediaNest DECISIONS #54). Both `POST /sources` and `POST /sources/validate` now also declare a 503 `ProviderUnavailable` response for a broken `MEDIANEST_BRIDGE_SOURCE_DEFAULTS`. `info.version` stays `1.0.0`. The vendored copy was re-synced from the canonical MediaNest branch commit `479b97ea4fa990def968f99db7052b04cafd5e0d` (#2404; description-only on top of the 503 declarations: the source-defaults 503 is per requested source type, and a bridge reporting `sourceDefaults` says `healthy` with nothing configured), and `contract_fixtures.json` `source_sha256` was re-locked.

MediaNest calls `POST /sources/validate` before `POST /sources` and treats any validate failure as fatal for the whole submission (`acquisition-source-write.dispatch.ts`'s `validate_source_failed`), so a broken source-defaults configuration therefore fails at validate-time as a real, actionable 503 the user can re-submit once an operator fixes it -- this is the 503 that matters for retries. `POST /sources`' own identical 503 remains a backstop for a race between the validate call and the create call that follows it (MediaNest's own error translation has no 503 case for a create-time failure specifically, so that path is reconciled as an unknown outcome rather than retried).

## Before tagging

- Merge the canonical contract PR in MediaNest. Then re-sync the vendored header SHA to the merged commit and re-lock `source_sha256`. The body stays byte-identical.
- Bump `medianest_bridge/config.py::BRIDGE_VERSION` to `1.1.0`.
- Update the "Fork delta" count in the README and `docs/upstream-sync.md` if an upstream sync lands in between. This release adds upstream touch points in `sync/models/media.py`, `sync/models/source.py`, `sync/models/metadata.py`, `sync/templates/sync/_mediaformatvars.html` and `sync/tasks.py` (nine upstream files, ten touch points in total).
- Follow MediaNest `docs/deployment/youtube-plex-tv-library-migration.md` for rollout. It covers the ZFS snapshot, backfill dry-run, pilot, new Plex library, and `PLEX_LIBRARY_KEY` switch.

## Known limits

- Locked media (another task holds its lock) are skipped and retried by
  the next run; a normal rename task may also move them later, without
  this command's checks.
- The rename-cascade gate reads `TUBESYNC_RENAME_ALL_SOURCES` and
  `TUBESYNC_RENAME_SOURCES` in the command's own process. Run it with the
  same environment as the workers.
- A leftover old-name sidecar is found only when its name contains the
  media key; a legacy `media_format` without `{key}` leaves no way to
  recognise it after the video has moved.
- `in_flight` is inferred from the media lock, which other media tasks
  also take briefly, so a busy source can report a false positive; re-run
  when it is idle.
- The in-flight check runs just before the source is saved. A download
  that starts between the check and the save still finishes under the old
  name; with the rename cascade on, the queued
  `rename_all_media_for_source` can then rename it without this command's
  checks. The post-save in-flight count reports it, but cannot cancel the
  queued cascade. Run the backfill with `TUBESYNC_RENAME_ALL_SOURCES=false`
  (and the sources out of `TUBESYNC_RENAME_SOURCES`) to close this window.
- Index-only sources (`download_media` off) only carry approximate
  listing dates until an item is downloaded, so their numbering can move.
- A source whose `media_format` does not use `{episode_mmddnn}` numbers
  same-day items live. Deleting a media keeps its place (TubeSync leaves a
  skipped placeholder row), but once an earlier same-day row is gone for
  good a later item's `<episode>` moves down on its next NFO rewrite, as
  upstream's `calculate_episode_number()` does. Bridge-created sources
  are unaffected.

## Rollback

- Bridge-created sources (new ones, and every source the backfill touched) store a `media_format` that uses the new `{episode_yyyy}`, `{episode_mmddnn}` and `{title_full_bounded}` keys. Unsetting `MEDIANEST_BRIDGE_SOURCE_DEFAULTS` does not change stored sources. An image older than this release does not know those keys, so every media save for those sources would fail. Do not roll the image back below this release until each affected source's previous `media_format` has been restored.
- The backfill moves files and updates the TubeSync database together. Snapshot and restore the TubeSync `/config` database together with the downloads dataset. Restoring only the files leaves `media_file` and `media_format` pointing at `Season YYYY/` paths that no longer exist, and TubeSync then marks that media skipped.

## Verification (2026-09-25, stack tip T4, after the rename-cascade-gate review sweep)

- `manage.py test sync medianest_bridge`: 508 tests OK. They ran inside `ghcr.io/kinginyellows/tubesync:bridge-v1.0.0` with the worktree mounted and `local_settings.py` copied from `local_settings.py.container`, as CI does.
- `ruff check` on the changed files: clean.
- Manual end-to-end smoke (earlier in this stack): a throwaway SQLite DB and scratch `DOWNLOAD_ROOT`, with fixture metadata and no network. `--all-bridge-sources --apply` produced `video/acq-src-*/tvshow.nfo` and `Season 2017/s2017e091101 - <title> [<key>].mkv|.nfo` for a channel and a playlist source. A non-`acq-src-` source was untouched. Every `.nfo` parsed with ElementTree (`xmllint` is not in the image).
- New this sweep: the rename-cascade gate (source not saved when the cascade is enabled and a refusal exists; proceeds when the cascade is disabled or nothing is refused; dry-run's informational note), the widened target-side sidecar-collision check, an adoption with a leftover stray sidecar, the per-source directory snapshot replacing a per-media `Path.rglob` walk, `TaskHistory.schedule(remove_duplicates=True)` for the channel-image job, stdout lines for every per-media/per-source failure, and `write_tvshow_nfo()`/`tvshow_nfo_needs_write()` sharing one `build_tvshow_nfo()` call.
- Not verifiable offline: Plex's actual NFO-agent parsing, which should be confirmed on the pilot source during the migration runbook.

## Verification (2026-09-25, second review follow-up sweep)

- `manage.py test sync medianest_bridge` in `ghcr.io/kinginyellows/tubesync:bridge-v1.0.0` (worktree mounted, `local_settings.py` from `local_settings.py.container`, `--entrypoint /usr/bin/python3`): 590 tests OK at the stack tip; 386, 440 and 514 at Plex T1, T2 and T3 (after the third review pass, which added the foreign-episode-NFO, symlink, directory and collision refusals and the contract re-vendor at `479b97ea`).
- `ruff check` with CI's rule set: no new findings.
- New tests cover the kept episode numbers, the NFO rewrite on rename, the `tvshow.nfo` checksum and preserved titles, the source-defaults checks on stored values, and every backfill change listed above.

## Verification (2026-09-26, fourth review follow-up sweep)

- `manage.py test sync medianest_bridge`, same image and setup as above: 600 tests OK at the stack tip; 386, 441 and 518 at Plex T1, T2 and T3.
- `ruff check` with CI's rule set: only the two known hits (`sync/views/sources.py`, `sync/youtube.py:314`).
- New this sweep: `tvshow.nfo` refreshed when a channel image fails (T2); a bad field in one type's source-defaults block no longer blocks the other type (T3); and for the backfill, a directory as the current file, dangling symlinks at the video and sidecar targets, an in-flight download refusing a cascade-enabled save (dry-run and apply summaries equal), the in-flight count for a `source_acodec`-only overlay, the dry-run stopping a gated source like `--apply`, and an old-name sidecar beside a same-directory target.
- Each new test was checked to fail with its fix reverted.

## Verification (2026-09-26, fifth review follow-up sweep)

- `manage.py test sync medianest_bridge`, same image and setup as above: 611 tests OK at the stack tip; 389, 447 and 524 at Plex T1, T2 and T3.
- `ruff check` with CI's rule set: only the two known hits.
- New this sweep: characterization tests for the legacy-format same-day drift and the placeholder that prevents it (T1); an emoji-only `<showtitle>` and dangling/live `tvshow.nfo` symlinks (T2); and for the backfill, a directory or an outside-root path at an already-in-place target, a sidecar onto an earlier media's projected video and a video onto an earlier media's projected sidecar (dry-run and apply summaries equal), and a dangling `tvshow.nfo` symlink surviving `--apply`.
- Each new fix's test was checked to fail with the fix reverted.
