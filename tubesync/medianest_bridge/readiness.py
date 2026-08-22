'''
    Per-component readiness checks for GET /health/ready.

    The contract (bridge-openapi.v1.yaml, ComponentStatus) is explicit: a
    component the bridge cannot cheaply/honestly verify MUST report
    "unknown", never a fabricated "healthy". This module follows that rule
    literally.

    T4 turned `workers` and `queues` from T1's honest-unknown into real
    checks, using a pattern this fork already establishes for itself:
    `healthcheck.py` (the container's own Docker HEALTHCHECK script)
    already queries s6-overlay's live service state via
    `s6-svstat -o pid /run/service/<name>` (see its get_service_pid()) and
    already manages a per-service `down` file at
    `/run/service/<name>/down` to pause huey-net-limited when yt-dlp is
    stale. The `queues` check reads the supervisor's live `wantedup`
    state (same s6-svstat field sync/views/services.py::S6OverlayReporter
    already uses) because stop-queue.py brings consumers down via
    `s6-svc -D`, which does not create a persistent `down` marker --
    inspecting only the down file would miss the real administrative-pause
    flow. A present `down` file is still counted when it exists. Both
    signals are genuinely observable from *this* process too:
    every huey consumer runs as an s6 longrun service inside the same
    container as gunicorn (config/root/etc/s6-overlay/s6-rc.d/huey-*), so
    `/run/service/` is on the same filesystem this app already reads.
    When that directory doesn't exist at all (dev/test environments,
    `manage.py runserver`, this app's own test suite -- none of which run
    under s6-overlay), both checks honestly fall back to "unknown" rather
    than fabricating a status for a precondition that doesn't hold.

    `youtube` is now a real probe (T-side follow-up to M6b's deployment
    work). T4 left this "unknown" pending confirmation of the production
    deployment's VPN/network topology -- that confirmation has since
    landed and closes DECISIONS #29: under the chosen wiring
    (`network_mode: service:gluetun` plus a LAN carve-out on
    `FIREWALL_OUTBOUND_SUBNETS`, documented in the M6b deployment/routing
    doc), the bridge web process genuinely shares yt-dlp's VPN egress for
    all internet-bound traffic -- a reachability probe from this process
    IS now a valid proxy for yt-dlp's own reachability, which is the
    exact precondition T4's version of this check said was missing.

    The probe: a single `GET` to `https://www.youtube.com/generate_204`
    (Google's own purpose-built connectivity-check convention, the same
    kind used for `www.gstatic.com/generate_204` elsewhere) -- no auth,
    no cookies, no custom headers, no yt-dlp invocation, no cookie-file
    involvement (this app's own test asserts no `headers`/`cookies`/`auth`
    kwarg is ever passed). Verified live with the *exact* request this
    code sends -- default `requests` User-Agent, no overrides, run from
    inside `ts-bridge-test:latest` (the same environment this app's own
    tests run in): a zero-byte HTTP 204 in ~50ms, no redirect/consent-wall
    concern (none observed) that a content page would risk. Response
    detail records only the outcome (HTTP status or exception class name,
    never the full exception message, which for `requests` can embed the
    request URL) and latency -- nothing else is logged or returned.

    Bounded by `requests`' own native `timeout=` parameter rather than
    `_call_with_timeout()`'s `ThreadPoolExecutor` wrapper: unlike
    `SELECT 1` or `statvfs()`, `requests.get()` already has a reliable
    timeout with no "blocks in the kernel forever" failure mode to work
    around, so wrapping it in that pattern too would be redundant rather
    than defensive. Stated precisely, not glossed over: `timeout=2`
    applies to the connect phase and the read phase *separately* (this is
    `requests`/`urllib3`'s own documented semantics, not a bug here), so
    the worst-case wall time for one probe call is closer to ~4s than a
    strict 2s ceiling -- looser than `_call_with_timeout`'s true
    total-wall-time bound, but still fully bounded (fires at most once
    per `_YOUTUBE_CACHE_TTL_SECONDS`, cached, can never hang
    indefinitely), which is what actually matters for `/health/ready`
    never blocking on this check.

    Cached under its own `_YOUTUBE_CACHE_TTL_SECONDS` (120s), separate
    from `collect_components()`'s shared 5s TTL: an external network
    probe against a third party is a different cost/frequency tradeoff
    than a local syscall -- 120s is frequent enough that a real VPN
    egress loss surfaces within about two polling cycles for anything
    watching `/health/ready`, infrequent enough not to look like scraping
    traffic against YouTube's own infrastructure. `check_youtube()`
    manages this cache internally (its own module-level dict), so
    `collect_components()`'s generic per-check loop and its own 5s cache
    are completely unaffected -- this is a one-function, additive change,
    not a restructuring of the shared cache.

    Enabled by default (`MEDIANEST_BRIDGE_YOUTUBE_PROBE_ENABLED`,
    defaulting to `true`, same fail-toward-default parsing as
    `config.py::is_read_only()`) because the M6b production topology is
    the shared-egress wiring this probe's meaning depends on. Cleanly
    disableable for any deployment that does NOT use that wiring, where a
    "youtube unreachable" result would actually be reporting on the wrong
    network path entirely -- disabling reports `not_configured` (an
    operator-controlled absence, matching `check_cookies()`'s existing
    precedent), not `unknown` (which would incorrectly imply this fork
    still can't determine the answer).

    No change to `aggregate_status()` was needed for this: only
    `database`/`application` reporting `unavailable` escalates the
    *overall* status to `unavailable` (see the aggregation rule below);
    every other component's `degraded`/`unavailable` -- `youtube`
    included -- already maps to overall `degraded` only, which is exactly
    "the bridge itself still works for reads even if YouTube is
    unreachable," the correct signal here.

    `plex` stays "unknown"/"not_configured" as in T1 -- reachability was
    out of T1's scope and remains out of T4's (no new endpoints, per the
    T4 brief).

    Failure isolation: every check function is called through
    _run_check(), which converts an unexpected exception into "unknown"
    (never lets one check's bug crash the whole /health/ready response)
    and every check that shells out has an explicit subprocess timeout.
    Results are cached for _CACHE_TTL_SECONDS per process (module-level,
    not a shared cache backend -- this fork has no CACHES setting and
    T4 adds no new dependency to provide one; gunicorn's sync worker
    class means each worker process's cache is independent, which still
    substantially bounds work under a polling load even though it isn't
    perfectly deduplicated across workers) so repeated readiness polling
    can't force a subprocess call or disk stat on every single request.

    Aggregation rule (contract-silent; documented here rather than invented
    silently, per the T1 take-over instructions):
      - database (or application) unavailable -> overall "unavailable"
      - any checked (non-unknown, non-not_configured) component
        degraded/unavailable -> overall "degraded"
      - "unknown"/"not_configured" components never worsen the overall
        status -- otherwise this endpoint would report permanently
        degraded outside a real s6-overlay deployment, which is not an
        honest signal either.
'''
import os
import shutil
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError

