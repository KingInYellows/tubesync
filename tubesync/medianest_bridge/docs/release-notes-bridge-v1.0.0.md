# Release notes — `bridge-v1.0.0` (proposed, not yet tagged)

First release of the `medianest_bridge` app inside the `KingInYellows/tubesync`
fork. Prepared by the MediaNest × TubeSync release-readiness session on
2026-09-05 against fork `main` `c802ff5e1efd49640bc32c1c460455041476e57a`
(merge of fork PR #8, T2–T6). Tagging, and any image publication, remain
human-gated actions for the repository owner; this file records what the tag
would contain and what was verified.

## What ships

- `medianest_bridge` Django app: diagnostics (`/health/live`, `/health/ready`,
  `/meta`, `/capabilities`), read endpoints (`GET /sources?key=`,
  `GET /sources/{uuid}`, `GET /sources/{uuid}/media`), and the three write
  endpoints (`POST /sources/validate`, `POST /sources`,
  `POST /sources/{uuid}/sync`) — the complete v1 contract surface, mounted at
  `/api/medianest/v1/`. Bearer-token auth with constant-time compare, CIDR
  allow-list, read-only mode, body-size limit, RFC 7807 error envelopes with
  redaction, structured request/audit logging, huey-aware sync dedup.
- Fork delta outside the app: five upstream touch points (settings
  `INSTALLED_APPS` + `BASICAUTH_PREFIX_ALLOW_URIS`, `urls.py` include,
  `common/middleware.py` prefix-match exemption, `Dockerfile`
  `ARG MEDIANEST_BRIDGE_UPSTREAM_SHA`), plus the fork-owned publish workflow
  `.github/workflows/medianest-bridge-release.yaml` and a `bridge-v*` guard in
  the inherited upstream `release.yaml` (job-level `if`, line 66).
- Zero fork-added migrations (`makemigrations --check` clean).

## Pins

| Item | Value | Source of truth |
| --- | --- | --- |
| Bridge version (`GET /meta` → `bridgeVersion`) | `1.0.0` | `medianest_bridge/config.py::BRIDGE_VERSION` |
| Contract | `bridge-openapi.v1.yaml`, `info.version 1.0.0` | vendored copy `medianest_bridge/contract/`; canonical `KingInYellows/medianest` `docs/planning/tubesync-integration/bridge-openapi.v1.yaml`. The canonical copy was re-synced to this vendored copy's additive 403/413/503 diagnostics responses during release readiness (MediaNest DECISIONS #34); the vendored file itself is unchanged for this tag. |
| Upstream base (`GET /meta` → `upstreamCommit`) | `3b9d72f28dda9c931776f76e7428a72a24a57f82` | `medianest_bridge/docs/UPSTREAM_SHA` (= `merge-base origin/main upstream/main`); `GET /meta` → `tubesyncVersion` reports `0.18.3` |
| Upstream drift at tag time | 85 commits behind `meeb/tubesync` `255b5b754` (v0.18.4, 2026-09-04) | Rehearsal merge into a throwaway worktree: zero conflicts, full suite 331/331 on the merge result. **Decision: v1 stays pinned; re-pin is the first post-release PR** (`docs/upstream-sync.md` process, single commit updating `UPSTREAM_SHA`, migration-upgrade rehearsal). |
| MediaNest minimum | `v1.0.0-rc2` (the MediaNest release carrying M0–M7; proposed) | MediaNest `CHANGELOG.md` / release PR |
| yt-dlp / FFmpeg | **Not pinned at source; resolved at image build time** by the workflow's `info` job (latest `yt-dlp/yt-dlp` release → `YTDLP_DATE`; latest dated `yt-dlp/FFmpeg-Builds` autobuild → `FFMPEG_DATE`/`FFMPEG_VERSION`; checksums verified in the Dockerfile). Decision for v1: keep upstream's build-time resolution (upstream-compatible; deterministic per build via the build-args) and record the resolved values from the publish run's log in the GitHub Release body. Values observed on 2026-09-05: yt-dlp `2026.08.19`, FFmpeg autobuild `2026-09-04-17-39` (`N-126405-g9f63b36a26`), ejs `0.8.0`. |

## Verification (2026-09-05, fork `c802ff5e`)

- CI workflow `CI`: run 33453586425 (push to `main`) and 33787539511
  (schedule) SUCCESS on this exact SHA (Python 3.12/3.13/3.14 matrix).
- Local mirror of the CI `test` job in a `python:3.12` container:
  `manage.py check` clean, `makemigrations --check` clean, full suite
  `Ran 313 tests — OK` (271 `medianest_bridge` tests), ruff (CI selection)
  `All checks passed!`.
- Integration harness (MediaNest `8dd54e716` + this SHA, lite bridge image,
  no huey worker): auth 401/401/200, CIDR gate rejects non-allow-listed
  callers, read-only mode 403, oversized body 413, validate 200/400, `/meta`
  200, `/capabilities` truthful. See MediaNest
  `docs/planning/tubesync-integration/EVIDENCE.md` § "Release-readiness
  session".
- Production image (`Dockerfile`, workflow build-args): **not buildable on the
  release host** — the WSL2 kernel's `WSLInterop` binfmt (magic `4d5a`)
  intercepts the QuickJS APE binary at stage `quickjs-extracted`
  (`qjs --assimilate` → exit 127), the same limitation
  `image-build-proof.md` records. The `MediaNest Bridge Release Image`
  workflow (`workflow_dispatch`, `push=false`) is the build-only proof and is
  owner-gated; it has never run as of this file.

## Known issues carried into v1

- Transitive `aiohttp 3.13.5` (via `hat-syslog==0.7.28` → `hat-juggler
  0.7.6`) has 14 PYSEC advisories (fixed in 3.14.0–3.14.3). Exposure: outbound
  syslog client only. To be revisited with the upstream re-pin (upstream
  v0.18.4 already adjusts `hat-juggler`). Fork CI has no `pip-audit` step.
- `GET /health/ready` reports `queues`/`workers` as `unknown` outside the s6
  container image (by design; the production image runs under s6).

## Licensing

- Repository license: AGPLv3 (`LICENSE`, unmodified). `medianest_bridge` is
  original fork code under the same license; the fork-delta NOTICE statement
  lives in `medianest_bridge/README.md` § License.
- Corresponding source: the fork repository at the tagged commit; the image
  reports `upstreamCommit` so the exact upstream base is recoverable.
- Open item, still flagged (not resolved): licensing/attribution header for the
  vendored contract file (`docs/agpl-compliance.md` § "Open item"). Legal
  review is a repository-owner action.

## Rollback

`docs/rollback.md` is current for this tag: image-level rollback = run the
previous `ghcr.io/kinginyellows/tubesync:<tag>` (there is no previous
`bridge-v*` tag; the pre-bridge fallback is the upstream image the deployment
used before, with `MEDIANEST_BRIDGE_TOKEN_FILE` unset so the bridge is
absent), zero fork migrations so no database rollback is needed, TubeSync
config/downloads volumes untouched.

## How this becomes a release (owner actions, in order)

1. `gh workflow run medianest-bridge-release.yaml --repo KingInYellows/tubesync -f tag=bridge-v1.0.0 -f push=false` — build-only proof; record the run id and the resolved yt-dlp/FFmpeg values.
2. `git tag -a bridge-v1.0.0 c802ff5e -m "medianest_bridge 1.0.0"` and `git push origin bridge-v1.0.0` — triggers the publish workflow with `push=true`.
3. Create the GitHub Release from the tag with this file as the body; the inherited `release.yaml` skips `bridge-v*` releases by design.
