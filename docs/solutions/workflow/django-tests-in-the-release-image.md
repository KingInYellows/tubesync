---
title: 'Run the Django suite in the release image without its s6 entrypoint'
date: 2026-09-25
category: workflow
track: knowledge
problem:
  'there is no local Django environment for this fork, and the image''s default
  entrypoint chowns and chmods /app, which wrecks a bind-mounted checkout'
tags: [testing, docker, s6-overlay, ruff, ci]
components: ['tubesync/tubesync/local_settings.py.container', '.github/workflows/ci.yaml']
---

## Context

CI runs `manage.py test` on a prepared runner. Locally there is no pipenv
environment, but the published image (`ghcr.io/kinginyellows/tubesync:bridge-v1.0.0`)
has every dependency. Its default s6-overlay entrypoint runs
`chown -R root:app /app && chmod -R 0750 /app`. On a bind-mounted checkout that
leaves the working tree root-owned and unreadable to the developer.

## Guidance

From a checkout root:

```bash
cp tubesync/tubesync/local_settings.py.container tubesync/tubesync/local_settings.py
docker run --rm --entrypoint /usr/bin/python3 \
  -v "$PWD/tubesync:/app" -v "$SCRATCH/tsconfig:/config" \
  -v "$SCRATCH/tsdownloads:/downloads" -w /app \
  ghcr.io/kinginyellows/tubesync:bridge-v1.0.0 \
  manage.py test sync medianest_bridge --verbosity=1
rm -f tubesync/tubesync/local_settings.py        # gitignored; never commit it
find . -path ./.git -prune -o ! -user "$(whoami)" -print   # must print nothing
```

- Always pass `--entrypoint /usr/bin/python3`.
- Use scratch directories for `/config` and `/downloads`, never real ones.
- Wrap the steps in a script that removes `local_settings.py` even when the run
  is interrupted. An interrupted run otherwise leaves it behind.

Lint with CI's exact rule set (from `.github/workflows/ci.yaml`):

```bash
ruff check --isolated --target-version py312 --select 'C4,E4,E7,E9,F' \
  --ignore 'C408,C409,C410,E701,E722,E731,I001,UP017,UP018'
```

`F` includes **F402**. Modules that import `gettext_lazy as _` must never use
`_` as a loop variable (`for _, field, spec, _ in ...`). Inside a
comprehension it is harmless, but in a plain `for` loop it fails CI.

## Why This Matters

The chown is silent and only shows up later, as permission errors in git or
the editor. The F402 trap passes a local `python -c` smoke test and fails only
in CI.

## When to Apply

Every time you run the fork's tests or lint outside CI.

## Examples

The full suite runs in about 20 to 45 seconds. Test labels can be narrowed
(`sync.tests.test_episode_numbering`, or a single `TestCase` class) for fast
iteration.
