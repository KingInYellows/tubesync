# medianest_bridge

A Django app inside this TubeSync fork that exposes a small, authenticated,
versioned JSON API for the [MediaNest](https://github.com/KingInYellows/medianest)
control plane to talk to. It is consumed only by MediaNest's backend, never
by a browser.

**T1 (bridge foundation)** shipped diagnostics endpoints and the auth
skeleton. **T2 (read API)** added the contract's read-only source and
media endpoints. **T3 (this slice, write API)** adds the contract's three
write endpoints -- the first mutation surface in this app.

Every slice deliberately implements only the endpoints the vendored
contract actually defines: no general paginated source list, no `GET
/media/{mediaId}`, no `/tasks` endpoint, no deletion of any kind anywhere
in this app. The M0 contract is scoped to the vertical-slice minimum by
design; a broader surface is a documented roadmap item, to be added
contract-first (proposed, accepted, re-vendored, then implemented) rather
than built ahead of the contract -- the same pattern that added
`REQUEST_TOO_LARGE` to `Error.code` after T1, and that codified T3's
`/sources/validate` scope reduction (below) as DECISIONS #27 on the
canonical contract.

## Purpose

TubeSync has no existing API (see the upstream audit referenced from
`docs/adr/ADR-0006-tubesync-as-youtube-acquisition-provider.md` in the
MediaNest repo): every route upstream is an HTML-rendering Django view.
`medianest_bridge` is a new, additive app that gives MediaNest a stable JSON
contract to depend on, built entirely from TubeSync's own models, forms, and
task-scheduling primitives -- it never talks to TubeSync's database directly
from outside the process, and it never bypasses TubeSync's own Django ORM
signal-driven task scheduling.

## Fork delta

Five upstream files are touched, four of them minimal:

1. `tubesync/tubesync/settings.py` -- `INSTALLED_APPS += 'medianest_bridge'`.
2. `tubesync/tubesync/urls.py` -- one `include('medianest_bridge.urls')` at
   `api/medianest/v1/`.
3. `tubesync/tubesync/settings.py` -- `BASICAUTH_PREFIX_ALLOW_URIS` gains
   `'/api/medianest/v1/'`, exempting the whole bridge namespace from
   `BasicAuthMiddleware` (see "Auth model" below).
4. `common/middleware.py` -- **a deliberate T2 fork deviation**, approved
   explicitly for this change: `BasicAuthMiddleware.process_request` now
   checks a separate `BASICAUTH_PREFIX_ALLOW_URIS` tuple for path-prefix
   matches (`startswith`), leaving `BASICAUTH_ALWAYS_ALLOW_URIS` as
   exact-match only (byte-identical to before for every existing entry
   like `/healthcheck`). This is generically useful beyond the bridge
   (any future exemption need with dynamic sub-paths), so it's flagged here
   as a candidate to propose upstream to `meeb/tubesync`, not just kept as
   a fork-only patch.
5. `Dockerfile` (T5) -- one `ARG MEDIANEST_BRIDGE_UPSTREAM_SHA=""` plus its
   fold into the final stage's existing `ENV` instruction (one line added to
   an existing multi-line `ENV`, no new `ENV` instruction). Empty by
   default, so every image build that doesn't pass the build-arg behaves
   identically to every pre-T5 build. See "Compatibility reporting" below.

Everything else the bridge needs is imported (models,
`common.utils.getenv`, `common.logger.log`, `sync.tasks` helpers), never
edited.

## Auth model

