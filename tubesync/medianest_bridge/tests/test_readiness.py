'''
    T4: dedicated tests for the workers/queues (s6-svstat-based) and
    storage-threshold readiness checks, plus caching and failure
    isolation. Mocks os.path.isdir/exists and subprocess.check_output
    with selective side_effects (falling through to the real function for
    anything not matching the exact paths/commands under test) rather
    than patching them unconditionally, so this doesn't destabilize
    unrelated filesystem/subprocess calls made elsewhere during a test.
'''
import subprocess
from unittest.mock import patch

from django.test import SimpleTestCase

from .. import readiness

REAL_ISDIR = readiness.os.path.isdir
REAL_EXISTS = readiness.os.path.exists


def _isdir_side_effect(present_dirs):
    def _isdir(path):
        if path in present_dirs:
            return True
        if path == '/run/service' or path.startswith('/run/service/'):
            return path in present_dirs
        return REAL_ISDIR(path)
    return _isdir


def _exists_side_effect(present_paths):
    def _exists(path):
        if path.startswith('/run/service/'):
            return path in present_paths
        return REAL_EXISTS(path)
    return _exists


def _svstat_side_effect(pids):
    '''pids: dict of service_name -> pid (0 means "down but observable").'''
    def _check_output(cmd, **kwargs):
        path = cmd[-1]
        name = path.rsplit('/', 1)[-1]
        if name not in pids:
            raise subprocess.CalledProcessError(1, cmd)
        return f'{pids[name]}\n'.encode()
    return _check_output


class ReadinessCacheResetMixin:
    def setUp(self):
        super().setUp()
        readiness._reset_cache()
        self.addCleanup(readiness._reset_cache)


class WorkersCheckTestCase(ReadinessCacheResetMixin, SimpleTestCase):

    def test_unknown_when_not_under_s6(self):
        with patch.object(readiness.os.path, 'isdir', _isdir_side_effect(set())):
            result = readiness.check_workers()
        self.assertEqual(result['status'], 'unknown')

    def test_healthy_when_all_four_running(self):
        present = {'/run/service'} | {f'/run/service/{n}' for n in readiness.HUEY_SERVICE_NAMES}
        pids = {n: 100 + i for i, n in enumerate(readiness.HUEY_SERVICE_NAMES)}
        with (
            patch.object(readiness.os.path, 'isdir', _isdir_side_effect(present)),
            patch.object(readiness.subprocess, 'check_output', _svstat_side_effect(pids)),
        ):
            result = readiness.check_workers()
        self.assertEqual(result['status'], 'healthy')

    def test_degraded_when_some_down(self):
        present = {'/run/service'} | {f'/run/service/{n}' for n in readiness.HUEY_SERVICE_NAMES}
        pids = dict.fromkeys(readiness.HUEY_SERVICE_NAMES, 100)
        pids['huey-net-limited'] = 0  # down/not started
        with (
            patch.object(readiness.os.path, 'isdir', _isdir_side_effect(present)),
            patch.object(readiness.subprocess, 'check_output', _svstat_side_effect(pids)),
        ):
            result = readiness.check_workers()
        self.assertEqual(result['status'], 'degraded')
        self.assertIn('huey-net-limited', result['detail'])

    def test_unavailable_when_none_running(self):
        present = {'/run/service'} | {f'/run/service/{n}' for n in readiness.HUEY_SERVICE_NAMES}
        pids = dict.fromkeys(readiness.HUEY_SERVICE_NAMES, 0)
        with (
            patch.object(readiness.os.path, 'isdir', _isdir_side_effect(present)),
            patch.object(readiness.subprocess, 'check_output', _svstat_side_effect(pids)),
        ):
            result = readiness.check_workers()
        self.assertEqual(result['status'], 'unavailable')

    def test_svstat_timeout_treated_as_down_not_crash(self):
        present = {'/run/service'} | {f'/run/service/{n}' for n in readiness.HUEY_SERVICE_NAMES}

        def raises_timeout(cmd, **kwargs):
            raise subprocess.TimeoutExpired(cmd, kwargs.get('timeout', 2))

        with (
            patch.object(readiness.os.path, 'isdir', _isdir_side_effect(present)),
            patch.object(readiness.subprocess, 'check_output', raises_timeout),
        ):
            result = readiness.check_workers()
        self.assertEqual(result['status'], 'unavailable')