import requests

_SUBPROCESS_TIMEOUT_SECONDS = 2
_BLOCKING_CALL_TIMEOUT_SECONDS = 2
_CACHE_TTL_SECONDS = 5

# Shared, bounded executor for database/storage checks. A per-call executor
# with shutdown(wait=False) leaked one orphaned thread on every timeout;
# under sustained readiness polling against a dead mount that unboundedly
# accumulated blocked threads. Two workers cap the stuck-thread count at
# the number of blocking checks this module runs (database + storage) while
# still bounding each caller's wait via Future.result(timeout=...).
_blocking_call_executor = ThreadPoolExecutor(
    max_workers=2,
    thread_name_prefix='medianest_bridge_readiness',
)

# youtube probe: see this module's docstring for the full rationale.
_YOUTUBE_PROBE_URL = 'https://www.youtube.com/generate_204'
_YOUTUBE_PROBE_TIMEOUT_SECONDS = 2
_YOUTUBE_CACHE_TTL_SECONDS = 120


def _call_with_timeout(fn, *, timeout=_BLOCKING_CALL_TIMEOUT_SECONDS):
    '''
        Bounds a blocking call (a DB query, a disk stat) that has no
        native Python timeout parameter -- unlike the subprocess calls
        elsewhere in this module, a plain syscall such as statvfs() on a
        dead network mount can block in the kernel for a very long time,
        uninterruptible by ordinary Python-level mechanisms. Running it
        on the module-level bounded executor and bounding *this* call's
        wait via Future.result(timeout=...) bounds the caller's wait even
        though the underlying thread may itself remain stuck forever (at
        most two such threads process-wide, not one new leak per poll).

        Raises FutureTimeoutError on timeout; callers decide what status
        that maps to (see check_database/check_storage below).

        Deliberately does NOT use `with ThreadPoolExecutor(...) as executor:`
        -- that context manager's __exit__ calls shutdown(wait=True),
        which blocks until the submitted thread finishes, defeating the
        entire point of the timeout the moment the wrapped call actually
        hangs. The shared executor is never shut down from this path.
    '''
    future = _blocking_call_executor.submit(fn)
    try:
        return future.result(timeout=timeout)
    except FutureTimeoutError:
        future.cancel()
        raise

