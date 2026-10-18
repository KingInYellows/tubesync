'''
    MED-589 (contract 1.1.0): POST /sources/resolve -- channel handle URL
    -> stable UC... channel ID. yt-dlp is always mocked; no test here
    makes a network call.
'''
import io
import json
from unittest.mock import MagicMock, patch

import yt_dlp
from django.urls import resolve

from sync.models import Source

from .. import channel_resolve
from .base import BridgeTestCase
from .test_endpoints import assert_matches_schema
from .test_write_sources import post_json

RESOLVE_URL = '/api/medianest/v1/sources/resolve'
CAPS_URL = '/api/medianest/v1/capabilities'
CHANNEL_ID = 'UCabcdefghijklmnopqrstuv'
YOUTUBE_DL = 'medianest_bridge.channel_resolve.yt_dlp.YoutubeDL'
_DEFAULT = object()


def _mock_youtube_dl(response=None, error=None):
    '''
        Returns (YoutubeDL class mock, extract_info mock). extract_info
        returns `response` or raises `error`.
    '''
    ydl_class = MagicMock(name='YoutubeDL')
    extract_info = ydl_class.return_value.__enter__.return_value.extract_info
    if error is not None:
        extract_info.side_effect = error
    else:
        extract_info.return_value = response
    return ydl_class, extract_info


def _http_404_download_error(message='ERROR: [youtube:tab] lookup failed'):
    '''
        The structure yt-dlp really raises for a 404: DownloadError
        wrapping (exc_info) an ExtractorError whose .cause is a
        networking HTTPError with status 404. The message deliberately
        does not mention 404, so the structural check is what's tested.
    '''
    http_response = yt_dlp.networking.Response(
        fp=io.BytesIO(b''), url='https://www.youtube.com/youtubei/v1/navigation/resolve_url',
        headers={}, status=404, reason='Not Found',
    )
    http_error = yt_dlp.networking.exceptions.HTTPError(http_response)
    extractor_error = yt_dlp.utils.ExtractorError('Unable to download API page', cause=http_error)
    return yt_dlp.utils.DownloadError(
        message, exc_info=(type(extractor_error), extractor_error, None),
    )


class ResolveTestMixin:

    def post_resolve(self, body, **extra):
        headers = dict(self.auth_header())
        headers.update(extra)
        return post_json(self.client, RESOLVE_URL, body, **headers)

    def resolve_ok(self, url, response=_DEFAULT):
        if response is _DEFAULT:
            response = {'channel_id': CHANNEL_ID, 'channel': 'Some Channel'}
        ydl_class, extract_info = _mock_youtube_dl(response=response)
        with patch(YOUTUBE_DL, ydl_class):
            http_response = self.post_resolve({'url': url})
        return http_response, ydl_class, extract_info