`common/middleware.py`'s `BasicAuthMiddleware` wraps every request in the
app unless it's exempted via `BASICAUTH_ALWAYS_ALLOW_URIS` (exact match)
or `BASICAUTH_PREFIX_ALLOW_URIS` (prefix match). T1 originally listed
each of its four routes as an individual exact-match entry, but that broke
down once T2 added path-parameterized routes (`/sources/{sourceUuid}`,
`/sources/{sourceUuid}/media`) -- a static tuple of exact strings can
never enumerate "every valid source UUID." T2 added one prefix entry,
`'/api/medianest/v1/'`, in `BASICAUTH_PREFIX_ALLOW_URIS` (see "Fork
delta" above) -- the entire bridge namespace is exempted as a unit, since
every route under it enforces its own complete, independent auth in
`BridgeView.dispatch()` regardless of Basic Auth.

Since the bridge's `Authorization` header carries a `Bearer <token>` value
that Basic Auth's own parser cannot understand, this exemption is required
for the bridge to be reachable at all when an operator has Basic Auth
enabled (`HTTP_USER`/`HTTP_PASS` set).

**Behavior note:** a request to an unmatched sub-path under
`/api/medianest/v1/` (no bridge route defines it) now falls through to
Django's ordinary URL-pattern-mismatch handling -- a plain 404, not a
Basic Auth challenge and not the bridge's own JSON error envelope (no
bridge view ever ran to produce one). This supersedes T1's "fails closed
behind Basic Auth" language for unlisted sub-paths, which described the
old exact-match design; there is no meaningful security regression, since
no route exists there for either Basic Auth or the bridge's own auth to
protect.

**The BasicAuth exemption and the bridge's own bearer-token check land in
the same commit.** Landing the exemption alone, even briefly, would leave
the bridge reachable with no authentication at all.

Every bridge request is gated by `medianest_bridge/views.py::BridgeView.dispatch()`,
in this order, each check short-circuiting on failure:

1. **Disabled check.** If `MEDIANEST_BRIDGE_TOKEN_FILE` is unset, unreadable,
   or empty, every request gets `503 PROVIDER_UNAVAILABLE`. Fail-closed: an
   unconfigured bridge is not reachable at all, not "reachable but
   unauthenticated." (503, not 404, because the contract's `Error.code`
   enum has no code that would honestly describe a 404 here.)
2. **CIDR allowlist** (`MEDIANEST_BRIDGE_ALLOWED_CIDRS`, optional,
   comma-separated). A mismatch returns `401 PROVIDER_AUTHENTICATION_FAILED`
   -- the vendored contract's `Unauthorized` response explicitly covers
   "missing or invalid bearer token, **or source CIDR not allowlisted**";
   there is no dedicated 403 for this case, only `ReadOnly` is 403.
3. **Bearer token** (`Authorization: Bearer <token>`), compared with
   `hmac.compare_digest` for constant-time comparison. Missing/invalid also
   returns `401 PROVIDER_AUTHENTICATION_FAILED`. The token is never logged
   or echoed in any response, success or error.
4. **Read-only gate** (`MEDIANEST_BRIDGE_READ_ONLY`, default `true`). Any
   non-GET/HEAD/OPTIONS request is rejected with `403 PROVIDER_READ_ONLY`
   while enabled -- implemented from T1 onward so T2/T3's write endpoints
   shipped read-only-by-default structurally, not by omission. As of T3
   this gate has teeth: `POST /sources` and `POST /sources/{uuid}/sync`
   genuinely mutate and are blocked by it (matching the contract's own
   403 `ReadOnly` response on both operations). `POST /sources/validate`
   is the one exception -- `BridgeView.read_only_exempt = True` on that
   view, because it never persists anything and the contract's own
   operation definition for it lists **no** 403 response at all (unlike
   the other two writes). Blocking a genuinely non-mutating request
   because of its HTTP method alone would have been a bug, not caution.
