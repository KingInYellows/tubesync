# Release notes: `bridge-v1.6.1`

This release tells MediaNest when each YouTube source next looks for new
videos, so the source page can show "Next check" beside the schedule
picker (MED-816). It adds one read-only field; nothing else changes.

Tagging and image publication are human-gated actions for the repository
owner. This file records what the tag would contain and what was verified.

## What ships

1. **`Source.nextCheckAt`** on every source read (`next_check.py`). It is
   the soonest index already queued for the source (sync-now, the first
   index after create, or the one TubeSync queues when the schedule
   changes), or else the hourly `schedule_indexing()` sweep that will queue
   one, worked out from `target_schedule`, `last_crawl` and the schedule.
   The sweep time is an estimate to the minute; a queued index is exact.
   It is null for a paused or inactive source, a video list, and while an
   index runs.
2. **`BRIDGE_VERSION` is `1.6.1`**, so `GET /meta` shows "bridge 1.6.1".

## Contract

`1.6.1`. Additive: `nextCheckAt` is optional and nullable, and not in
`Source.required`. MediaNest releases that speak 1.6.0 ignore it.

## Known limits

- A queued index that TubeSync revoked as a duplicate keeps a row that
  looks pending. Only rows recorded after the last crawl count, which
  rules those out in the usual case. One that slips through can only make
  the time early, never late, and the next index clears it.
- An index waiting for a free worker can report a time a few minutes in
  the past.
- Each source read runs one more small `TaskHistory` query.

## Before tagging

- Done: MediaNest #3615 merged contract 1.6.1 as `61db623`. The vendored
  contract header points at that commit (the body is byte-identical), and
  `source_sha256` in `contract/contract_fixtures.json` matches.
- Tag `bridge-v1.6.1` on the `main` commit that merges this PR, then run
  "MediaNest Bridge Image Refresh" by hand with `force` checked, as for
  1.6.0.

## Upgrade and rollback

No migrations and no new settings. Recreate the container on the new
image. Rolling back to `bridge-v1.6.0` only hides the next-check time.
