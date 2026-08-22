import json
import uuid
from unittest.mock import patch

from django.test import RequestFactory

from ..views import BridgeView, HealthLiveView
from .base import BRIDGE_TOKEN, BridgeTestCase

LIVE_URL = '/api/medianest/v1/health/live'
READY_URL = '/api/medianest/v1/health/ready'
META_URL = '/api/medianest/v1/meta'
CAPS_URL = '/api/medianest/v1/capabilities'


class DisabledBridgeTestCase(BridgeTestCase):

    def test_no_token_configured_returns_503(self):
        response = self.client.get(LIVE_URL, **self.auth_header())
        self.assertEqual(response.status_code, 503)
        body = json.loads(response.content)
        self.assertEqual(body['code'], 'PROVIDER_UNAVAILABLE')
        self.assertIn('requestId', body)


class CidrGateTestCase(BridgeTestCase):

    def test_denied_ip_returns_401_not_403(self):
        # Contract's Unauthorized response covers CIDR mismatch too; there
        # is no dedicated 403 for it (only ReadOnly is 403).
        self.enable_bridge(MEDIANEST_BRIDGE_ALLOWED_CIDRS='192.168.1.68/32')
        response = self.client.get(
            LIVE_URL, HTTP_X_REAL_IP='10.0.0.1', **self.auth_header(),
        )
        self.assertEqual(response.status_code, 401)
        body = json.loads(response.content)
        self.assertEqual(body['code'], 'PROVIDER_AUTHENTICATION_FAILED')

    def test_allowed_ip_passes_through_to_auth_check(self):
        self.enable_bridge(MEDIANEST_BRIDGE_ALLOWED_CIDRS='127.0.0.1/32')
        response = self.client.get(
            LIVE_URL, HTTP_X_REAL_IP='127.0.0.1', **self.auth_header(),
        )
        self.assertEqual(response.status_code, 200)


class BearerAuthTestCase(BridgeTestCase):

    def test_missing_token_returns_401(self):
        self.enable_bridge()
        response = self.client.get(LIVE_URL)
        self.assertEqual(response.status_code, 401)
        body = json.loads(response.content)
        self.assertEqual(body['code'], 'PROVIDER_AUTHENTICATION_FAILED')

    def test_invalid_token_returns_401(self):
        self.enable_bridge()
        response = self.client.get(LIVE_URL, HTTP_AUTHORIZATION='Bearer wrong')
        self.assertEqual(response.status_code, 401)

    def test_valid_token_returns_200(self):
        self.enable_bridge()
        response = self.client.get(LIVE_URL, **self.auth_header())
        self.assertEqual(response.status_code, 200)

    def test_token_absent_from_error_response_body(self):
        self.enable_bridge()
        response = self.client.get(LIVE_URL, HTTP_AUTHORIZATION='Bearer wrong')
        self.assertNotIn(BRIDGE_TOKEN.encode(), response.content)

    def test_token_absent_from_success_response_body(self):
        self.enable_bridge()
        response = self.client.get(LIVE_URL, **self.auth_header())
        self.assertNotIn(BRIDGE_TOKEN.encode(), response.content)

    def test_token_absent_from_logs(self):
        self.enable_bridge()
        with patch('medianest_bridge.views.log') as mock_log:
            self.client.get(LIVE_URL, **self.auth_header())
            self.client.get(LIVE_URL, HTTP_AUTHORIZATION='Bearer wrong')
        for call in mock_log.mock_calls:
            self.assertNotIn(BRIDGE_TOKEN, str(call))
            self.assertNotIn('wrong', str(call))


class ReadOnlyGateTestCase(BridgeTestCase):

    def test_post_blocked_by_default(self):
        self.enable_bridge()
        response = self.client.post(LIVE_URL, **self.auth_header())
        self.assertEqual(response.status_code, 403)
        body = json.loads(response.content)
        self.assertEqual(body['code'], 'PROVIDER_READ_ONLY')

    def test_get_never_blocked_by_read_only(self):
        self.enable_bridge()
        response = self.client.get(LIVE_URL, **self.auth_header())
        self.assertEqual(response.status_code, 200)

    def test_post_with_read_only_disabled_falls_through_to_view(self):
        # health/live has no post() handler, so with the read-only gate
        # disabled the request should reach Django's own "method not
        # allowed" handling (405), proving the gate itself -- not the lack
        # of a POST implementation -- was what previously produced 403.
        self.enable_bridge(MEDIANEST_BRIDGE_READ_ONLY='false')
        response = self.client.post(LIVE_URL, **self.auth_header())
        self.assertEqual(response.status_code, 405)