class ResolveAcceptedShapesTestCase(ResolveTestMixin, BridgeTestCase):

    def test_accepted_shapes_rebuild_lookup_url(self):
        self.enable_bridge()
        cases = {
            'https://www.youtube.com/@SomeHandle': 'https://www.youtube.com/@SomeHandle',
            'https://youtube.com/@some.handle-1_x': 'https://www.youtube.com/@some.handle-1_x',
            'https://m.youtube.com/@abc': 'https://www.youtube.com/@abc',
            'https://WWW.YouTube.COM/@abc': 'https://www.youtube.com/@abc',
            'HTTPS://www.youtube.com/@abc': 'https://www.youtube.com/@abc',
            'https://www.youtube.com/@abc/': 'https://www.youtube.com/@abc',
            'https://www.youtube.com/@abc/videos': 'https://www.youtube.com/@abc',
            'https://www.youtube.com/@abc/videos/': 'https://www.youtube.com/@abc',
            'https://www.youtube.com/@abc/shorts': 'https://www.youtube.com/@abc',
            'https://www.youtube.com/@abc/streams': 'https://www.youtube.com/@abc',
            'https://www.youtube.com/@abc/featured': 'https://www.youtube.com/@abc',
            'https://www.youtube.com/@abc/playlists': 'https://www.youtube.com/@abc',
            'https://www.youtube.com/@abc/about': 'https://www.youtube.com/@abc',
            'https://www.youtube.com/c/SomeName': 'https://www.youtube.com/c/SomeName',
            'https://youtube.com/c/a/videos': 'https://www.youtube.com/c/a',
            'https://www.youtube.com/user/legacyname': 'https://www.youtube.com/user/legacyname',
            'https://m.youtube.com/user/legacy.name/': 'https://www.youtube.com/user/legacy.name',
            # Query/fragment are ignored, never forwarded (YouTube's own
            # channel share links carry ?si=...).
            'https://youtube.com/@abc?si=XyZ123': 'https://www.youtube.com/@abc',
            'https://www.youtube.com/@abc#top': 'https://www.youtube.com/@abc',
            '  https://www.youtube.com/@abc  ': 'https://www.youtube.com/@abc',
        }
        for raw_url, lookup_url in cases.items():
            with self.subTest(url=raw_url):
                response, _, extract_info = self.resolve_ok(raw_url)
                self.assertEqual(response.status_code, 200, response.content)
                extract_info.assert_called_once_with(lookup_url, download=False)

    def test_handle_length_bounds(self):
        self.enable_bridge()
        for handle in ('abc', 'a' * 30):
            with self.subTest(handle=handle):
                response, _, _ = self.resolve_ok(f'https://www.youtube.com/@{handle}')
                self.assertEqual(response.status_code, 200)

    def test_name_length_bounds(self):
        self.enable_bridge()
        for name in ('a', 'a' * 100):
            with self.subTest(name=name):
                response, _, _ = self.resolve_ok(f'https://www.youtube.com/c/{name}')
                self.assertEqual(response.status_code, 200)


