# Release notes: `bridge-v1.2.3`

This release lets TubeSync's SQLite database run in WAL mode, so the
bridge's reads stop waiting behind TubeSync's writes during busy indexing.

Tagging and image publication are human-gated actions for the repository
owner. This file records what the tag would contain and what was verified.

## What ships

1. **Opt-in SQLite WAL** (MED-636, #33). TubeSync's database used a
   rollback journal, where one writer blocks every reader. During index
   crawls and catch-up, bridge reads waited the full 10 s lock timeout,
   which is also MediaNest's request timeout. On the homelab that showed
   up as 204 "database is locked" lines in 72 h, all in bursts.
   `TUBESYNC_SQLITE_JOURNAL_MODE` now picks `TRUNCATE` (the default,
   unchanged from upstream), `DELETE` or `WAL`. With `WAL`:
   - `synchronous = NORMAL` (SQLite's recommended pairing; a power loss
     can roll back the last few commits, never corrupt the file).
   - The per-connection `auto_vacuum = INCREMENTAL` and
     `incremental_vacuum(100)` are skipped. Both need the write lock, so
     they would make every new connection wait behind the writer again.
     `auto_vacuum` is stored in the file; freed pages are still reused.
   - An unsupported value logs a warning and keeps `TRUNCATE`. Start-up
     logs `Requested SQLite journal mode: <mode>`.
   Fork touch point 13 (`local_settings.py.container` + `common/utils.py`).
2. **`BRIDGE_VERSION` is `1.2.3`**, so `GET /meta` and MediaNest's
   Connections tab show "bridge 1.2.3".

## Contract

Unchanged: contract `1.2.0`.

## Before tagging

- Tag the release-prep merge commit `bridge-v1.2.3`. That triggers
  `medianest-bridge-release.yaml` to publish
  `ghcr.io/kinginyellows/tubesync:bridge-v1.2.3`, and the next refresh run
  builds from it.

## Deploying with WAL

Follow the README's "Switching the SQLite journal mode" runbook:

1. Check that no persisted `/config/tubesync/local_settings.py` exists
   (it would replace the image's settings file and hide the variable),
   and that `/config` is on a local filesystem, not NFS or SMB.
2. Stop TubeSync and back up `db.sqlite3` and the env file.
3. `sqlite3 db.sqlite3 'PRAGMA journal_mode=WAL;'` prints `wal`.
4. Set `TUBESYNC_SQLITE_JOURNAL_MODE=WAL`, point the image at
   `bridge-v1.2.3` and start the container. Confirm the log line and
   `PRAGMA journal_mode;` on the file.

Without the variable, `bridge-v1.2.3` behaves exactly like `bridge-v1.2.2`.

## Upgrade and rollback

No migrations. Before rolling back to an image older than `bridge-v1.2.3`
on a WAL database, stop the container and run
`sqlite3 db.sqlite3 'PRAGMA journal_mode=DELETE;'`: older images force
`TRUNCATE` on every connection, and switching back offline also folds the
`-wal` file into the main file.

## Known limits

- The journal-mode result is not read back per connection; the start-up
  line records what was requested. Verify the file with
  `PRAGMA journal_mode;`.
- The dashboard's database size counts only `db.sqlite3`, not
  `db.sqlite3-wal`. `journal_size_limit` (64 MiB) is not a hard cap: it
  trims the WAL file only after a checkpoint resets it. A long-running
  reader that holds back checkpoints lets the WAL grow past 64 MiB while
  writes continue, so check `ls -l /config/db.sqlite3*` for real disk use.
- If lock bursts continue under WAL, the fallback is Postgres through
  TubeSync's `DATABASE_CONNECTION` (MED-636), which needs its own plan.
