'''
    T2 endpoint tests: GET /sources (key lookup), GET /sources/{sourceUuid},
    GET /sources/{sourceUuid}/media (paginated). Contract-shape assertions
    reuse test_endpoints.py's assert_matches_schema/SCHEMAS rather than
    duplicating them.
'''
import json
import uuid

from django.utils import timezone

from sync.choices import Val, YouTube_SourceType
from sync.models import Media, Source

from .base import BridgeTestCase
from .test_endpoints import assert_matches_schema

SOURCES_URL = '/api/medianest/v1/sources'


def make_source(**overrides):
    defaults = dict(
        source_type=Val(YouTube_SourceType.CHANNEL),
        key='UC_test_channel',
        name='Test Channel',
        directory='test_channel',
    )
    defaults.update(overrides)
    return Source.objects.create(**defaults)


def make_media(source, **overrides):
    defaults = dict(source=source, key='video1', title='Test Video', published=timezone.now())
    defaults.update(overrides)
    return Media.objects.create(**defaults)


class SourceLookupEndpointTestCase(BridgeTestCase):

    def test_missing_key_returns_400(self):
        self.enable_bridge()
        response = self.client.get(SOURCES_URL, **self.auth_header())
        self.assertEqual(response.status_code, 400)
        self.assertEqual(json.loads(response.content)['code'], 'SOURCE_INVALID')

    def test_empty_key_returns_400(self):
        self.enable_bridge()
        response = self.client.get(SOURCES_URL + '?key=', **self.auth_header())
        self.assertEqual(response.status_code, 400)

    def test_no_match_returns_empty_data(self):
        self.enable_bridge()
        response = self.client.get(SOURCES_URL + '?key=nonexistent', **self.auth_header())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(response.content), {'data': []})

    def test_match_returns_single_item_matching_contract(self):
        source = make_source()
        self.enable_bridge()
        response = self.client.get(
            SOURCES_URL + f'?key={source.key}', **self.auth_header(),
        )
        self.assertEqual(response.status_code, 200)
        body = json.loads(response.content)
        self.assertEqual(len(body['data']), 1)
        assert_matches_schema(self, body['data'][0], 'Source')
        self.assertEqual(body['data'][0]['uuid'], str(source.pk))

    def test_requires_auth(self):
        self.enable_bridge()
        response = self.client.get(SOURCES_URL + '?key=x')
        self.assertEqual(response.status_code, 401)


