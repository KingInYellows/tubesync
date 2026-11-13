# Release notes: `bridge-v1.4.0`

This release lets MediaNest remove a YouTube source (MED-657, MediaNest
ADR-0022 phase 2a): bridge contract 1.4.0 adds a route that removes a
source from TubeSync, keeping or deleting its files, and a route that
deletes the folder of a source already removed with files kept.

Tagging and image publication are human-gated actions for the repository
owner. This file records what the tag would contain and what was verified.

## What ships

1. **`DELETE /sources/{uuid}?deleteFiles=<bool>`** (#69). Runs TubeSync's
   own source delete (the logic behind its UI's delete button, moved into
   `sync/source_delete.py` so both share it). Answers `202` with
   `{"uuid", "canonicalKey", "directory", "deleteFiles",
   "normalizedState": "deleting"}`; a repeat call while the deletion runs
   returns the same answer and does nothing new.
   - Files are kept by default, even on a source whose TubeSync setting
     "delete files on disk" is on. A keep-files removal also leaves the
     folder where it is: TubeSync's own rename would have moved every file
     into `<folder>/deleted`.
   - `deleteFiles=true` removes the whole download folder once TubeSync's
     background delete task runs.
2. **A source being deleted is reported as such.** Until TubeSync's task
   finishes, `GET /sources/{uuid}` returns the source with
   `normalizedState: "deleting"`, then `404`. `GET /sources?key=` no
   longer returns it, and `POST /sources` for its key answers
   `409 SOURCE_DELETING` (retryable) instead of offering the dying row for
   adoption. This also applies to a source deleted in TubeSync's own UI.
3. **`DELETE /folders/{sourceKey}`** deletes `acq-src-<key>` (the name
   MediaNest gives every source it creates, and nothing else) under
   `video/` and `audio/`. It refuses while any source still uses the key or
   folder (by key, by directory, or by resolved path, so `./acq-src-x` or
   a subfolder counts), and refuses a symlinked folder or one resolving
   outside the download root; `404 FOLDER_NOT_FOUND` means nothing is on
   disk. The folder is first renamed into a hidden `.medianest-trash/`
   next to it, so a Plex scan right after sees it gone, and its contents
   are removed in the background. The check runs again after the rename:
   if a source for that folder was created in between, the folder is
   moved back and the call answers `409` (if the new source already wrote
   into a recreated folder, the old contents are kept in
   `.medianest-trash/kept-...` for a person to merge; no sweep removes
   them).
4. **Capabilities.** `GET /capabilities` reports `deleteSource: true` and
   the new `deleteSourceFiles: true`. MediaNest offers each action only
   when its flag is reported, so older MediaNest builds are unaffected.
5. **`BRIDGE_VERSION` is `1.4.0`**, so `GET /meta` and MediaNest's
   Connections tab show "bridge 1.4.0".

Both routes are writes: `MEDIANEST_BRIDGE_READ_ONLY=true` blocks them, and
every call writes a `mutation_audit` log line.

## Contract

`1.4.0`, from MediaNest #3161 (vendored byte-for-byte). All additions are
optional: new paths, the `SourceDeletion` and `FolderDeletion` schemas,
`Error.code` `SOURCE_DELETING` and `FOLDER_NOT_FOUND`,
`Source.normalizedState` `deleting`, and `Capabilities.deleteSourceFiles`.
No required field changed. MediaNest before MED-658 maps `deleting` to
`unknown` and treats a `409` without `existingSourceUuid` as an unknown
create outcome that its reconciler settles.

## Before tagging

- Merge MediaNest #3161 first and update the vendored contract header and
  the README's "Contract" paragraph to its squash-merge SHA.
- Tag `bridge-v1.4.0` on the `main` commit that includes #69 and this
  release prep. That triggers `medianest-bridge-release.yaml` to publish
  `ghcr.io/kinginyellows/tubesync:bridge-v1.4.0`.
- A tag push does not move `bridge-stable`; after the release image is
  published, run "MediaNest Bridge Image Refresh" by hand with `force`
  checked.

## Upgrade and rollback

No migrations and no new environment variables. Rolling back to
`bridge-v1.3.1` needs no database step; MediaNest then sees
`deleteSource: false` and hides the remove actions. A deletion already in
progress finishes under either image, since TubeSync's own task does the
work. Leftover `.medianest-trash/` entries from an interrupted folder
delete are swept by the next folder delete call, including a retry of the
same key that answers `404`, or can be removed by hand. The sweep only
touches entries named like its own (`acq-src-<key>-<32 hex>`); a move
stays `pending-...` until its claim check passes, so a concurrent sweep
cannot remove a folder that may still go back; one left by a crash is moved
back to its folder after 10 minutes, never deleted.

## Verified

- Full Django suite on Python 3.13: 864 tests OK, including 37 new tests
  in `tests/test_delete_sources.py` that run TubeSync's real
  `delete_all_media_for_source` in-process: files and folder survive a
  keep-files delete on a source with "delete files on disk" on, the folder
  goes with `deleteFiles=true`, the `deleting` state and `409
  SOURCE_DELETING` hold until the task ends, and the folder route deletes
  only the named folder, refuses a symlink, a folder outside the download
  root and a folder a source names another way, gives a source created
  during the move its folder back, and sweeps only its own leftovers. A
  92-character key fits TubeSync's temporary rename.
- CI's ruff flags, `manage.py check` and `makemigrations --check` clean.

## Known limits

- Not yet run against the live dockerdownload host. Check a keep-files
  removal of a test source there before relying on it.
- A user-made TubeSync source that never indexes or downloads and whose
  filter text is the uuid of a deleted source would be read as "being
  deleted". TubeSync's own delete is the only thing that makes such a row.