# The four huey consumer services this fork ships
# (config/root/etc/s6-overlay/s6-rc.d/), matching sync/choices.py's
# TaskQueue values via their queue-name suffix.
HUEY_SERVICE_NAMES = (
    'huey-database', 'huey-filesystem', 'huey-net-limited', 'huey-network',
)


def _status(status, *, version=None, detail=None):
    return {'status': status, 'version': version, 'detail': detail}


def _run_check(check_fn):
    try:
        return check_fn()
    except Exception:
        return _status('unknown', detail='check raised an unexpected exception')


def check_application():
    # Reaching this function at all means the WSGI app, URL routing, and
    # this view's own dispatch gating all worked.
    return _status('healthy')


def _select_1():
    from django.db import connection
    with connection.cursor() as cursor:
        cursor.execute('SELECT 1')
        cursor.fetchone()


def check_database():
    # T4 verifier LOW: bounded the same way workers/queues' subprocess
    # calls already are. A plain SELECT 1 is normally instant, but
    # "normally" stops holding the moment DOWNLOAD_ROOT-style network
    # storage concerns apply to the DB connection too (a remote
    # Postgres/MySQL backend per common/utils.py's
    # parse_database_connection_string(), not just SQLite) -- an
    # unresponsive network DB should report unavailable within
    # _BLOCKING_CALL_TIMEOUT_SECONDS, not hang this endpoint.
    try:
        _call_with_timeout(_select_1)
    except FutureTimeoutError:
        return _status('unavailable', detail='database query timed out')
    except Exception:
        return _status('unavailable', detail='database query failed')
    return _status('healthy')


def _s6_service_pid(service_name):
    '''
        Reproduces healthcheck.py::get_service_pid()'s exact approach
        (this app does not import that standalone script -- it isn't
        part of the Django app -- but deliberately mirrors its command
        and path convention rather than inventing a different one).
        Returns an int PID, or None if the service directory doesn't
        exist, s6-svstat isn't available, or the call fails/times out.
    '''
    path = os.path.join('/run/service', service_name)
    if not os.path.isdir(path):
        return None
    try:
        output = subprocess.check_output(
            ['/command/s6-svstat', '-o', 'pid', path],
            timeout=_SUBPROCESS_TIMEOUT_SECONDS,
            stderr=subprocess.DEVNULL,
        )
        return int(output.strip())
    except Exception:
        return None


