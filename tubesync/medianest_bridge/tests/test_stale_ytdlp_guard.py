'''
    MED-623: the fork's change to the container HEALTHCHECK script
    (tubesync/healthcheck.py), which is a standalone script, not a Django
    module. Loaded by path; its import-time `no_proxy` change is undone.
'''
import importlib.util
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.test import SimpleTestCase

HEALTHCHECK_PATH = Path(__file__).resolve().parents[2] / 'healthcheck.py'


def _load_healthcheck():
    saved = os.environ.get('no_proxy')
    try:
        spec = importlib.util.spec_from_file_location('_fork_healthcheck', HEALTHCHECK_PATH)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        if saved is None:
            os.environ.pop('no_proxy', None)
        else:
            os.environ['no_proxy'] = saved
    return module


healthcheck = _load_healthcheck()


class StaleActionTestCase(SimpleTestCase):

    def test_defaults_to_warn(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop('TUBESYNC_STALE_YTDLP_ACTION', None)
            self.assertEqual(healthcheck.stale_ytdlp_action(), 'warn')

    def test_pause_restores_upstream_behavior(self):
        with patch.dict(os.environ, {'TUBESYNC_STALE_YTDLP_ACTION': ' Pause '}):
            self.assertEqual(healthcheck.stale_ytdlp_action(), 'pause')

    def test_unknown_values_warn(self):
        with patch.dict(os.environ, {'TUBESYNC_STALE_YTDLP_ACTION': 'stop'}):
            self.assertEqual(healthcheck.stale_ytdlp_action(), 'warn')


class IsOlderTestCase(SimpleTestCase):

    def test_numeric_comparison(self):
        self.assertTrue(healthcheck.is_older('2026.9.1', '2026.10.01'))
        self.assertFalse(healthcheck.is_older('2026.11.03', '2026.11.03'))

    def test_nightly_ahead_of_stable_is_not_stale(self):
        self.assertFalse(healthcheck.is_older('2026.11.03.232', '2026.11.03'))

    def test_unknown_latest_is_not_stale(self):
        self.assertFalse(healthcheck.is_older('2026.11.03', ''))
        self.assertFalse(healthcheck.is_older(None, '2026.11.03'))


class WriteReleaseStateTestCase(SimpleTestCase):

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.version_file = self.dir / 'v.instance'
        self.warned_file = self.dir / 'v.instance.l'
        self.state_file = self.dir / 'state' / 'yt-dlp-release.json'

    def _write(self, action, installed='2026.10.01'):
        with patch.object(healthcheck, 'yt_dlp_version', installed):
            healthcheck.write_release_state(
                str(self.state_file), str(self.version_file), str(self.warned_file), action,
            )
        return json.loads(self.state_file.read_text())

    def test_no_file_until_the_latest_release_is_known(self):
        with patch.object(healthcheck, 'yt_dlp_version', '2026.10.01'):
            healthcheck.write_release_state(
                str(self.state_file), str(self.version_file), str(self.warned_file), 'warn',
            )
        self.assertFalse(self.state_file.exists())

    def test_records_stale_in_warn_mode_without_pause(self):
        self.version_file.write_text('2026.11.03')
        self.warned_file.write_text('1')
        state = self._write('warn')
        self.assertEqual(state['latest'], '2026.11.03')
        self.assertEqual(state['installed'], '2026.10.01')
        self.assertTrue(state['stale'])
        self.assertFalse(state['paused'])

    def test_records_pause_only_once_the_guard_fired(self):
        self.version_file.write_text('2026.11.03')
        self.assertFalse(self._write('pause')['paused'])
        self.warned_file.write_text('1')
        self.assertTrue(self._write('pause')['paused'])

    def test_up_to_date_is_not_stale(self):
        self.version_file.write_text('2026.10.01')
        state = self._write('pause')
        self.assertFalse(state['stale'])
        self.assertFalse(state['paused'])

    def test_state_file_matches_what_readiness_reads(self):
        from .. import readiness
        self.assertEqual(readiness.YTDLP_RELEASE_STATE_FILE, healthcheck.STALE_YTDLP_STATE_FILE)
