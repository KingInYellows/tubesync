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
     kept, and episode NFOs then take their `<showtitle>` from it as
     written, emoji included.
   - Writing it is best-effort: a missing source directory is skipped and
     any other error is logged, never failing or retrying the task. A
     path that already holds a video's own NFO (a `media_format`
     rendering to `tvshow`), any other `<tvshow>` this writer did not
     create, or a non-empty file that does not parse as XML at all (a
     Kodi URL-only NFO, upstream `create-tvshow-nfo`'s own output when
     a channel name has a raw `&`, or a declared encoding such as
     `ANSI` or `UTF-32` that the XML parser cannot read) is left alone with a logged warning; a
     zero-byte file is still replaceable. A symlinked `tvshow.nfo`, dangling
     or not, is never replaced; a live one's `<title>` still names the show.
     Anything else that is not a regular file (a directory, or a FIFO,
     which a read would block on) is left alone the same way, never read.
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
     or whose target is a symlink, resolves outside `DOWNLOAD_ROOT`, or
     belongs to another media (its video, a sidecar of one, or a file an
     earlier rename in this run moves there), is also an error -- nothing is adopted, moved, or deleted. A stray
     old-name sidecar in the target's own directory (a format that only
     changed the file name) counts too; only the target's own sidecars
     (its stem followed by a `.`) are excluded, so a leftover whose old
     stem merely starts with the new one is still found.
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
     its NFO and thumbnail are written. A rename whose current or target
     directory goes through a symlink inside `DOWNLOAD_ROOT` is refused
     too: `rename_files()` resolves both paths, so it would gather sidecars
     next to the resolved current file that these checks never saw, and
     record the resolved new path, not the target (the rename would look
     failed after the move and later runs would find the target occupied).
     More generally, paths must be canonical: a run whose media storage
     location (`DOWNLOAD_ROOT`) goes through a symlink is refused before
     anything happens, and a source any of whose downloaded rows is
     recorded through a symlinked directory is refused before it is saved
     (another row's video, seen through the alias, would otherwise look
     like an unclaimed sidecar and be moved). A source whose directory
     contains another source's directory, or lies inside one, is refused
     too, for every `media_format` (a parent's files beside the child's
     videos would look like the child's sidecars), and so is a source
     with another source's downloaded media whose recorded file resolves
     inside its tree (an alias elsewhere included): the old-stem glob beside each recorded video can
     already reach a nested directory, and the recursive `{key}` sweep
     would reach it as well, either of which this source's ownership
     checks cannot see. Bridge-created `acq-src-*` directories are
     siblings, never nested.
   - A `media_format` that renders an episode file named `tvshow` in the
     source directory would put its episode NFO at `tvshow.nfo`, after
     which the show-level NFO could never be written, and one whose
     extension is `.nfo` would put the episode NFO on the video itself
     (`rename_files()` would replace the video with XML); such a source is
     refused in both modes before anything is saved or moved.
   - A cached thumbnail that is a symlink or not a regular file (a
     directory or FIFO) is never copied; both modes print a note.
   - `--apply` saves the profile only if no field the checks relied on
     (the path fields, `directory`, `key`, `name`, `source_type`,
     `write_nfo`, `copy_thumbnails`, `copy_channel_images`) changed since the run read
     the source; otherwise nothing is saved or moved and the source is an
     error, so a concurrent edit can never make the save act on unchecked
     paths.
   - **Reserved paths.** Each media's paths, its video (current and
     target) and the episode NFO and thumbnail it will generate, must not
     meet the show's `tvshow.nfo`, a channel-image file name while channel
     images are on (the image download would overwrite it), the video's own
     target, or a sidecar another media generates (two videos with the
     same stem would share one `.nfo` and `.jpg`). The destinations of the
     sidecar and `{key}` moves a rename would make are claimed the same way,
     whatever the options, and with channel images on they count against
     the image names too (an existing `.jpg` moved onto `poster.jpg`), and a video
     recorded, or targeted, inside another source's directory is a
     collision as well (a stored `media_format` with a `..` segment), as
     are a target that does not resolve to itself (`sub/../x`), two media
     rendering to one video target, two rows recording one video file,
     and a target directory that cannot be created (an ancestor is a
     regular file). A sidecar move that would land on the renamed video
     itself refuses that media. A dry-run treats a path an earlier rename
     moves away from as free, and a file an earlier rename puts in place as
     present (in later old-stem globs too), as apply finds them. A
     collision refuses the source in both modes before anything is saved or moved; a download
     that finishes during `--apply` is checked against the same claims and
     skipped as an error on a collision. Media not downloaded yet
     (in-flight and skipped ones included, since a filter change or a
     manual re-enable can make a skipped row eligible later) meet every
     one of these checks too before the profile is saved, since they get
     the profile's paths when they land. Collisions the profile makes for
     every media, read from a rendered example of the format so format
     specs cannot hide them (a `.nfo` extension with `write_nfo`, a `.jpg`
     extension with `copy_thumbnails` (the thumbnail copy would replace the
     video), a `tvshow` or channel-image name in the source directory, or,
     with any of those options on, a format whose last segment does not end
     in a fixed extension, since a media's own data could then supply one,
     or, in the source directory itself, a stem that media data could turn
     into `tvshow` or a channel-image name, such as `{title_full}`; a bare
     `{key}` never can, being a whole 11-character YouTube ID, but a
     shortened one such as `{key:.6}` can) are refused
     even for a source with no media yet, and an in-place video's existing
     same-stem sidecars count against the channel-image names. A media not
     downloaded yet whose episode NFO path already holds a file that is
     not its own NFO refuses the source too (its download writes the NFO
     unconditionally), as does one whose video target or thumbnail path is
     already taken by a file (yt-dlp would adopt a file at the target as
     the finished download, and the thumbnail copy would replace one).
   - A source directory outside `DOWNLOAD_ROOT` (an absolute legacy or
     custom path) is refused in both modes before anything is saved, since
     the save would create it there.
   - **Videos inside their own source.** Every downloaded row's recorded
     video must resolve inside its own source's directory; a legacy or
     custom layout recording one elsewhere refuses the source (the tree
     checks only cover the source's directory, while `rename_files()`
     globs sidecars beside wherever the video actually is).
   - **Plain source trees only.** Before anything else, each source's
     directory is walked once (without following links), and the source
     is refused in both modes when it holds any symlink (live or dangling)
     or any entry that is neither a regular file nor a directory (a FIFO,
     socket or device). `rename_files()` resolves symlinks while the
     command's checks compare paths lexically, skips dangling links, and
     moves special files as if they were sidecars, so this one
     precondition covers every such case; the per-path checks described
     above remain as defense in depth. TubeSync itself never creates
     symlinks under a source directory, so only operator-made ones can
     trigger it, and the dry-run lists them before anything changes. The
     source directory's own path must be canonical as well (no symlinked
     parent), or the save could create the directory wherever the link
     points; and a source that another source reaches through an alias
     overlaps it for every `media_format` (the old-stem glob alone could
     take the other source's files), and a nested source overlaps it for
     every `media_format` too (the old-stem glob beside a video in a
     nested directory can reach it).
   - **Foreign episode NFOs.** An existing `.nfo` at the target that is not
     this media's own (`<episodedetails>` whose `<id>`/`<uniqueid>` is its
     key), or is a symlink, is never overwritten -- for a rename, an
     already-in-place media
     or an adoption alike; it is reported as an error. An `.nfo` path that
     is not a regular file (a directory or FIFO) counts as foreign and is
     never read. The same check
     covers an `.nfo` that either move set would carry onto the target NFO
     name (an old-name one beside the video, or a key match), because
     `rename_files()` rewrites the NFO right after the move. An `.nfo` in
     an encoding the XML parser cannot read counts as foreign.
   - **Targeted source save.** Only the overlay fields that change are
     saved, onto a freshly read row, so concurrent edits and
     `target_schedule` are kept and other fields are not re-normalized.
   - **Downloads during the run.** Media that finish downloading while
     `--apply` runs are processed before it ends; media still busy (their
     `media:<uuid>` lock is held, even if marked skipped meanwhile) after
     an overlay that can change the
     rendered path (`media_format`, `source_resolution`, `source_vcodec`,
     `source_acodec`, `prefer_60fps`, `prefer_hdr` or `fallback`), or
     the sidecars a finishing download writes (`write_nfo`,
     `copy_thumbnails`), are
     counted as `in_flight` and fail the run so it is repeated. A save that
     would queue the channel image job waits for running downloads the same
     way, with or without the rename cascade.
   - Turning `copy_channel_images` on makes TubeSync's own signal queue an
     image download even when `poster.jpg` exists, and that download
     replaces the existing images; both modes count it and print a note.
   - The channel-image download writes `thumbnail.jpg`, `banner.jpg`,
     `background.jpg`, `poster.jpg` and `season-poster.jpg` with a plain
     `open()`, which follows a symlink. The command never queues it while
     any of those is a symlink, even a dangling one, or something other
     than a regular file (a directory would make it fail and retry, a FIFO
     could block it), or while the source
     directory is missing and no save in this run recreates it (the task
     never creates it, so it would fail and retry); it prints a note
     instead. A symlinked `poster.jpg` also counts as present. When an
     overlay would turn `copy_channel_images` on while such a link exists
     (or the source directory resolves outside `DOWNLOAD_ROOT`), saving
     the overlay would queue TubeSync's own image download unconditionally
     -- so the command refuses the source as an error in both modes
     *before* saving anything, rather than saving it and only noting the
     problem afterwards.
   - A source whose directory path is taken by something that is not a
     directory (a regular file, or a symlink that does not resolve to one)
     is an error in both modes and nothing is saved: TubeSync's own
     directory check on save would fail with `FileExistsError`.
   - A source directory that resolves outside `DOWNLOAD_ROOT` is an error
     in both modes: neither `tvshow.nfo` nor the image download is
     written or queued (`write_text_file()` would otherwise create its
     temporary file there before its own containment check fails).
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

