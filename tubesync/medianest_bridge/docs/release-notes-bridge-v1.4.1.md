# Release notes: `bridge-v1.4.1`

This release stops read-only database work from taking SQLite's write
lock, the remaining cause of the nightly "database is locked" bursts and
MediaNest bridge timeouts (MED-636).

Tagging and image publication are human-gated actions for the repository
owner. This file records what the tag would contain and what was verified.

## What ships

1. **Reads no longer take the write lock** (MED-636, #71). TubeSync's
   SQLite settings use `transaction_mode: IMMEDIATE`, so every outermost
   `transaction.atomic()` starts with `BEGIN IMMEDIATE`, which takes the
   write lock even when the block only reads. With `bridge-v1.3.1` and
   `bridge-v1.4.0` the 2026-11-13 and 2026-11-14 crawls still logged about
   26 and 22 lock errors and 14 and 17 MediaNest provider timeouts. Every
   bridge request that took 25 s or more was `GET /sources/{uuid}`, which
   resolves the show title inside such a block and so queued for the
   write lock for up to 30 s, past MediaNest's 10 s timeout.
   - New `common.transactions.read_atomic()`: on SQLite outside a
     transaction it opens no transaction, so the read runs in autocommit
     and, under WAL, never waits for a writer. Inside an existing
     transaction, or on another database, it behaves like
     `transaction.atomic()`.
   - Used by `resolve_show_title` (the bridge's `GET /sources/{uuid}`),
     the `tvshow.nfo` content build, and the dashboard's task list
     (`thqs_from_huey_ids`), which also returned 500s during the crawl.
   - Writes are unchanged: they still take the lock with `BEGIN IMMEDIATE`
     and wait up to `TUBESYNC_SQLITE_TIMEOUT` (default 30 s).
2. **`BRIDGE_VERSION` is `1.4.1`**, so `GET /meta` and MediaNest's
   Connections tab show "bridge 1.4.1".

## Contract

`1.4.0`, unchanged from `bridge-v1.4.0`. No path, request or response
changed.

## Before tagging

- Tag `bridge-v1.4.1` on the `main` commit that merges this release-prep
  PR (it includes #71, merged as `28f5221`). That triggers
  `medianest-bridge-release.yaml` to publish
  `ghcr.io/kinginyellows/tubesync:bridge-v1.4.1`.
- A tag push does not move `bridge-stable`; only the refresh does. After
  the release image is published, run "MediaNest Bridge Image Refresh"
  by hand with `force` checked, so `bridge-stable` points at a
  `bridge-v1.4.1` build at once.

## Upgrade and rollback

No migrations and no new settings. `TUBESYNC_SQLITE_JOURNAL_MODE=WAL` and
`TUBESYNC_SQLITE_TIMEOUT` carry over unchanged. Without WAL the change is
harmless but helps less, because a rollback-journal read still waits for
a writer's commit.

Rolling back to `bridge-v1.4.0` needs no database step; reads take the
write lock again.

## Verified

- `manage.py test` with the four new `ReadAtomicTestCase` tests in
  `common/tests.py`: `read_atomic()` opens no transaction on SQLite
  outside a transaction, joins an open one, and uses `atomic()` on other
  databases. A raw WAL test shows an autocommit read succeeding while
  another connection holds `BEGIN IMMEDIATE`, and a second
  `BEGIN IMMEDIATE` timing out.
- CI on #71 green; Codex code and security reviews had no findings.

## Known limits

- Only the read paths seen in the 2026-11-14 crawl logs moved to
  `read_atomic()`. The other lock errors that night came from huey task
  history saves and the download progress hook, which are real writes
  and still wait for the lock (up to 30 s, without failing a MediaNest
  request). Whether the crawl now stays at 5 or fewer lock errors is
  checked over the next two nightly crawls after deploy.