5. **Body-size gate** (`MEDIANEST_BRIDGE_MAX_BODY_BYTES`, default `65536`).
   Oversized requests get `413` with code `REQUEST_TOO_LARGE` -- flagged
   as a contract gap during T1 (no existing code described an
   oversized-body rejection honestly) and accepted by the contract owner
   as a canonical addition to `Error.code` for the T2 re-vendor
   (`bridge-openapi.v1.yaml` @ `713f9b4ac9efc24e0f285f9af58a50276f29ebb9`);
   it is a normal contract code now, not a proposed one.
   **T3 hardening** (a due obligation carried from the T1 verifier,
   through T2, to T3's first body-reading endpoints): the gate now
   actually reads the body (bounded to `limit + 1` bytes via
   `request.read()`) and checks the real byte count, rather than
   trusting the `Content-Length` header's stated value without
   verification -- and applies the same check even when that header is
   absent, instead of skipping the check entirely in that case (T1/T2's
   behavior). Precise scope of what this improves, researched by reading
   Django's own WSGI request-body handling directly rather than assumed:
   `django.core.handlers.wsgi.WSGIRequest` already wraps the WSGI input
   stream in a `LimitedStream` bounded by the `Content-Length` header's
   own value (defaulting to 0 bytes if that header is absent or
   malformed), and `LimitedStream.read()` enforces that bound even for an
   unbounded `.read()` call. For this fork's synchronous gunicorn worker
   class (`worker_class = 'sync'` in `gunicorn.py`), that means a request
   can never actually deliver more bytes to this app than its own
   declared `Content-Length`, regardless of what this gate does -- there
   is no "read unlimited bytes into memory" vulnerability at this layer
   to fix. What the T3 hardening actually adds: the size decision is now
   based on bytes Django's stream actually handed back, not a
   client-supplied number taken on faith. When `Content-Length` is
   absent or malformed, Django 6 assigns a zero-byte `LimitedStream`, so
   this gate observes 0 bytes and cannot detect an oversized chunked
   body -- it does not claim to. See
   `medianest_bridge/views.py::_read_body_is_oversized`'s docstring for
   the full reasoning.

None of this is implemented as Django middleware (`settings.MIDDLEWARE`) --
that would be a fourth upstream touch point beyond the three enumerated
above. It is a `dispatch()` override on a shared `BridgeView` base class
that every bridge view subclasses.

### Client IP for the CIDR gate

`medianest_bridge/auth.py::client_ip()` deliberately does **not** reuse
`common.utils.get_client_ip()`. That helper trusts the first entry of a
client-supplied `X-Forwarded-For` header, which a client can forge by
prepending arbitrary IPs. That's an acceptable trade-off for its one
existing caller (`HealthCheckView`'s `HEALTHCHECK_ALLOWED_IPS`, a
defense-in-depth check on an unauthenticated endpoint), but ADR-0006
promotes the bridge's CIDR allowlist to **primary access control** across a
real two-host LAN segment -- reusing the spoofable helper here would
silently defeat that.

The trustworthy signal instead is `X-Real-IP`, which this fork's own
openresty config (`config/root/etc/nginx/nginx.conf`) sets unconditionally
from `$remote_addr` on every proxied request (`proxy_set_header X-Real-IP
$remote_addr;`) -- nginx overwrites this header rather than passing through
any client-supplied value. `gunicorn.py` binds to `127.0.0.1` by default, so
gunicorn is only reachable through nginx's `proxy_pass`, not directly from
the LAN. Under that topology `X-Real-IP` is the actual TCP peer address as
seen by nginx and cannot be forged by the client. `REMOTE_ADDR` is used as a
fallback for direct-to-Django access with no proxy in front (`runserver`,
the Django test client).

This trust chain assumes nginx remains the sole ingress in front of
gunicorn; if a load balancer or another proxy is ever placed in front of
nginx, `X-Real-IP`'s trustworthiness needs re-establishing at that new hop.

**This assumption is not verified at the network layer -- only warned
about.** `LISTEN_HOST` (read by `gunicorn.py`, default `127.0.0.1`) is the
knob that must stay loopback for the assumption above to hold. Nothing in
this app can force gunicorn's actual bind address; if an operator sets
`LISTEN_HOST` to a non-loopback address while
`MEDIANEST_BRIDGE_ALLOWED_CIDRS` is also configured, a LAN host can reach
gunicorn directly, bypass nginx, and set `X-Real-IP` itself to impersonate
an allowlisted address -- silently degrading the CIDR gate to a no-op (the
bearer token is unaffected and still required, so this is a control
degradation, not a full auth bypass).

`medianest_bridge/auth.py::cidr_trust_warning()` detects exactly that
combination (CIDRs configured + `LISTEN_HOST` not loopback, checked via
the same env var and default `gunicorn.py` itself reads) and
`BridgeView.dispatch()` logs it loudly on every request while the
condition holds -- it never hard-fails the app, since an operator may have
a legitimate alternate ingress this app has no way to verify. The warning
text never contains the bearer token.

## Environment variables

| Variable | Default | Meaning |
| --- | --- | --- |
| `MEDIANEST_BRIDGE_TOKEN_FILE` | unset (disabled) | Path to a file whose stripped contents are the bearer token. Unset/missing/empty ⇒ bridge disabled, `503` on every request. |
| `MEDIANEST_BRIDGE_READ_ONLY` | `true` | Any value other than exactly `false` (case-insensitive) is treated as `true` -- fails closed to read-only. |
| `MEDIANEST_BRIDGE_MAX_BODY_BYTES` | `65536` | Integer. Enforced via `Content-Length` before any field-level validation. |
| `MEDIANEST_BRIDGE_ALLOWED_CIDRS` | unset (gate disabled) | Comma-separated CIDR list, e.g. `192.168.1.68/32`. A malformed entry fails closed to an empty (deny-all) list rather than silently admitting everything. |
| `MEDIANEST_BRIDGE_UPSTREAM_SHA` | `unknown` | Build-time git SHA of the tracked upstream commit, injected at image build time via the `Dockerfile`'s `ARG MEDIANEST_BRIDGE_UPSTREAM_SHA` (T5). `GET /meta`'s `upstreamCommit` reports the literal string `"unknown"` (not a fabricated value) whenever this is unset or empty -- true for the bare/local path (no container involved at all) and for any image build that omits the build-arg. See "Compatibility reporting" below for the wired value, its single canonical source, and build+run proof. |
| `MEDIANEST_BRIDGE_STORAGE_WARN_BYTES` | `5368709120` (5 GiB) | `storage` readiness component reports `degraded` at or below this many free bytes on `DOWNLOAD_ROOT`. |
| `MEDIANEST_BRIDGE_STORAGE_CRITICAL_BYTES` | `1073741824` (1 GiB) | `storage` readiness component reports `unavailable` at or below this many free bytes. Defaults are round numbers, not derived from any measured workload -- an operator with a better sense of their own disk growth rate should override them. |
| `MEDIANEST_BRIDGE_YOUTUBE_PROBE_ENABLED` | `true` | Any value other than exactly `false` (case-insensitive) is treated as `true`. Enables the `youtube` readiness component's real network probe (a `GET https://www.youtube.com/generate_204`, no auth/cookies, cached 120s). Meaningful only under the shared-egress-namespace deployment wiring (DECISIONS #29, M6b routing doc) where this process shares yt-dlp's VPN egress -- set to `false` for any deployment that does NOT use that wiring, where a probe result would describe the wrong network path; the component then honestly reports `not_configured`. See `medianest_bridge/readiness.py`'s module docstring for the full design rationale. |
| `LISTEN_HOST` | `127.0.0.1` | **Not a `medianest_bridge` setting** -- read by `gunicorn.py` to choose gunicorn's bind address. Must stay loopback (the default) for the CIDR gate's `X-Real-IP` trust to hold; see the warning behavior described just above. |

None of these are registered as Django settings in `settings.py` -- the app
reads them directly from the environment (via `common.utils.getenv`, reused
rather than reimplemented) each time they're needed, so the fork's
upstream-touch list stays limited to the three files listed above, and an
operator can rotate the token file's contents or flip `MEDIANEST_BRIDGE_READ_ONLY`
without a process restart.

## Compatibility reporting

`GET /meta` reports `bridgeVersion` (this app's own version constant,
`config.BRIDGE_VERSION`), `tubesyncVersion` (upstream's own `settings.VERSION`
string -- known stale relative to the actually-checked-out commit, see the
upstream audit; not something this app can fix, it's TubeSync's own
constant), and `upstreamCommit`. Current, verified truthful state of each
path:

- **Bare local path** (dev server, `manage.py test`, this repo's own CI if
  Actions is ever enabled without a build-arg pipeline): `upstreamCommit`
  reports the literal string `"unknown"`, never a fabricated value. Tested
  directly (`tests/test_endpoints.py::MetaEndpointTestCase`).
- **Image-build path (T5, wired)**: the `Dockerfile`'s final stage declares
  `ARG MEDIANEST_BRIDGE_UPSTREAM_SHA=""` and folds it into that stage's
  `ENV` block, so `config.py::upstream_sha()` reads whatever value the
  build invocation supplies (or the honest `"unknown"` fallback -- via
  that function's own `.strip() or 'unknown'` -- when the build-arg is
  omitted, exactly matching the bare-local behavior). No application code
  change was needed on top of T1-T4: `config.py::upstream_sha()` already
  read exactly this env var name.

  **The value itself is `bridge-openapi.v1.yaml`'s own definition, taken
  literally**: "Full git SHA of the upstream commit the fork tracks" --
  the fixed upstream base this fork branched from
  (`medianest_bridge/docs/migration-upgrade-proof.md`'s upstream-base
  commit), *not* this image's own build commit, which changes on every
  fork commit and is a different fact than what the contract field
  describes. That value has exactly one canonical source in this repo,
  `medianest_bridge/docs/UPSTREAM_SHA` (a single 40-character SHA, nothing
  else) -- both the fork's release workflow
  (`.github/workflows/medianest-bridge-release.yaml`) and this doc read
  from that one file rather than duplicating the literal SHA anywhere
  else, so a future upstream re-sync only has one place to update (see
  `medianest_bridge/docs/upstream-sync.md`).

  Build+run proof (both paths -- built with the pin, and built without any
  `MEDIANEST_BRIDGE_UPSTREAM_SHA` build-arg at all) is captured verbatim in
  `medianest_bridge/docs/image-build-proof.md`: exact `docker build`
  invocations, exact `docker run` + authenticated `curl /meta` transcripts,
  and the truthful value in one case and the literal `"unknown"` string in
  the other.

## Endpoints

All under `/api/medianest/v1/`, all requiring the bearer token. T1/T2
reads are `GET`; T3 writes are `POST`.

**T1 (diagnostics):**

- `GET /health/live` -- liveness only, no database access.
- `GET /health/ready` -- per-component readiness (`application`,
  `database`, `queues`, `workers`, `ytDlp`, `ffmpeg`, `storage`, `youtube`,
  `cookies`, `plex`). Components that cannot be cheaply/honestly verified
  from the web process report `unknown`, never a fabricated `healthy` --
  see `medianest_bridge/readiness.py`'s module docstring for exactly which
  components and why, and for the (contract-silent, documented-here)
  overall-status aggregation rule.
- `GET /meta` -- bridge version, upstream `VERSION` string (known stale
  relative to the actual checked-out commit -- see the upstream audit), and
  `upstreamCommit`.
- `GET /capabilities` -- capability negotiation. As of T2, `health`,
  `readSources`, and `readMedia` report `true`; every other capability
  (writes, tasks, Plex actions) stays `false` until the corresponding
  endpoints ship.

**T2 (read API, `medianest_bridge/views_sources.py`, all backed by
`medianest_bridge/mapping.py`'s pure Source/Media -> contract-schema
functions):**

- `GET /sources?key=<canonicalKey>` -- key-based lookup. `key` is
  required; missing or empty returns `400 SOURCE_INVALID`. Returns
  `{"data": [...]}` with zero or one item (`Source.key` is unique) --
  never a general paginated listing.
- `GET /sources/{sourceUuid}` -- source detail. A malformed UUID segment
  and a well-formed-but-nonexistent UUID both return
  `404 SOURCE_NOT_FOUND` (no separate 400 for bad ID syntax). The route
  uses Django's `<str:...>` path converter rather than `<uuid:...>`
  specifically so a malformed ID still reaches the view and gets this
  JSON envelope, instead of Django's ordinary URL-pattern-mismatch 404.
- `GET /sources/{sourceUuid}/media` -- paginated media for a source
  (`page`, default `1`; `limit`, default `50`, max `200`). Out-of-bounds
  or non-integer `page`/`limit` values are rejected with
  `400 SOURCE_INVALID`, not silently clamped -- the contract's bounds
  describe a valid request, not an auto-correction instruction. Ordered
  newest-published-first, matching the existing upstream convention in
  `sync/views/sources.py::SourceView`.

TubeSync-state -> contract-`normalizedState` mapping has no
contract-defined precedence, so `mapping.py` documents its own considered
choices inline -- most notably, TubeSync's `MediaState.UNKNOWN` maps to
`"discovered"`, not `"unknown"`, since it's a well-determined state
(indexed, no work item yet), not one the bridge genuinely cannot verify.

**T3 (write API, `medianest_bridge/views_write.py`, backed by
`source_forms.py` and `sync_dedup.py`) -- the first mutation surface:**

- `POST /sources/validate` -- validates a candidate source **without
  persisting anything**. Runs `SourceForm`'s field-level checks for
  `sourceType`/`canonicalKey` plus a pure `validate_url()` shape
  cross-check that `canonicalUrl` matches the declared `sourceType` (no
  network call). Does **not** run the directory-traversal/media-format
  checks POST /sources runs -- `ValidateSourceRequest` has no `directory`
  field to check them against; this scope was flagged to the contract
  owner during T3 and codified as DECISIONS #27 on the canonical
  contract. `displayName` in the response is `canonicalKey` echoed back
  as an explicit placeholder (no TubeSync-side display name exists
  without a live metadata fetch, out of scope here) -- also codified in
  the contract's own schema description. Not blocked by
  `MEDIANEST_BRIDGE_READ_ONLY` (see the read-only gate section above).
- `POST /sources` -- create-or-adopt on the canonical key. A `key`
  collision (an existing source already uses it) returns `409
  SOURCE_CONFLICT` with `existingSourceUuid`, adopt semantics, and always
  takes priority over a name/directory collision. A `name`/`directory`
  collision without a matching `key` collision returns `400
  SOURCE_NAMESPACE_CONFLICT` (a genuinely different canonical source,
  never merged). Concurrent creates for the same key converge via the
  real DB-level unique constraint: `form.save()` is wrapped in
  `try/except IntegrityError`, and on conflict the code re-queries
  (get-after-conflict) rather than trusting an earlier pre-check's
  now-stale result. `sourceType: "channel"` maps to TubeSync's
  `CHANNEL_ID` source type, never `CHANNEL` -- see `source_forms.py`'s
  module docstring for the reasoning and its consequence for what
  MediaNest must supply as `canonicalKey`. Fields the request schema
  doesn't supply (media format, resolution, codecs, filters, etc.) use
  TubeSync's own `Source` model defaults, obtained via `model_to_dict()`
  on a blank instance -- never a bridge-invented default. `profile` is
  accepted and structurally validated but not currently mapped onto any
  TubeSync field (no contract-level field-name/enum-value mapping exists
  yet); created sources use TubeSync's own defaults for everything
  `profile` might have described. **Side effect, deliberate:** a
  successful create goes through `Source`'s real `.save()`, firing
  `sync/signals.py`'s `post_save` receiver exactly as the HTML UI would
  -- schedules `check_source_directory_exists`, conditionally
  `download_source_images`, `TaskHistory.schedule(index_source,
  delay=600)` if the source is active, and
  `TaskHistory.schedule(save_all_media_for_source, ...)`.
- `POST /sources/{sourceUuid}/sync` -- wraps TubeSync's own
  `SourceSyncNowView` mechanism exactly (same `TaskHistory.schedule()`
  call, same `index_source` task, same `delay` setting). Bridge-side
  dedup checks for any *non-completed* `index_source` task for this
  source (running **or** merely scheduled/enqueued), not just actively
  executing ones -- `sync_dedup.py`'s module docstring documents the
  exact `TaskHistory.start_at`/`end_at` predicate this uses, derived from
  reading `common/huey.py`'s signal handler directly. Per the contract's
  own caveat, this is a static-analysis-derived best-effort dedup, not a
  dynamically-verified idempotency guarantee.

Every response (success and error) echoes `X-Request-ID`. A caller-supplied
`X-Request-ID` header is echoed verbatim when it is a valid UUID; otherwise
the bridge generates a new UUID4. `X-Correlation-ID`, if supplied, is logged (paired with the request
ID) but is not itself part of any response schema.

Errors use the vendored contract's RFC 7807-style envelope
(`type`/`title`/`status`/`code`/`detail`/`requestId`/`retryable`). No raw
stack traces or exception messages ever reach a response body -- unhandled
exceptions are caught by `BridgeView.dispatch()`'s catch-all, logged
server-side via `common.logger.log.exception(...)`, and returned as a
sanitized `500 INTERNAL_PROVIDER_ERROR`.

## Structured logging (not a metrics stack)

T4 researched what this fork already has for operational metrics before
building anything: no Prometheus, no metrics endpoint, no existing hook
anywhere in the codebase to attach to (confirmed by inspection, not
assumed -- there is no `django_prometheus`/`prometheus_client` dependency
in `Pipfile`, and no metrics-shaped view in `common/` or `sync/`).
Bolting on a new metrics stack for this alone would be a new dependency
and a new operational surface for a single fork app to carry; the honest
T4 deliverable instead is consistent, greppable request/outcome logging
through the same `common.logger.log` this app already uses everywhere
else -- no new dependency, no new infrastructure.

Every bridge request produces exactly one line via `BridgeView.dispatch()`
(`views.py::_log_outcome`), regardless of which gate or view produced the
response:

```
medianest_bridge: request_complete route=<path> method=<verb> status=<code> duration_ms=<float> request_id=<uuid>
```

Every mutation *attempt* (any non-GET/HEAD/OPTIONS request to a bridge
route -- accepted or rejected, including a read-only rejection) also
produces one audit-shaped line (`views.py::_log_mutation_audit`):

```
medianest_bridge: mutation_audit route=<path> outcome=<accepted|rejected_<CODE>> source_uuid=<uuid-or-'-'> request_id=<uuid>
```

`source_uuid` comes from the URL's own `source_uuid` kwarg when the route
names one (`POST /sources/{uuid}/sync`), or from a successful
`POST /sources`'s response body (the uuid didn't exist before that
request). Neither line ever contains the bearer token or a filesystem
path -- by construction, not by scrubbing: the five/four fields logged
are route, method, status, duration, request id, outcome, and source
uuid, none of which can carry either.