class ResolveRejectedShapesTestCase(ResolveTestMixin, BridgeTestCase):

    def assert_rejected_without_lookup(self, body):
        ydl_class, _ = _mock_youtube_dl(response={'channel_id': CHANNEL_ID})
        with patch(YOUTUBE_DL, ydl_class):
            response = self.post_resolve(body)
        self.assertEqual(response.status_code, 400, response.content)
        payload = json.loads(response.content)
        assert_matches_schema(self, payload, 'Error')
        self.assertEqual(payload['code'], 'SOURCE_INVALID')
        self.assertFalse(payload['retryable'])
        ydl_class.assert_not_called()
        return payload

    def test_rejected_urls(self):
        self.enable_bridge()
        rejected = [
            'http://www.youtube.com/@abc',
            'ftp://www.youtube.com/@abc',
            '//www.youtube.com/@abc',
            'www.youtube.com/@abc',
            '@abc',
            'https://evil.example/@abc',
            'https://youtube.com.evil.example/@abc',
            'https://music.youtube.com/@abc',
            'https://youtu.be/@abc',
            'https://www.youtube.com:443/@abc',
            'https://www.youtube.com:8443/@abc',
            'https://user:pass@www.youtube.com/@abc',
            'https://user@www.youtube.com/@abc',
            'https://www.youtube.com@evil.example/@abc',
            f'https://www.youtube.com/channel/{CHANNEL_ID}',
            'https://www.youtube.com/watch?v=dQw4w9WgXcQ',
            'https://www.youtube.com/playlist?list=PLabcdefghij',
            'https://www.youtube.com/',
            'https://www.youtube.com',
            'https://www.youtube.com/abc',
            'https://www.youtube.com/@abc/videos/extra',
            'https://www.youtube.com/@abc/community',
            'https://www.youtube.com/@abc/VIDEOS',
            'https://www.youtube.com/@abc//',
            'https://www.youtube.com/@abc/videos/shorts',
            'https://www.youtube.com/c/',
            'https://www.youtube.com/user/',
            'https://www.youtube.com/c/name/extra',
            'https://www.youtube.com/C/name',
            'https://www.youtube.com/@ab',
            'https://www.youtube.com/@' + 'a' * 31,
            'https://www.youtube.com/c/' + 'a' * 101,
            'https://www.youtube.com/@ab%63',
            'https://www.youtube.com/@abc%2Fvideos',
            'https://www.youtube.com/@ab c',
            'https://www.youtube.com/@abc!',
            'https://www.youtube.com/@abéc',
            'https://www.youtube.com/@@abc',
            'https://www.youtube.com/@abc/../user/x',
            'https://www.youtube.com/user/a$b',
            'https://[::1]/@abc',
            '',
            ' ',
        ]
        for url in rejected:
            with self.subTest(url=url):
                self.assert_rejected_without_lookup({'url': url})

    def test_error_detail_never_echoes_the_url(self):
        self.enable_bridge()
        payload = self.assert_rejected_without_lookup(
            {'url': 'https://evil.example/@abc-marker'},
        )
        self.assertNotIn('abc-marker', payload['detail'])

    def test_missing_url_returns_400(self):
        self.enable_bridge()
        self.assert_rejected_without_lookup({})

    def test_extra_key_returns_400(self):
        self.enable_bridge()
        self.assert_rejected_without_lookup(
            {'url': 'https://www.youtube.com/@abc', 'sourceType': 'channel'},
        )

    def test_non_string_url_returns_400(self):
        self.enable_bridge()
        for value in (None, 123, True, ['https://www.youtube.com/@abc'], {'u': 1}):
            with self.subTest(value=value):
                self.assert_rejected_without_lookup({'url': value})

    def test_url_over_2048_chars_returns_400(self):
        self.enable_bridge()
        self.assert_rejected_without_lookup({'url': 'https://www.youtube.com/@abc?' + 'x' * 2048})

    def test_non_object_body_returns_400(self):
        self.enable_bridge()
        for body in (['https://www.youtube.com/@abc'], 'https://www.youtube.com/@abc', None):
            with self.subTest(body=body):
                self.assert_rejected_without_lookup(body)

    def test_malformed_json_returns_400(self):
        self.enable_bridge()
        response = self.client.post(
            RESOLVE_URL, data=b'{not json', content_type='application/json',
            **self.auth_header(),
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(json.loads(response.content)['code'], 'SOURCE_INVALID')


class ResolveLookupTestCase(ResolveTestMixin, BridgeTestCase):

    def test_success_body_matches_contract(self):
        self.enable_bridge()
        response, _, _ = self.resolve_ok('https://www.youtube.com/@abc')
        self.assertEqual(response.status_code, 200)
        body = json.loads(response.content)
        assert_matches_schema(self, body, 'ResolvedSource')
        self.assertEqual(body, {
            'sourceType': 'channel',
            'canonicalKey': CHANNEL_ID,
            'canonicalUrl': f'https://www.youtube.com/channel/{CHANNEL_ID}',
            'displayName': 'Some Channel',
        })
        self.assertIn('X-Request-ID', response)

    def test_works_in_read_only_mode_and_persists_nothing(self):
        # MEDIANEST_BRIDGE_READ_ONLY defaults true.
        self.enable_bridge()
        response, _, _ = self.resolve_ok('https://www.youtube.com/@abc')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Source.objects.count(), 0)

    def test_lookup_options_match_get_channel_id_and_keep_cookies(self):
        self.enable_bridge()
        with patch('sync.youtube.get_yt_opts', return_value={'cookiefile': 'cookies.txt'}):
            _, ydl_class, _ = self.resolve_ok('https://www.youtube.com/@abc')
        opts = ydl_class.call_args.args[0]
        self.assertEqual(opts['cookiefile'], 'cookies.txt')
        self.assertTrue(opts['extract_flat'])
        self.assertEqual(opts['playlist_items'], '1')
        self.assertTrue(opts['skip_download'])
        self.assertTrue(opts['simulate'])
        self.assertFalse(opts['check_formats'])
        self.assertEqual(opts['socket_timeout'], channel_resolve.LOOKUP_SOCKET_TIMEOUT_SECONDS)

    def test_display_name_fallbacks(self):
        self.enable_bridge()
        cases = [
            ({'channel': '  Chan  ', 'uploader': 'Up', 'title': 'T'}, 'Chan'),
            ({'channel': '   ', 'uploader': 'Up', 'title': 'T'}, 'Up'),
            ({'channel': None, 'uploader': None, 'title': 'Title - Videos'}, 'Title - Videos'),
            ({}, None),
            ({'channel': '', 'uploader': ' ', 'title': None}, None),
            ({'channel': 123, 'title': 'T'}, 'T'),
            ({'channel': 'x' * 250}, 'x' * 200),
        ]
        for extra, expected in cases:
            with self.subTest(response=extra):
                response, _, _ = self.resolve_ok(
                    'https://www.youtube.com/@abc', response={'channel_id': CHANNEL_ID, **extra},
                )
                self.assertEqual(response.status_code, 200)
                body = json.loads(response.content)
                assert_matches_schema(self, body, 'ResolvedSource')
                self.assertEqual(body['displayName'], expected)

    def test_invalid_channel_id_returns_400(self):
        self.enable_bridge()
        for lookup_response in (
            {'channel_id': None},
            {},
            {'channel_id': 'PLabcdefghijklmnopqrstuv'},
            {'channel_id': 'UCshort'},
            {'channel_id': CHANNEL_ID + 'x'},
            {'channel_id': CHANNEL_ID + '\n'},
            {'channel_id': 'UCabcdefghijklmnopqrst!v'},
            {'channel_id': 12345},
            None,
            'not a dict',
        ):
            with self.subTest(response=lookup_response):
                response, _, _ = self.resolve_ok(
                    'https://www.youtube.com/@abc', response=lookup_response,
                )
                self.assertEqual(response.status_code, 400)
                body = json.loads(response.content)
                assert_matches_schema(self, body, 'Error')
                self.assertEqual(body['code'], 'SOURCE_INVALID')
                self.assertIn('no valid channel id', body['detail'])

    def lookup_error(self, error):
        ydl_class, _ = _mock_youtube_dl(error=error)
        with patch(YOUTUBE_DL, ydl_class), \
                patch('medianest_bridge.views_resolve.log') as mock_log:
            response = self.post_resolve({'url': 'https://www.youtube.com/@abc'})
        return response, mock_log

    def test_not_found_messages_map_to_404(self):
        self.enable_bridge()
        messages = [
            ('ERROR: [youtube:tab] @abc: Unable to download API page: HTTP Error 404: Not Found '
             '(caused by <HTTPError 404: Not Found>)'),
            ('ERROR: [youtube:tab] @abc: The channel/playlist does not exist and the URL '
             'redirected to youtube.com home page'),
            'ERROR: [youtube:tab] UCabc: YouTube said: This channel does not exist.',
            'ERROR: [youtube:tab] @abc: Failed to resolve url (does the playlist exist?)',
        ]
        for message in messages:
            with self.subTest(message=message):
                response, mock_log = self.lookup_error(yt_dlp.utils.DownloadError(message))
                self.assertEqual(response.status_code, 404)
                body = json.loads(response.content)
                assert_matches_schema(self, body, 'Error')
                self.assertEqual(body['code'], 'SOURCE_NOT_FOUND')
                self.assertFalse(body['retryable'])
                self.assertNotIn('youtube:tab', body['detail'])
                mock_log.warning.assert_called_once()
                mock_log.error.assert_not_called()
                mock_log.exception.assert_not_called()

    def test_structural_http_404_maps_to_404(self):
        self.enable_bridge()
        response, _ = self.lookup_error(_http_404_download_error())
        self.assertEqual(response.status_code, 404)
        self.assertEqual(json.loads(response.content)['code'], 'SOURCE_NOT_FOUND')

    def test_other_failures_map_to_retryable_503(self):
        self.enable_bridge()
        messages = [
            ('ERROR: [youtube:tab] @abc: Unable to download API page: HTTP Error 503: '
             'Service Unavailable'),
            'ERROR: [youtube:tab] @abc: Unable to download webpage: <urlopen error timed out>',
            ('ERROR: [youtube:tab] @abc: Sign in to confirm you are not a bot. '
             'cookiefile=/config/cookies.txt auth_token=s3cr3tvalue'),
            'ERROR: [youtube:tab] @abc: HTTP Error 429: Too Many Requests',
        ]
        for message in messages:
            with self.subTest(message=message):
                response, mock_log = self.lookup_error(yt_dlp.utils.DownloadError(message))
                self.assertEqual(response.status_code, 503)
                body = json.loads(response.content)
                assert_matches_schema(self, body, 'Error')
                self.assertEqual(body['code'], 'PROVIDER_UNAVAILABLE')
                self.assertTrue(body['retryable'])
                content = response.content.decode('utf-8')
                self.assertNotIn('youtube:tab', content)
                self.assertNotIn('cookies.txt', content)
                self.assertNotIn('s3cr3tvalue', content)
                mock_log.warning.assert_called_once()
                mock_log.error.assert_not_called()
                mock_log.exception.assert_not_called()
                # The server-side log line is sanitized too.
                self.assertNotIn('s3cr3tvalue', str(mock_log.warning.call_args))
                self.assertNotIn('/config/cookies.txt', str(mock_log.warning.call_args))

    def test_unexpected_exception_is_sanitized_500(self):
        self.enable_bridge()
        response, _ = self.lookup_error(RuntimeError('boom /config/cookies.txt'))
        self.assertEqual(response.status_code, 500)
        self.assertNotIn('boom', response.content.decode('utf-8'))


