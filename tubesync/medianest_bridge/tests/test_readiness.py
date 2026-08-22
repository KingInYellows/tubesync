from unittest.mock import patch

from django.test import SimpleTestCase

from .. import readiness


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