## Contract

`medianest_bridge/contract/bridge-openapi.v1.yaml` is a vendored, read-only
copy of the canonical contract (MediaNest repo,
`docs/planning/tubesync-integration/bridge-openapi.v1.yaml` @
`35a9c069fe4f1512ff7b606c33c0c2a11c7efa76`, re-vendored for T4). History:
T1 vendored `ce17a28773a6f3866c9c9235ae4eae04f4bafff4`; T2 re-vendored
`713f9b4ac9efc24e0f285f9af58a50276f29ebb9` (`REQUEST_TOO_LARGE` joining
`Error.code`'s enum); T4's re-vendor is description-only (DECISIONS #27:
codifies `/sources/validate`'s slice-1 scope and
`ValidatedSource.displayName`'s placeholder, both already implemented
exactly this way since T3) -- no schema/enum changes, so no bridge
behavior changed here, only the contract's own prose catching up to it.
Do not edit this file directly -- re-vendor from the canonical source
instead.

`medianest_bridge/contract/contract_fixtures.json` is a small JSON
extraction (required fields + enums for the schemas this app exercises)
generated once from that YAML, so `medianest_bridge/tests/test_endpoints.py` can
assert response shape using only the stdlib `json` module -- no new Pipfile
dependency. `test_contract_conformance.py` sha256-locks the fixture against
the vendored YAML (so the two can never silently drift) and, only when
PyYAML happens to be importable (it is a transitive dependency of the
`hat-syslog` pin already in `Pipfile`, via `hat-json` -- not a dependency
this app adds), cross-checks the fixture was actually derived correctly
from the YAML rather than hand-edited.

## Tests

`medianest_bridge/tests/` (Django test runner, same as upstream):

- `test_config.py` -- env var parsing/defaults/fail-closed behavior.
- `test_auth.py` -- client-IP resolution, CIDR allow/deny,
  `hmac.compare_digest` patch-asserted as the actual comparison mechanism.
- `test_dispatch.py` -- the full gate order end-to-end: disabled,
  CIDR-denied, missing/invalid/valid token, read-only blocking non-safe
  methods, oversized body, request-ID echo/generation, unhandled-exception
  sanitization, and that the token never appears in a response body or a
  log call.
- `test_endpoints.py` -- each of the T1 diagnostic endpoints' response
  shape against the vendored contract fixtures, plus the "never fabricate
  healthy" invariant for `queues`/`workers` (still unverifiable outside a
  real s6-overlay deployment) and that `youtube`'s real probe result
  (mocked, never a live network call) is actually wired through to the
  response. See `test_readiness.py::YoutubeProbeTestCase` for the probe's
  own success/timeout/refused/disabled/caching behavior.
