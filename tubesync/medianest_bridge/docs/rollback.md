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

No image-side state migration, no data transformation, no special
procedure -- the previous tag is a complete, previously-working image.

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
no migration was ever applied in either direction by this app. (This
claim covers `medianest_bridge` specifically -- an upstream TubeSync
schema change, if one ever ships in the same release, is upstream's own
migration and follows upstream's own rollback semantics, unrelated to
this app's zero-migration guarantee.)

## What is not covered by "config-only"

- **Configuration drift between versions.** If a rolled-back-to version
  predates an environment variable a newer version introduced (see
  `medianest_bridge/README.md`'s "Environment variables" table), that
  variable is simply unread by the older code -- harmless, not a rollback
  blocker, but worth knowing if an operator's deployment config carries
  settings a rollback target doesn't understand.
- **The GitHub Actions publish workflow itself is unproven** (see
  `medianest_bridge/docs/image-build-proof.md` and this workflow's own
  header comment) -- rollback here assumes a previously *published* tag
  exists to roll back to, which requires Actions to have been enabled and
  a successful publish to have already happened at least once. Until
  then, "run the prior tag" has no prior tag to run.