def _s6_service_wanted_up(service_name):
    '''
        Returns True/False for s6's wantedup flag, or None when the
        service directory is absent or s6-svstat fails. Mirrors
        sync/views/services.py::S6OverlayReporter's is_wanted_up field
        without importing that view-layer module into readiness.
    '''
    path = os.path.join('/run/service', service_name)
    if not os.path.isdir(path):
        return None
    try:
        output = subprocess.check_output(
            ['/command/s6-svstat', '-o', 'wantedup', path],
            timeout=_SUBPROCESS_TIMEOUT_SECONDS,
            stderr=subprocess.DEVNULL,
        )
        return output.strip() == b'true'
    except Exception:
        return None


def _s6_root_present():
    return os.path.isdir('/run/service')


def check_workers():
    if not _s6_root_present():
        return _status(
            'unknown',
            detail='not running under s6-overlay (/run/service absent) -- '
                   'expected outside the container image',
        )
    up = []
    down = []
    for name in HUEY_SERVICE_NAMES:
        pid = _s6_service_pid(name)
        # A PID of 0 (or the call failing) means s6 does not consider the
        # service up; s6-svstat reports 0 for a down/not-yet-started
        # service rather than raising.
        (up if pid else down).append(name)
    if not down:
        return _status('healthy', detail=f'{len(up)}/{len(HUEY_SERVICE_NAMES)} huey consumers running')
    if not up:
        return _status('unavailable', detail=f'no huey consumers running: {", ".join(down)}')
    return _status(
        'degraded',
        detail=f'{len(up)}/{len(HUEY_SERVICE_NAMES)} huey consumers running; down: {", ".join(down)}',
    )


def check_queues():
    if not _s6_root_present():
        return _status(
            'unknown',
            detail='not running under s6-overlay (/run/service absent) -- '
                   'expected outside the container image',
        )
    paused = []
    for name in HUEY_SERVICE_NAMES:
        wanted_up = _s6_service_wanted_up(name)
        if wanted_up is False:
            paused.append(name)
            continue
        if os.path.exists(os.path.join('/run/service', name, 'down')):
            paused.append(name)
    if not paused:
        return _status('healthy', detail='no queue is administratively paused')
    return _status(
        'degraded',
        detail=f'administratively paused: {", ".join(paused)}',
    )


def check_yt_dlp():
    try:
        import yt_dlp
        version = getattr(yt_dlp, 'version', None)
        version = getattr(version, '__version__', None) if version else None
    except ImportError:
        return _status('unavailable', detail='yt_dlp module not importable')
    return _status('healthy', version=version)


def check_ffmpeg():
    path = shutil.which('ffmpeg')
    if not path:
        return _status('unavailable', detail='ffmpeg not found on PATH')
    try:
        result = subprocess.run(
            ['ffmpeg', '-version'],
            capture_output=True,
            text=True,
            timeout=_SUBPROCESS_TIMEOUT_SECONDS,
            check=False,
        )
    except Exception:
        return _status('unavailable', detail='ffmpeg version probe failed')
    if result.returncode != 0:
        return _status('unavailable', detail='ffmpeg version probe exited nonzero')
    first_line = (result.stdout or '').splitlines()[0] if result.stdout else None
    if not first_line:
        return _status('unavailable', detail='ffmpeg version probe produced no output')
    return _status('healthy', version=first_line)


def _storage_thresholds():
    '''
        Configurable via env vars (not contract fields -- purely an
        operational tuning knob, same pattern as the other
        MEDIANEST_BRIDGE_* settings in config.py). Defaults are
        conservative round numbers, not derived from any measured
        workload -- an operator with a better sense of their own disk
        growth rate should override them.
    '''
    from common.utils import getenv
    warn = getenv('MEDIANEST_BRIDGE_STORAGE_WARN_BYTES', 5 * 1024 ** 3, integer=True)  # 5 GiB
    critical = getenv('MEDIANEST_BRIDGE_STORAGE_CRITICAL_BYTES', 1 * 1024 ** 3, integer=True)  # 1 GiB
    return warn, critical


