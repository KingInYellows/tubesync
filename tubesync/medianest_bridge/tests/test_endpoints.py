'''
    Structural conformance of each T1 endpoint's response against the
    vendored contract's schemas, driven by contract_fixtures.json (stdlib
    json only -- see test_contract_conformance.py for how that fixture is
    kept honest against the vendored YAML).
'''
import json
import re
from pathlib import Path
from unittest.mock import Mock, patch

from .. import readiness
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


def _is_json_type(value, type_name):
    '''
        JSON-Schema-flavored type check. Bool is deliberately NOT treated
        as a valid "integer"/"number" match even though Python's bool is
        an int subclass -- a JSON boolean is not a JSON integer, and a
        contract field typed "integer" that actually received True/False
        should fail this check, not pass it by Python subtyping accident.
    '''
    if type_name == 'boolean':
        return isinstance(value, bool)
    if type_name == 'integer':
        return isinstance(value, int) and not isinstance(value, bool)
    if type_name == 'number':
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if type_name == 'string':
        return isinstance(value, str)
    if type_name == 'array':
        return isinstance(value, list)
    if type_name == 'object':
        return isinstance(value, dict)
    if type_name == 'null':
        return value is None
    # Unrecognized type name in the fixture -- don't fail on it, the enum/
    # pattern/required checks still cover what they can.
    return True


def assert_matches_schema(test, body, schema_name):
    schema = SCHEMAS[schema_name]
    for field in schema['required']:
        test.assertIn(field, body, f'{schema_name} missing required field {field!r}')
    for field, spec in schema['properties'].items():
        if field not in body:
            continue
        value = body[field]
        enum = spec.get('enum')
        if enum:
            test.assertIn(
                value, enum,
                f'{schema_name}.{field}={value!r} not in {enum!r}',
            )
        type_spec = spec.get('type')
        if type_spec:
            type_names = type_spec if isinstance(type_spec, list) else [type_spec]
            test.assertTrue(
                any(_is_json_type(value, name) for name in type_names),
                f'{schema_name}.{field}={value!r} ({type(value).__name__}) '
                f'does not match declared type(s) {type_names!r}',
            )
        pattern = spec.get('pattern')
        if pattern and isinstance(value, str):
            # The contract's own pattern descriptions (e.g.
            # relativePath's) explicitly say the pattern does not apply
            # to a null value -- only enforce it when the field actually
            # is a string.
            test.assertIsNotNone(
                re.match(pattern, value),
                f'{schema_name}.{field}={value!r} does not match pattern {pattern!r}',
            )


class HealthLiveEndpointTestCase(BridgeTestCase):

    def test_shape_matches_contract(self):
        self.enable_bridge()
        response = self.client.get(LIVE_URL, **self.auth_header())
        self.assertEqual(response.status_code, 200)
        body = json.loads(response.content)
        assert_matches_schema(self, body, 'HealthLive')


class HealthReadyEndpointTestCase(BridgeTestCase):
    '''
        youtube (T-side follow-up to M6b) is a real network probe now, not
        permanently "unknown" -- readiness.requests.get is mocked for
        every test in this class so /health/ready never makes a live
        network call from this suite (this program's "no live YouTube in
        CI" rule), and the readiness cache is reset around each test so
        no mocked/real result leaks between tests. See
        test_readiness.py::YoutubeProbeTestCase for the probe's own
        success/timeout/refused/disabled/caching behavior in detail --
        this class only needs one fixed, known response to assert
        contract shape and endpoint-level wiring.
    '''

    def setUp(self):
        super().setUp()
        readiness._reset_cache()
        self.addCleanup(readiness._reset_cache)
        mock_response = Mock()
        mock_response.status_code = 204
        patcher = patch.object(readiness.requests, 'get', return_value=mock_response)
        patcher.start()
        self.addCleanup(patcher.stop)

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
            queues and workers became real checks in T4
            (readiness.py::check_workers/check_queues, via s6-svstat
            against /run/service) but this test environment doesn't run
            under s6-overlay, so they correctly fall back to "unknown"
            here -- see test_readiness.py for the healthy/degraded/
            unavailable cases with /run/service mocked.
        '''
        self.enable_bridge()
        response = self.client.get(READY_URL, **self.auth_header())
        body = json.loads(response.content)
        for name in ('queues', 'workers'):
            self.assertEqual(body['components'][name]['status'], 'unknown')

    def test_youtube_reports_the_mocked_probe_result(self):
        '''
            Unlike queues/workers above, youtube IS verifiable in this
            environment now -- it's a real probe, mocked here to a fixed
            204 so this asserts the endpoint actually wires the probe's
            real result through, not just a fixed "unknown".
        '''
        self.enable_bridge()
        response = self.client.get(READY_URL, **self.auth_header())
        body = json.loads(response.content)
        self.assertEqual(body['components']['youtube']['status'], 'healthy')

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

    def test_health_is_always_true(self):
        self.enable_bridge()
        response = self.client.get(CAPS_URL, **self.auth_header())
        body = json.loads(response.content)
        self.assertTrue(body['health'])
        # The full "which flags are true" assertion, including T2's
        # readSources/readMedia, lives in
        # tests/test_sources.py::CapabilitiesReflectsT2TestCase -- kept
        # there so this file doesn't need updating every time a slice
        # flips another capability true.