The contract gains one additive, optional component, `HealthReady.components.sourceDefaults`, which is not in `required` (MediaNest DECISIONS #54). Both `POST /sources` and `POST /sources/validate` now also declare a 503 `ProviderUnavailable` response for a broken `MEDIANEST_BRIDGE_SOURCE_DEFAULTS`. `info.version` stays `1.0.0`. The vendored copy was re-synced from the canonical MediaNest commit `0e7d2375b42ac99505b11c4c1b88294f234d8d03`, the squash merge of #2404 to `main` (its contract file is byte-identical to the pre-merge branch commit `479b97ea4fa990def968f99db7052b04cafd5e0d` first vendored here; description-only on top of the 503 declarations: the source-defaults 503 is per requested source type, and a bridge reporting `sourceDefaults` says `healthy` with nothing configured), and `contract_fixtures.json` `source_sha256` was re-locked.

MediaNest calls `POST /sources/validate` before `POST /sources` and treats any validate failure as fatal for the whole submission (`acquisition-source-write.dispatch.ts`'s `validate_source_failed`), so a broken source-defaults configuration therefore fails at validate-time as a real, actionable 503 the user can re-submit once an operator fixes it -- this is the 503 that matters for retries. `POST /sources`' own identical 503 remains a backstop for a race between the validate call and the create call that follows it (MediaNest's own error translation has no 503 case for a create-time failure specifically, so that path is reconciled as an unknown outcome rather than retried).

## Before tagging

- ~~Merge the canonical contract PR in MediaNest, then re-sync the vendored header SHA to the merged commit and re-lock `source_sha256`.~~ Done 2026-09-26: #2404 merged as `0e7d2375b`; the header names it, the body is byte-identical, and `source_sha256` is re-locked.
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
  queued cascade. It counts only rows not yet marked downloaded, so a
  download that finishes between the save and that count (already marked
  downloaded, its lock still held, its own rename not yet run) is missed;
  counting every locked row would also count TubeSync's brief cleanup
  and migration locks on downloaded media. Run the backfill with `TUBESYNC_RENAME_ALL_SOURCES=false`
  (and the sources out of `TUBESYNC_RENAME_SOURCES`) to close this window.
- A source whose directory holds any symlink or special file cannot be
  backfilled until those entries are replaced or removed (see "Plain
  source trees only" above).
- Index-only sources (`download_media` off) only carry approximate
  listing dates until an item is downloaded, so their numbering can move.
- A source whose `media_format` does not use `{episode_mmddnn}` numbers
  same-day items live. Deleting a media keeps its place (TubeSync leaves a
  skipped placeholder row), but once an earlier same-day row is gone for
  good a later item's `<episode>` moves down on its next NFO rewrite, as
  upstream's `calculate_episode_number()` does. Bridge-created sources
  are unaffected.
- A `Metadata` row ingested before this release from metadata with only
  an `upload_date` (no `timestamp` or `release_timestamp`) stored
  `Media.published` or the retrieval time as its `published`, and
  `episode_date` prefers that stored value. Such an item can land on the
  wrong day, season or same-day number. YouTube metadata nearly always carries a timestamp, so this is
  rare; fixing the stored rows would need a data migration of
  upstream-owned data, which this release does not do. Check the pilot
  source's filenames against the upload dates.
- The backfill knows the paths of downloaded media only. A download still
  in progress has no `media_file` yet, so a stem glob or a `{key}` match
  of another media could, in principle, move its `.part` or finished
  file. That needs two media's names to overlap; the in-flight gate
  covers the common case, and running the backfill while the source is
  idle (see above) closes it.

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

## Verification (2026-09-26, sixth review follow-up sweep)

- `manage.py test sync medianest_bridge`, same image and setup as above: 620 tests OK at the stack tip; 389, 450 and 527 at Plex T1, T2 and T3.
- `ruff check` with CI's rule set: only the two known hits.
- New this sweep: a foreign `tvshow.nfo` declaring an encoding the parser cannot read (`ANSI`, `UTF-32`) and a foreign `<title>` with emoji, kept as written in `<showtitle>` (T2); and for the backfill, a foreign `.nfo` beside the old video, a foreign key-matched `.nfo` and one in an unreadable encoding (each refused in both modes with equal summaries), the media's own old `.nfo` still moving, a dangling `poster.jpg` symlink queueing no image download, and a leftover whose old stem starts with the new stem.
- The legacy stored publish dates (T1) and downloads still in progress are documented under "Known limits" instead of fixed.
- Each new fix's test was checked to fail with the fix reverted.

## Verification (2026-09-26, seventh review follow-up sweep)

- `manage.py test sync medianest_bridge`, same image and setup as above: 623 tests OK at the stack tip (T1-T3 unchanged: 389, 450 and 527).
- CI's configuration as well: `manage.py test --no-input --buffer` (every app, `common` included) with `TUBESYNC_DEBUG=True` and a final `DEBUG = False`, as CI's `local_settings.py.example` sets it: 631 tests OK at the stack tip.
- `ruff check` with CI's rule set: only the two known hits.
- New this sweep, for the backfill: a missing media is never adopted onto a file an earlier rename in this run moves to its target, nor onto another media's existing sidecar, and a stem match that is the sidecar of a media whose video is missing is never moved (dry-run and apply summaries equal). Each test was checked to fail with its fix reverted.

## Verification (2026-09-26, eighth review follow-up sweep)

- `manage.py test sync medianest_bridge`, same image and setup as above: 626 tests OK at the stack tip (T1-T3 unchanged). In CI's configuration (every app, `TUBESYNC_DEBUG=True`, final `DEBUG = False`): 634 tests OK.
- `ruff check` run as CI runs it (from `tubesync/`, reading `ruff.toml`): only the two known hits.
- New this sweep, for the backfill: a symlink at any file the channel-image download writes, or a source directory resolving outside `DOWNLOAD_ROOT`, queues no image download in either mode; and a download marked skipped while it runs still counts as `in_flight` (dry-run and apply summaries equal). Each test was checked to fail with its fix reverted.

## Verification (2026-09-26, ninth review follow-up sweep)

- `manage.py test sync medianest_bridge`, same image and setup as above: 629 tests OK at the stack tip (T1-T3 unchanged). In CI's configuration (every app, `TUBESYNC_DEBUG=True`, final `DEBUG = False`): 637 tests OK.
- `ruff check` run as CI runs it: only the two known hits.
- New this sweep, for the backfill: a source directory resolving outside `DOWNLOAD_ROOT` writes nothing (no `tvshow.nfo`, no temporary file, no image job) and is an error in both modes with equal summaries; a missing source directory that nothing recreates queues no image download, while one a save recreates still does (dry-run and apply equal). Each test was checked to fail with its fix reverted.
- A download that finishes between the source save and the post-save in-flight count is documented under "Known limits" instead of fixed.

## Verification (2026-09-26, tenth review follow-up sweep)

- `manage.py test sync medianest_bridge`, same image and setup as above: 631 tests OK at the stack tip (T1-T3 unchanged). In CI's configuration (every app, `TUBESYNC_DEBUG=True`, final `DEBUG = False`): 639 tests OK.
- `ruff check` run as CI runs it: only the two known hits.
- New this sweep, for the backfill: an overlay that would turn `copy_channel_images` on is refused before it is saved, in both modes with equal summaries, when an image destination is a symlink or the source directory resolves outside `DOWNLOAD_ROOT`; TubeSync's own `source_pre_save` image job is never queued and the source row is unchanged. Each test was checked to fail with the gate removed.

## Verification (2026-09-26, eleventh review follow-up sweep)

- `manage.py test sync medianest_bridge`, same image and setup as above: 633 tests OK at the stack tip (T1-T3 unchanged). In CI's configuration (every app, `TUBESYNC_DEBUG=True`, final `DEBUG = False`): 641 tests OK.
- `ruff check` run as CI runs it: only the two known hits.
- New this sweep, for the backfill: a regular file or a dangling symlink at a source's directory path makes the source an error in both modes with equal summaries, before anything is saved. Each test was checked to fail with the check removed.

## Verification (2026-09-26, twelfth review follow-up sweep)

- `manage.py test sync medianest_bridge`, same image and setup as above: 636 tests OK at the stack tip; 389, 450 and 527 at Plex T1, T2 and T3 (T3 re-run after its contract header re-sync to MediaNest's merged `0e7d2375b`: the sha256 lock and the PyYAML derivation cross-check both pass). In CI's configuration (every app, `TUBESYNC_DEBUG=True`, final `DEBUG = False`): 644 tests OK.
- `ruff check` run as CI runs it: only the two known hits.
- New this sweep, for the backfill: a rename whose target directory goes through a symlink inside `DOWNLOAD_ROOT` is refused before anything moves; a directory or FIFO at a channel-image destination queues no image download and refuses an overlay that would turn `copy_channel_images` on (dry-run and apply summaries equal). Each test was checked to fail with its fix reverted.

## Verification (2026-09-26, thirteenth review follow-up sweep)

- `manage.py test sync medianest_bridge`, same image and setup as above: 641 tests OK at the stack tip; 389, 451 and 528 at Plex T1, T2 and T3. In CI's configuration (every app, `TUBESYNC_DEBUG=True`, final `DEBUG = False`): 649 tests OK.
- `ruff check` run as CI runs it: only the two known hits.
- New this sweep, closing two classes rather than single cases: every NFO read in the stack (`tvshow.nfo` in T2; the episode NFO at the target and beside the old video in the backfill) treats a path that is not a regular file as foreign and never reads it, so a FIFO cannot block and a directory cannot raise; and a rename refuses a current or target directory reached through a symlink inside `DOWNLOAD_ROOT`, because `rename_files()` resolves both. Each test was checked to fail with its fix reverted; the FIFO tests use an alarm that raises a `BaseException`, so a regression fails the test instead of hanging the run.

## Verification (2026-09-26, fourteenth review follow-up sweep)

- `manage.py test sync medianest_bridge`, same image and setup as above: 643 tests OK at the stack tip (T1-T3 unchanged: 389, 451 and 528). In CI's configuration (every app, `TUBESYNC_DEBUG=True`, final `DEBUG = False`): 651 tests OK.
- `ruff check` run as CI runs it: only the two known hits.
- New this sweep, making canonical paths a precondition instead of a per-media check: a symlinked media storage location refuses the whole run in both modes, and a source with any downloaded row recorded through a symlinked directory is refused in both modes (equal summaries) before anything is saved or moved. Two earlier tests now meet this earlier refusal and were updated to expect it. Each new test was checked to fail with its check removed.

## Verification (2026-09-26, fifteenth review follow-up sweep)

- `manage.py test sync medianest_bridge`, same image and setup as above: 645 tests OK at the stack tip (T1-T3 unchanged: 389, 451 and 528). In CI's configuration (every app, `TUBESYNC_DEBUG=True`, final `DEBUG = False`): 653 tests OK.
- `ruff check` run as CI runs it: only the two known hits.
- New this sweep: with `{key}` in the profile, a source whose directory contains another source's directory is refused in both modes before anything is saved or moved (the nested source's file carrying the same video key is untouched); a sibling source directory is not affected. The refusal test was checked to fail with the check removed.

## Verification (2026-09-26, sixteenth review follow-up sweep)

- `manage.py test sync medianest_bridge`, same image and setup as above: 648 tests OK at the stack tip (T1-T3 unchanged: 389, 451 and 528). In CI's configuration (every app, `TUBESYNC_DEBUG=True`, final `DEBUG = False`): 656 tests OK.
- `ruff check` run as CI runs it: only the two known hits.
- New this sweep, replacing further per-path fixes with one precondition: a source tree holding any symlink or special file is refused in both modes before anything is saved or moved (a dangling-symlink sidecar and an adoption through a symlinked directory are the new cases). Twenty-three earlier symlink/FIFO test cases now meet this earlier refusal; they were updated to expect it and still assert that nothing moved, was written or was saved. The source-level alias check keeps its own test for an alias outside the source tree. Each new test was checked to fail with its check removed.

## Verification (2026-09-26, seventeenth review follow-up sweep)

- `manage.py test sync medianest_bridge`, same image and setup as above: 651 tests OK at the stack tip (T1-T3 unchanged: 389, 451 and 528). In CI's configuration (every app, `TUBESYNC_DEBUG=True`, final `DEBUG = False`): 659 tests OK.
- `ruff check` run as CI runs it: only the two known hits.
- New this sweep: a source directory path through a symlinked parent is refused before the save can create anything (the outside directory stays empty); a source another source reaches through an alias counts as overlapping under `{key}`; and a dry-run counts an old-stem `.jpg` its rename projects to the target as an existing thumbnail, as apply does (equal summaries). Each test was checked to fail with its fix reverted.

## Verification (2026-09-27, eighteenth review follow-up sweep)

- `manage.py test sync medianest_bridge`, same image and setup as above: 652 tests OK at the stack tip (T1-T3 unchanged: 389, 451 and 528). In CI's configuration (every app, `TUBESYNC_DEBUG=True`, final `DEBUG = False`): 660 tests OK.
- `ruff check` run as CI runs it: only the two known hits.
- New this sweep: another source resolving to the same directory overlaps it even without `{key}` in the format (the old-stem glob alone could move its files). The test was checked to fail with the equality check tied to `{key}` again.

## Verification (2026-09-27, nineteenth review follow-up sweep)

- `manage.py test sync medianest_bridge`, same image and setup as above: 653 tests OK at the stack tip (T1-T3 unchanged: 389, 451 and 528). In CI's configuration (every app, `TUBESYNC_DEBUG=True`, final `DEBUG = False`): 661 tests OK.
- `ruff check` run as CI runs it: only the two known hits.
- New this sweep: a source whose directory contains another source's directory is refused for every `media_format`, not only a `{key}` one (the old-stem glob beside a video already in a nested directory can reach it). The test was checked to fail with the `{key}` condition restored.

## Verification (2026-09-27, twentieth review follow-up sweep)

- `manage.py test sync medianest_bridge`, same image and setup as above: 655 tests OK at the stack tip (T1-T3 unchanged: 389, 451 and 528). In CI's configuration (every app, `TUBESYNC_DEBUG=True`, final `DEBUG = False`): 663 tests OK.
- `ruff check` run as CI runs it: only the two known hits.
- New this sweep: a source nested inside another source's directory is refused (overlap is now checked in both directions), and a `media_format` rendering an episode NFO at `tvshow.nfo` is refused in both modes before anything is written. Each test was checked to fail with its fix reverted.

## Verification (2026-09-27, twenty-first review follow-up sweep)

- `manage.py test sync medianest_bridge`, same image and setup as above: 657 tests OK at the stack tip (T1-T3 unchanged: 389, 451 and 528). In CI's configuration (every app, `TUBESYNC_DEBUG=True`, final `DEBUG = False`): 665 tests OK.
- `ruff check` run as CI runs it: only the two known hits.
- New this sweep: a `media_format` whose episode NFO would land on the video file itself (a `.nfo` extension) is refused before anything moves, and a cached thumbnail that is a directory or FIFO is never copied. Each test was checked to fail with its fix reverted.

## Verification (2026-09-27, twenty-second review follow-up sweep)

- `manage.py test sync medianest_bridge`, same image and setup as above: 658 tests OK at the stack tip (T1-T3 unchanged: 389, 451 and 528). In CI's configuration (every app, `TUBESYNC_DEBUG=True`, final `DEBUG = False`): 666 tests OK.
- `ruff check` run as CI runs it: only the two known hits.
- New this sweep: with channel images on, a video recorded or renamed at a channel-image file name refuses the source in both modes before the save (neither this command nor TubeSync's signal queues the image job). The test was checked to fail with the check removed.

## Verification (2026-09-27, twenty-third review follow-up sweep)

- `manage.py test sync medianest_bridge`, same image and setup as above: 661 tests OK at the stack tip (T1-T3 unchanged: 389, 451 and 528). In CI's configuration (every app, `TUBESYNC_DEBUG=True`, final `DEBUG = False`): 669 tests OK.
- `ruff check` run as CI runs it: only the two known hits.
- New this sweep: one reserved-path check replaces the separate `tvshow.nfo`, NFO-on-video and channel-image checks and adds generated sidecars (episode NFO, thumbnail) across media and the episode thumbnail against channel-image names; downloads finishing during the run meet the same checks. Each test was checked to fail with its part of the check removed.

## Verification (2026-09-27, twenty-fourth review follow-up sweep)

- `manage.py test sync medianest_bridge`, same image and setup as above: 663 tests OK at the stack tip (T1-T3 unchanged: 389, 451 and 528). In CI's configuration (every app, `TUBESYNC_DEBUG=True`, final `DEBUG = False`): 671 tests OK.
- `ruff check` run as CI runs it: only the two known hits.
- New this sweep: a row recorded inside a sibling source's directory refuses the source, and an existing `.jpg` a rename would move onto a channel-image name counts even with thumbnail copying off. Each test was checked to fail with its fix reverted.

## Verification (2026-09-27, twenty-fifth review follow-up sweep)

- `manage.py test sync medianest_bridge`, same image and setup as above: 666 tests OK at the stack tip (on fork `main` after #16-#18 merged). In CI's configuration (every app, `TUBESYNC_DEBUG=True`, final `DEBUG = False`): 674 tests OK.
- `ruff check` run as CI runs it: only the two known hits.
- New this sweep: a concurrent edit to a field the preflight relied on stops the save; a would-be target inside another source's directory is refused; a symlinked cached thumbnail is never copied. Each test was checked to fail with its fix reverted.

## Verification (2026-09-27, twenty-sixth review follow-up sweep)

- `manage.py test sync medianest_bridge`, same image and setup as above: 668 tests OK at the stack tip. In CI's configuration (every app, `TUBESYNC_DEBUG=True`, final `DEBUG = False`): 676 tests OK.
- `ruff check` run as CI runs it: only the two known hits.
- New this sweep: another source's downloaded media recorded inside this source's tree refuses it, and a concurrent change to the source's `name` (which `{source}` renders from) stops the save. Each test was checked to fail with its fix reverted.

## Verification (2026-09-27, twenty-seventh review follow-up sweep)

- `manage.py test sync medianest_bridge`, same image and setup as above: 670 tests OK at the stack tip. In CI's configuration (every app, `TUBESYNC_DEBUG=True`, final `DEBUG = False`): 678 tests OK.
- `ruff check` run as CI runs it: only the two known hits.
- New this sweep: other sources' recorded media are matched by resolved path (an alias outside this tree resolving into it is caught), and media still to be downloaded are reserved against channel-image names. Each test was checked to fail with its fix reverted.

## Verification (2026-09-27, twenty-eighth review follow-up sweep)

- `manage.py test sync medianest_bridge`, same image and setup as above: 672 tests OK at the stack tip. In CI's configuration (every app, `TUBESYNC_DEBUG=True`, final `DEBUG = False`): 680 tests OK.
- `ruff check` run as CI runs it: only the two known hits.
- New this sweep: a row whose recorded video lies outside its own source's directory refuses the source, which closes the shared-third-directory and outside-tree-sidecar cases. The tests were checked to fail with the check removed.

## Verification (2026-09-27, twenty-ninth review follow-up sweep)

- `manage.py test sync medianest_bridge`, same image and setup as above: 673 tests OK at the stack tip. In CI's configuration (every app, `TUBESYNC_DEBUG=True`, final `DEBUG = False`): 681 tests OK.
- `ruff check` run as CI runs it: only the two known hits.
- New this sweep: media still to be downloaded meet every reserved-path check before the profile is saved (an NFO on the video itself included), not only the channel-image one. The test was checked to fail with the check removed.

## Verification (2026-09-27, thirtieth review follow-up sweep)

- `manage.py test sync medianest_bridge`, same image and setup as above: 675 tests OK at the stack tip. In CI's configuration (every app, `TUBESYNC_DEBUG=True`, final `DEBUG = False`): 683 tests OK.
- `ruff check` run as CI runs it: only the two known hits.
- New this sweep: a non-canonical target is refused before the rename, and two media (pending ones included) rendering to one video target refuse the source up front; two earlier tests that expected the per-media refusal of the second row now expect this source-level refusal. Each new test was checked to fail with its check removed.

## Verification (2026-09-27, thirty-first review follow-up sweep)

- `manage.py test sync medianest_bridge`, same image and setup as above: 676 tests OK at the stack tip. In CI's configuration (every app, `TUBESYNC_DEBUG=True`, final `DEBUG = False`): 684 tests OK.
- `ruff check` run as CI runs it: only the two known hits.
- New this sweep: skipped rows that are not downloaded meet the reserved-path checks as well. The late-download test now creates its row after the preflight, since a row pending at preflight time is checked there. The new test was checked to fail with the skip filter restored.

## Verification (2026-09-27, thirty-second review follow-up sweep)

- `manage.py test sync medianest_bridge`, same image and setup as above: 678 tests OK at the stack tip. In CI's configuration (every app, `TUBESYNC_DEBUG=True`, final `DEBUG = False`): 686 tests OK.
- `ruff check` run as CI runs it: only the two known hits.
- New this sweep: two rows recording one file, and a target whose directory cannot be created, each refuse the source before anything is saved or moved. Each test was checked to fail with its check removed.

## Verification (2026-09-27, thirty-third review follow-up sweep)

- `manage.py test sync medianest_bridge`, same image and setup as above: 680 tests OK at the stack tip. In CI's configuration (every app, `TUBESYNC_DEBUG=True`, final `DEBUG = False`): 688 tests OK.
- `ruff check` run as CI runs it: only the two known hits.
- New this sweep: a sidecar move landing on the renamed video is refused, and a dry-run models paths vacated by earlier renames, so it predicts the same renames apply makes. Each test was checked to fail with its fix reverted.

## Verification (2026-09-27, thirty-fourth review follow-up sweep)

- `manage.py test sync medianest_bridge`, same image and setup as above: 682 tests OK at the stack tip. In CI's configuration (every app, `TUBESYNC_DEBUG=True`, final `DEBUG = False`): 690 tests OK.
- `ruff check` run as CI runs it: only the two known hits.
- New this sweep: profile-level collisions are refused without any media, and an in-place video's sidecars count against channel-image names. The late-download test now uses a collision specific to the late row, since profile-wide ones are refused up front. Each test was checked to fail with its fix reverted.

## Verification (2026-09-27, thirty-fifth review follow-up sweep)

- `manage.py test sync medianest_bridge`, same image and setup as above: 683 tests OK at the stack tip. In CI's configuration (every app, `TUBESYNC_DEBUG=True`, final `DEBUG = False`): 691 tests OK.
- `ruff check` run as CI runs it: only the two known hits.
- New this sweep: the dry-run and cascade-preflight old-stem glob includes files earlier renames project into the directory (and excludes those they vacate), so a later media meets the same refusal apply gives. The test was checked to fail with the projection removed.

## Verification (2026-09-27, thirty-sixth review follow-up sweep)

- `manage.py test sync medianest_bridge`, same image and setup as above: 684 tests OK at the stack tip. In CI's configuration (every app, `TUBESYNC_DEBUG=True`, final `DEBUG = False`): 692 tests OK.
- `ruff check` run as CI runs it: only the two known hits.
- New this sweep: the destinations of existing sidecar moves are claimed across media whatever the options, so another media's generated NFO or thumbnail cannot land on one. Three earlier tests of per-media refusals in this class now expect the source-level refusal and still assert that nothing was overwritten. The new test was checked to fail with the claims removed.

## Verification (2026-09-27, thirty-seventh review follow-up sweep)

- `manage.py test sync medianest_bridge`, same image and setup as above: 686 tests OK at the stack tip. In CI's configuration (every app, `TUBESYNC_DEBUG=True`, final `DEBUG = False`): 694 tests OK.
- `ruff check` run as CI runs it: only the two known hits.
- New this sweep: a pending media's NFO path holding a foreign file refuses the source, and turning `write_nfo` or `copy_thumbnails` on counts a running download as in flight. Each test was checked to fail with its fix reverted.

## Verification (2026-09-27, thirty-eighth review follow-up sweep)

- `manage.py test sync medianest_bridge`, same image and setup as above: 688 tests OK at the stack tip. In CI's configuration (every app, `TUBESYNC_DEBUG=True`, final `DEBUG = False`): 696 tests OK.
- `ruff check` run as CI runs it: only the two known hits.
- New this sweep: a pending media's video target or thumbnail path already taken by a file refuses the source. Each test was checked to fail with its check removed.

## Verification (2026-09-27, thirty-ninth review follow-up sweep)

- `manage.py test sync medianest_bridge`, same image and setup as above: 691 tests OK at the stack tip. In CI's configuration (every app, `TUBESYNC_DEBUG=True`, final `DEBUG = False`): 699 tests OK.
- `ruff check` run as CI runs it: only the two known hits.
- New this sweep: a thumbnail rendered on the video itself is refused (per media, and for a `.jpg` profile even without media), and a source directory outside the download root is refused before any save. Each test was checked to fail with its check removed.

## Verification (2026-09-27, fortieth review follow-up sweep)

- `manage.py test sync medianest_bridge`, same image and setup as above: 693 tests OK at the stack tip. In CI's configuration (every app, `TUBESYNC_DEBUG=True`, final `DEBUG = False`): 701 tests OK.
- `ruff check` run as CI runs it: only the two known hits.
- New this sweep: the profile-level check reads TubeSync's rendered example of the format, so `{key}.{ext:.0}nfo` or `{key}.{ext:.0}jpg` is refused like a literal suffix. The tests were checked to fail against the raw template.

## Verification (2026-09-27, forty-first review follow-up sweep)

- `manage.py test sync medianest_bridge`, same image and setup as above: 694 tests OK at the stack tip. In CI's configuration (every app, `TUBESYNC_DEBUG=True`, final `DEBUG = False`): 702 tests OK.
- `ruff check` run as CI runs it: only the two known hits.
- New this sweep: with NFO writing, thumbnail copying or channel images on, a format whose last segment does not end in a fixed extension (`.{ext}` or a literal one) is refused, since media data could supply a sidecar or channel-image suffix. The bridge and TubeSync default formats pass. The test was checked to fail with the rule removed.

## Verification (2026-09-27, forty-second review follow-up sweep)

- `manage.py test sync medianest_bridge`, same image and setup as above: 698 tests OK at the stack tip. In CI's configuration (every app, `TUBESYNC_DEBUG=True`, final `DEBUG = False`): 706 tests OK.
- `ruff check` run as CI runs it: only the two known hits.
- New this sweep: a data-driven stem in the source directory that could become `tvshow` or a channel-image name is refused (a `{key}` stem is not), and a save that queues the channel image job waits for running downloads with the cascade off. Each test was checked to fail with its check removed.

## Verification (2026-09-27, forty-third review follow-up sweep)

- `manage.py test sync medianest_bridge`, same image and setup as above: 699 tests OK at the stack tip. In CI's configuration (every app, `TUBESYNC_DEBUG=True`, final `DEBUG = False`): 707 tests OK.
- `ruff check` run as CI runs it: only the two known hits.
- New this sweep: only a bare `{key}` counts as a fixed 11-character ID in the stem check; a key with a format spec is treated as data. The test was checked to fail with any key field treated as 11 characters.

## Verification (2026-09-27, forty-fourth review follow-up sweep)

- `manage.py test sync medianest_bridge`, same image and setup as above: 700 tests OK at the stack tip. In CI's configuration (every app, `TUBESYNC_DEBUG=True`, final `DEBUG = False`): 708 tests OK.
- `ruff check` run as CI runs it: only the two known hits.
- New this sweep: the stem check parses the format with Python's own `string.Formatter`, so nested fields such as `{title_full:.{video_order}}` count as data. The test was checked to fail with the earlier regex tokenizer.

## Verification (2026-09-28, forty-fifth review follow-up sweep)

- `manage.py test sync medianest_bridge`, same image and setup as above: 703 tests OK at the stack tip. In CI's configuration: 711 tests OK.
- `ruff check` run as CI runs it: only the two known hits.
- New this sweep, each test checked to fail without its fix:
  - A dry-run's `{key}` sweep now includes the files earlier renames project into the source tree and leaves out the ones they move away, as the old-stem glob already did. A later media whose key appears in an earlier media's new name is refused in both modes.
  - A directory segment made only of fields (and dots) can render empty, so the profile check treats such a format as placing files in the source directory. `{uploader}/poster.jpg` with channel images on is refused.

## Verification (2026-09-28, forty-sixth review follow-up sweep)

- `manage.py test sync medianest_bridge`, same image and setup as above: 706 tests OK at the stack tip. In CI's configuration: 714 tests OK.
- `ruff check` run as CI runs it: only the two known hits.
- New this sweep, each test checked to fail without its fix:
  - Any directory segment that holds a field counts as able to reach the source directory, because media data can render it empty, `.` or `..` (`title_full` keeps dots). `fixed/{title_full}/poster.jpg` with channel images on is refused.
  - `uploader` and `playlist_title` are not cleaned and can hold `/`. In the file name they free the whole name, so `fixed/s{uploader}.jpg` counts as able to name a video after a channel image.
  - Literal `.` and `..` segments in a stored (unvalidated) format are resolved the way the path would resolve them.