def _stat_download_root():
    '''
        The actual blocking syscalls (exists/access/disk_usage -- all
        stat-family calls) bundled into one unit so a single
        _call_with_timeout() bounds all of them together, not just
        disk_usage alone. Returns (exists, writable, usage_or_None).
    '''
    from django.conf import settings
    root = settings.DOWNLOAD_ROOT
    exists = root.exists()
    if not exists:
        return False, False, None
    writable = os.access(root, os.W_OK)
    if not writable:
        return True, False, None
    try:
        usage = shutil.disk_usage(root)
    except OSError:
        usage = None
    return True, True, usage


def check_storage():
    # T4 verifier LOW: M6 may put DOWNLOAD_ROOT on a network mount: an
    # unresponsive/dead NFS mount can block a stat-family syscall
    # (exists/access/disk_usage) in the kernel for a very long time.
    # Bounded the same way check_database() now is.
    try:
        exists, writable, usage = _call_with_timeout(_stat_download_root)
    except FutureTimeoutError:
        return _status('unavailable', detail='DOWNLOAD_ROOT stat timed out')
    except OSError:
        return _status('unavailable', detail='DOWNLOAD_ROOT could not be statted')
    if not exists:
        return _status('unavailable', detail='DOWNLOAD_ROOT does not exist')
    if not writable:
        return _status('unavailable', detail='DOWNLOAD_ROOT is not writable')
    if usage is None:
        return _status(
            'healthy', detail='DOWNLOAD_ROOT is writable; free space could not be determined',
        )
    warn_bytes, critical_bytes = _storage_thresholds()
    detail = f'free_bytes={usage.free}'
    if usage.free <= critical_bytes:
        return _status('unavailable', detail=f'{detail} (<= critical threshold {critical_bytes})')
    if usage.free <= warn_bytes:
        return _status('degraded', detail=f'{detail} (<= warn threshold {warn_bytes})')
    return _status('healthy', detail=detail)


_youtube_cache = {'expires_at': 0.0, 'component': None}


def _youtube_probe_enabled():
    from common.utils import getenv
    return 'false' != getenv('MEDIANEST_BRIDGE_YOUTUBE_PROBE_ENABLED', 'true').strip().lower()


def _probe_youtube():
    '''
        The actual network call, isolated from check_youtube()'s caching
        and enabled/disabled logic so each is independently testable.
        Records only an HTTP status or an exception CLASS NAME (never
        `str(exc)`, which for `requests` exceptions commonly embeds the
        full request URL) plus latency -- nothing else about the request
        or failure is ever included in the returned detail.

        allow_redirects=False, deliberately: `requests` defaults to
        following redirects, which would silently resolve any 3xx to its
        final destination before this function ever saw the 3xx status --
        the `200 <= status_code < 400` check below would then only ever
        observe the chain's last response, not the bare 3xx a redirecting
        generate_204 would actually return. generate_204 isn't expected
        to redirect at all, but a bare 3xx already proves reachability
        (all this probe measures) without paying for a redirect chain's
        extra requests and latency on top of it -- caught in review
        before this shipped, not after.

        timeout=_YOUTUBE_PROBE_TIMEOUT_SECONDS applies to the connect and
        read phases SEPARATELY (requests/urllib3's own documented
        semantics, not a bug here) -- worst-case wall time for one call
        is closer to ~4s than a strict 2s ceiling. Still fully bounded
        (this function is called at most once per
        _YOUTUBE_CACHE_TTL_SECONDS, cached, never on every request) --
        see this module's docstring for why that's an acceptable, not
        merely tolerated, tradeoff versus _call_with_timeout's true
        total-wall-time bound.
    '''
    start = time.monotonic()
    try:
        response = requests.get(
            _YOUTUBE_PROBE_URL,
            timeout=_YOUTUBE_PROBE_TIMEOUT_SECONDS,
            allow_redirects=False,
        )
    except requests.exceptions.RequestException as exc:
        latency_ms = round((time.monotonic() - start) * 1000, 1)
        return _status(
            'unavailable',
            detail=f'{type(exc).__name__} after {latency_ms}ms',
        )
    latency_ms = round((time.monotonic() - start) * 1000, 1)
    if 200 <= response.status_code < 400:
        return _status('healthy', detail=f'HTTP {response.status_code} in {latency_ms}ms')
    return _status('unavailable', detail=f'HTTP {response.status_code} in {latency_ms}ms')


