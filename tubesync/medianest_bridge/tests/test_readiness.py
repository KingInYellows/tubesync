'''
    T4: dedicated tests for the workers/queues (s6-svstat-based) and
    storage-threshold readiness checks, plus caching and failure
    isolation. Mocks os.path.isdir/exists and subprocess.check_output
    with selective side_effects (falling through to the real function for
    anything not matching the exact paths/commands under test) rather
    than patching them unconditionally, so this doesn't destabilize
    unrelated filesystem/subprocess calls made elsewhere during a test.

    T-side follow-up (post-M6b egress determination): YoutubeProbeTestCase
    below adds the real youtube reachability probe. Every test mocks
    readiness.requests.get -- no live network call is ever made from this
    suite, matching this program's "no live YouTube in CI" rule.
'''
import subprocess
from unittest.mock import Mock, patch

import requests
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
            return name == 'huey-net-limited' and False or True

        with (
            patch.object(readiness.os.path, 'isdir', _isdir_side_effect(present_dirs)),
            patch.object(readiness.os.path, 'exists', _exists_side_effect(set())),
            patch.object(readiness, '_s6_service_wanted_up', side_effect=wanted_up),
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
        self.assertNotIn('headers', kwargs)
        self.assertNotIn('cookies', kwargs)
        self.assertNotIn('auth', kwargs)

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
