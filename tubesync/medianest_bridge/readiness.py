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
    stale. Both signals are genuinely observable from *this* process too:
    every huey consumer runs as an s6 longrun service inside the same
    container as gunicorn (config/root/etc/s6-overlay/s6-rc.d/huey-*), so
    `/run/service/` is on the same filesystem this app already reads.
    When that directory doesn't exist at all (dev/test environments,
    `manage.py runserver`, this app's own test suite -- none of which run
    under s6-overlay), both checks honestly fall back to "unknown" rather
    than fabricating a status for a precondition that doesn't hold.

    `youtube` stays "unknown" -- deliberately, not by omission. A
    reachability probe from this process would only be a valid proxy for
    yt-dlp's own reachability if this process shares yt-dlp's actual
    egress path, including any VPN routing (ADR-0006 describes a Gluetun
    VPN boundary on the production DOCKERDOWNLOAD host). This repository
    has zero references to Gluetun/VPN anywhere (checked directly --
    `grep -rli 'gluetun\\|vpn'` across the whole tree, no matches); that
    topology is MediaNest-side deployment infrastructure this repo cannot
    see. Baking in either assumption (shares egress / doesn't) would be
    exactly the "VPN egress assumption" this check was told to avoid, so
    it stays unknown until the deployment topology is confirmed
    elsewhere -- flagged in the T4 PR body as a question for the
    deployment docs, not decided silently here.

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

_SUBPROCESS_TIMEOUT_SECONDS = 2
_BLOCKING_CALL_TIMEOUT_SECONDS = 2
_CACHE_TTL_SECONDS = 5


def _call_with_timeout(fn, *, timeout=_BLOCKING_CALL_TIMEOUT_SECONDS):
    '''
        Bounds a blocking call (a DB query, a disk stat) that has no
        native Python timeout parameter -- unlike the subprocess calls
        elsewhere in this module, a plain syscall such as statvfs() on a
        dead network mount can block in the kernel for a very long time,
        uninterruptible by ordinary Python-level mechanisms. Running it
        in a throwaway thread and bounding *this* call's wait via
        Future.result(timeout=...) bounds the caller's wait even though
        the underlying thread may itself remain stuck forever (a leaked
        thread on that rare pathological path, not a hung response --
        the tradeoff this module's checks all make, matching "one slow
        check can't hang /health/ready").

        Raises FutureTimeoutError on timeout; callers decide what status
        that maps to (see check_database/check_storage below).

        Deliberately does NOT use `with ThreadPoolExecutor(...) as executor:`
        -- that context manager's __exit__ calls shutdown(wait=True),
        which blocks until the submitted thread finishes, defeating the
        entire point of the timeout the moment the wrapped call actually
        hangs (caught in review before this shipped, not after).
        shutdown(wait=False) lets this function return promptly on
        timeout while the orphaned thread is abandoned to finish (or
        hang) on its own, unobserved.
    '''
    executor = ThreadPoolExecutor(max_workers=1)
    future = executor.submit(fn)
    try:
        return future.result(timeout=timeout)
    finally:
        executor.shutdown(wait=False)

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
    paused = [
        name for name in HUEY_SERVICE_NAMES
        if os.path.exists(os.path.join('/run/service', name, 'down'))
    ]
    if not paused:
        return _status('healthy', detail='no queue is administratively paused')
    return _status(
        'degraded',
        detail=f'administratively paused (down file present): {", ".join(paused)}',
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
        first_line = (result.stdout or '').splitlines()[0] if result.stdout else None
    except Exception:
        first_line = None
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


def check_youtube():
    return _status(
        'unknown',
        detail='reachability from this process is not a confirmed proxy for '
               'yt-dlp\'s own egress path (possible VPN routing is a '
               'deployment-topology fact this repo has no visibility into) '
               '-- see this module\'s docstring',
    )


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
    '''Test-only: forces the next collect_components() call to recompute.'''
    _cache['components'] = None
    _cache['expires_at'] = 0.0


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
