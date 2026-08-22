# Image-build proof (T5)

Three claims, verified separately:

1. The `Dockerfile`'s `ARG MEDIANEST_BRIDGE_UPSTREAM_SHA=""` /
   `ENV MEDIANEST_BRIDGE_UPSTREAM_SHA="${MEDIANEST_BRIDGE_UPSTREAM_SHA}"`
   wiring (the two lines this file's transcripts quote verbatim, extracted
   from the committed `Dockerfile` with `grep -n
   MEDIANEST_BRIDGE_UPSTREAM_SHA Dockerfile`, not retyped by hand) correctly
   plumbs a `--build-arg` into a running container's process environment,
   and correctly falls back to an empty string when the build-arg is
   omitted.
2. `medianest_bridge/config.py::upstream_sha()` and `GET /meta` correctly
   read that environment variable at request time and report it (or the
   honest `"unknown"` sentinel) -- proven against the real application
   code, over real HTTP, with a real bearer token.
3. A full end-to-end build of the actual production `Dockerfile` in this
   sandbox is **blocked** by a pre-existing, T5-unrelated environment
   limitation, documented below rather than worked around or silently
   skipped.

## What could NOT be proven, and exactly why

A full `docker buildx build` of the real `Dockerfile` (`linux/amd64`,
`--load`, with `IMAGE_NAME`, `FFMPEG_DATE=2026-08-20-17-12`,
`FFMPEG_VERSION=N-126229-gf101fce22d` -- the current stable
`yt-dlp/FFmpeg-Builds` release at the time this was run, fetched via `gh
release list -R yt-dlp/FFmpeg-Builds` -- and
`MEDIANEST_BRIDGE_UPSTREAM_SHA` set from `medianest_bridge/docs/UPSTREAM_SHA`)
reached stage 61 of 63+ before failing:

```
#61 [quickjs-extracted 2/2] RUN ... unzip "${f}" ... install -v -p -t /assimilated /extracted/qjs ; /assimilated/qjs --assimilate
#61 4.780 Archive:  /downloaded/quickjs-cosmo-2025-09-13.zip
#61 4.781   inflating: readme.txt
#61 4.781   inflating: run-test262
#61 4.801   inflating: qjs
#61 4.825 + install -v -p -t /assimilated /extracted/qjs
#61 4.830 '/extracted/qjs' -> '/assimilated/qjs'
#61 4.830 + /assimilated/qjs --assimilate
#61 4.831 /bin/sh: 1: /assimilated/qjs: not found
#61 ERROR: process "...install -v -p -t /assimilated /extracted/qjs ; /assimilated/qjs --assimilate" did not complete successfully: exit code: 127
```

