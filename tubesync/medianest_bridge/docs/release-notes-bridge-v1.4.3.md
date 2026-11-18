# Release notes: `bridge-v1.4.3`

This release stops huey's task-history writes and `save_media` tasks
failing with "database is locked" during the nightly crawl (MED-695).

Tagging and image publication are human-gated actions for the repository
owner. This file records what the tag would contain and what was verified.

## What ships

1. **Renaming a source's files no longer holds the write lock for the
   whole run** (MED-695, #75 and #76). TubeSync opens every transaction
   with `BEGIN IMMEDIATE`, so `rename_all_media_for_source`, which ran
   inside one transaction, held SQLite's write lock while it walked every
   downloaded item and checked its files on the NAS. Any other writer
   waiting longer than the 30 s lock timeout failed. That task is queued
   right after the `save_media` burst at the end of each source's crawl,
   which is where the remaining lock errors clustered after
   `bridge-v1.4.2`.
   - The task now reads the item keys once, then renames each item in its
     own short transaction. It re-reads each item first, so an item a user
     changed mid-run (for example skipped) is passed over.
   - An item locked by another task is handed to `rename_media` to retry
     later, as before.
2. **`save_media` no longer sleeps inside its transaction** (MED-695).
   On SQLite each save is followed by a short random back-off. It used to
   run while the transaction, and so the write lock, was still open. It
   now runs after the commit. The lookup and the save stay in one
   transaction so no other writer's change is overwritten.
3. **A locked task-history write is logged, not raised** (MED-695).
   huey's `historical_task` signal receiver writes the dashboard's task
   history row. If SQLite still reports the database locked (and the
   receiver is not inside a caller's transaction), it logs a warning that
   keeps the "database is locked" text and lets the task carry on. That
   history update is skipped, not replayed. Other errors, and other
   databases, still raise.
4. **Build fix from MED-668** (#74): asfald's GitHub API lookups during the
   image build use the `github_token` BuildKit secret, so builds stop
   failing on GitHub's anonymous rate limit.
5. **`BRIDGE_VERSION` is `1.4.3`**, so `GET /meta` and MediaNest's
   Connections tab show "bridge 1.4.3".

## Contract

`1.4.0`, unchanged from `bridge-v1.4.0`. No path, request or response
changed.

## Before tagging

- Tag `bridge-v1.4.3` on the `main` commit that merges this release-prep
  PR. That triggers `medianest-bridge-release.yaml` to publish
  `ghcr.io/kinginyellows/tubesync:bridge-v1.4.3`. The workflow can also
  be run by hand (`workflow_dispatch`) with the tag name on that commit.
- A tag push does not move `bridge-stable`. After the release image is
  published, run "MediaNest Bridge Image Refresh" by hand with `force`
  checked.

## Upgrade and rollback

No migrations and no new settings. Recreate the container on the new
image. Rolling back to `bridge-v1.4.2` needs no database step.

## Verified

- `manage.py test`: 888 tests pass, including the new
  `sync/tests/test_task_history_locks.py`: the history row is written; a
  locked database is skipped with a warning; other errors, MySQL's
  "Account is locked." and a lock inside a caller's transaction still
  raise; each rename runs in its own transaction; an item that stops
  qualifying mid-run is passed over; `save_media` sleeps outside its
  transaction and looks up and saves inside one.
- GitHub CI on `main` at `a7e81ec` (test 3.12, 3.13, 3.14 and the image
  build) passed.

## Known limits

- Whether the lock errors have stopped is checked on the first 02:00Z
  crawl after deploy, by counting "database is locked" lines in the
  TubeSync log and MediaNest provider timeouts.
