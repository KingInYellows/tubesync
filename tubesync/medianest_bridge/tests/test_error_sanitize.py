'''
    T4: sanitize_error_message() against realistic upstream error-string
    shapes (the kind sync.tasks.get_error_message() actually hands back
    -- already stripped of the exception-class prefix, per that
    function's own docstring, so these fixtures reflect its *output*
    shape, not a raw traceback).
'''
from django.test import SimpleTestCase

from ..error_sanitize import sanitize_error_message


class SanitizeErrorMessageTestCase(SimpleTestCase):

    def test_none_and_empty_pass_through(self):
        self.assertIsNone(sanitize_error_message(None))
        self.assertEqual(sanitize_error_message(''), '')

    def test_plain_message_without_paths_unchanged(self):
        message = 'HTTP Error 403: Forbidden'
        self.assertEqual(sanitize_error_message(message), message)

    def test_video_unavailable_message_unchanged(self):
        message = 'Video unavailable. This video has been removed by the uploader'
        self.assertEqual(sanitize_error_message(message), message)

    def test_download_root_path_redacted(self):
        from django.conf import settings
        message = (
            f"[Errno 2] No such file or directory: "
            f"'{settings.DOWNLOAD_ROOT}/some_channel/video.mp4'"
        )
        result = sanitize_error_message(message)
        self.assertNotIn(str(settings.DOWNLOAD_ROOT), result)
        self.assertNotIn('some_channel', result)
        self.assertIn('<redacted>', result)
        self.assertIn('[Errno 2] No such file or directory', result)

    def test_cookies_file_path_redacted(self):
        from django.conf import settings
        message = f'ERROR: Failed to load cookies from {settings.COOKIES_FILE}: file is malformed'
        result = sanitize_error_message(message)
        self.assertNotIn(str(settings.COOKIES_FILE), result)
        self.assertIn('<redacted>', result)
        self.assertIn('ERROR: Failed to load cookies from', result)
        self.assertIn('file is malformed', result)

    def test_generic_absolute_path_redacted(self):
        message = (
            "Unable to write data: [Errno 28] No space left on device: "
            "'/some/other/mount/point/video.info.json'"
        )
        result = sanitize_error_message(message)
        self.assertNotIn('/some/other/mount/point/video.info.json', result)
        self.assertIn('<redacted>', result)
        self.assertIn('No space left on device', result)

    def test_credential_shaped_key_value_redacted(self):
        message = 'request rejected: auth_token=abcd1234567890secretvalue was invalid'
        result = sanitize_error_message(message)
        self.assertNotIn('abcd1234567890secretvalue', result)
        self.assertIn('auth_token=<redacted>', result)
        self.assertIn('request rejected', result)
        self.assertIn('was invalid', result)

    def test_password_key_value_redacted(self):
        message = 'login failed for password=hunter2 on remote host'
        result = sanitize_error_message(message)
        self.assertNotIn('hunter2', result)
        self.assertIn('password=<redacted>', result)

    def test_author_key_value_survives(self):
        '''
            T4 verifier LOW: an earlier substring-based credential regex
            redacted "author=" because it contains "auth" as a substring,
            not a whole segment. "author" != "auth" as a word/segment and
            must survive untouched.
        '''
        message = 'request rejected: author=John Doe was invalid'
        result = sanitize_error_message(message)
        self.assertIn('author=John Doe', result)

    def test_primary_key_value_survives(self):
        '''
            T4 verifier LOW: "key" is deliberately not a credential word
            at all (see error_sanitize.py's module docstring) -- a SQL
            constraint message mentioning "primary_key" is common,
            diagnostically useful, and not a secret.
        '''
        message = 'IntegrityError: duplicate primary_key=42 on sync_source'
        result = sanitize_error_message(message)
        self.assertIn('primary_key=42', result)

    def test_turkey_survives(self):
        '''"turkey" contains "key" as a substring, not a segment.'''
        message = 'unrelated message mentioning turkey: sandwich'
        result = sanitize_error_message(message)
        self.assertIn('turkey: sandwich', result)

    def test_rejected_colon_does_not_swallow_following_credential_kv(self):
        '''
            The exact bug this fix addresses: an earlier version's regex
            matched ANY \\w+ before ':'/'=' first and checked
            credential-ness afterward, so "rejected:" (a plain English
            colon, unrelated to any key=value pair) got matched as the
            "identifier:separator" and greedily swallowed the REAL
            "auth_token=..." that followed into its "value" group,
            leaving the actual secret completely unredacted. Verified
            directly against the failure this test is named for, not
            just the fixed behavior.
        '''
        message = 'request rejected: auth_token=REALSECRETVALUE1234 was invalid'
        result = sanitize_error_message(message)
        self.assertNotIn('REALSECRETVALUE1234', result)
        self.assertIn('auth_token=<redacted>', result)

    def test_youtube_channel_id_not_redacted(self):
        '''
            A channel/video ID appearing in an error message is useful
            diagnostic detail, not a secret -- must survive sanitization
            (this is the "don't blanket-redact long tokens" boundary
            error_sanitize.py's module docstring documents).
        '''
        message = 'failed to index channel UCabcdefghijklmnopqrstuv: rate limited'
        result = sanitize_error_message(message)
        self.assertIn('UCabcdefghijklmnopqrstuv', result)

    def test_video_id_not_redacted(self):
        message = 'video dQw4w9WgXcQ is unavailable in your country'
        result = sanitize_error_message(message)
        self.assertIn('dQw4w9WgXcQ', result)

    def test_multiple_findings_in_one_message_all_redacted(self):
        from django.conf import settings
        message = (
            f"cookies={settings.COOKIES_FILE} rejected; "
            f"could not write to {settings.DOWNLOAD_ROOT}/x/y.mp4; "
            f"token=deadbeefcafefeed1234 expired"
        )
        result = sanitize_error_message(message)
        self.assertNotIn(str(settings.COOKIES_FILE), result)
        self.assertNotIn(str(settings.DOWNLOAD_ROOT), result)
        self.assertNotIn('deadbeefcafefeed1234', result)
        self.assertIn('rejected', result)
        self.assertIn('expired', result)
