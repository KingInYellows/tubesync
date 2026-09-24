'''
    T4: dedicated tests for the workers/queues (s6-svstat-based) and
    storage-threshold readiness checks, plus caching and failure
    isolation. Mocks os.path.isdir/exists and subprocess.check_output
    with selective side_effects (falling through to the real function for
    anything not matching the exact paths/commands under test) rather
    than patching them unconditionally, so this doesn't destabilize
    unrelated filesystem/subprocess calls made elsewhere during a test.
    StorageThresholdTestCase is the one exception: it patches the
    purpose-built _stat_download_root seam wholesale (see its docstring)
    rather than the stdlib calls behind it. StatDownloadRootSeamTestCase
    keeps one focused test of the real seam wiring.

    T-side follow-up (post-M6b egress determination): YoutubeProbeTestCase
    below adds the real youtube reachability probe. Every test mocks
    readiness.requests.get -- no live network call is ever made from this
    suite, matching this program's "no live YouTube in CI" rule.
'''
import subprocess
import tempfile
import time
from pathlib import Path
from unittest.mock import Mock, patch

import requests
from django.test import SimpleTestCase, override_settings

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
            patch.object(readiness, '_s6_service_wanted_up', return_value=True),
        ):
            result = readiness.check_queues()
        self.assertEqual(result['status'], 'healthy')

    def test_degraded_when_queue_administratively_paused_via_down_file(self):
        present_dirs = {'/run/service'} | {f'/run/service/{n}' for n in readiness.HUEY_SERVICE_NAMES}
        down_files = {'/run/service/huey-net-limited/down'}
        with (
            patch.object(readiness.os.path, 'isdir', _isdir_side_effect(present_dirs)),
            patch.object(readiness.os.path, 'exists', _exists_side_effect(down_files)),
            patch.object(readiness, '_s6_service_wanted_up', return_value=True),
        ):
            result = readiness.check_queues()
        self.assertEqual(result['status'], 'degraded')
        self.assertIn('huey-net-limited', result['detail'])

    def test_degraded_when_queue_administratively_paused_via_wantedup(self):
        present_dirs = {'/run/service'} | {f'/run/service/{n}' for n in readiness.HUEY_SERVICE_NAMES}

        def wanted_up(name):
            return False if name == 'huey-net-limited' else True

        with (
            patch.object(readiness.os.path, 'isdir', _isdir_side_effect(present_dirs)),
            patch.object(readiness.os.path, 'exists', _exists_side_effect(set())),
            patch.object(readiness, '_s6_service_wanted_up', side_effect=wanted_up),
        ):
            result = readiness.check_queues()
        self.assertEqual(result['status'], 'degraded')
        self.assertIn('huey-net-limited', result['detail'])

    def test_unknown_when_wantedup_cannot_be_queried(self):
        present_dirs = {'/run/service'} | {f'/run/service/{n}' for n in readiness.HUEY_SERVICE_NAMES}

        def wanted_up(name):
            return None if name == 'huey-net-limited' else True

        with (
            patch.object(readiness.os.path, 'isdir', _isdir_side_effect(present_dirs)),
            patch.object(readiness.os.path, 'exists', _exists_side_effect(set())),
            patch.object(readiness, '_s6_service_wanted_up', side_effect=wanted_up),
        ):
            result = readiness.check_queues()
        self.assertEqual(result['status'], 'unknown')
        self.assertIn('huey-net-limited', result['detail'])


