import os

from .. import config
from .base import BridgeTestCase


class TokenConfigTestCase(BridgeTestCase):

    def test_no_token_file_configured_is_disabled(self):
        self.assertIsNone(config.get_token())
        self.assertFalse(config.bridge_enabled())

    def test_token_file_missing_on_disk_is_disabled(self):
        os.environ['MEDIANEST_BRIDGE_TOKEN_FILE'] = '/nonexistent/path/token.txt'
        self.assertIsNone(config.get_token())
        self.assertFalse(config.bridge_enabled())

    def test_empty_token_file_is_disabled(self):
        path = self.enable_bridge(token='   ')
        self.assertIsNone(config.get_token())
        self.assertFalse(config.bridge_enabled())
        self.assertTrue(os.path.exists(path))

    def test_valid_token_file_enables_bridge(self):
        self.enable_bridge(token='real-token')
        self.assertEqual(config.get_token(), 'real-token')
        self.assertTrue(config.bridge_enabled())

    def test_token_is_stripped(self):
        self.enable_bridge(token='  real-token  \n')
        self.assertEqual(config.get_token(), 'real-token')


class ReadOnlyConfigTestCase(BridgeTestCase):

    def test_defaults_to_read_only(self):
        self.assertTrue(config.is_read_only())

    def test_explicit_false_disables_read_only(self):
        os.environ['MEDIANEST_BRIDGE_READ_ONLY'] = 'false'
        self.assertFalse(config.is_read_only())

    def test_explicit_true(self):
        os.environ['MEDIANEST_BRIDGE_READ_ONLY'] = 'true'
        self.assertTrue(config.is_read_only())

    def test_garbage_value_fails_closed_to_read_only(self):
        os.environ['MEDIANEST_BRIDGE_READ_ONLY'] = 'nonsense'
        self.assertTrue(config.is_read_only())


class MaxBodyBytesConfigTestCase(BridgeTestCase):

    def test_default_is_65536(self):
        self.assertEqual(config.max_body_bytes(), 65536)

    def test_override(self):
        os.environ['MEDIANEST_BRIDGE_MAX_BODY_BYTES'] = '1024'
        self.assertEqual(config.max_body_bytes(), 1024)


class AllowedCidrsConfigTestCase(BridgeTestCase):

    def test_unset_returns_none(self):
        self.assertIsNone(config.allowed_cidrs())

    def test_single_cidr(self):
        os.environ['MEDIANEST_BRIDGE_ALLOWED_CIDRS'] = '192.168.1.68/32'
        nets = config.allowed_cidrs()
        self.assertEqual(len(nets), 1)
        self.assertEqual(str(nets[0]), '192.168.1.68/32')

    def test_multiple_comma_separated(self):
        os.environ['MEDIANEST_BRIDGE_ALLOWED_CIDRS'] = '192.168.1.68/32, 10.0.0.0/24'
        nets = config.allowed_cidrs()
        self.assertEqual(len(nets), 2)

    def test_malformed_entry_fails_closed_to_empty_list(self):
        os.environ['MEDIANEST_BRIDGE_ALLOWED_CIDRS'] = 'not-a-cidr'
        self.assertEqual(config.allowed_cidrs(), [])


class UpstreamShaConfigTestCase(BridgeTestCase):

    def test_default_is_unknown(self):
        self.assertEqual(config.upstream_sha(), 'unknown')

    def test_override(self):
        os.environ['MEDIANEST_BRIDGE_UPSTREAM_SHA'] = 'a' * 40
        self.assertEqual(config.upstream_sha(), 'a' * 40)