class BodySizeGateTestCase(BridgeTestCase):

    def setUp(self):
        super().setUp()
        self.factory = RequestFactory()

    def test_oversized_body_returns_413(self):
        self.enable_bridge(
            MEDIANEST_BRIDGE_READ_ONLY='false',
            MEDIANEST_BRIDGE_MAX_BODY_BYTES='16',
        )
        response = self.client.post(
            LIVE_URL,
            data=b'x' * 64,
            content_type='application/octet-stream',
            **self.auth_header(),
        )
        self.assertEqual(response.status_code, 413)
        body = json.loads(response.content)
        self.assertEqual(body['code'], 'REQUEST_TOO_LARGE')

    def test_body_within_limit_not_rejected_for_size(self):
        self.enable_bridge(
            MEDIANEST_BRIDGE_READ_ONLY='false',
            MEDIANEST_BRIDGE_MAX_BODY_BYTES='1024',
        )
        response = self.client.post(
            LIVE_URL,
            data=b'x' * 16,
            content_type='application/octet-stream',
            **self.auth_header(),
        )
        # 405 (no post() handler), not 413 -- proves the body-size gate
        # passed and the request reached the view.
        self.assertEqual(response.status_code, 405)

    def test_chunked_transfer_encoding_without_content_length_returns_413(self):
        self.enable_bridge(
            MEDIANEST_BRIDGE_READ_ONLY='false',
            MEDIANEST_BRIDGE_MAX_BODY_BYTES='1024',
        )
        request = self.factory.generic(
            'POST',
            LIVE_URL,
            HTTP_TRANSFER_ENCODING='chunked',
            **self.auth_header(),
        )
        request.META.pop('CONTENT_LENGTH', None)
        response = HealthLiveView.as_view()(request)
        self.assertEqual(response.status_code, 413)
        body = json.loads(response.content)
        self.assertEqual(body['code'], 'REQUEST_TOO_LARGE')


class RequestIdTestCase(BridgeTestCase):

    def test_echoes_supplied_valid_uuid_request_id(self):
        self.enable_bridge()
        supplied = '550e8400-e29b-41d4-a716-446655440000'
        response = self.client.get(
            LIVE_URL, HTTP_X_REQUEST_ID=supplied, **self.auth_header(),
        )
        self.assertEqual(response.headers['X-Request-ID'], supplied)

    def test_generates_request_id_when_absent(self):
        self.enable_bridge()
        response = self.client.get(LIVE_URL, **self.auth_header())
        generated = response.headers.get('X-Request-ID')
        self.assertTrue(generated)
        uuid.UUID(generated)

    def test_invalid_supplied_request_id_is_replaced_with_uuid(self):
        self.enable_bridge()
        response = self.client.get(
            LIVE_URL, HTTP_X_REQUEST_ID='caller-supplied-id', **self.auth_header(),
        )
        generated = response.headers['X-Request-ID']
        self.assertNotEqual(generated, 'caller-supplied-id')
        uuid.UUID(generated)

    def test_error_envelope_request_id_matches_header(self):
        self.enable_bridge()
        supplied = '6ba7b810-9dad-11d1-80b4-00c04fd430c8'
        response = self.client.get(
            LIVE_URL, HTTP_X_REQUEST_ID=supplied, HTTP_AUTHORIZATION='Bearer wrong',
        )
        body = json.loads(response.content)
        self.assertEqual(body['requestId'], supplied)
        self.assertEqual(response.headers['X-Request-ID'], supplied)


class _BoomView(BridgeView):
    '''Test-only view that always raises, to exercise the catch-all.'''

    def get(self, request, *args, **kwargs):
        raise RuntimeError('should never reach the client: /etc/tubesync/secret')


class UnhandledExceptionTestCase(BridgeTestCase):

    def setUp(self):
        super().setUp()
        self.factory = RequestFactory()

    def test_unhandled_exception_returns_sanitized_500(self):
        self.enable_bridge()
        request = self.factory.get('/', **self.auth_header())
        with patch('medianest_bridge.views.log'):
            response = _BoomView.as_view()(request)
        self.assertEqual(response.status_code, 500)
        body = json.loads(response.content)
        self.assertEqual(body['code'], 'INTERNAL_PROVIDER_ERROR')
        self.assertNotIn('RuntimeError', response.content.decode())
        self.assertNotIn('/etc/tubesync/secret', response.content.decode())
        self.assertNotIn('Traceback', response.content.decode())

    def test_unhandled_exception_is_logged_server_side(self):
        self.enable_bridge()
        request = self.factory.get('/', **self.auth_header())
        with patch('medianest_bridge.views.log') as mock_log:
            _BoomView.as_view()(request)
        mock_log.exception.assert_called_once()
