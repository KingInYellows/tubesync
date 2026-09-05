# Rollback evidence (T5)

Two rollback surfaces, both already have direct evidence elsewhere in this
repo -- this document is the pointer that ties them together for a real
incident, not new claims.

## Image-level rollback: run the prior tag

The fork's release workflow (`.github/workflows/medianest-bridge-release.yaml`)
publishes each release under its own immutable tag
(`ghcr.io/kinginyellows/tubesync:<tag>`, `bridge-v*` namespace -- see
`medianest_bridge/docs/upstream-sync.md`'s tag-namespace note). Rolling
back a bad release is exactly:

```bash
docker pull ghcr.io/kinginyellows/tubesync:<previous-tag>
# stop the current container, start the previous tag in its place
```

**Gate this on both tags sharing the same upstream base first.** This
recipe is safe exactly when `medianest_bridge`'s own zero-migration
guarantee applies (see below) -- but that guarantee says nothing about
*upstream* TubeSync's own Django migrations. If the release being rolled
back FROM was built against a newer `UPSTREAM_SHA` than the release being
rolled back TO (compare each tag's `GET /meta` `upstreamCommit` field, or
see `medianest_bridge/docs/compatibility-matrix.md`), and upstream shipped
a migration in that gap that has already run against the live database,
the older image's bundled upstream code can fail to read -- or corrupt --
rows that migration touched, even though this bridge app itself never
migrates anything. Before pulling the previous tag: confirm both tags'
`upstreamCommit` values are identical. If they differ, treat this as an
upstream rollback (follow upstream's own migration-rollback procedure for
the intervening commits) before or instead of swapping images -- "just run
the old tag" is only a complete answer when the upstream base didn't move
(chatgpt-codex-connector review).

No image-side state migration, no data transformation, no special
procedure otherwise -- the previous tag is a complete, previously-working
image whenever that gate holds.

## Why this is safe: zero migrations, always

The bridge (`medianest_bridge`) defines zero Django models and therefore
adds zero migrations, in every T1-T5 commit -- this is a **standing,
automated suite assertion** (`medianest_bridge/tests/test_migrations.py::ZeroMigrationsTestCase`,
running `manage.py makemigrations --check --dry-run` as a genuine
subprocess on every test run, not merely a command someone has to
remember to run manually) and has additionally been verified with a real
scripted upgrade rehearsal against a real database file
(`medianest_bridge/docs/migration-upgrade-proof.md`: "No migrations to
apply", "No changes detected", "System check identified no issues").

Because of this, a rollback from a newer bridge image to an older one is
**config-only**: there is no forward-only schema state a downgrade could
strand. The older image's Django app connects to the same database schema
the newer image left behind, with no reverse migration required, because
no migration was ever applied in either direction by this app. This
claim covers `medianest_bridge` specifically -- an upstream TubeSync
schema change, if one ever ships in the same release, is upstream's own
migration and follows upstream's own rollback semantics, unrelated to
this app's zero-migration guarantee. See the upstream-base gate in
"Image-level rollback" above: it's the reason that recipe isn't
unconditionally safe just because this app's own guarantee holds.

## What is not covered by "config-only"

- **Configuration drift between versions.** If a rolled-back-to version
  predates an environment variable a newer version introduced (see
  `medianest_bridge/README.md`'s "Environment variables" table), that
  variable is simply unread by the older code. Metadata-only variables
  are then inert. Security-affecting ones are not: rolling back past
  `MEDIANEST_BRIDGE_ALLOWED_CIDRS` or `MEDIANEST_BRIDGE_READ_ONLY`
  disables those gates even though bearer auth still runs. Confirm the
  rollback target still enforces CIDR allowlisting and read-only writes
  before treating the extra env as leftover config.
- **The publish workflow has not yet run end-to-end** (GitHub Actions is
  enabled on the fork and `CI` is green on `main`, but
  `medianest-bridge-release.yaml` has never been dispatched and no
  `bridge-v*` tag exists as of the `bridge-v1.0.0` release package,
  2026-09-05 -- see `release-notes-bridge-v1.0.0.md`). Rollback here assumes
  a previously *published* tag exists to roll back to; for the first tag
  there is none. **Pre-bridge fallback for the first release:** run the
  upstream-equivalent image the deployment used before the fork (or a
  locally built image from the prior commit, see `image-build-proof.md`)
  with `MEDIANEST_BRIDGE_TOKEN_FILE` unset -- the bridge then reports
  `PROVIDER_UNAVAILABLE`/fails closed and MediaNest degrades honestly
  (its own rollback is the provider-mode flip documented in MediaNest's
  `docs/deployment/two-host-rollback.md`). TubeSync config and downloads
  volumes are untouched either way (zero fork migrations).
