# Release notes: `bridge-v1.2.0`

This release lets MediaNest accept the YouTube channel links people usually
copy: `youtube.com/@handle`, `/c/NAME` and `/user/NAME`, each with an optional
tab such as `/videos`. Until now only `/channel/UC…` links worked. The bridge
resolves the link to its stable `UC…` channel ID, so the lookup stays on
TubeSync's YouTube egress behind its VPN (ADR-0006 §2).

Tagging and image publication are human-gated actions for the repository
owner. This file records what the tag would contain and what was verified.

## What ships

1. **`POST /api/medianest/v1/sources/resolve`** (#27, new
   `medianest_bridge/channel_resolve.py`, no upstream edits).
   - Accepts https URLs only, with no credentials or port, on `youtube.com`,
     `www.youtube.com` or `m.youtube.com`, in the `/@handle`, `/c/NAME` or
     `/user/NAME` shape.
   - Rebuilds the lookup URL itself and runs one yt-dlp `extract_flat`
     lookup with the options `sync/youtube.py:get_channel_id` uses, plus
     `socket_timeout=10`. `get_channel_id` is unchanged.
   - Returns the `UC…` channel ID, its `/channel/` URL and the channel title.
   - Errors: 400 `SOURCE_INVALID` for a bad shape or a non-`UC…` result,
     404 `SOURCE_NOT_FOUND` when YouTube reports no such channel, and 503
     `PROVIDER_UNAVAILABLE` (retryable) for any other yt-dlp `DownloadError`.
     An unexpected exception falls through to `BridgeView`'s 500
     `INTERNAL_PROVIDER_ERROR`.
   - yt-dlp output is logged sanitized and capped at 500 characters, and no
     response ever carries it.
   - The view persists nothing, so it stays available in read-only mode.
     Auth, CIDR and body-size gating come from `BridgeView` as usual.
2. **`GET /capabilities` reports `resolveChannelHandle: true`.** MediaNest
   calls the new endpoint only when it sees this flag. Older bridges omit it
   and MediaNest keeps rejecting handle URLs with a pointer to the
   `/channel/UC…` form.
3. **`BRIDGE_VERSION` is `1.2.0`**, so `GET /meta` and MediaNest's
   Connections tab show "bridge 1.2.0".

## Contract

Contract `1.1.0`, additive only: `POST /sources/resolve` (`resolveSource`),
the `ResolveSourceRequest` and `ResolvedSource` schemas, and the optional
`Capabilities.resolveChannelHandle` (default `false`, not in `required`). No
existing path, schema or response changed. The vendored copy was re-synced to
MediaNest `03aae130702e1a1cc36e9839f3d807c2bc3a28b5`, the squash merge of
#2783 to `main`. Its body is byte-identical to the branch copy first vendored
in #27; the header now names the merge commit and `source_sha256` is re-locked.

## Before tagging

- ~~Merge the canonical contract PR in MediaNest, re-sync the vendored header
  SHA and re-lock `source_sha256`.~~ Done 2026-10-19: #2783 merged as
  `03aae1307`.
- ~~Bump `medianest_bridge/config.py::BRIDGE_VERSION` to `1.2.0`.~~ Done in
  the release-prep PR that this tag sits on.
- Tag the release-prep merge commit `bridge-v1.2.0`. That triggers
  `medianest-bridge-release.yaml` to publish
  `ghcr.io/kinginyellows/tubesync:bridge-v1.2.0`.
- Point MediaNest's `TUBESYNC_IMAGE_TAG` at `bridge-v1.2.0` and redeploy
  TubeSync.

## Upgrade and rollback

No migrations and no new settings. Rolling back to `bridge-v1.1.0` only
removes handle-URL support: MediaNest sees the capability disappear and goes
back to rejecting those links. Sources already created from handle URLs are
stored by their `UC…` channel ID and keep working.

## Known limits

- Each resolve is one live YouTube lookup. MediaNest applies its own
  per-user create rate limit before calling it.
- Handle URLs with other paths (`/@name/shorts/ID`, `/@name/community`) are
  rejected rather than guessed at.
