'''
    Verifies the BASICAUTH_ALWAYS_ALLOW_URIS exemption (the third of the
    three enumerated upstream touches) actually does what it needs to:

      1. Every bridge route is reachable using the bearer token alone, with
         no Basic Auth credentials, even when Basic Auth is enabled
         app-wide.
      2. A non-bridge route is still challenged by Basic Auth when it is
         enabled -- proving the exemption is scoped to the bridge, not a
         global bypass.
      3. The exemption tuple's bridge entries and the bridge's actual routes
         never drift apart. common/middleware.py's BasicAuthMiddleware does
         an exact `request.path in BASICAUTH_ALWAYS_ALLOW_URIS` match, not a
         prefix match (confirmed by reading that file), so a T2/T3 route
         added without a matching settings.py entry would silently fall
         back to being challenged by Basic Auth instead of the bridge's own
         auth -- this test catches that drift immediately rather than
         relying on someone remembering to update both places.
'''
import base64

from django.conf import settings
from django.test import override_settings
from django.urls import reverse

from medianest_bridge import urls as bridge_urls

from .base import BridgeTestCase

BASIC_AUTH_OVERRIDES = dict(
    BASICAUTH_DISABLE=False,
    BASICAUTH_USERS={'admin': 'super-secret-password'},
)


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

    def test_exemption_tuple_matches_bridge_routes_exactly(self):
        expected_paths = {
            reverse(f'medianest_bridge:{pattern.name}')
            for pattern in bridge_urls.urlpatterns
        }
        exempted = set(settings.BASICAUTH_ALWAYS_ALLOW_URIS)
        bridge_exempted = {p for p in exempted if p.startswith('/api/medianest/v1/')}
        self.assertEqual(
            bridge_exempted, expected_paths,
            'BASICAUTH_ALWAYS_ALLOW_URIS bridge entries drifted from the '
            'actual bridge routes -- a route was added/renamed without a '
            'matching settings.py update, or vice versa.',
        )