class QueuesCheckTestCase(ReadinessCacheResetMixin, SimpleTestCase):

    def test_unknown_when_not_under_s6(self):
        with patch.object(readiness.os.path, 'isdir', _isdir_side_effect(set())):
            result = readiness.check_queues()
        self.assertEqual(result['status'], 'unknown')

    def test_healthy_when_no_down_file(self):
        present_dirs = {'/run/service'} | {f'/run/service/{n}' for n in readiness.HUEY_SERVICE_NAMES}
        with (
            patch.object(readiness.os.path, 'isdir', _isdir_side_effect(present_dirs)),
            patch.object(readiness.os.path, 'exists', _exists_side_effect(set())),
        ):
            result = readiness.check_queues()
        self.assertEqual(result['status'], 'healthy')

    def test_degraded_when_queue_administratively_paused(self):
        present_dirs = {'/run/service'} | {f'/run/service/{n}' for n in readiness.HUEY_SERVICE_NAMES}
        down_files = {'/run/service/huey-net-limited/down'}
        with (
            patch.object(readiness.os.path, 'isdir', _isdir_side_effect(present_dirs)),
            patch.object(readiness.os.path, 'exists', _exists_side_effect(down_files)),
        ):
            result = readiness.check_queues()
        self.assertEqual(result['status'], 'degraded')
        self.assertIn('huey-net-limited', result['detail'])


class StorageThresholdTestCase(ReadinessCacheResetMixin, SimpleTestCase):

    def _usage(self, free_bytes):
        class Usage:
            free = free_bytes
        return Usage()

    def test_healthy_above_warn_threshold(self):
        with (
            patch('medianest_bridge.readiness.shutil.disk_usage', return_value=self._usage(10 * 1024 ** 3)),
            patch('os.access', return_value=True),
        ):
            result = readiness.check_storage()
        self.assertEqual(result['status'], 'healthy')

    def test_degraded_between_warn_and_critical(self):
        with (
            patch('medianest_bridge.readiness.shutil.disk_usage', return_value=self._usage(2 * 1024 ** 3)),
            patch('os.access', return_value=True),
        ):
            result = readiness.check_storage()
        self.assertEqual(result['status'], 'degraded')

    def test_unavailable_below_critical_threshold(self):
        with (
            patch('medianest_bridge.readiness.shutil.disk_usage', return_value=self._usage(100)),
            patch('os.access', return_value=True),
        ):
            result = readiness.check_storage()
        self.assertEqual(result['status'], 'unavailable')

    def test_thresholds_configurable_via_env(self):
        from .base import env_override
        with (
            env_override(
                MEDIANEST_BRIDGE_STORAGE_WARN_BYTES=str(50 * 1024 ** 3),
                MEDIANEST_BRIDGE_STORAGE_CRITICAL_BYTES=str(20 * 1024 ** 3),
            ),
            patch('medianest_bridge.readiness.shutil.disk_usage', return_value=self._usage(30 * 1024 ** 3)),
            patch('os.access', return_value=True),
        ):
            # 30 GiB free is below the overridden 50 GiB warn threshold.
            result = readiness.check_storage()
        self.assertEqual(result['status'], 'degraded')


class FailureIsolationTestCase(ReadinessCacheResetMixin, SimpleTestCase):

    def test_one_check_raising_does_not_crash_collect_components(self):
        def boom():
            raise RuntimeError('simulated check failure')

        with patch.dict(readiness.CHECKS, {'ffmpeg': boom}):
            components = readiness.collect_components()
        self.assertEqual(components['ffmpeg']['status'], 'unknown')
        # Every other component still ran normally.
        self.assertEqual(components['application']['status'], 'healthy')


class CachingTestCase(ReadinessCacheResetMixin, SimpleTestCase):

    def test_repeated_calls_within_ttl_do_not_recompute(self):
        call_count = {'n': 0}

        def counting_check():
            call_count['n'] += 1
            return readiness._status('healthy')

        with patch.dict(readiness.CHECKS, {'ffmpeg': counting_check}):
            readiness.collect_components()
            readiness.collect_components()
            readiness.collect_components()
        self.assertEqual(call_count['n'], 1)

    def test_cache_expires_after_ttl(self):
        call_count = {'n': 0}

        def counting_check():
            call_count['n'] += 1
            return readiness._status('healthy')

        with patch.dict(readiness.CHECKS, {'ffmpeg': counting_check}):
            readiness.collect_components()
            # Simulate TTL elapsing without a real sleep.
            readiness._cache['expires_at'] = 0.0
            readiness.collect_components()
        self.assertEqual(call_count['n'], 2)
