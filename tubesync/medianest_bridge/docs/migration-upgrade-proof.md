# Migration/upgrade proof (T4)

Two claims, verified separately, per the T4 hardening brief:

1. **The fork adds zero migrations.** Automated, in the test suite:
   `medianest_bridge/tests/test_migrations.py::ZeroMigrationsTestCase`
   runs `manage.py makemigrations --check --dry-run` as a genuinely
   separate `subprocess.run(...)` call and fails the test if its exit
   code is non-zero -- i.e. if any model change (in `medianest_bridge` or
   anywhere else) is ever left uncaptured by an existing migration file.
   **Not** `django.core.management.call_command()` in-process: an earlier
   version tried exactly that, and it corrupted shared Django app/
   migration-loader state for every test running afterward in the same
   process (11 unrelated test failures + 1 error appeared across both
   medianest_bridge and upstream sync tests the moment it ran in-process;
   confirmed by toggling it on/off, not guessed -- see this test's own
   docstring for the full account). The subprocess form doesn't share
   that state with the surrounding test run at all, so it's the correct
   isolation here, not merely a workaround. This has also been checked
   manually via `manage.py makemigrations --check --dry-run` on every
   T1-T4 commit ("No changes detected" every time -- see each commit
   message/PR body), but it is now a standing assertion in the suite
   itself, not only a command someone has to remember to run.

2. **A container upgrade from upstream-latest to the fork's codebase
   applies cleanly against an existing (already-migrated) TubeSync
   database.** Not expressible as a single `manage.py test` run --
   requires two different code checkouts running sequentially against
   the same database file -- so this was executed once as a scripted
   procedure and the output captured here. No destructive step: a fresh
   throwaway sqlite file, a disposable git worktree, both discarded
   afterward.

## Procedure (reproducible)

```bash
# 1. A disposable worktree at the upstream-base commit this fork tracks --
#    read from medianest_bridge/docs/UPSTREAM_SHA (the single canonical
#    source, per upstream-sync.md's Step 3), not hardcoded here, so
#    re-running this procedure after a re-pin checks out the *current*
#    base, not the one recorded when this doc was first written. (The
#    original pin was confirmed identical to origin/main and
#    upstream/main at fork creation time, per the T1 upstream audit.)
git worktree add --detach /tmp/upstream-base-checkout \
  "$(cat tubesync/medianest_bridge/docs/UPSTREAM_SHA)"

mkdir -p /tmp/shared-upgrade-db

# 2. Both checkouts' local_settings.py point DATABASES at the SAME file:
#    /shared-db/upgrade-proof.sqlite3 (mounted from /tmp/shared-upgrade-db).
#    Otherwise identical to tubesync/tubesync/local_settings.py.example.

# 3. Run against the container image this app's own test suite uses
#    (all Pipfile deps pre-installed; see this app's README "Tests"
#    section for how that image is built).
docker run --rm \
  -v <fork-worktree>:/workspace-fork \
  -v /tmp/upstream-base-checkout:/workspace-base \
  -v /tmp/shared-upgrade-db:/shared-db \
  ts-bridge-test:latest bash -c '
    cp -a -t /usr/local/lib/python3.12/site-packages/yt_dlp/ /workspace-fork/patches/yt_dlp/*
    cp /path/to/shared-local-settings.py /workspace-base/tubesync/tubesync/local_settings.py
    cd /workspace-base/tubesync && python3 manage.py migrate --no-input

    cp /path/to/shared-local-settings.py /workspace-fork/tubesync/tubesync/local_settings.py
    cd /workspace-fork/tubesync
    python3 manage.py migrate --no-input --verbosity=2
    python3 manage.py makemigrations --check --dry-run --verbosity=2
    python3 manage.py check
  '

# 4. Cleanup (no destructive step against anything persistent):
git worktree remove /tmp/upstream-base-checkout
rm -rf /tmp/shared-upgrade-db
```

## Captured result (this exact run)

**Step 1 -- upstream-base checkout, fresh database:**
```
Operations to perform:
  Apply all migrations: admin, auth, common, contenttypes, sessions, sync
Running migrations:
  Applying contenttypes.0001_initial... OK
  [... 30 migrations total, all OK ...]
  Applying sync.0037_alter_source_fallback... OK
```
30 migrations applied, none failed. This is the "existing TubeSync
database" state the upgrade proof starts from.

**Step 2 -- same database file, codebase swapped to the fork HEAD
(`medianest_bridge` now in `INSTALLED_APPS`):**
```
Operations to perform:
  Apply all migrations: admin, auth, common, contenttypes, sessions, sync
Running pre-migrate handlers for application admin
[... pre-migrate handlers for every installed app, including django_huey ...]
Running migrations:
  No migrations to apply.
Running post-migrate handlers for application admin
[... post-migrate handlers for every installed app ...]
```
**"No migrations to apply."** -- the upgrade is a clean no-op at the
schema level. `medianest_bridge` appearing in `INSTALLED_APPS` triggers
Django's pre/post-migrate signal machinery for it like any other app, but
requires zero schema changes, because it defines zero models.

**Step 3 -- `makemigrations --check --dry-run` on the upgraded database's
codebase:**
```
No changes detected
```

**Step 4 -- `manage.py check` on the fork against the upgraded database:**
```
System check identified no issues (0 silenced).
```

## What this does and does not prove

Proves: the fork's `INSTALLED_APPS`/`urls.py`/`common/middleware.py`
changes (T1-T4's full diff) can be applied to a database that already
has every upstream migration applied, with zero migration operations,
zero errors, and a clean post-upgrade `check`/`makemigrations --check`.

Does not prove: anything about a real Docker image upgrade's *file
system* layer (image layer changes, volume permissions, s6-overlay
service definition changes) -- only the Django/database layer, which is
what "the fork adds zero migrations" is actually a claim about. A full
image-level upgrade rehearsal is out of scope for T4 (no destructive
steps, no live deployment, per this program's standing safety rules) and
would belong to whichever slice first does a real container upgrade
exercise.