The file exists, is installed with executable permissions (`install -v -p`
reports success), and the shell still reports "not found" -- exit code 127,
not a normal "command failed" exit. This is the textbook signature of a
[Cosmopolitan](https://github.com/jart/cosmopolitan) Actually Portable
Executable (APE) polyglot binary failing to exec: the file downloaded
(`quickjs-cosmo-2025-09-13.zip`, the "cosmo" build of QuickJS) is an APE
binary by design, and this sandbox's container runtime does not support
executing that format.

**Verified independently, not just inferred from the build failure**: a
throwaway `alpine:latest` container, unrelated to this `Dockerfile` or to
`medianest_bridge` entirely, downloading and `chmod +x`-ing the exact same
`qjs` binary and trying to run `./qjs --help` directly:

```
$ docker run --rm alpine:latest sh -c '
    apk add --no-cache --quiet curl unzip >/dev/null 2>&1
    cd /tmp
    curl -sL -o q.zip "https://bellard.org/quickjs/binary_releases/quickjs-cosmo-2025-09-13.zip"
    unzip -q q.zip
    chmod +x qjs
    ./qjs --help
  '
sh: ./qjs: not found

$ echo "WRAPPING SHELL EXIT: $?"
WRAPPING SHELL EXIT: 127
```

Identical symptom (`not found`) and identical exit code (127) as the real
build's failure. This confirms the limitation is this sandbox's container
runtime, not anything about the real `Dockerfile`, this fork's changes,
or T5's wiring -- every stage before
and after this one in dependency order (`tubesync-base`, `tubesync-asfald`,
`tubesync-openresty` through step 4/4, `tubesync-prepare-app` through all 7
steps including the `bgutil-ytdlp-pot-provider`/`ejs`/`yt-cipher` fetches,
`ffmpeg-download` through both steps with the real pinned FFmpeg release,
and even the final `tubesync` stage's own first apt-install step) built
successfully; only the QuickJS APE assimilation step failed.

**This blocks completing the full build+run+curl proof exactly as
requested in this sandbox.** It is not a T5 code defect, not something
this slice's Dockerfile edit caused, and not something safely fixable from
within a build step -- the underlying fix (registering the APE format with
the host kernel's `binfmt_misc`, if even possible in this sandbox) is a
host-level change outside a container build's control and outside this
task's scope to make unilaterally. Flagged for whoever runs this on an
unrestricted Linux build host, or once GitHub Actions is enabled (see
`.github/workflows/medianest-bridge-release.yaml`'s own header comment) --
GitHub's standard `ubuntu-latest` runners are not known to have this
limitation, so the fork's actual release builds are not expected to hit
this at all; it is specific to this development sandbox.

## What WAS proven, in place of the full build

### 1. The exact ARG/ENV mechanism, isolated

A minimal standalone `Dockerfile` containing only the exact two lines
added to the real `Dockerfile` (verified via `grep`, not retyped), on a
trivial `alpine:latest` base with no dependency on any of the stages
above:

```dockerfile
FROM alpine:latest
ARG MEDIANEST_BRIDGE_UPSTREAM_SHA=""
ENV MEDIANEST_BRIDGE_UPSTREAM_SHA="${MEDIANEST_BRIDGE_UPSTREAM_SHA}"
CMD ["sh", "-c", "printf -- 'MEDIANEST_BRIDGE_UPSTREAM_SHA=[%s]\\n' \"${MEDIANEST_BRIDGE_UPSTREAM_SHA}\""]
```

Built and run twice:

```
$ docker build --build-arg MEDIANEST_BRIDGE_UPSTREAM_SHA=3b9d72f28dda9c931776f76e7428a72a24a57f82 -t arg-env-proof:with-sha .
$ docker run --rm arg-env-proof:with-sha
MEDIANEST_BRIDGE_UPSTREAM_SHA=[3b9d72f28dda9c931776f76e7428a72a24a57f82]

$ docker build -t arg-env-proof:without-sha .
$ docker run --rm arg-env-proof:without-sha
MEDIANEST_BRIDGE_UPSTREAM_SHA=[]
```

Proves: a build-arg passed at build time reaches the running container's
process environment exactly as supplied; a build without that build-arg
falls back to the `ARG`'s empty-string default, exactly as documented.

### 2. The real application, over real HTTP, both ways

Using `ts-bridge-test:latest` (the same container image this fork's own
test suite runs in -- Python 3.12, all `Pipfile` dependencies installed,
the actual `medianest_bridge` code from this worktree bind-mounted in),
started with `manage.py runserver` and a real bearer token, both with and
without `MEDIANEST_BRIDGE_UPSTREAM_SHA` set in the container's environment:

**Case A -- `MEDIANEST_BRIDGE_UPSTREAM_SHA` set to the pinned upstream SHA:**

```
$ docker run -d --name bridge-proof-a \
    -e MEDIANEST_BRIDGE_TOKEN_FILE=/token/token.txt \
    -e MEDIANEST_BRIDGE_UPSTREAM_SHA=3b9d72f28dda9c931776f76e7428a72a24a57f82 \
    -p 18001:8000 ts-bridge-test:latest bash -c '... manage.py runserver 0.0.0.0:8000 --noreload'

$ curl -H "Authorization: Bearer <token>" http://127.0.0.1:18001/api/medianest/v1/meta
{"bridgeVersion": "1.0.0", "tubesyncVersion": "0.18.3", "upstreamCommit": "3b9d72f28dda9c931776f76e7428a72a24a57f82"}
HTTP_STATUS:200
```

**Case B -- `MEDIANEST_BRIDGE_UPSTREAM_SHA` not set at all (confirmed absent
via `docker exec ... env`):**

```
$ docker run -d --name bridge-proof-b \
    -e MEDIANEST_BRIDGE_TOKEN_FILE=/token/token.txt \
    -p 18002:8000 ts-bridge-test:latest bash -c '... manage.py runserver 0.0.0.0:8000 --noreload'

$ curl -H "Authorization: Bearer <token>" http://127.0.0.1:18002/api/medianest/v1/meta
{"bridgeVersion": "1.0.0", "tubesyncVersion": "0.18.3", "upstreamCommit": "unknown"}
HTTP_STATUS:200
```

Proves: the real, unmodified `medianest_bridge` code correctly reports the
env var when present and honestly reports `"unknown"` (never a fabricated
value) when absent -- over a real HTTP request/response cycle with a real
bearer token, not a Django test-client shortcut. (The existing
`tests/test_endpoints.py::MetaEndpointTestCase.test_upstream_commit_reflects_env_when_set`
and `.test_upstream_commit_defaults_to_unknown_sentinel` already covered
this at the test-client level as of T1/T4; this is the same claim verified
again over a real socket, for the specific purpose of this build proof.)

Note: these two runs required an ephemeral `ALLOWED_HOSTS = ['*']` addition
to this worktree's gitignored, untracked `tubesync/tubesync/local_settings.py`
(confirmed via `git check-ignore -v` before editing) purely so
`manage.py runserver` would start with `DEBUG` left at its production
default -- that file is never committed and carries no bearing on the
actual `Dockerfile`, the fork's committed settings, or any other code in
this PR.

## Combined, what this proves

Every piece of the `MEDIANEST_BRIDGE_UPSTREAM_SHA` -> `upstreamCommit`
chain that T5 actually changes -- the `Dockerfile`'s `ARG`/`ENV` wiring,
and the application code reading it -- is proven correct, for real, in
this sandbox. The one link this sandbox cannot exercise is the full
production `Dockerfile` building end-to-end at all, for a reason
independently confirmed to be a pre-existing sandbox limitation unrelated
to this change.
