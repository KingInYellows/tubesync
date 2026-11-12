# Release notes: `bridge-v1.3.1`

This release stops the TubeSync log viewer's database from growing without
bound, makes "database is locked" errors during the nightly crawl less
likely (MED-636), and keeps the `bridge-stable` channel tag on the `bridge-v1` line
now that MediaNest's install template defaults to it (MED-647).

Tagging and image publication are human-gated actions for the repository
owner. This file records what the tag would contain and what was verified.

## What ships

1. **`syslog.db` size cap** (MED-655, #55). The `hat-syslog-server` s6
   service ran with the library defaults (10M rows high, 1M low) and
   archiving on, so `/config/state/hat/syslog.db` passed 1.7 GB in two
   months on the homelab and then wrote archive files beside it.
   - The database now trims its oldest rows at
     `TUBESYNC_SYSLOG_DB_HIGH_SIZE` rows (default 1,000,000, about 360 MB)
     back to the newest `TUBESYNC_SYSLOG_DB_LOW_SIZE` (default 250,000).
   - Archive files are off unless `TUBESYNC_SYSLOG_DB_ARCHIVE=true`.
   - Invalid values log a warning at start-up and use the defaults.
     A low size at or above the high size also warns, and uses a quarter
     of the high size (rounded down, at least 1).
   - Fork point 14 in the README's "Fork delta"; settings in its
     environment variable table.
2. **`bridge-stable` stays on `bridge-v1`** (MED-647). The scheduled
   refresh (`medianest-bridge-refresh.yaml`) rebuilt the newest
   `bridge-vX.Y.Z` tag of any major and moved `bridge-stable` to it. It
   now only considers `bridge-v1.Y.Z` tags, so a future `bridge-v2` that
   changes the contract can never reach installs that follow
   `bridge-stable`. A `bridge-v2` line would get its own channel tag.
3. **Longer SQLite lock wait, fewer writers** (MED-636, #67). With WAL on,
   the 2026-11-11 crawl still logged 107 "database is locked" errors in ten
   minutes, because Django's SQLite timeout was a fixed 10 s.
   - New `TUBESYNC_SQLITE_TIMEOUT` (default 30 s, was a fixed 10 s;
     whole numbers, clamped to 5-80 s). Start-up logs
     "SQLite lock timeout: <n> s". Ignored when `DATABASE_CONNECTION` is set.
   - On SQLite the `database` huey queue runs 1 process worker instead of
     2 threads, so fewer writers compete for the lock.
   - No compose change is needed for the new defaults.
4. **`BRIDGE_VERSION` is `1.3.1`**, so `GET /meta` and MediaNest's
   Connections tab show "bridge 1.3.1".

## Contract

`1.3.0`, unchanged from `bridge-v1.3.0`. No path, request or response
changed.

## Before tagging

- Tag the release-prep merge commit `bridge-v1.3.1`. That triggers
  `medianest-bridge-release.yaml` to publish
  `ghcr.io/kinginyellows/tubesync:bridge-v1.3.1`.
- A tag push does not move `bridge-stable`; only the refresh does. After
  the release image is published, run "MediaNest Bridge Image Refresh"
  by hand with `force` checked, so `bridge-stable` points at a
  `bridge-v1.3.1` build at once instead of after the next new yt-dlp
  release or Monday rebuild.

## Upgrade and rollback

No migrations. The four new variables are optional; unset gives the
defaults above. `TUBESYNC_SQLITE_JOURNAL_MODE=WAL` from
`bridge-v1.2.3` carries over unchanged.

An existing large `syslog.db` stops growing but keeps its size, because
SQLite reuses the space of deleted rows instead of returning it. To
shrink it, follow "Shrinking an existing syslog.db" in the README: run the
new image long enough for one trim, stop the container and `VACUUM` the
file (needs free disk up to twice its size), or with the container
stopped move the file away and let the service create a new one.

Rolling back to `bridge-v1.3.0` needs no database step. The old image
runs with the old defaults again, so the file grows as before and the
lock wait goes back to 10 s.

## Verified

- `bash -n` on the s6 `run` script, and its bounds checks run on `main`
  at `12d87db`: unset gives 1,000,000 / 250,000 with archiving off;
  `abc`, `0` and a 13-digit high size each warn and use 1,000,000; a low
  size of 200 against a high size of 100 warns and uses 25; `yes` turns
  archiving on; `maybe` warns and keeps it off.
- The refresh workflow's tag selection, run by hand against
  `bridge-v1.2.3`, `bridge-v1.3.0`, `bridge-v1.3.1`, `bridge-v2.0.0` and
  `bridge-v1.3.1-yt2026.11.3`, picks `bridge-v1.3.1`.

## Known limits

- The cap counts rows, not bytes. Very long log lines make the file
  larger than the 360 MB estimate.
- The scheduled refresh failed on 2026-11-09 (it rebuilt
  `bridge-v1.2.2` code, which predates the Debian 13.7 build fix in #36)
  and on 2026-11-10 (a TLS handshake timeout fetching
  `https://openresty.org/package/pubkey.gpg` from the hosted runner). So
  `bridge-stable` may still point at an older build until the next
  successful refresh; the hand-run refresh above fixes that.
