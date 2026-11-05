#!/usr/bin/python3
'''

    Perform an HTTP request to a URL and exit with an exit code of 1 if the
    request did not return an HTTP/200 status code.

    Usage:
    $ ./healthcheck.py http://some.url.here/healthcheck/resource

'''


import hashlib
import json
import os
import random
import requests
import subprocess
import sys
import threading
import time
import urllib.request

try:
    from common.third_party_versions import yt_dlp_version
except:
    yt_dlp_version = None


TIMEOUT = 5  # Seconds
# Fork change (MED-623): a stable, instance-independent record of the
# newest yt-dlp release this check has seen, read by the medianest_bridge
# readiness check (medianest_bridge/readiness.py::check_yt_dlp). The
# per-instance `v.<id>` files below stay as they are upstream.
STALE_YTDLP_STATE_FILE = '/dev/shm/.healthcheck/yt-dlp-release.json'
HTTP_USER = os.getenv('HTTP_USER')
HTTP_PASS = os.getenv('HTTP_PASS')
# never use proxy for healthcheck requests
os.environ['no_proxy'] = '*'


def do_heatlhcheck(url):
    headers = {'User-Agent': 'healthcheck'}
    auth = None
    if HTTP_USER and HTTP_PASS:
        auth = (HTTP_USER, HTTP_PASS)
    response = requests.get(url, headers=headers, auth=auth, timeout=TIMEOUT)
    return 200 == response.status_code

def atomic_write(path, data):
    tmp = f'{path}.aw.tmp'
    try:
        with open(tmp, 'w') as f:
            f.write(str(data))
        os.replace(tmp, path)
    except:
        pass

def bg_v(s, lock):
    u = 'https://github.com/yt-dlp/yt-dlp/releases/latest'
    try:
        with lock:
            req = urllib.request.Request(u, method='HEAD')
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                v = resp.geturl().split('/')[-1]
            now = time.time()
            atomic_write(s, v)
            atomic_write(s + '.t', now)
            atomic_write(s + '.i', random.randint(1200, 43200))
    except:
        pass

def exists_read(p, default=None):
    if os.path.exists(p):
        with open(p) as f:
            return f.read()
    else:
        return default

def get_container_id():
    try:
        # Parse mountinfo (Full ID)
        content = exists_read('/proc/self/mountinfo')
        if not content:
            return get_hostname_as_short_id()

        target_mounts = ('/', '/etc/hostname', '/etc/hosts', '/etc/resolv.conf')

        for line in content.splitlines():
            parts = line.split()
            # Strict filtering: check mount point at index 4
            if 5 > len(parts) or parts[4] not in target_mounts:
                continue

            try:
                sep_index = parts.index('-')
            except ValueError:
                sep_index = len(parts)

            # Search priority (constant on the left):
            # 3 + sep_index: Superblock options (upperdir/workdir for overlay2)
            # 2 + sep_index: Mount Source
            # 3: Root (bind-mount host path)
            for idx in (3 + sep_index, 2 + sep_index, 3):
                if idx < len(parts):
                    field = parts[idx]
                    for opt in field.split(','):
                        # Skip noise from read-only layers
                        if opt.startswith('lowerdir='):
                            continue

                        for segment in opt.split('/'):
                            if is_hex(segment, 64):
                                return segment
    except:
        pass

    return get_hostname_as_short_id()

def get_down_file(service_name):
    path = os.path.join('/run/service', service_name)
    return os.path.join(path, 'down')

def get_hostname_as_short_id():
    try:
        hostname = exists_read('/proc/sys/kernel/hostname')
        if hostname and is_hex(hostname.strip(), 12):
            return hostname
    except:
        pass
    return None

def get_root_start_time(curr=None):
    if curr is None or 0 >= curr:
        curr = os.getpid()
    start_time = '0'
    # Climb the process tree to find the namespace root (PPID 0)
    while 0 != curr:
        stat = exists_read(f'/proc/{curr}/stat')
        if not stat:
            break
        # Use rsplit to safely skip the process name field (Field 2)
        parts = stat.rsplit(')', 1)[-1].split()
        # Field 22 (start_time) is index 19; Field 4 (ppid) is index 1
        start_time = parts[19]
        curr = int(parts[1])
    return start_time

def get_service_pid(service_name):
    try:
        path = os.path.join('/run/service', service_name)
        pid_str = subprocess.check_output(['/command/s6-svstat', '-o', 'pid', path])
        return int(pid_str.strip())
    except:
        return None

def get_unique_id(service_name=None):
    # Unique ID based on the OS boot + the top-level process start time

    boot_id = exists_read('/proc/sys/kernel/random/boot_id', 'no-boot-id').strip()
    cgroup_data = exists_read('/proc/self/cgroup', 'no-cgroup').strip()

    container_id = get_container_id()
    if not container_id:
        container_id = 'not-a-container'

    root_start = 'no-service-pid'
    service_pid = get_service_pid(service_name)
    if service_pid is not None:
        root_start = get_root_start_time(service_pid).strip()

    # Hash to create a stable ID for this specific instance/session
    combined_seed = f'{boot_id}_{container_id}_{root_start}_{cgroup_data}'
    return hashlib.md5(combined_seed.encode()).hexdigest()[:8]

