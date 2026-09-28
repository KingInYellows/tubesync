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

Save this as a script and run it from a checkout root:

```bash
#!/usr/bin/env bash
set -euo pipefail
settings=tubesync/tubesync/local_settings.py   # gitignored; never commit it
if [ -e "$settings" ]; then
  echo "$settings already exists; move it aside first" >&2
  exit 1
fi
# Installed before the copy, so a failure or interrupt from here on still
# removes it. The check above makes sure it is only ever our own copy.
trap 'rm -f "$settings"' EXIT
scratch="${SCRATCH:-$(mktemp -d)}"   # never the real /config or /downloads
mkdir -p "$scratch/tsconfig" "$scratch/tsdownloads"
cp tubesync/tubesync/local_settings.py.container "$settings"
# CI copies local_settings.py.example, which ends with DEBUG = False.
printf '\nDEBUG = False\n' >> "$settings"
status=0
# TUBESYNC_DEBUG=True as in CI; settings.py reads it for DJANGO_HUEY and
# LOGGING. "|| status=$?" keeps set -e from exiting before the check below.
docker run --rm --entrypoint /usr/bin/python3 -e TUBESYNC_DEBUG=True \
  -v "$PWD/tubesync:/app" -v "$scratch/tsconfig:/config" \
  -v "$scratch/tsdownloads:/downloads" -w /app \
  ghcr.io/kinginyellows/tubesync:bridge-v1.0.0 \
  manage.py test --no-input --buffer --verbosity=1 || status=$?
# find's own errors (an unreadable directory) are reported as foreign too.
foreign=$(find . -path ./.git -prune -o ! -user "$(whoami)" -print 2>&1 || true)
if [ -n "$foreign" ]; then
  printf 'files not owned by you:\n%s\n' "$foreign" >&2
  if [ "$status" -eq 0 ]; then status=1; fi
fi
exit "$status"   # the test run's own status when it failed
```

- Always pass `--entrypoint /usr/bin/python3`.
- Match CI's settings. `.github/workflows/ci.yaml` runs with
  `TUBESYNC_DEBUG=True`, which `settings.py` reads while building
  `DJANGO_HUEY` and `LOGGING`, and then copies `local_settings.py.example`,
  which sets `DEBUG = False` at the end. The script does both. It still
  copies `local_settings.py.container`, because `.example` puts the config
  and download directories inside the checkout, where the container's root
  user would leave root-owned files. The one remaining difference is
  `.example`'s own `LOGGING`.
- Use scratch directories for `/config` and `/downloads`, never real ones.
- `manage.py test` with no labels runs every installed app, `common`
  included, as CI does. `manage.py test sync medianest_bridge` is a narrower
  run that skips `common`.
- The script refuses to overwrite an existing `local_settings.py` and deletes
  only the copy it made.

Lint with CI's exact rule set (from `.github/workflows/ci.yaml`), from the
`tubesync/` directory as CI does. From the checkout root, ruff also scans
`patches/yt_dlp/` and reports F821 errors that CI never sees.

```bash
cd tubesync && ruff check --target-version py312 \
  --select 'C4,E4,E7,E9,F' \
  --ignore 'C408,C409,C410,E701,E722,E731,I001,UP017,UP018'
```

Do not add ruff's own `--isolated`. In CI, `--isolated` is a `uvx` option
(`uvx ... --isolated ruff check`) that isolates the tool's environment, and
ruff still reads `tubesync/ruff.toml`, including its Python 3.10 target for
`shasum.py`. After `ruff check`, `--isolated` makes ruff ignore that file.

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
