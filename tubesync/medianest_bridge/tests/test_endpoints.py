'''
    Structural conformance of each T1 endpoint's response against the
    vendored contract's schemas, driven by contract_fixtures.json (stdlib
    json only -- see test_contract_conformance.py for how that fixture is
    kept honest against the vendored YAML).
'''
import json
import re
from pathlib import Path

from .base import BridgeTestCase

FIXTURES_PATH = (
    Path(__file__).resolve().parent.parent / 'contract' / 'contract_fixtures.json'
)
FIXTURES = json.loads(FIXTURES_PATH.read_text(encoding='utf-8'))
SCHEMAS = FIXTURES['schemas']

LIVE_URL = '/api/medianest/v1/health/live'
READY_URL = '/api/medianest/v1/health/ready'
META_URL = '/api/medianest/v1/meta'
CAPS_URL = '/api/medianest/v1/capabilities'


def assert_matches_schema(test, body, schema_name):
    schema = SCHEMAS[schema_name]
    for field in schema['required']:
        test.assertIn(field, body, f'{schema_name} missing required field {field!r}')
    for field, spec in schema['properties'].items():
        if field not in body:
            continue
        value = body[field]
        expected_type = spec.get('type')
        if expected_type == 'string':
            test.assertIsInstance(value, str, f'{schema_name}.{field} must be a string')
        elif expected_type == 'boolean':
            test.assertIsInstance(value, bool, f'{schema_name}.{field} must be a boolean')
        elif expected_type == 'integer':
            test.assertIsInstance(value, int, f'{schema_name}.{field} must be an integer')
        elif expected_type == 'object':
            test.assertIsInstance(value, dict, f'{schema_name}.{field} must be an object')
        elif isinstance(expected_type, list):
            if 'string' in expected_type and value is not None:
                test.assertIsInstance(value, str, f'{schema_name}.{field} must be a string or null')
        enum = spec.get('enum')
        if enum:
            test.assertIn(
                value, enum,
                f'{schema_name}.{field}={value!r} not in {enum!r}',
            )
        if field.endswith('At') and isinstance(value, str):
            test.assertRegex(
                value,
                re.compile(r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$'),
                f'{schema_name}.{field}={value!r} is not ISO-8601 date-time',
            )


class HealthLiveEndpointTestCase(BridgeTestCase):

    def test_shape_matches_contract(self):
        self.enable_bridge()
        response = self.client.get(LIVE_URL, **self.auth_header())
        self.assertEqual(response.status_code, 200)
        body = json.loads(response.content)
        assert_matches_schema(self, body, 'HealthLive')


class HealthReadyEndpointTestCase(BridgeTestCase):

    def test_shape_matches_contract(self):
        self.enable_bridge()
        response = self.client.get(READY_URL, **self.auth_header())
        self.assertEqual(response.status_code, 200)
        body = json.loads(response.content)
        assert_matches_schema(self, body, 'HealthReady')

        expected_components = set(FIXTURES['health_ready_component_names'])
        self.assertEqual(set(body['components'].keys()), expected_components)
        for name, component in body['components'].items():
            assert_matches_schema(self, component, 'ComponentStatus')

    def test_never_fabricates_healthy_for_unverifiable_components(self):
        '''
            queues, workers, youtube are not cheaply verifiable from the web
            process in T1 (see readiness.py's module docstring) and must
            report "unknown", never "healthy".
        '''
        self.enable_bridge()
        response = self.client.get(READY_URL, **self.auth_header())
        body = json.loads(response.content)
        for name in ('queues', 'workers', 'youtube'):
            self.assertEqual(body['components'][name]['status'], 'unknown')

    def test_overall_status_is_enum_valid(self):
        self.enable_bridge()
        response = self.client.get(READY_URL, **self.auth_header())
        body = json.loads(response.content)
        self.assertIn(body['status'], SCHEMAS['HealthReady']['properties']['status']['enum'])


class MetaEndpointTestCase(BridgeTestCase):

    def test_shape_matches_contract(self):
        self.enable_bridge()
        response = self.client.get(META_URL, **self.auth_header())
        self.assertEqual(response.status_code, 200)
        body = json.loads(response.content)
        assert_matches_schema(self, body, 'Meta')

    def test_upstream_commit_defaults_to_unknown_sentinel(self):
        self.enable_bridge()
        response = self.client.get(META_URL, **self.auth_header())
        body = json.loads(response.content)
        self.assertEqual(body['upstreamCommit'], 'unknown')

    def test_upstream_commit_reflects_env_when_set(self):
        self.enable_bridge(MEDIANEST_BRIDGE_UPSTREAM_SHA='a' * 40)
        response = self.client.get(META_URL, **self.auth_header())
        body = json.loads(response.content)
        self.assertEqual(body['upstreamCommit'], 'a' * 40)


class CapabilitiesEndpointTestCase(BridgeTestCase):

    def test_shape_matches_contract(self):
        self.enable_bridge()
        response = self.client.get(CAPS_URL, **self.auth_header())
        self.assertEqual(response.status_code, 200)
        body = json.loads(response.content)
        assert_matches_schema(self, body, 'Capabilities')

    def test_t1_honesty_only_health_is_true(self):
        self.enable_bridge()
        response = self.client.get(CAPS_URL, **self.auth_header())
        body = json.loads(response.content)
        self.assertTrue(body['health'])
        other_flags = {k: v for k, v in body.items() if k != 'health'}
        self.assertTrue(
            all(v is False for v in other_flags.values()),
            f'T1 must report every non-health capability as false, got {other_flags}',
        )
