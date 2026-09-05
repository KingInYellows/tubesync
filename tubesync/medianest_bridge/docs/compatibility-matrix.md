# Compatibility matrix (T5)

Three pins and one matrix, each with a single canonical source so none of
them can silently drift against each other.

## 1. Upstream base

**the value in `medianest_bridge/docs/UPSTREAM_SHA`** -- the `meeb/tubesync`
commit this fork was branched from, confirmed identical to `origin/main`
and `upstream/main` at fork creation time (T1 upstream audit). Not
duplicated as a literal SHA here (this table is a living reference, not a
point-in-time snapshot -- see "Single canonical source" immediately below):
a re-pin only has one file to update.

Single canonical source in this repo: `medianest_bridge/docs/UPSTREAM_SHA`
(one line, nothing else). Read from that file by:

- `medianest_bridge/docs/migration-upgrade-proof.md`'s reproducible
  procedure (item 2's upstream-base checkout).
- `.github/workflows/medianest-bridge-release.yaml`'s `MEDIANEST_BRIDGE_UPSTREAM_SHA`
  build-arg, which the `Dockerfile`'s `ARG`/`ENV` wiring (T5) plumbs into
  `GET /meta`'s `upstreamCommit` field at runtime (see
  `medianest_bridge/docs/image-build-proof.md`).

This value changes only when the fork deliberately re-syncs against a
newer upstream commit -- see `medianest_bridge/docs/upstream-sync.md` for
that process, which updates this one file rather than the several places
that used to hardcode the literal SHA before T5.

## 2. Fork delta (the bridge branch chain)

Each slice is a stacked draft PR against `KingInYellows/tubesync`,
building on the previous slice's branch tip:

| Slice | Branch | PR | Scope |
| --- | --- | --- | --- |
| T1 | `feat/medianest-bridge-foundation` | #1 | Diagnostics endpoints (`/health/live`, `/health/ready`, `/meta`, `/capabilities`) + bearer-token auth skeleton. |
| T2 | `feat/medianest-bridge-read-api` | #2 | Read-only `/sources` and `/sources/{uuid}` (+ media) endpoints, backed by `mapping.py`'s pure Source/Media -> contract-schema functions. |
| T3 | `feat/medianest-bridge-write-api` | #3 | The contract's three write endpoints: `/sources/validate`, `POST /sources`, `POST /sources/{uuid}/sync`. |
| T4 | `feat/medianest-bridge-hardening` | #4 | Operational hardening: deeper readiness checks, error-detail redaction, structured request/audit logging, migration/upgrade proof. One post-review fix-up commit closed a verifier FAIL (universal error-detail sanitization, a redaction regex bug, two other LOWs). |
| T5 | `feat/medianest-bridge-release` | (this PR) | Release image: build-time `upstreamCommit` wiring, a fork-owned (not upstream-modifying) publish workflow, this compatibility matrix, AGPL verification, the upstream-sync process, and rollback evidence. |

Every slice preserves upstream tests and the fork's own zero-migrations
guarantee (`medianest_bridge/docs/migration-upgrade-proof.md`); every
slice's fork delta stays inside `medianest_bridge/` plus the small,
explicitly enumerated set of upstream touch points documented in this
app's `README.md` ("Fork delta" section) -- five files as of T5, four of
them a few lines each.

## 3. Bridge contract version

**`bridge-openapi.v1.yaml` @ `35a9c069fe4f1512ff7b606c33c0c2a11c7efa76`**
(canonical source: `KingInYellows/medianest`,
`docs/planning/tubesync-integration/bridge-openapi.v1.yaml`), vendored
read-only into `medianest_bridge/contract/bridge-openapi.v1.yaml`. `info.version:
"1.0.0"` inside that file, echoed by `GET /meta`'s `bridgeVersion` field
(`config.py::BRIDGE_VERSION`). Full re-vendor history is in
`medianest_bridge/README.md`'s "Contract" section; not repeated here to
avoid a second place that can drift out of sync with the first.

## 4. Compatibility matrix -- bridge API v1 <-> MediaNest milestones

The contract's own `info.description` states plainly what this matrix
formalizes: request/response shapes in `bridge-openapi.v1.yaml` are
"load-bearing for T1-T3 (tubesync fork) and M3-M4 (MediaNest)
implementers," and `/sources/{sourceUuid}/media`'s response shape is
separately pinned by ADR-0006 §4 as "the interface M6's Plex
reconciliation consumes."

**Scope note, stated plainly rather than assumed**: this matrix's
TubeSync-side ("Implemented in this fork") column is directly verifiable
from this repository -- endpoint code, tests, and the vendored contract
itself. Its MediaNest-side milestone descriptions are quoted from what the
vendored contract states MediaNest consumes; this repository has no
visibility into MediaNest's actual M1-M6 implementation *status* (only
TubeSync's own side is this agent's assigned scope, per this program's
repository-boundary rules -- see the workspace `CLAUDE.md`). Confirming
which MediaNest milestones are actually complete against this contract
version is the MediaNest repository's own verification to make, not
something asserted here.

| Contract endpoint | Implemented in this fork | MediaNest consumer (per contract/ADR) |
| --- | --- | --- |
| `GET /health/live`, `GET /health/ready`, `GET /meta`, `GET /capabilities` | T1 | Provider-readiness/version negotiation, covered by the contract's general "T1-T3 / M3-M4" framing above -- not pinned to a more specific milestone by the contract or any ADR this repo has visibility into. |
| `GET /sources`, `GET /sources/{sourceUuid}` | T2 | Source lookup/adoption flow. |
| `POST /sources/validate` | T3 (slice-1 scope: `SourceForm` field-level checks + `validate_url()` shape cross-check only -- no directory-traversal/media-format checks, no network calls; DECISIONS #27) | Pre-submission validation in MediaNest's playlist-submission UI. |
| `POST /sources` | T3 | Source creation/adoption. |
| `POST /sources/{sourceUuid}/sync` | T3 | Sync-now trigger. |
| `GET /sources/{sourceUuid}/media` | T2 (list), response shape ADR-0006 §4-pinned | **M6's Plex reconciliation consumes this directly, per ADR-0006 §4** -- this response shape "MUST NOT change without an ADR amendment" (contract's own words, quoted, not paraphrased). |

Endpoints the contract explicitly defers (out of v1.0.0 scope, per the
contract's own `info.description`): `PATCH`/`DELETE` sources, media
retry/skip/enable, task listing. None of these exist anywhere in this
fork; `GET /capabilities`' `false` flags for each are the bridge's own
truthful statement of that (see `medianest_bridge/README.md`'s
"Endpoints" section).

## 5. Release rows

One row per `bridge-v*` tag. Added at release-readiness time; the tag itself
is created by the repository owner.

| Bridge tag | Fork commit | `UPSTREAM_SHA` (upstream version) | Contract | MediaNest min | Notes |
| --- | --- | --- | --- | --- | --- |
| `bridge-v1.0.0` (proposed) | fork `main` after PR #9 merges (docs-only on top of `c802ff5e1efd49640bc32c1c460455041476e57a`; bridge code identical to that SHA) | `3b9d72f28dda9c931776f76e7428a72a24a57f82` (v0.18.3 era; 85 behind v0.18.4 at tag time, rehearsal merge clean) | `bridge-openapi.v1.yaml` `1.0.0` | `v1.0.0-rc2` (proposed) | See `release-notes-bridge-v1.0.0.md`. yt-dlp/FFmpeg resolved at build time (not source-pinned). |
