# medianest_bridge

A Django app inside this TubeSync fork that exposes a small, authenticated,
versioned JSON API for the [MediaNest](https://github.com/KingInYellows/medianest)
control plane to talk to. It is consumed only by MediaNest's backend, never
by a browser.

This is **T1 (bridge foundation)**: diagnostics endpoints and the auth
skeleton only. It does not yet expose source, media, or task endpoints --
those land in T2 (read API) and T3 (write API), which build on this
foundation and consume the same vendored contract.

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

Exactly three upstream files are touched, all minimal:

1. `tubesync/tubesync/settings.py` -- `INSTALLED_APPS += 'medianest_bridge'`.
2. `tubesync/tubesync/urls.py` -- one `include('medianest_bridge.urls')` at
   `api/medianest/v1/`.
3. `tubesync/tubesync/settings.py` -- `BASICAUTH_ALWAYS_ALLOW_URIS` gains the
   bridge's four route paths, exempting them from `BasicAuthMiddleware`
   (see "Auth model" below for why, and why this landed in the same commit
   as the bridge's own auth).

No other upstream file is modified. Everything else the bridge needs is
imported (models, `common.utils.getenv`, `common.logger.log`), never edited.

## Auth model

`common/middleware.py`'s `BasicAuthMiddleware` wraps every request in the
app unless `request.path` is an **exact** match against
`BASICAUTH_ALWAYS_ALLOW_URIS` (confirmed by reading that file -- it is
`request.path in bypass_uris`, not a prefix match). Since the bridge's
`Authorization` header carries a `Bearer <token>` value that Basic Auth's
own parser cannot understand, the bridge's four T1 routes are listed
individually in that tuple. Because the match is exact, a future PR adding a
new bridge route must add its exact path too --
`medianest_bridge/tests/test_basicauth_exemption.py` asserts the two lists
never drift apart, and an unlisted sub-path under `api/medianest/v1/` fails
closed behind Basic Auth rather than silently bypassing it.

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
   while enabled -- T1 has no mutating endpoints yet, but the gate is
   implemented now so T2/T3's write endpoints ship read-only-by-default from
   day one, structurally, not by omission.
5. **Body-size gate** (`MEDIANEST_BRIDGE_MAX_BODY_BYTES`, default `65536`).
   Checked via `Content-Length` when present; chunked transfer encoding
   without `Content-Length` is rejected with `413 REQUEST_TOO_LARGE`
   because the body cannot be size-checked before read. Oversized requests
   get `413` with code `REQUEST_TOO_LARGE`.

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
| `MEDIANEST_BRIDGE_UPSTREAM_SHA` | `unknown` | Build-time git SHA of the tracked upstream commit, injected at image build time. `GET /meta`'s `upstreamCommit` reports the literal string `"unknown"` (not a fabricated value) when unset -- true for every T1-T4 local/CI build, since the fork's own image-publish workflow does not exist until T5. |
| `LISTEN_HOST` | `127.0.0.1` | **Not a `medianest_bridge` setting** -- read by `gunicorn.py` to choose gunicorn's bind address. Must stay loopback (the default) for the CIDR gate's `X-Real-IP` trust to hold; see the warning behavior described just above. |

None of these are registered as Django settings in `settings.py` -- the app
reads them directly from the environment (via `common.utils.getenv`, reused
rather than reimplemented) each time they're needed, so the fork's
upstream-touch list stays limited to the three files listed above, and an
operator can rotate the token file's contents or flip `MEDIANEST_BRIDGE_READ_ONLY`
without a process restart.

## Endpoints (T1)

All under `/api/medianest/v1/`, all `GET`, all requiring the bearer token:

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
- `GET /capabilities` -- capability negotiation. T1 reports `health: true`
  and every other capability `false`, honestly reflecting that no
  source/media/task endpoints exist yet.

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

## Contract

`medianest_bridge/contract/bridge-openapi.v1.yaml` is a vendored, read-only
copy of the canonical contract (MediaNest repo,
`docs/planning/tubesync-integration/bridge-openapi.v1.yaml` @
`ce17a28773a6f3866c9c9235ae4eae04f4bafff4`). Do not edit it directly --
re-vendor from the canonical source instead.

`medianest_bridge/contract/contract_fixtures.json` is a small JSON
extraction (required fields + enums for the schemas T1 exercises) generated
once from that YAML, so `medianest_bridge/tests/test_endpoints.py` can
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
- `test_endpoints.py` -- each of the four endpoints' response shape against
  the vendored contract fixtures, plus the "never fabricate healthy"
  invariant for `queues`/`workers`/`youtube`.
- `test_contract_conformance.py` -- the fixture/YAML sha256 lock described
  above.
- `test_basicauth_exemption.py` -- bridge routes reachable with bearer-only
  auth while Basic Auth is enabled app-wide; a non-bridge route still
  requires Basic Auth; the exemption tuple and the bridge's actual routes
  never drift apart.
- `test_listen_host_trust.py` -- the `LISTEN_HOST`/CIDR trust warning:
  emitted when CIDRs are configured and `LISTEN_HOST` is non-loopback, not
  emitted otherwise, never contains the token, and never blocks the
  request it's logged alongside.

Run with `cd tubesync && python3 manage.py test medianest_bridge` (or omit
the app label to run the full suite, upstream included).

## License

This app is part of the `KingInYellows/tubesync` fork and is distributed
under the same AGPLv3 license as the rest of this repository (see
`LICENSE` at the repository root). Corresponding source for any deployment
of this fork -- including this app -- is available via the public fork
repository, satisfying AGPLv3 §13's network-use clause.
