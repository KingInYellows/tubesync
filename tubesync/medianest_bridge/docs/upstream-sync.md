# Upstream-sync process (T5)

This fork (`KingInYellows/tubesync`) tracks `meeb/tubesync` as its
`upstream` remote (read-only -- push is disabled on that remote by
convention in every worktree this program uses). This document is the
maintained-fork workflow for keeping the bridge branch chain current
against upstream without losing the fork delta's traceability. It is
written to be operational: follow it step by step, not as a description of
a process that exists only in principle.

## Before syncing: know what "the fork delta" actually is

Everything the bridge needs lives in `medianest_bridge/` plus five
explicitly enumerated upstream touch points (see
`medianest_bridge/README.md`'s "Fork delta" section for the exact
files/lines -- not repeated here, so this doc can't drift out of sync with
that one). A sync that touches anything outside that list has, by
definition, either picked up an unrelated upstream change (expected and
fine) or introduced a merge conflict against one of those five files
(expected occasionally, and the specific reason this fork's delta is kept
deliberately small -- a five-file, mostly-few-line delta is cheap to
re-apply by hand if a merge conflicts).

## Step 1 -- fetch upstream

```bash
git fetch upstream
git log --oneline upstream/main -10   # sanity-check what's new
```

## Step 2 -- decide merge vs. rebase, per branch

- **The trunk-tracking branch** (whatever branch in this fork mirrors
  `upstream/main`, typically this fork's own `main`): merge, not rebase.
  `git merge upstream/main`. This is the only branch that should ever
  directly incorporate upstream's history; every bridge branch descends
  from a specific *pinned* commit on this branch (see
  `medianest_bridge/docs/UPSTREAM_SHA`), not from a moving target.
- **The bridge branch chain** (`feat/medianest-bridge-foundation` through
  `feat/medianest-bridge-release` and beyond): do **not** rebase these
  onto a newer upstream commit as a matter of routine. They are stacked
  PRs against each other, not against upstream directly, and a rebase of
  an already-reviewed stack risks invalidating verifier sign-off recorded
  against specific commit SHAs (see, e.g., PR #4's fix-up commit
  `f79944f1`, verified against that exact SHA). Only rebase the stack onto
  a new upstream base as a deliberate, single, documented step (Step 4
  below) -- not silently, not as a side effect of an unrelated change.

## Step 3 -- re-pin `UPSTREAM_SHA` only when actually re-basing the stack

`medianest_bridge/docs/UPSTREAM_SHA` must always name the commit the
*currently shipping* bridge branch chain is actually built on -- not
merely the newest commit upstream happens to have. Update it in the same
commit that rebases/re-bases the stack, never separately:

```bash
git rev-parse upstream/main > tubesync/medianest_bridge/docs/UPSTREAM_SHA
printf -- '\n' >> tubesync/medianest_bridge/docs/UPSTREAM_SHA   # keep the trailing newline
```

Every other place that needs this value -- the release workflow's
build-arg, the compatibility matrix, the migration-upgrade-proof
procedure -- reads this one file (see
`medianest_bridge/docs/compatibility-matrix.md`, "Upstream base"). Nothing
else should ever hardcode the literal SHA; if a `grep -rn
'3b9d72f2\|<new-sha>'` after a sync finds a second hardcoded occurrence
outside this file, that's a bug in this doc's own discipline to fix, not
an acceptable second source of truth.

## Step 4 -- re-run the migration-upgrade rehearsal

A re-pin changes what "the fork adds zero migrations against an
upstream-migrated database" actually claims to be true of. Re-run
`medianest_bridge/docs/migration-upgrade-proof.md`'s reproducible
procedure end to end against the *new* pinned commit (the procedure
already reads the upstream-base commit as a parameter -- update it to the
new `UPSTREAM_SHA` value, per Step 3, before running) and replace that
document's "Captured result" section with the new run's actual output.
Do not carry forward the old captured output under a new claimed SHA --
if the procedure hasn't actually been re-run against the new pin, the doc
must say so plainly rather than imply it has.

## Step 5 -- how contract re-vendoring interacts with a sync

The bridge contract (`medianest_bridge/contract/bridge-openapi.v1.yaml`)
is vendored from the *MediaNest* repository, entirely independent of
`meeb/tubesync` upstream syncs -- an upstream TubeSync sync never touches
the contract, and a contract re-vendor never touches
`medianest_bridge/docs/UPSTREAM_SHA`. These are two unrelated pins (see
`medianest_bridge/docs/compatibility-matrix.md`, sections 1 and 3); keep
them that way. A contract re-vendor follows the existing process already
documented in `medianest_bridge/README.md`'s "Contract" section -- fetch
the new YAML from its canonical MediaNest-repo source, replace
`contract/bridge-openapi.v1.yaml`, regenerate `contract_fixtures.json`,
run the full suite (`test_contract_conformance.py` will fail loudly on
any accidental schema drift the fixture regeneration didn't account for).

## Step 6 -- gates before pushing a synced branch

Same gates as every other change in this program (see
`medianest_bridge/docs/migration-upgrade-proof.md` and this program's own
`AGENTS.md`/`CLAUDE.md` for the exact commands): full container test
suite, `ruff check` with CI's exact flags, `py_compile`, `manage.py
check`, `manage.py makemigrations --check --dry-run`. A sync that changes
`UPSTREAM_SHA` should show a **passing** migration-upgrade rehearsal
(Step 4) as part of this gate set, not as an optional follow-up.

## What a sync does *not* need to touch

`.github/workflows/medianest-bridge-release.yaml` (T5) is written
specifically to survive a sync untouched: it reads
`medianest_bridge/docs/UPSTREAM_SHA` at workflow run time rather than
hardcoding the SHA, and it is a wholly fork-owned file with no
counterpart in `meeb/tubesync` for `git merge`/`git rebase` to conflict
against. If a future upstream commit happens to add a same-named workflow
file, that is the one collision this design does not protect against --
extremely unlikely given the name is namespaced with `medianest-bridge-`,
but worth knowing rather than assuming impossible.
