'''
    Per-component readiness checks for GET /health/ready.

    The contract (bridge-openapi.v1.yaml, ComponentStatus) is explicit: a
    component the bridge cannot cheaply/honestly verify MUST report
    "unknown", never a fabricated "healthy". This module follows that rule
    literally -- several components below are "unknown" by design, not by
    omission, because a trustworthy check either requires a live external
    call (youtube), isn't observable from the request-handling process
    without deeper huey introspection (queues, workers -- see the upstream
    audit's note that even upstream's own /healthcheck does not see a
    paused huey-net-limited queue), or would require behaviour outside
    T1's scope (plex reachability, as opposed to plex *configuration*,
    which is cheap and is checked).

    Aggregation rule (contract-silent; documented here rather than invented
    silently, per the T1 take-over instructions):
      - database (or application) unavailable -> overall "unavailable"
      - any checked (non-unknown, non-not_configured) component
        degraded/unavailable -> overall "degraded"
      - "unknown"/"not_configured" components never worsen the overall
        status -- otherwise T1 (queues/workers/youtube/plex all
        legitimately unknown at this stage) would report permanently
        degraded, which is not an honest signal either.
'''
import shutil
import subprocess

from django.db import connection


def _status(status, *, version=None, detail=None):
    return {'status': status, 'version': version, 'detail': detail}


def check_application():
    # Reaching this function at all means the WSGI app, URL routing, and
    # this view's own dispatch gating all worked.
    return _status('healthy')


def check_database():
    try:
        with connection.cursor() as cursor:
            cursor.execute('SELECT 1')
            cursor.fetchone()
    except Exception:
        return _status('unavailable', detail='database query failed')
    return _status('healthy')


def check_queues():
    return _status(
        'unknown',
        detail='huey queue backlog/pause state is not observable from the web process in T1',
    )


def check_workers():
    return _status(
        'unknown',
        detail='huey consumer process liveness is not observable from the web process in T1',
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
            timeout=2,
            check=False,
        )
        first_line = (result.stdout or '').splitlines()[0] if result.stdout else None
    except Exception:
        first_line = None
    return _status('healthy', version=first_line)


def check_storage():
    from django.conf import settings
    root = settings.DOWNLOAD_ROOT
    try:
        exists = root.exists()
    except OSError:
        return _status('unavailable', detail='DOWNLOAD_ROOT could not be statted')
    if not exists:
        return _status('unavailable', detail='DOWNLOAD_ROOT does not exist')
    import os
    if not os.access(root, os.W_OK):
        return _status('unavailable', detail='DOWNLOAD_ROOT is not writable')
    try:
        usage = shutil.disk_usage(root)
        detail = f'free_bytes={usage.free}'
    except OSError:
        detail = 'DOWNLOAD_ROOT is writable; free space could not be determined'
    return _status('healthy', detail=detail)


def check_youtube():
    return _status(
        'unknown',
        detail='no live upstream reachability check is performed in T1',
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
        detail='a Plex media server is configured; reachability is not checked in T1',
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


def collect_components():
    return {name: check() for name, check in CHECKS.items()}


def aggregate_status(components):
    if components.get('database', {}).get('status') == 'unavailable':
        return 'unavailable'
    if components.get('application', {}).get('status') == 'unavailable':
        return 'unavailable'
    for name, component in components.items():
        if component.get('status') in _AGGREGATING_STATUSES:
            return 'degraded'
    return 'healthy'