class StatDownloadRootSeamTestCase(SimpleTestCase):
    '''
        Exercises the real _stat_download_root() wiring (exists/access/
        disk_usage) so regressions there cannot slip past the mocked
        check_storage() tests below. Uses a controlled temp directory and
        patches only the stdlib calls the seam delegates to.
    '''

    def _usage(self, free_bytes):
        class Usage:
            free = free_bytes
        return Usage()

    def test_wires_exists_access_and_disk_usage_for_writable_root(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            with (
                override_settings(DOWNLOAD_ROOT=root),
                patch(
                    'medianest_bridge.readiness.shutil.disk_usage',
                    return_value=self._usage(99),
                ) as mock_disk_usage,
            ):
                exists, writable, usage = readiness._stat_download_root()
            mock_disk_usage.assert_called_once_with(root)

        self.assertTrue(exists)
        self.assertTrue(writable)
        self.assertEqual(usage.free, 99)

    def test_missing_root_skips_access_and_disk_usage(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            missing = Path(tmpdir) / 'does-not-exist'
            with (
                override_settings(DOWNLOAD_ROOT=missing),
                patch('medianest_bridge.readiness.os.access') as mock_access,
                patch('medianest_bridge.readiness.shutil.disk_usage') as mock_disk_usage,
            ):
                exists, writable, usage = readiness._stat_download_root()

        self.assertFalse(exists)
        self.assertFalse(writable)
        self.assertIsNone(usage)
        mock_access.assert_not_called()
        mock_disk_usage.assert_not_called()

    def test_non_writable_root_skips_disk_usage(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            with (
                override_settings(DOWNLOAD_ROOT=root),
                patch('medianest_bridge.readiness.os.access', return_value=False) as mock_access,
                patch('medianest_bridge.readiness.shutil.disk_usage') as mock_disk_usage,
            ):
                exists, writable, usage = readiness._stat_download_root()
            mock_access.assert_called_once_with(root, readiness.os.W_OK)

        self.assertTrue(exists)
        self.assertFalse(writable)
        self.assertIsNone(usage)
        mock_disk_usage.assert_not_called()

    def test_disk_usage_oserror_yields_none_usage(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            with (
                override_settings(DOWNLOAD_ROOT=root),
                patch(
                    'medianest_bridge.readiness.shutil.disk_usage',
                    side_effect=OSError('stale file handle'),
                ),
            ):
                exists, writable, usage = readiness._stat_download_root()

        self.assertTrue(exists)
        self.assertTrue(writable)
        self.assertIsNone(usage)


class StorageThresholdTestCase(ReadinessCacheResetMixin, SimpleTestCase):
    '''
        check_storage() stats DOWNLOAD_ROOT (exists/access/disk_usage, bundled
        in _stat_download_root) before it ever looks at the thresholds. These
        tests patch that one seam rather than disk_usage + a process-global
        os.access, so they no longer depend on DOWNLOAD_ROOT existing on disk
        (a fresh clone has no downloads/ directory; the full suite only
        passed because an upstream sync test happened to create it first).
    '''

    def setUp(self):
        super().setUp()
        from .base import env_override
        self._storage_threshold_env = env_override(
            MEDIANEST_BRIDGE_STORAGE_WARN_BYTES=None,
            MEDIANEST_BRIDGE_STORAGE_CRITICAL_BYTES=None,
        )
        self._storage_threshold_env.__enter__()
        self.addCleanup(self._storage_threshold_env.__exit__, None, None, None)

    def _usage(self, free_bytes):
        class Usage:
            free = free_bytes
        return Usage()

    def _patched_stat_download_root(self, free_bytes=None, *, exists=True, writable=True):
        '''
            Context manager replacing _stat_download_root with a stub that
            returns the (exists, writable, usage) tuple the real function
            would; usage is None when free_bytes is None.
        '''
        usage = None if free_bytes is None else self._usage(free_bytes)
        return patch(
            'medianest_bridge.readiness._stat_download_root',
            return_value=(exists, writable, usage),
        )

    def test_healthy_above_warn_threshold(self):
        with self._patched_stat_download_root(10 * 1024 ** 3):
            result = readiness.check_storage()
        self.assertEqual(result['status'], 'healthy')
        self.assertIn('free_bytes=', result['detail'])

    def test_degraded_between_warn_and_critical(self):
        with self._patched_stat_download_root(2 * 1024 ** 3):
            result = readiness.check_storage()
        self.assertEqual(result['status'], 'degraded')
        self.assertIn('free_bytes=', result['detail'])

    def test_unavailable_below_critical_threshold(self):
        with self._patched_stat_download_root(100):
            result = readiness.check_storage()
        self.assertEqual(result['status'], 'unavailable')
        # Must be the threshold branch, not the missing-directory branch --
        # this test used to pass for the wrong reason when downloads/ was absent.
        self.assertIn('free_bytes=', result['detail'])
        self.assertIn('critical threshold', result['detail'])

    def test_thresholds_are_inclusive_at_the_boundary(self):
        # Defaults: warn 5 GiB, critical 1 GiB; both comparisons are <=.
        with self._patched_stat_download_root(1 * 1024 ** 3):
            at_critical = readiness.check_storage()
        readiness._reset_cache()
        with self._patched_stat_download_root(5 * 1024 ** 3):
            at_warn = readiness.check_storage()
        self.assertEqual(at_critical['status'], 'unavailable')
        self.assertEqual(at_warn['status'], 'degraded')

    def test_thresholds_configurable_via_env(self):
        from .base import env_override
        with (
            env_override(
                MEDIANEST_BRIDGE_STORAGE_WARN_BYTES=str(50 * 1024 ** 3),
                MEDIANEST_BRIDGE_STORAGE_CRITICAL_BYTES=str(20 * 1024 ** 3),
            ),
            self._patched_stat_download_root(30 * 1024 ** 3),
        ):
            # 30 GiB free is below the overridden 50 GiB warn threshold.
            result = readiness.check_storage()
        self.assertEqual(result['status'], 'degraded')
        # The overridden value, not the 5 GiB default, must be what tripped it.
        self.assertIn(f'warn threshold {50 * 1024 ** 3}', result['detail'])

    def test_unavailable_when_download_root_missing(self):
        with self._patched_stat_download_root(exists=False):
            result = readiness.check_storage()
        self.assertEqual(result['status'], 'unavailable')
        self.assertEqual(result['detail'], 'DOWNLOAD_ROOT does not exist')

    def test_unavailable_when_download_root_not_writable(self):
        with self._patched_stat_download_root(exists=True, writable=False):
            result = readiness.check_storage()
        self.assertEqual(result['status'], 'unavailable')
        self.assertEqual(result['detail'], 'DOWNLOAD_ROOT is not writable')

    def test_healthy_when_free_space_unknown(self):
        # Models the outcome of disk_usage raising OSError inside
        # _stat_download_root (usage=None): writable, so healthy, but the
        # detail says the free-space figure is unavailable.
        with self._patched_stat_download_root(None):
            result = readiness.check_storage()
        self.assertEqual(result['status'], 'healthy')
        self.assertIn('free space could not be determined', result['detail'])

    def test_unavailable_when_stat_times_out(self):
        # _call_with_timeout re-raises the executor's FutureTimeoutError; the
        # check maps it to unavailable rather than letting it escape.
        with patch(
            'medianest_bridge.readiness._stat_download_root',
            side_effect=readiness.FutureTimeoutError(),
        ):
            result = readiness.check_storage()
        self.assertEqual(result['status'], 'unavailable')
        self.assertEqual(result['detail'], 'DOWNLOAD_ROOT stat timed out')

    def test_unavailable_when_stat_raises_oserror(self):
        # exists()/access() themselves raising (stale NFS handle) -- only
        # disk_usage is guarded inside _stat_download_root.
        with patch(
            'medianest_bridge.readiness._stat_download_root',
            side_effect=OSError('stale file handle'),
        ):
            result = readiness.check_storage()
        self.assertEqual(result['status'], 'unavailable')
        self.assertEqual(result['detail'], 'DOWNLOAD_ROOT could not be statted')


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


class CheckFfmpegTestCase(SimpleTestCase):

    @patch('medianest_bridge.readiness.shutil.which', return_value=None)
    def test_missing_ffmpeg_is_unavailable(self, _which):
        self.assertEqual(
            readiness.check_ffmpeg(),
            {'status': 'unavailable', 'version': None, 'detail': 'ffmpeg not found on PATH'},
        )

    @patch('medianest_bridge.readiness.shutil.which', return_value='/usr/bin/ffmpeg')
    @patch('medianest_bridge.readiness.subprocess.run')
    def test_nonzero_exit_is_unavailable(self, mock_run, _which):
        mock_run.return_value.returncode = 1
        mock_run.return_value.stdout = ''
        self.assertEqual(readiness.check_ffmpeg()['status'], 'unavailable')

    @patch('medianest_bridge.readiness.shutil.which', return_value='/usr/bin/ffmpeg')
    @patch('medianest_bridge.readiness.subprocess.run')
    def test_empty_output_is_unavailable(self, mock_run, _which):
        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = ''
        self.assertEqual(readiness.check_ffmpeg()['status'], 'unavailable')

    @patch('medianest_bridge.readiness.shutil.which', return_value='/usr/bin/ffmpeg')
    @patch('medianest_bridge.readiness.subprocess.run')
    def test_successful_probe_is_healthy(self, mock_run, _which):
        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = 'ffmpeg version 6.1.1\n'
        result = readiness.check_ffmpeg()
        self.assertEqual(result['status'], 'healthy')
        self.assertEqual(result['version'], 'ffmpeg version 6.1.1')

    @patch('medianest_bridge.readiness.shutil.which', return_value='/usr/bin/ffmpeg')
    @patch('medianest_bridge.readiness.subprocess.run', side_effect=OSError('boom'))
    def test_probe_exception_is_unavailable(self, _mock_run, _which):
        self.assertEqual(readiness.check_ffmpeg()['status'], 'unavailable')


class YoutubeProbeTestCase(ReadinessCacheResetMixin, SimpleTestCase):
    '''
        Every test here mocks readiness.requests.get directly -- no live
        network call is ever made. See this module's docstring.
    '''

    def _response(self, status_code):
        response = Mock()
        response.status_code = status_code
        return response

    def test_healthy_on_204(self):
        with patch.object(readiness.requests, 'get', return_value=self._response(204)):
            result = readiness.check_youtube()
        self.assertEqual(result['status'], 'healthy')
        self.assertIn('HTTP 204', result['detail'])

    def test_healthy_on_other_2xx_3xx(self):
        with patch.object(readiness.requests, 'get', return_value=self._response(302)):
            result = readiness.check_youtube()
        self.assertEqual(result['status'], 'healthy')

    def test_unavailable_on_non_2xx_3xx_status(self):
        with patch.object(readiness.requests, 'get', return_value=self._response(503)):
            result = readiness.check_youtube()
        self.assertEqual(result['status'], 'unavailable')
        self.assertIn('HTTP 503', result['detail'])

    def test_unavailable_on_timeout(self):
        with patch.object(
            readiness.requests, 'get',
            side_effect=requests.exceptions.Timeout('connect timed out'),
        ):
            result = readiness.check_youtube()
        self.assertEqual(result['status'], 'unavailable')
        self.assertIn('Timeout', result['detail'])

    def test_unavailable_when_probe_exceeds_wall_clock_timeout(self):
        def hang_forever(*args, **kwargs):
            time.sleep(10)

        with patch.object(readiness.requests, 'get', side_effect=hang_forever):
            start = time.monotonic()
            result = readiness.check_youtube()
            elapsed = time.monotonic() - start
        self.assertEqual(result['status'], 'unavailable')
        self.assertIn('probe timed out', result['detail'])
        self.assertLess(elapsed, 5)

    def test_unavailable_on_connection_refused(self):
        with patch.object(
            readiness.requests, 'get',
            side_effect=requests.exceptions.ConnectionError('refused'),
        ):
            result = readiness.check_youtube()
        self.assertEqual(result['status'], 'unavailable')
        self.assertIn('ConnectionError', result['detail'])

    def test_detail_never_contains_the_probe_url_or_raw_exception_text(self):
        '''
            requests' own exception __str__ commonly embeds the full
            request URL and low-level connection text -- the probe must
            record only the exception's class name, never str(exc).
        '''
        message_with_url = (
            "HTTPSConnectionPool(host='www.youtube.com', port=443): "
            "Max retries exceeded with url: /generate_204 "
            "(Caused by NewConnectionError('secret-internal-detail'))"
        )
        with patch.object(
            readiness.requests, 'get',
            side_effect=requests.exceptions.ConnectionError(message_with_url),
        ):
            result = readiness.check_youtube()
        self.assertNotIn(readiness._YOUTUBE_PROBE_URL, result['detail'])
        self.assertNotIn('secret-internal-detail', result['detail'])
        self.assertNotIn('youtube.com', result['detail'])

    def test_disabled_reports_not_configured_and_makes_no_network_call(self):
        from .base import env_override
        with (
            env_override(MEDIANEST_BRIDGE_YOUTUBE_PROBE_ENABLED='false'),
            patch.object(readiness.requests, 'get') as mock_get,
        ):
            result = readiness.check_youtube()
        self.assertEqual(result['status'], 'not_configured')
        mock_get.assert_not_called()

    def test_enabled_by_default(self):
        with patch.object(readiness.requests, 'get', return_value=self._response(204)) as mock_get:
            result = readiness.check_youtube()
        self.assertEqual(result['status'], 'healthy')
        mock_get.assert_called_once()

    def test_probes_the_expected_url_with_a_bounded_timeout_no_auth_no_cookies(self):
        with patch.object(readiness.requests, 'get', return_value=self._response(204)) as mock_get:
            readiness.check_youtube()
        args, kwargs = mock_get.call_args
        self.assertEqual(args[0], 'https://www.youtube.com/generate_204')
        self.assertEqual(kwargs.get('timeout'), readiness._YOUTUBE_PROBE_TIMEOUT_SECONDS)
        self.assertIs(kwargs.get('allow_redirects'), False)
        self.assertNotIn('headers', kwargs)
        self.assertNotIn('cookies', kwargs)
        self.assertNotIn('auth', kwargs)

    def test_bare_3xx_is_treated_as_healthy_without_following_it(self):
        '''
            requests defaults to following redirects, which would hide a
            bare 3xx behind whatever the chain's final response was --
            allow_redirects=False (asserted above) is what makes this
            302-as-healthy interpretation actually reachable/meaningful,
            not dead code that only ever sees a chain's last response.
        '''
        with patch.object(readiness.requests, 'get', return_value=self._response(302)) as mock_get:
            result = readiness.check_youtube()
        self.assertIs(mock_get.call_args.kwargs.get('allow_redirects'), False)
        self.assertEqual(result['status'], 'healthy')

    def test_repeated_calls_within_ttl_do_not_reprobe(self):
        with patch.object(readiness.requests, 'get', return_value=self._response(204)) as mock_get:
            readiness.check_youtube()
            readiness.check_youtube()
            readiness.check_youtube()
        self.assertEqual(mock_get.call_count, 1)

    def test_cache_expires_after_its_own_ttl(self):
        with patch.object(readiness.requests, 'get', return_value=self._response(204)) as mock_get:
            readiness.check_youtube()
            # Simulate the youtube-specific TTL elapsing without a real sleep --
            # this cache is independent of the shared _cache dict's TTL.
            readiness._youtube_cache['expires_at'] = 0.0
            readiness.check_youtube()
        self.assertEqual(mock_get.call_count, 2)

    def test_youtube_cache_is_independent_of_shared_components_cache(self):
        '''
            check_youtube()'s own cache must outlive collect_components()'s
            shared 5s TTL -- calling collect_components() repeatedly (which
            invalidates and recomputes the shared cache each time it's
            forced to) must not re-probe youtube on every call.
        '''
        with patch.object(readiness.requests, 'get', return_value=self._response(204)) as mock_get:
            readiness.collect_components()
            readiness._cache['expires_at'] = 0.0  # force the shared cache to miss
            readiness.collect_components()
        self.assertEqual(mock_get.call_count, 1)

    def test_disabled_component_never_worsens_overall_status(self):
        '''
            Tests aggregate_status() directly against a synthetic,
            otherwise-all-healthy components dict -- not the real
            collect_components() output, which pulls in this actual test
            environment's other real signals (e.g. check_ffmpeg() reports
            "unavailable" here because ts-bridge-test:latest has no
            ffmpeg binary at all, unrelated to youtube and already
            enough to degrade the real aggregate on its own). This test
            is specifically about the aggregation RULE's interaction
            with youtube's "not_configured" status, isolated from every
            other component's real-environment state.
        '''
        from .base import env_override
        with env_override(MEDIANEST_BRIDGE_YOUTUBE_PROBE_ENABLED='false'):
            youtube_component = readiness.check_youtube()
        self.assertEqual(youtube_component['status'], 'not_configured')
        components = {name: readiness._status('healthy') for name in readiness.CHECKS}
        components['youtube'] = youtube_component
        self.assertEqual(readiness.aggregate_status(components), 'healthy')

    def test_unavailable_youtube_degrades_not_unavailable_overall(self):
        '''Same isolation rationale as the test above.'''
        with patch.object(
            readiness.requests, 'get',
            side_effect=requests.exceptions.ConnectionError('refused'),
        ):
            youtube_component = readiness.check_youtube()
        self.assertEqual(youtube_component['status'], 'unavailable')
        components = {name: readiness._status('healthy') for name in readiness.CHECKS}
        components['youtube'] = youtube_component
        self.assertEqual(readiness.aggregate_status(components), 'degraded')


class SourceDefaultsCheckTestCase(SimpleTestCase):
    '''
        readiness.check_source_defaults() -- the sourceDefaults readiness
        component (T3). Uses env_override() directly, like
        YoutubeProbeTestCase/StorageThresholdTestCase above: this class is
        a SimpleTestCase, not BridgeTestCase, so BridgeTestMixin's own env
        clearing doesn't apply here. See test_config.py for
        config.source_defaults()/validate_source_defaults()'s own
        parsing/merge/field-level coverage -- this class only checks the
        component wiring: does a valid/invalid config map to
        healthy/unavailable, and does it flow into overall aggregation
        like any other component.

        No DB access happens on either path: build_synthetic_source_form()
        (source_forms.py) overrides SourceForm.validate_unique() to a
        no-op before calling is_valid(), so even the "valid config" case
        below never queries the database -- safe under SimpleTestCase.
    '''

    def test_healthy_when_unset(self):
        from .base import env_override
        with env_override(MEDIANEST_BRIDGE_SOURCE_DEFAULTS=None):
            component = readiness.check_source_defaults()
        self.assertEqual(component['status'], 'healthy')

    def test_healthy_when_valid_overlay_configured(self):
        from .base import env_override
        with env_override(
            MEDIANEST_BRIDGE_SOURCE_DEFAULTS='{"*": {}, "channel": {"write_nfo": true}}',
        ):
            component = readiness.check_source_defaults()
        self.assertEqual(component['status'], 'healthy')

    def test_unavailable_when_json_is_malformed(self):
        from .base import env_override
        with env_override(MEDIANEST_BRIDGE_SOURCE_DEFAULTS='{not json'):
            component = readiness.check_source_defaults()
        self.assertEqual(component['status'], 'unavailable')
        self.assertIn('not valid JSON', component['detail'])

    def test_unavailable_when_media_format_cannot_produce_a_filename(self):
        from .base import env_override
        # "*": {} covers playlist so this test isolates the media_format
        # check itself, not the type-coverage requirement (see
        # test_uncovered_type_is_unavailable below).
        with env_override(
            MEDIANEST_BRIDGE_SOURCE_DEFAULTS='{"*": {}, "channel": {"media_format": "{not_a_real_format_key}"}}',
        ):
            component = readiness.check_source_defaults()
        self.assertEqual(component['status'], 'unavailable')

    def test_uncovered_type_is_unavailable(self):
        '''
            A type named by neither its own key nor "*" is a
            configuration error, not a silent "no overrides" -- see
            config.source_defaults()'s docstring.
        '''
        from .base import env_override
        with env_override(
            MEDIANEST_BRIDGE_SOURCE_DEFAULTS='{"channel": {"write_nfo": true}}',
        ):
            component = readiness.check_source_defaults()
        self.assertEqual(component['status'], 'unavailable')
        self.assertIn('playlist', component['detail'])

    def test_never_echoes_the_raw_env_value_in_detail(self):
        from .base import env_override
        secret_marker = 'super-secret-path-marker-should-not-leak'
        with env_override(
            MEDIANEST_BRIDGE_SOURCE_DEFAULTS=(
                '{"*": {}, "channel": {"media_format": "' + secret_marker + '-{not_a_real_format_key}"}}'
            ),
        ):
            component = readiness.check_source_defaults()
        self.assertEqual(component['status'], 'unavailable')
        self.assertNotIn(secret_marker, component['detail'])

    def test_registered_in_checks_and_degrades_overall_status(self):
        self.assertIn('sourceDefaults', readiness.CHECKS)
        components = {name: readiness._status('healthy') for name in readiness.CHECKS}
        components['sourceDefaults'] = readiness._status('unavailable', detail='bad config')
        self.assertEqual(readiness.aggregate_status(components), 'degraded')
