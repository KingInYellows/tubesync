'''
    MED-634: the sync app's model receivers must be connected in every
    process, not only after the URLconf loads.

    `manage.py shell` skips system checks, so the URLconf (and with it
    sync/views/sources.py, which upstream relied on to import
    sync/signals.py) never loads there. A source whose quality settings
    were changed in a shell then scheduled no "Checking all media" task.
    The check runs in a fresh interpreter, because the test runner has
    already loaded the URLconf and connected the receivers.
'''
import os
import subprocess
import sys
from pathlib import Path

from django.test import SimpleTestCase

PROJECT_DIR = Path(__file__).resolve().parents[2]

PROBE = '''
import sys
import django
django.setup()
from django.db.models.signals import post_save, pre_save
from sync.models import Media, Source
def names(signal, sender):
    return sorted(
        getattr(r, '__name__', '') for r in signal._live_receivers(sender)[0]
    )
print('urlconf_loaded=%s' % ('sync.views.sources' in sys.modules))
print('source_post_save=%s' % ('source_post_save' in names(post_save, Source)))
print('source_pre_save=%s' % ('source_pre_save' in names(pre_save, Source)))
print('media_post_save=%s' % ('media_post_save' in names(post_save, Media)))
'''


class SignalWiringTestCase(SimpleTestCase):

    def test_receivers_connected_without_urlconf(self):
        env = dict(os.environ)
        env.setdefault('DJANGO_SETTINGS_MODULE', 'tubesync.settings')
        result = subprocess.run(
            [sys.executable, '-c', PROBE],
            cwd=PROJECT_DIR,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = dict(
            line.split('=', 1)
            for line in result.stdout.splitlines()
            if '=' in line
        )
        # The probe must not have loaded the URLconf, or it proves nothing.
        self.assertEqual(lines.get('urlconf_loaded'), 'False', result.stdout)
        self.assertEqual(lines.get('source_post_save'), 'True', result.stdout)
        self.assertEqual(lines.get('source_pre_save'), 'True', result.stdout)
        self.assertEqual(lines.get('media_post_save'), 'True', result.stdout)
