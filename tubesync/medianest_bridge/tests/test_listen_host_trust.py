'''
    Verifies the MEDIUM hardening fix from the T1 verifier: the CIDR gate's
    trust in X-Real-IP depends on gunicorn staying loopback-bound
    (LISTEN_HOST). Nothing enforces that at the network layer from inside
    this app, so a non-loopback LISTEN_HOST combined with a configured
    MEDIANEST_BRIDGE_ALLOWED_CIDRS must produce a loud, honest warning --
    never a silent degrade, never a hard failure (an operator may have a
    legitimate alternate ingress this app cannot verify).
'''
from unittest.mock import patch

from .. import auth
from .base import BRIDGE_TOKEN, BridgeTestCase, env_override

LIVE_URL = '/api/medianest/v1/health/live'


class ListenHostLoopbackDetectionTestCase(BridgeTestCase):
    '''Unit-level coverage of the pure predicate, independent of Django.'''

    def test_unset_listen_host_defaults_to_loopback(self):
        with env_override(LISTEN_HOST=None):
            self.assertTrue(auth._listen_host_is_loopback())

    def test_explicit_127_0_0_1_is_loopback(self):
        with env_override(LISTEN_HOST='127.0.0.1'):
            self.assertTrue(auth._listen_host_is_loopback())

    def test_localhost_string_is_loopback(self):
        with env_override(LISTEN_HOST='localhost'):
            self.assertTrue(auth._listen_host_is_loopback())

    def test_other_loopback_range_address_is_loopback(self):
        with env_override(LISTEN_HOST='127.0.0.5'):
            self.assertTrue(auth._listen_host_is_loopback())

    def test_lan_address_is_not_loopback(self):
        with env_override(LISTEN_HOST='192.168.1.68'):
            self.assertFalse(auth._listen_host_is_loopback())

    def test_all_interfaces_is_not_loopback(self):
        with env_override(LISTEN_HOST='0.0.0.0'):
            self.assertFalse(auth._listen_host_is_loopback())

    def test_unresolvable_hostname_fails_toward_warning(self):
        with env_override(LISTEN_HOST='gunicorn-host.internal'):
            self.assertFalse(auth._listen_host_is_loopback())


class CidrTrustWarningTestCase(BridgeTestCase):

    def test_no_warning_when_cidrs_not_configured(self):
        with env_override(LISTEN_HOST='192.168.1.68'):
            self.assertIsNone(auth.cidr_trust_warning())

    def test_no_warning_when_listen_host_is_loopback(self):
        self.enable_bridge(MEDIANEST_BRIDGE_ALLOWED_CIDRS='192.168.1.68/32')
        with env_override(LISTEN_HOST='127.0.0.1'):
            self.assertIsNone(auth.cidr_trust_warning())

    def test_warning_when_cidrs_configured_and_listen_host_non_loopback(self):
        self.enable_bridge(MEDIANEST_BRIDGE_ALLOWED_CIDRS='192.168.1.68/32')
        with env_override(LISTEN_HOST='192.168.1.68'):
            warning = auth.cidr_trust_warning()
        self.assertIsNotNone(warning)
        self.assertIn('LISTEN_HOST', warning)
        self.assertIn('MEDIANEST_BRIDGE_ALLOWED_CIDRS', warning)

    def test_warning_text_never_contains_the_token(self):
        self.enable_bridge(MEDIANEST_BRIDGE_ALLOWED_CIDRS='192.168.1.68/32')
        with env_override(LISTEN_HOST='192.168.1.68'):
            warning = auth.cidr_trust_warning()
        self.assertNotIn(BRIDGE_TOKEN, warning)


class DispatchWarningIntegrationTestCase(BridgeTestCase):
    '''
        End-to-end: hitting a real bridge route with the degraded
        configuration logs the warning (via the shared BridgeView.dispatch
        gate) without ever blocking the request or leaking the token.
    '''

    def test_warning_logged_on_request_when_degraded(self):
        self.enable_bridge(MEDIANEST_BRIDGE_ALLOWED_CIDRS='127.0.0.1/32')
        with env_override(LISTEN_HOST='192.168.1.68'):
            with patch('medianest_bridge.views.log') as mock_log:
                response = self.client.get(
                    LIVE_URL, HTTP_X_REAL_IP='127.0.0.1', **self.auth_header(),
                )
        self.assertEqual(response.status_code, 200)
        mock_log.warning.assert_called_once()
        warning_text = mock_log.warning.call_args.args[0]
        self.assertIn('LISTEN_HOST', warning_text)
        self.assertNotIn(BRIDGE_TOKEN, warning_text)

    def test_no_warning_logged_when_listen_host_loopback(self):
        self.enable_bridge(MEDIANEST_BRIDGE_ALLOWED_CIDRS='127.0.0.1/32')
        with env_override(LISTEN_HOST='127.0.0.1'):
            with patch('medianest_bridge.views.log') as mock_log:
                self.client.get(
                    LIVE_URL, HTTP_X_REAL_IP='127.0.0.1', **self.auth_header(),
                )
        mock_log.warning.assert_not_called()

    def test_no_warning_logged_when_no_cidrs_configured(self):
        self.enable_bridge()
        with env_override(LISTEN_HOST='192.168.1.68'):
            with patch('medianest_bridge.views.log') as mock_log:
                self.client.get(LIVE_URL, **self.auth_header())
        mock_log.warning.assert_not_called()