def check_youtube():
    if not _youtube_probe_enabled():
        return _status(
            'not_configured',
            detail='MEDIANEST_BRIDGE_YOUTUBE_PROBE_ENABLED=false -- probe '
                   'disabled by operator (correct for any deployment not '
                   'using the shared-egress-namespace wiring; see this '
                   'module\'s docstring)',
        )
    now = time.monotonic()
    if _youtube_cache['component'] is not None and _youtube_cache['expires_at'] > now:
        return _youtube_cache['component']
    component = _probe_youtube()
    _youtube_cache['component'] = component
    _youtube_cache['expires_at'] = now + _YOUTUBE_CACHE_TTL_SECONDS
    return component


def check_cookies():
    from django.conf import settings
    cookies_file = settings.COOKIES_FILE
    try:
        present = cookies_file.exists()
    except OSError:
        present = False
    if present:
        # Contents are never inspected or reported -- presence only.
        return _status('healthy', detail='cookies file present')
    return _status('not_configured', detail='no cookies file configured')


def check_plex():
    try:
        from sync.choices import MediaServerType, Val
        from sync.models import MediaServer
        configured = MediaServer.objects.filter(
            server_type=Val(MediaServerType.PLEX),
        ).exists()
    except Exception:
        return _status('unknown', detail='plex configuration could not be queried')
    if not configured:
        return _status('not_configured', detail='no Plex media server configured')
    return _status(
        'unknown',
        detail='a Plex media server is configured; reachability is not checked',
    )


CHECKS = {
    'application': check_application,
    'database': check_database,
    'queues': check_queues,
    'workers': check_workers,
    'ytDlp': check_yt_dlp,
    'ffmpeg': check_ffmpeg,
    'storage': check_storage,
    'youtube': check_youtube,
    'cookies': check_cookies,
    'plex': check_plex,
}

# Components whose degraded/unavailable status affects overall aggregation.
# "unknown" and "not_configured" never worsen the overall status (see
# module docstring).
_AGGREGATING_STATUSES = {'degraded', 'unavailable'}

_cache = {'expires_at': 0.0, 'components': None}


def _reset_cache():
    '''
        Test-only: forces the next collect_components() call to recompute,
        including the youtube probe's own separately-TTL'd cache (see
        check_youtube() -- it isn't swept up by the shared _cache dict
        above, so it needs resetting here too or a test could observe a
        stale probe result left over from an earlier test).
    '''
    _cache['components'] = None
    _cache['expires_at'] = 0.0
    _youtube_cache['component'] = None
    _youtube_cache['expires_at'] = 0.0


def collect_components():
    now = time.monotonic()
    if _cache['components'] is not None and _cache['expires_at'] > now:
        return _cache['components']
    components = {name: _run_check(check) for name, check in CHECKS.items()}
    _cache['components'] = components
    _cache['expires_at'] = now + _CACHE_TTL_SECONDS
    return components


def aggregate_status(components):
    if components.get('database', {}).get('status') == 'unavailable':
        return 'unavailable'
    if components.get('application', {}).get('status') == 'unavailable':
        return 'unavailable'
    for component in components.values():
        if component.get('status') in _AGGREGATING_STATUSES:
            return 'degraded'
    return 'healthy'
