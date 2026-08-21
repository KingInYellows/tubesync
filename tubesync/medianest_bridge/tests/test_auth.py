import os
from unittest.mock import patch

from django.test import RequestFactory

from .. import auth
from .base import BridgeTestCase


class ClientIpTestCase(BridgeTestCase):

    def setUp(self):
        super().setUp()
        self.factory = RequestFactory()

    def test_prefers_x_real_ip_over_remote_addr(self):
        request = self.factory.get(
            '/', REMOTE_ADDR='203.0.113.5', HTTP_X_REAL_IP='192.168.1.68',
        )
        self.assertEqual(auth.client_ip(request), '192.168.1.68')

    def test_falls_back_to_remote_addr_when_no_x_real_ip(self):
        request = self.factory.get('/', REMOTE_ADDR='192.168.1.68')
        self.assertEqual(auth.client_ip(request), '192.168.1.68')

    def test_does_not_trust_client_supplied_x_forwarded_for(self):
        # A client can set X-Forwarded-For itself; it must never be used as
        # the CIDR-gate signal (see auth.py's module docstring). Only
        # X-Real-IP (nginx-overwritten) or REMOTE_ADDR are trusted.
        request = self.factory.get(
            '/',
            REMOTE_ADDR='203.0.113.5',
            HTTP_X_FORWARDED_FOR='192.168.1.68, 203.0.113.5',
        )
        self.assertEqual(auth.client_ip(request), '203.0.113.5')


class CidrAllowedTestCase(BridgeTestCase):

    def setUp(self):
        super().setUp()
        self.factory = RequestFactory()

    def test_no_cidrs_configured_allows_everything(self):
        request = self.factory.get('/', REMOTE_ADDR='8.8.8.8')
        self.assertTrue(auth.cidr_allowed(request))

    def test_matching_ip_allowed(self):
        os.environ['MEDIANEST_BRIDGE_ALLOWED_CIDRS'] = '192.168.1.68/32'
        request = self.factory.get('/', HTTP_X_REAL_IP='192.168.1.68')
        self.assertTrue(auth.cidr_allowed(request))

    def test_non_matching_ip_denied(self):
        os.environ['MEDIANEST_BRIDGE_ALLOWED_CIDRS'] = '192.168.1.68/32'
        request = self.factory.get('/', HTTP_X_REAL_IP='10.0.0.1')
        self.assertFalse(auth.cidr_allowed(request))

    def test_missing_client_ip_denied_when_configured(self):
        os.environ['MEDIANEST_BRIDGE_ALLOWED_CIDRS'] = '192.168.1.68/32'
        request = self.factory.get('/', REMOTE_ADDR='')
        self.assertFalse(auth.cidr_allowed(request))


class BearerTokenValidTestCase(BridgeTestCase):

    def setUp(self):
        super().setUp()
        self.factory = RequestFactory()

    def test_no_token_configured_always_invalid(self):
        request = self.factory.get('/', HTTP_AUTHORIZATION='Bearer anything')
        self.assertFalse(auth.bearer_token_valid(request))

    def test_missing_authorization_header(self):
        self.enable_bridge()
        request = self.factory.get('/')
        self.assertFalse(auth.bearer_token_valid(request))

    def test_wrong_scheme(self):
        self.enable_bridge()
        request = self.factory.get(
            '/', HTTP_AUTHORIZATION='Basic dXNlcjpwYXNz',
        )
        self.assertFalse(auth.bearer_token_valid(request))

    def test_wrong_token(self):
        self.enable_bridge()
        request = self.factory.get('/', HTTP_AUTHORIZATION='Bearer wrong-token')
        self.assertFalse(auth.bearer_token_valid(request))

    def test_correct_token(self):
        self.enable_bridge()
        request = self.factory.get(
            '/', **self.auth_header(),
        )
        self.assertTrue(auth.bearer_token_valid(request))

    def test_uses_hmac_compare_digest(self):
        '''
            Patch-assert the actual comparison mechanism: bearer_token_valid
            must go through hmac.compare_digest, not == or any other
            comparison, so the constant-time guarantee is a property of the
            code path itself, not merely an incidental correct result.
        '''
        self.enable_bridge()
        request = self.factory.get('/', **self.auth_header())
        with patch('medianest_bridge.auth.hmac.compare_digest', return_value=True) as mocked:
            result = auth.bearer_token_valid(request)
        self.assertTrue(result)
        mocked.assert_called_once()
        called_args = mocked.call_args.args
        self.assertEqual(called_args[0], b'unit-test-bridge-token-do-not-use-in-prod')
        self.assertEqual(called_args[1], b'unit-test-bridge-token-do-not-use-in-prod')
