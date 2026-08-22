'''
    Verifies the BasicAuth exemption for the bridge namespace via
    BASICAUTH_PREFIX_ALLOW_URIS (T2 redesign). T1 used per-route exact
    matches in BASICAUTH_ALWAYS_ALLOW_URIS, which structurally cannot cover
    path-parameterized routes like /sources/{sourceUuid} (every distinct
    UUID is a different exact path).

    common/middleware.py::BasicAuthMiddleware.process_request checks
    BASICAUTH_ALWAYS_ALLOW_URIS for exact matches and
    BASICAUTH_PREFIX_ALLOW_URIS for startswith() prefix matches.

    Coverage:
      1. The bridge prefix entry exists in BASICAUTH_PREFIX_ALLOW_URIS.
      2. Every named bridge route's reverse()d path starts with that
         prefix -- catches a route ever being added outside the bridge's
         own URL namespace.
      3. Functional: with HTTP_USER/HTTP_PASS set (Basic Auth enabled), a
         *parameterized* bridge route (a real UUID in the path) reaches
         the bridge's own auth and returns the bridge's 401
         PROVIDER_AUTHENTICATION_FAILED envelope -- not a Basic Auth
         challenge -- while a non-bridge route still gets the Basic Auth
         challenge (proves the exemption did not widen beyond the bridge
         namespace).
      4. The prefix match has an exact boundary: '/api/medianest/v1x...'
         (no slash) must NOT be treated as inside the bridge namespace and
         must still get the Basic Auth challenge.
      5. A trailing-slash entry in BASICAUTH_ALWAYS_ALLOW_URIS remains an
         exact match, not a prefix match.
'''
import base64
import json
import uuid

from django.conf import settings
from django.test import override_settings
from django.urls import reverse

from .base import BridgeTestCase

BASIC_AUTH_OVERRIDES = dict(
    BASICAUTH_DISABLE=False,
    BASICAUTH_USERS={'admin': 'super-secret-password'},
)

BRIDGE_PREFIX = '/api/medianest/v1/'


def _basic_auth_header(username, password):
    raw = f'{username}:{password}'.encode()
    return {'HTTP_AUTHORIZATION': 'Basic ' + base64.b64encode(raw).decode()}


class BasicAuthExemptionTestCase(BridgeTestCase):

    @override_settings(**BASIC_AUTH_OVERRIDES)
    def test_bridge_route_reachable_with_bearer_only_no_basic_auth(self):
        self.enable_bridge()
        response = self.client.get(
            '/api/medianest/v1/health/live', **self.auth_header(),
        )
        self.assertEqual(response.status_code, 200)

    @override_settings(**BASIC_AUTH_OVERRIDES)
    def test_non_bridge_route_still_requires_basic_auth(self):
        # '/' is sync's DashboardView, never exempted.
        response = self.client.get('/')
        self.assertEqual(response.status_code, 401)

    @override_settings(**BASIC_AUTH_OVERRIDES)
    def test_non_bridge_route_succeeds_with_correct_basic_auth(self):
        response = self.client.get('/', **_basic_auth_header('admin', 'super-secret-password'))
        self.assertNotEqual(response.status_code, 401)

    def test_prefix_entry_present(self):
        self.assertIn(BRIDGE_PREFIX, settings.BASICAUTH_PREFIX_ALLOW_URIS)

    @override_settings(
        **BASIC_AUTH_OVERRIDES,
        BASICAUTH_ALWAYS_ALLOW_URIS=('/exact-only/',),
        BASICAUTH_PREFIX_ALLOW_URIS=(),
    )
    def test_trailing_slash_exact_entry_does_not_prefix_match(self):
        '''
            A trailing-slash entry in BASICAUTH_ALWAYS_ALLOW_URIS must
            match only that exact path, not every descendant.
        '''
        response = self.client.get('/exact-only/subpath')
        self.assertEqual(response.status_code, 401)
        response = self.client.get('/exact-only/')
        self.assertNotEqual(response.status_code, 401)

    def test_every_bridge_route_is_under_the_prefix(self):
        sample_uuid = {'source_uuid': str(uuid.uuid4())}
        bridge_routes = {
            'health-live': {},
            'health-ready': {},
            'meta': {},
            'capabilities': {},
            'source-lookup': {},
            'source-detail': sample_uuid,
            'source-media': sample_uuid,
        }
        for name, kwargs in bridge_routes.items():
            path = reverse(f'medianest_bridge:{name}', kwargs=kwargs)
            self.assertTrue(
                path.startswith(BRIDGE_PREFIX),
                f'{name} resolves to {path!r}, outside the exempted bridge prefix',
            )

    @override_settings(**BASIC_AUTH_OVERRIDES)
    def test_parameterized_route_reaches_bridge_auth_not_basic_auth(self):
        '''
            The route this whole redesign exists for: a real UUID in the
            path, Basic Auth enabled, no bearer token supplied. Must get
            the bridge's own 401 envelope (proving BasicAuthMiddleware let
            it through to the bridge's dispatch()), not a Basic Auth
            challenge.
        '''
        self.enable_bridge()
        response = self.client.get(f'/api/medianest/v1/sources/{uuid.uuid4()}')
        self.assertEqual(response.status_code, 401)
        body = json.loads(response.content)
        self.assertEqual(body['code'], 'PROVIDER_AUTHENTICATION_FAILED')

    @override_settings(**BASIC_AUTH_OVERRIDES)
    def test_parameterized_route_works_end_to_end_with_bearer_token(self):
        self.enable_bridge()
        response = self.client.get(
            f'/api/medianest/v1/sources/{uuid.uuid4()}', **self.auth_header(),
        )
        # 404 SOURCE_NOT_FOUND (no such source), not a Basic Auth
        # challenge and not a routing 404 -- proves the full path from
        # Basic Auth exemption through the bridge's own auth to the view
        # works for a parameterized route.
        self.assertEqual(response.status_code, 404)
        body = json.loads(response.content)
        self.assertEqual(body['code'], 'SOURCE_NOT_FOUND')

    @override_settings(**BASIC_AUTH_OVERRIDES)
    def test_prefix_match_has_exact_boundary(self):
        '''
            '/api/medianest/v1x' (no slash before 'x') must NOT match the
            '/api/medianest/v1/' prefix -- startswith() on a string missing
            the trailing slash would otherwise be too permissive.
        '''
        response = self.client.get('/api/medianest/v1xsomething')
        self.assertEqual(response.status_code, 401)
        self.assertNotIn(b'PROVIDER_AUTHENTICATION_FAILED', response.content)