class ResolveGatingTestCase(ResolveTestMixin, BridgeTestCase):

    def test_route_is_not_swallowed_by_source_detail(self):
        self.assertEqual(resolve(RESOLVE_URL).url_name, 'source-resolve')

    def test_get_is_not_allowed(self):
        self.enable_bridge()
        response = self.client.get(RESOLVE_URL, **self.auth_header())
        self.assertEqual(response.status_code, 405)

    def test_requires_bearer_token_and_never_looks_up(self):
        self.enable_bridge()
        ydl_class, _ = _mock_youtube_dl(response={'channel_id': CHANNEL_ID})
        with patch(YOUTUBE_DL, ydl_class):
            response = post_json(
                self.client, RESOLVE_URL, {'url': 'https://www.youtube.com/@abc'},
            )
        self.assertEqual(response.status_code, 401)
        assert_matches_schema(self, json.loads(response.content), 'Error')
        ydl_class.assert_not_called()

    def test_disabled_bridge_returns_503(self):
        response = post_json(
            self.client, RESOLVE_URL, {'url': 'https://www.youtube.com/@abc'},
            **self.auth_header(),
        )
        self.assertEqual(response.status_code, 503)

    def test_oversized_body_returns_413(self):
        self.enable_bridge(MEDIANEST_BRIDGE_MAX_BODY_BYTES='64')
        ydl_class, _ = _mock_youtube_dl(response={'channel_id': CHANNEL_ID})
        with patch(YOUTUBE_DL, ydl_class):
            response = self.post_resolve({'url': 'https://www.youtube.com/@abc?' + 'x' * 100})
        self.assertEqual(response.status_code, 413)
        self.assertEqual(json.loads(response.content)['code'], 'REQUEST_TOO_LARGE')
        ydl_class.assert_not_called()


