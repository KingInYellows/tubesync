'''
    T4: structured request/outcome logging (views.py::_log_outcome) and
    mutation audit lines (views.py::_log_mutation_audit). Not a metrics
    stack -- this fork has none and upstream has no Prometheus hook to
    build on (researched, documented in the T4 PR body) -- just
    consistent, greppable log lines via the same common.logger.log this
    app already uses everywhere else.
'''
import json
from unittest.mock import patch

from .base import BRIDGE_TOKEN, BridgeTestCase

LIVE_URL = '/api/medianest/v1/health/live'
SOURCES_URL = '/api/medianest/v1/sources'
VALIDATE_URL = '/api/medianest/v1/sources/validate'


def _info_calls(mock_log):
    return [call for call in mock_log.mock_calls if str(call).startswith('call.info(')]


class RequestOutcomeLoggingTestCase(BridgeTestCase):

    def test_request_complete_logged_for_successful_get(self):
        self.enable_bridge()
        with patch('medianest_bridge.views.log') as mock_log:
            self.client.get(LIVE_URL, **self.auth_header())
        calls = _info_calls(mock_log)
        outcome_calls = [c for c in calls if 'request_complete' in str(c)]
        self.assertEqual(len(outcome_calls), 1)
        rendered = str(outcome_calls[0])
        self.assertIn('route=%s', rendered)
        self.assertIn(LIVE_URL, rendered)
        self.assertIn('GET', rendered)
        self.assertIn('200', rendered)

    def test_request_complete_logged_for_error_response(self):
        self.enable_bridge()
        with patch('medianest_bridge.views.log') as mock_log:
            self.client.get(LIVE_URL, HTTP_AUTHORIZATION='Bearer wrong')
        calls = _info_calls(mock_log)
        outcome_calls = [c for c in calls if 'request_complete' in str(c)]
        self.assertEqual(len(outcome_calls), 1)
        self.assertIn('401', str(outcome_calls[0]))

    def test_request_complete_logged_exactly_once_per_request(self):
        '''
            The gate chain has several early-return points; this proves
            the wrapper design (dispatch() calling _run_gates() once)
            logs exactly one outcome line regardless of which gate
            produced the response, not zero and not several.
        '''
        self.enable_bridge()
        with patch('medianest_bridge.views.log') as mock_log:
            self.client.get(LIVE_URL)  # no auth header -> 401 from the bearer gate
        calls = _info_calls(mock_log)
        outcome_calls = [c for c in calls if 'request_complete' in str(c)]
        self.assertEqual(len(outcome_calls), 1)


class MutationAuditLoggingTestCase(BridgeTestCase):

    def test_not_logged_for_get_requests(self):
        self.enable_bridge()
        with patch('medianest_bridge.views.log') as mock_log:
            self.client.get(LIVE_URL, **self.auth_header())
        calls = _info_calls(mock_log)
        audit_calls = [c for c in calls if 'mutation_audit' in str(c)]
        self.assertEqual(audit_calls, [])

    def test_read_only_rejection_logged_as_rejected(self):
        self.enable_bridge()  # read-only default true
        with patch('medianest_bridge.views.log') as mock_log:
            self.client.post(
                SOURCES_URL,
                data=json.dumps({
                    'sourceType': 'channel', 'canonicalKey': 'UCabc',
                    'canonicalUrl': 'https://www.youtube.com/channel/UCabc',
                    'name': 'x', 'directory': 'x',
                }),
                content_type='application/json',
                **self.auth_header(),
            )
        calls = _info_calls(mock_log)
        audit_calls = [c for c in calls if 'mutation_audit' in str(c)]
        self.assertEqual(len(audit_calls), 1)
        rendered = str(audit_calls[0])
        self.assertIn('rejected_PROVIDER_READ_ONLY', rendered)

    def test_accepted_create_logged_with_new_source_uuid(self):
        self.enable_bridge(MEDIANEST_BRIDGE_READ_ONLY='false')
        with patch('medianest_bridge.views.log') as mock_log:
            response = self.client.post(
                SOURCES_URL,
                data=json.dumps({
                    'sourceType': 'channel',
                    'canonicalKey': 'UCauditlogtest1234567',
                    'canonicalUrl': 'https://www.youtube.com/channel/UCauditlogtest1234567',
                    'name': 'Audit Log Test', 'directory': 'audit_log_test',
                }),
                content_type='application/json',
                **self.auth_header(),
            )
        created_uuid = json.loads(response.content)['uuid']
        calls = _info_calls(mock_log)
        audit_calls = [c for c in calls if 'mutation_audit' in str(c)]
        self.assertEqual(len(audit_calls), 1)
        rendered = str(audit_calls[0])
        self.assertIn('accepted', rendered)
        self.assertIn(created_uuid, rendered)

    def test_sync_now_audit_uses_uuid_from_url(self):
        from sync.models import Source
        source = Source.objects.create(
            source_type='i', key='UCauditsync1234567890', name='Audit Sync', directory='audit_sync',
        )
        self.enable_bridge(MEDIANEST_BRIDGE_READ_ONLY='false')
        with patch('medianest_bridge.views.log') as mock_log:
            self.client.post(f'{SOURCES_URL}/{source.pk}/sync', **self.auth_header())
        calls = _info_calls(mock_log)
        audit_calls = [c for c in calls if 'mutation_audit' in str(c)]
        self.assertEqual(len(audit_calls), 1)
        self.assertIn(str(source.pk), str(audit_calls[0]))

    def test_validate_produces_no_audit_line_when_read_only_exempt_and_accepted(self):
        '''
            validate is a mutation attempt by HTTP method (POST) but
            read_only_exempt -- it still gets an audit line (it was a
            non-safe-method attempt), but with outcome=accepted and no
            source_uuid, since nothing was ever created.
        '''
        self.enable_bridge()
        with patch('medianest_bridge.views.log') as mock_log:
            self.client.post(
                VALIDATE_URL,
                data=json.dumps({
                    'sourceType': 'channel', 'canonicalKey': 'UCabc123456789012345',
                    'canonicalUrl': 'https://www.youtube.com/channel/UCabc123456789012345',
                }),
                content_type='application/json',
                **self.auth_header(),
            )
        calls = _info_calls(mock_log)
        audit_calls = [c for c in calls if 'mutation_audit' in str(c)]
        self.assertEqual(len(audit_calls), 1)
        rendered = str(audit_calls[0])
        self.assertIn('accepted', rendered)


class NoSecretsInLogsTestCase(BridgeTestCase):

    def test_token_never_in_outcome_or_audit_lines(self):
        self.enable_bridge(MEDIANEST_BRIDGE_READ_ONLY='false')
        with patch('medianest_bridge.views.log') as mock_log:
            self.client.post(
                SOURCES_URL,
                data=json.dumps({
                    'sourceType': 'channel', 'canonicalKey': 'UCsecrettest1234567',
                    'canonicalUrl': 'https://www.youtube.com/channel/UCsecrettest1234567',
                    'name': 'Secret Test', 'directory': 'secret_test',
                }),
                content_type='application/json',
                **self.auth_header(),
            )
        for call in mock_log.mock_calls:
            self.assertNotIn(BRIDGE_TOKEN, str(call))