def stale_ytdlp_action():
    # Fork change (MED-623): upstream stops the huey-net-limited consumer
    # as soon as the bundled yt-dlp is older than the newest release, which
    # halts every download until the image is rebuilt. The fork only warns
    # by default; TUBESYNC_STALE_YTDLP_ACTION=pause restores upstream.
    value = (os.getenv('TUBESYNC_STALE_YTDLP_ACTION') or 'warn').strip().lower()
    return 'pause' if 'pause' == value else 'warn'

def version_key(version):
    try:
        return tuple(int(part) for part in version.strip().split('.'))
    except (AttributeError, ValueError):
        return None

def is_older(installed, latest):
    # yt-dlp versions are dotted dates (2026.11.03, nightly 2026.11.03.232).
    # Compare them numerically so a nightly build newer than the latest
    # stable release is not called stale; fall back to inequality for
    # anything that does not parse.
    if not installed or not latest:
        return False
    installed_key = version_key(installed)
    latest_key = version_key(latest)
    if installed_key is not None and latest_key is not None:
        return installed_key < latest_key
    return installed.strip() != latest.strip()

def write_release_state(state_file, version_file, pause_marker, action):
    # `paused` means the guard asked stop-queue to pause the download
    # queue (pause mode, pause marker present). stop-queue runs detached,
    # so readiness only uses it to label a queue it sees paused.
    latest = exists_read(version_file, '').strip()
    if not latest:
        return
    stale = is_older(yt_dlp_version, latest)
    state = {
        'installed': yt_dlp_version,
        'latest': latest,
        'stale': bool(stale),
        'action': action,
        'paused': bool(stale and 'pause' == action and os.path.exists(pause_marker)),
        'checkedAt': exists_read(version_file + '.t', '').strip() or None,
    }
    os.makedirs(os.path.dirname(state_file), exist_ok=True)
    atomic_write(state_file, json.dumps(state, sort_keys=True))

def is_hex(s, length):
    # Strict length and character validation
    hex_chars = '0123456789abcdef'
    return length == len(s) and all(c in hex_chars for c in s.lower())

def is_old(s, lock, down_file):
    os.makedirs(os.path.dirname(s), exist_ok=True)
    latest_version = exists_read(s, '').strip()
    fail_fast = (
        yt_dlp_version is None or
        os.path.exists(down_file)
    )
    if fail_fast:
        return False
    try:
        iv = float(exists_read(s + '.i', 1200))
        lt = float(exists_read(s + '.t', 0))
        now = time.time()
        if now - lt >= iv:
            threading.Thread(target=bg_v, args=(s, lock), daemon=True).start()
    except:
        pass
    return is_older(yt_dlp_version, latest_version)


if '__main__' == __name__:
    lock = threading.Lock()
    service_name = 'huey-net-limited'
    container_instance = get_unique_id(service_name)
    vf = f'/dev/shm/.healthcheck/v.{container_instance}'
    lf = vf + '.l'
    df = get_down_file(service_name)
    action = stale_ytdlp_action()
    # The pause marker (`.l`, upstream's) and the warn marker (`.w`, fork)
    # are separate, so an earlier warning never suppresses a later pause.
    marker = lf if 'pause' == action else vf + '.w'
    if is_old(vf, lock, df) and not os.path.exists(marker):
        atomic_write(marker, 1)
        if os.path.exists(marker):
            latest_version = exists_read(vf, '').strip()
            if 'pause' == action:
                cmd = [
                    '/usr/bin/env', 'python3', '/app/manage.py', 'stop-queue',
                    '--installed', yt_dlp_version,
                    '--latest', latest_version,
                    '--name', 'yt-dlp',
                    service_name,
                ]
                subprocess.Popen(cmd, stdout=-1, stderr=-1, start_new_session=True)
            else:
                print(
                    f'WARNING: yt-dlp {yt_dlp_version} is older than the latest '
                    f'release {latest_version}; this check does not pause '
                    'downloads. Pull or rebuild a newer image '
                    '(TUBESYNC_STALE_YTDLP_ACTION=pause stops the download '
                    'queue instead).',
                    file=sys.stderr,
                )
    try:
        write_release_state(STALE_YTDLP_STATE_FILE, vf, lf, action)
    except Exception:
        pass
    # if gunicorn is marked as intentionally down, nothing else matters
    df = get_down_file('gunicorn')
    if os.path.exists(df):
        lock.acquire(timeout=TIMEOUT)
        sys.exit(0)
    try:
        url = sys.argv[1]
    except IndexError:
        try:
            from tubesync.gunicorn import get_bind
            host_port = get_bind()
        except:
            host = os.getenv('LISTEN_HOST', '127.0.0.1')
            port = os.getenv('LISTEN_PORT', '8080')
            host_port = f'{host}:{port}'
        url = f'http://{host_port}/healthcheck'
    if do_heatlhcheck(url):
        lock.acquire(timeout=TIMEOUT)
        sys.exit(0)
    else:
        sys.exit(1)