class ResolveCapabilityTestCase(BridgeTestCase):

    def test_resolve_channel_handle_is_true(self):
        self.enable_bridge()
        response = self.client.get(CAPS_URL, **self.auth_header())
        body = json.loads(response.content)
        assert_matches_schema(self, body, 'Capabilities')
        self.assertIs(body['resolveChannelHandle'], True)


class YtDlpLoggerTestCase(BridgeTestCase):

    def test_yt_dlp_errors_are_logged_at_warning(self):
        with patch('medianest_bridge.channel_resolve.log') as mock_log:
            channel_resolve._YtDlpLogger().error('ERROR: [youtube:tab] HTTP Error 404')
        mock_log.warning.assert_called_once()
        mock_log.error.assert_not_called()

    def test_yt_dlp_messages_are_sanitized_and_bounded(self):
        secret = 'ERROR: cookies at /config/cookies.txt token=abc123secret '
        with patch('medianest_bridge.channel_resolve.log') as mock_log:
            logger = channel_resolve._YtDlpLogger()
            logger.error(secret + 'x' * 2000)
            logger.debug(secret)
        for call in (mock_log.warning.call_args, mock_log.debug.call_args):
            logged = call.args[1]
            self.assertNotIn('/config/cookies.txt', logged)
            self.assertNotIn('abc123secret', logged)
        self.assertLessEqual(len(mock_log.warning.call_args.args[1]), 500)

    def test_not_found_detection(self):
        self.assertTrue(channel_resolve._is_not_found(_http_404_download_error()))
        self.assertTrue(channel_resolve._is_not_found(
            yt_dlp.utils.DownloadError('YouTube said: This channel does not exist.'),
        ))
        self.assertFalse(channel_resolve._is_not_found(
            yt_dlp.utils.DownloadError('HTTP Error 500: Internal Server Error'),
        ))