- `test_contract_conformance.py` -- the fixture/YAML sha256 lock described
  above.
- `test_basicauth_exemption.py` -- redesigned for T2's prefix-based
  exemption: the prefix entry is present, every bridge route (including
  the parameterized ones) resolves under it, a real UUID path reaches the
  bridge's own auth (not a Basic Auth challenge) while a non-bridge route
  still requires Basic Auth, and the prefix match has an exact boundary
  (`/api/medianest/v1x...` does not match).
- `test_listen_host_trust.py` -- the `LISTEN_HOST`/CIDR trust warning:
  emitted when CIDRs are configured and `LISTEN_HOST` is non-loopback, not
  emitted otherwise, never contains the token, and never blocks the
  request it's logged alongside.
- `test_mapping.py` -- `mapping.py`'s Source/Media -> contract-schema
  functions against real ORM rows: state-mapping precedence, the
  `source_type` channel/playlist collapse, downloaded-vs-undownloaded
  media field population, and the `MediaState.UNKNOWN` -> `"discovered"`
  choice.
- `test_sources.py` -- the T2 read endpoints end-to-end: contract shape
  (via `test_endpoints.py`'s `assert_matches_schema`), key-lookup
  required-param handling, malformed/nonexistent-ID 404 handling,
  pagination bounds (rejected not clamped, including the exact-200
  boundary), ordering, and non-GET method rejection (12 method x route
  combinations).
- `test_write_sources.py` -- the three T3 write endpoints: valid/invalid
  requests for each, the `channel` -> `CHANNEL_ID` mapping assertion,
  read-only-mode behavior (blocked for create/sync, exempt for validate),
  key/name/directory conflict handling (409 vs 400, with
  `existingSourceUuid` on the former), that create genuinely schedules an
  `index_source` `TaskHistory` row (a real, directly-assertable side
  effect, not mocked), that a repeated sync-now does not duplicate a
  still-pending task, directory-traversal rejection, that validate never
  persists anything, contract-shape conformance for every success
  response, and body-size hardening against a real write route with a
  real JSON payload.

Run with `cd tubesync && python3 manage.py test medianest_bridge` (or omit
the app label to run the full suite, upstream included).

## License

This app is part of the `KingInYellows/tubesync` fork and is distributed
under the same AGPLv3 license as the rest of this repository (see
`LICENSE` at the repository root, byte-identical to upstream's own
`LICENSE` -- verified, not assumed, see
`medianest_bridge/docs/agpl-compliance.md`). Corresponding source for any
deployment of this fork -- including this app -- is available via the
public fork repository (`github.com/KingInYellows/tubesync`, confirmed
public), satisfying AGPLv3 §13's network-use clause.

**Fork-delta notice**: `medianest_bridge` is original code added by this
fork; it is not present in upstream `meeb/tubesync` at the pinned
upstream-base commit (`medianest_bridge/docs/UPSTREAM_SHA`). It is
licensed identically to the rest of this repository, AGPLv3, under the
unmodified `LICENSE` at the repository root. The five upstream files this
fork's delta touches ("Fork delta" section above) remain licensed as
upstream TubeSync itself is licensed, modified only as that section
describes. See `medianest_bridge/docs/agpl-compliance.md` for the full
verification record and open items flagged for legal review.
