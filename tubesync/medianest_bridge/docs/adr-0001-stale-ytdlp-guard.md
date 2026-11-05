# ADR-0001: Warn instead of pausing downloads when yt-dlp is stale

- Status: Accepted (2026-11-05)
- Tickets: MED-623, MED-624
- Contract: bridge-openapi.v1.yaml 1.2.0 (MediaNest PR #3082)

## Context

Upstream TubeSync's Docker HEALTHCHECK (`healthcheck.py`) looks up the
newest yt-dlp release every 20 minutes to 12 hours. As soon as the
bundled yt-dlp is older, it runs `manage.py stop-queue huey-net-limited`,
which stops the download consumer with `s6-svc -D`. Downloads stay
stopped until someone rebuilds the image and recreates the container.

On the homelab this happened on 2026-11-03: a yt-dlp release came out,
the guard stopped `huey-net-limited`, and nothing downloaded until the
image was rebuilt. The old yt-dlp was still working at that point; a new
release does not mean the old one broke.

The bridge's `/health/ready` did not show the problem either (MED-624).
`workers` read each consumer's PID with `s6-svstat -o pid`, which prints
`-1` for a stopped service. The check counted any truthy PID as running,
so it reported 4/4 consumers while the download consumer was stopped.

## Decision

1. **Warn by default.** The fork's `healthcheck.py` no longer stops the
   download queue when yt-dlp is stale. It prints one warning per
   container instance to the health log and records the state in
   `/dev/shm/.healthcheck/yt-dlp-release.json`.
   `TUBESYNC_STALE_YTDLP_ACTION=pause` restores upstream's behavior for
   anyone who prefers a hard stop.
2. **Report it.** `/health/ready` shows the state with two new optional,
   nullable `ComponentStatus` fields (contract 1.2.0):
   - `ytDlp`: `degraded`, `reason: "stale_ytdlp"`, `version` (installed)
     and `latestVersion`. Overall status becomes `degraded`, not
     `unavailable`.
   - `queues`: `reason: "queue_paused"` for a paused consumer, or
     `"stale_ytdlp"` when the opt-in pause mode did it.
   - `workers`: only a PID above 0 counts as running; anything else is
     down with `reason: "worker_down"`.
   MediaNest shows these to the admin (MediaNest PR #3082).
3. **Refresh the image.** `.github/workflows/medianest-bridge-refresh.yaml`
   rebuilds the newest `bridge-vX.Y.Z` release tag:
   - daily, when a yt-dlp release has no image yet:
     `bridge-vX.Y.Z-yt<yt-dlp>`;
   - every Monday regardless, for base-image and FFmpeg fixes:
     `bridge-vX.Y.Z-yt<yt-dlp>-r<YYYYMMDD>`.
   Each of those tags is immutable. The refresh also moves one channel
   tag, `bridge-stable`, to the new image.
4. Version comparison is numeric on the dotted date, so a nightly yt-dlp
   ahead of the newest stable release is not called stale.

## Consequences

- Downloads keep running on an older yt-dlp. If YouTube has really broken
  that version, individual downloads fail and retry (TubeSync's normal
  task retry) instead of the whole queue stopping. Health and the
  MediaNest admin page say why, so the operator still knows to update.
- A deployment pinned to `bridge-stable` gets the new yt-dlp with
  `docker compose pull tubesync && docker compose up -d --no-deps
  tubesync`. A deployment pinned to an exact tag (the homelab today)
  keeps it until the operator changes the tag; the refreshed tag names
  say which yt-dlp they carry.
- `healthcheck.py` is now a fork touch point (README "Fork delta" item
  11). An upstream sync must keep the warn/pause switch and the state
  file.
- Until `bridge-v1.2.2` is tagged, the refresh rebuilds `bridge-v1.2.0`
  code, which still pauses. Tag `bridge-v1.2.2` first.

## Alternatives considered

- **Keep pausing, alert harder.** Rejected: a release is not a breakage,
  and stopping every download for it is the bigger harm.
- **Upgrade yt-dlp inside the running container (`pip install -U`).**
  Rejected: unpinned code changes in a running container, lost on
  recreate, and the image would no longer match its tag.
- **One moving tag rebuilt in place.** Rejected: the release workflow's
  immutable-tag rule exists so rollback can rely on a tag's contents.
  The single `bridge-stable` channel tag is the only exception.