class SourceDetailEndpointTestCase(BridgeTestCase):

    def test_malformed_uuid_returns_404_source_not_found(self):
        self.enable_bridge()
        response = self.client.get(
            f'{SOURCES_URL}/not-a-uuid', **self.auth_header(),
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(json.loads(response.content)['code'], 'SOURCE_NOT_FOUND')

    def test_wellformed_but_nonexistent_uuid_returns_404(self):
        self.enable_bridge()
        response = self.client.get(
            f'{SOURCES_URL}/{uuid.uuid4()}', **self.auth_header(),
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(json.loads(response.content)['code'], 'SOURCE_NOT_FOUND')

    def test_existing_source_matches_contract(self):
        source = make_source()
        self.enable_bridge()
        response = self.client.get(
            f'{SOURCES_URL}/{source.pk}', **self.auth_header(),
        )
        self.assertEqual(response.status_code, 200)
        body = json.loads(response.content)
        assert_matches_schema(self, body, 'Source')
        self.assertEqual(body['uuid'], str(source.pk))
        self.assertEqual(body['canonicalKey'], source.key)

    def test_requires_auth(self):
        source = make_source()
        self.enable_bridge()
        response = self.client.get(f'{SOURCES_URL}/{source.pk}')
        self.assertEqual(response.status_code, 401)

    def test_read_only_default_does_not_block_get(self):
        source = make_source()
        self.enable_bridge()
        response = self.client.get(
            f'{SOURCES_URL}/{source.pk}', **self.auth_header(),
        )
        self.assertEqual(response.status_code, 200)


class SourceMediaEndpointTestCase(BridgeTestCase):

    def test_nonexistent_source_returns_404(self):
        self.enable_bridge()
        response = self.client.get(
            f'{SOURCES_URL}/{uuid.uuid4()}/media', **self.auth_header(),
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(json.loads(response.content)['code'], 'SOURCE_NOT_FOUND')

    def test_empty_source_returns_empty_page_matching_contract(self):
        source = make_source()
        self.enable_bridge()
        response = self.client.get(
            f'{SOURCES_URL}/{source.pk}/media', **self.auth_header(),
        )
        self.assertEqual(response.status_code, 200)
        body = json.loads(response.content)
        assert_matches_schema(self, body, 'MediaPage')
        self.assertEqual(body['data'], [])
        self.assertEqual(body['page'], {'page': 1, 'limit': 50, 'total': 0})

    def test_items_matching_contract(self):
        source = make_source()
        make_media(source, key='v1', title='Video 1')
        make_media(source, key='v2', title='Video 2')
        self.enable_bridge()
        response = self.client.get(
            f'{SOURCES_URL}/{source.pk}/media', **self.auth_header(),
        )
        body = json.loads(response.content)
        self.assertEqual(len(body['data']), 2)
        for item in body['data']:
            assert_matches_schema(self, item, 'MediaItem')
        self.assertEqual(body['page']['total'], 2)

    def test_limit_bounds_page_size(self):
        source = make_source()
        for i in range(5):
            make_media(source, key=f'v{i}', title=f'Video {i}')
        self.enable_bridge()
        response = self.client.get(
            f'{SOURCES_URL}/{source.pk}/media?limit=2', **self.auth_header(),
        )
        body = json.loads(response.content)
        self.assertEqual(len(body['data']), 2)
        self.assertEqual(body['page'], {'page': 1, 'limit': 2, 'total': 5})

    def test_page_beyond_available_returns_empty_data_not_error(self):
        source = make_source()
        make_media(source, key='v1', title='Video 1')
        self.enable_bridge()
        response = self.client.get(
            f'{SOURCES_URL}/{source.pk}/media?page=5&limit=10', **self.auth_header(),
        )
        self.assertEqual(response.status_code, 200)
        body = json.loads(response.content)
        self.assertEqual(body['data'], [])
        self.assertEqual(body['page']['total'], 1)

    def test_page_zero_rejected(self):
        source = make_source()
        self.enable_bridge()
        response = self.client.get(
            f'{SOURCES_URL}/{source.pk}/media?page=0', **self.auth_header(),
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(json.loads(response.content)['code'], 'SOURCE_INVALID')

    def test_negative_page_rejected(self):
        source = make_source()
        self.enable_bridge()
        response = self.client.get(
            f'{SOURCES_URL}/{source.pk}/media?page=-1', **self.auth_header(),
        )
        self.assertEqual(response.status_code, 400)

    def test_non_integer_page_rejected(self):
        source = make_source()
        self.enable_bridge()
        response = self.client.get(
            f'{SOURCES_URL}/{source.pk}/media?page=abc', **self.auth_header(),
        )
        self.assertEqual(response.status_code, 400)

    def test_limit_zero_rejected(self):
        source = make_source()
        self.enable_bridge()
        response = self.client.get(
            f'{SOURCES_URL}/{source.pk}/media?limit=0', **self.auth_header(),
        )
        self.assertEqual(response.status_code, 400)

    def test_limit_over_200_rejected_not_clamped(self):
        source = make_source()
        self.enable_bridge()
        response = self.client.get(
            f'{SOURCES_URL}/{source.pk}/media?limit=201', **self.auth_header(),
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(json.loads(response.content)['code'], 'SOURCE_INVALID')

    def test_limit_exactly_200_accepted(self):
        source = make_source()
        self.enable_bridge()
        response = self.client.get(
            f'{SOURCES_URL}/{source.pk}/media?limit=200', **self.auth_header(),
        )
        self.assertEqual(response.status_code, 200)

    def test_requires_auth(self):
        source = make_source()
        self.enable_bridge()
        response = self.client.get(f'{SOURCES_URL}/{source.pk}/media')
        self.assertEqual(response.status_code, 401)

    def test_ordering_is_newest_published_first(self):
        source = make_source()
        older = make_media(
            source, key='old', title='Older',
            published=timezone.now() - timezone.timedelta(days=1),
        )
        newer = make_media(source, key='new', title='Newer', published=timezone.now())
        self.enable_bridge()
        response = self.client.get(
            f'{SOURCES_URL}/{source.pk}/media', **self.auth_header(),
        )
        body = json.loads(response.content)
        self.assertEqual(body['data'][0]['id'], str(newer.pk))
        self.assertEqual(body['data'][1]['id'], str(older.pk))

    def test_null_published_sorts_after_dated_media(self):
        source = make_source()
        dated = make_media(
            source, key='dated', title='Dated',
            published=timezone.now() - timezone.timedelta(days=1),
        )
        undated = make_media(source, key='undated', title='Undated', published=None)
        self.enable_bridge()
        response = self.client.get(
            f'{SOURCES_URL}/{source.pk}/media', **self.auth_header(),
        )
        body = json.loads(response.content)
        self.assertEqual(body['data'][0]['id'], str(dated.pk))
        self.assertEqual(body['data'][1]['id'], str(undated.pk))


class CapabilitiesReflectsT2TestCase(BridgeTestCase):

    def test_read_sources_and_read_media_are_now_true(self):
        self.enable_bridge()
        response = self.client.get(
            '/api/medianest/v1/capabilities', **self.auth_header(),
        )
        body = json.loads(response.content)
        self.assertTrue(body['readSources'])
        self.assertTrue(body['readMedia'])
        # Everything else stays false -- T2 is read-only.
        write_flags = {
            k: v for k, v in body.items()
            if k not in ('health', 'readSources', 'readMedia')
        }
        self.assertTrue(all(v is False for v in write_flags.values()), write_flags)


class NonGetMethodRejectionTestCase(BridgeTestCase):
    '''
        Verifier LOW finding: encode as suite coverage, not just a manual
        out-of-band check, that every non-GET method on all three T2
        routes is blocked by the read-only gate (403 PROVIDER_READ_ONLY,
        MEDIANEST_BRIDGE_READ_ONLY defaults true) -- 3 routes x 4 methods
        = 12 combinations.
    '''

    def _routes(self):
        source = make_source()
        return {
            'source-lookup': f'{SOURCES_URL}?key={source.key}',
            'source-detail': f'{SOURCES_URL}/{source.pk}',
            'source-media': f'{SOURCES_URL}/{source.pk}/media',
        }

    def test_all_non_get_methods_rejected_on_all_three_routes(self):
        self.enable_bridge()
        methods = ('post', 'put', 'patch', 'delete')
        for route_name, path in self._routes().items():
            for method_name in methods:
                client_method = getattr(self.client, method_name)
                with self.subTest(route=route_name, method=method_name):
                    response = client_method(path, **self.auth_header())
                    self.assertEqual(
                        response.status_code, 403,
                        f'{method_name.upper()} {path} expected 403, got {response.status_code}',
                    )
                    body = json.loads(response.content)
                    self.assertEqual(body['code'], 'PROVIDER_READ_ONLY')
