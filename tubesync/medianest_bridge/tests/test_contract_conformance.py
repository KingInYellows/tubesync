'''
    Locks the checked-in JSON fixture mirror (used by test_endpoints.py, and
    stdlib json only -- no PyYAML required for these assertions to run) to
    the vendored YAML contract by sha256, and -- only when PyYAML happens to
    be importable in the current environment -- cross-checks that the JSON
    mirror was actually derived from that YAML and not hand-edited out of
    sync.

    See contract/contract_fixtures.json's "_provenance" block and this
    module's own comments for the full rationale.
'''
import hashlib
import json
from pathlib import Path

from django.test import SimpleTestCase

CONTRACT_DIR = Path(__file__).resolve().parent.parent / 'contract'
YAML_PATH = CONTRACT_DIR / 'bridge-openapi.v1.yaml'
FIXTURES_PATH = CONTRACT_DIR / 'contract_fixtures.json'


class ContractFixturesLockTestCase(SimpleTestCase):

    def test_fixtures_file_exists(self):
        self.assertTrue(FIXTURES_PATH.exists())

    def test_vendored_yaml_matches_locked_sha256(self):
        fixtures = json.loads(FIXTURES_PATH.read_text(encoding='utf-8'))
        recorded_sha256 = fixtures['_provenance']['source_sha256']
        actual_sha256 = hashlib.sha256(YAML_PATH.read_bytes()).hexdigest()
        self.assertEqual(
            actual_sha256, recorded_sha256,
            'medianest_bridge/contract/bridge-openapi.v1.yaml changed without '
            're-generating contract_fixtures.json -- regenerate the fixture '
            '(and its recorded sha256) from the new YAML.',
        )

    def test_fixture_derivation_matches_yaml_when_pyyaml_available(self):
        try:
            import yaml
        except ImportError:
            self.skipTest('PyYAML not importable in this environment')
        doc = yaml.safe_load(YAML_PATH.read_bytes())
        schemas = doc['components']['schemas']
        fixtures = json.loads(FIXTURES_PATH.read_text(encoding='utf-8'))
        for name, expected in fixtures['schemas'].items():
            live_required = schemas[name].get('required', [])
            self.assertEqual(
                sorted(expected['required']), sorted(live_required),
                f'{name}.required drifted from the live YAML',
            )
        live_component_names = sorted(
            schemas['HealthReady']['properties']['components']['required'],
        )
        self.assertEqual(
            fixtures['health_ready_component_names'], live_component_names,
        )
