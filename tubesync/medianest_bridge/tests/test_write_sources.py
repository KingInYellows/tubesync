'''
    T3 endpoint tests: POST /sources/validate, POST /sources,
    POST /sources/{sourceUuid}/sync -- the first mutation surface.
'''
import json
import uuid

from common.models import TaskHistory
from sync.models import Source

from .base import BridgeTestCase
from .test_endpoints import assert_matches_schema

VALIDATE_URL = '/api/medianest/v1/sources/validate'
SOURCES_URL = '/api/medianest/v1/sources'
CAPS_URL = '/api/medianest/v1/capabilities'


def post_json(client, url, body, **extra):
    return client.post(
        url, data=json.dumps(body), content_type='application/json', **extra,
    )


class ValidateSourceEndpointTestCase(BridgeTestCase):

    def test_valid_channel_request(self):
        self.enable_bridge()
        response = post_json(self.client, VALIDATE_URL, {
            'sourceType': 'channel',
            'canonicalKey': 'UCabcdefghijklmnopqrstuv',
            'canonicalUrl': 'https://www.youtube.com/channel/UCabcdefghijklmnopqrstuv',
        }, **self.auth_header())
        self.assertEqual(response.status_code, 200)
        body = json.loads(response.content)
        assert_matches_schema(self, body, 'ValidatedSource')
        self.assertEqual(body['sourceType'], 'channel')
        self.assertEqual(body['canonicalKey'], 'UCabcdefghijklmnopqrstuv')
        self.assertEqual(body['displayName'], 'UCabcdefghijklmnopqrstuv')
        self.assertIsNone(body['thumbnailUrl'])

    def test_valid_playlist_request(self):
        self.enable_bridge()
        response = post_json(self.client, VALIDATE_URL, {
            'sourceType': 'playlist',
            'canonicalKey': 'PLabcdefghij',
            'canonicalUrl': 'https://www.youtube.com/playlist?list=PLabcdefghij',
        }, **self.auth_header())
        self.assertEqual(response.status_code, 200)
        body = json.loads(response.content)
        assert_matches_schema(self, body, 'ValidatedSource')

    def test_missing_required_field_returns_400(self):
        self.enable_bridge()
        response = post_json(self.client, VALIDATE_URL, {
            'sourceType': 'channel',
            'canonicalKey': 'UCabc',
            # canonicalUrl missing
        }, **self.auth_header())
        self.assertEqual(response.status_code, 400)
        self.assertEqual(json.loads(response.content)['code'], 'SOURCE_INVALID')

    def test_unknown_top_level_field_returns_400(self):
        self.enable_bridge()
        response = post_json(self.client, VALIDATE_URL, {
            'sourceType': 'channel',
            'canonicalKey': 'UCabc',
            'canonicalUrl': 'https://www.youtube.com/channel/UCabc',
            'notAContractField': 'x',
        }, **self.auth_header())
        self.assertEqual(response.status_code, 400)

    def test_unknown_profile_field_returns_400(self):
        self.enable_bridge()
        response = post_json(self.client, VALIDATE_URL, {
            'sourceType': 'channel',
            'canonicalKey': 'UCabc',
            'canonicalUrl': 'https://www.youtube.com/channel/UCabc',
            'profile': {'notARealProfileField': True},
        }, **self.auth_header())
        self.assertEqual(response.status_code, 400)

    def test_invalid_source_type_enum_returns_400(self):
        self.enable_bridge()
        response = post_json(self.client, VALIDATE_URL, {
            'sourceType': 'not-a-real-type',
            'canonicalKey': 'UCabc',
            'canonicalUrl': 'https://www.youtube.com/channel/UCabc',
        }, **self.auth_header())
        self.assertEqual(response.status_code, 400)

    def test_url_shape_mismatch_with_source_type_returns_400(self):
        self.enable_bridge()
        # A playlist URL submitted with sourceType "channel".
        response = post_json(self.client, VALIDATE_URL, {
            'sourceType': 'channel',
            'canonicalKey': 'PLabcdefghij',
            'canonicalUrl': 'https://www.youtube.com/playlist?list=PLabcdefghij',
        }, **self.auth_header())
        self.assertEqual(response.status_code, 400)
        self.assertEqual(json.loads(response.content)['code'], 'SOURCE_INVALID')

    def test_malformed_json_body_returns_400(self):
        self.enable_bridge()
        response = self.client.post(
            VALIDATE_URL, data=b'not json', content_type='application/json',
            **self.auth_header(),
        )
        self.assertEqual(response.status_code, 400)

    def test_never_persists_anything(self):
        self.enable_bridge()
        before = Source.objects.count()
        post_json(self.client, VALIDATE_URL, {
            'sourceType': 'channel',
            'canonicalKey': 'UCabcdefghijklmnopqrstuv',
            'canonicalUrl': 'https://www.youtube.com/channel/UCabcdefghijklmnopqrstuv',
        }, **self.auth_header())
        self.assertEqual(Source.objects.count(), before)

    def test_not_blocked_by_read_only_mode(self):
        '''
            The contract's own /sources/validate operation lists no 403
            ReadOnly response (unlike POST /sources and
            POST /sources/{uuid}/sync). MEDIANEST_BRIDGE_READ_ONLY
            defaults true and this must still succeed.
        '''
        self.enable_bridge()  # read-only left at its default (true)
        response = post_json(self.client, VALIDATE_URL, {
            'sourceType': 'channel',
            'canonicalKey': 'UCabcdefghijklmnopqrstuv',
            'canonicalUrl': 'https://www.youtube.com/channel/UCabcdefghijklmnopqrstuv',
        }, **self.auth_header())
        self.assertEqual(response.status_code, 200)

    def test_requires_auth(self):
        self.enable_bridge()
        response = post_json(self.client, VALIDATE_URL, {
            'sourceType': 'channel', 'canonicalKey': 'x', 'canonicalUrl': 'x',
        })
        self.assertEqual(response.status_code, 401)


class CreateSourceEndpointTestCase(BridgeTestCase):

    def _valid_channel_body(self, **overrides):
        body = {
            'sourceType': 'channel',
            'canonicalKey': 'UCabcdefghijklmnopqrstuv',
            'canonicalUrl': 'https://www.youtube.com/channel/UCabcdefghijklmnopqrstuv',
            'name': 'Test Channel',
            'directory': 'test_channel',
        }
        body.update(overrides)
        return body

    def test_blocked_by_default_read_only(self):
        self.enable_bridge()
        response = post_json(self.client, SOURCES_URL, self._valid_channel_body(), **self.auth_header())
        self.assertEqual(response.status_code, 403)
        self.assertEqual(json.loads(response.content)['code'], 'PROVIDER_READ_ONLY')
        self.assertEqual(Source.objects.count(), 0)

    def test_valid_channel_create(self):
        self.enable_bridge(MEDIANEST_BRIDGE_READ_ONLY='false')
        response = post_json(self.client, SOURCES_URL, self._valid_channel_body(), **self.auth_header())
        self.assertEqual(response.status_code, 201)
        body = json.loads(response.content)
        assert_matches_schema(self, body, 'Source')
        self.assertEqual(body['sourceType'], 'channel')
        self.assertEqual(body['canonicalKey'], 'UCabcdefghijklmnopqrstuv')
        self.assertEqual(Source.objects.count(), 1)
        source = Source.objects.get()
        self.assertEqual(source.source_type, 'i')  # CHANNEL_ID, not CHANNEL ('c')

    def test_valid_playlist_create(self):
        self.enable_bridge(MEDIANEST_BRIDGE_READ_ONLY='false')
        response = post_json(self.client, SOURCES_URL, {
            'sourceType': 'playlist',
            'canonicalKey': 'PLabcdefghij',
            'canonicalUrl': 'https://www.youtube.com/playlist?list=PLabcdefghij',
            'name': 'Test Playlist',
            'directory': 'test_playlist',
        }, **self.auth_header())
        self.assertEqual(response.status_code, 201)
        self.assertEqual(Source.objects.get().source_type, 'p')

    def test_create_schedules_index_source_task(self):
        '''
            Side effect, deliberate per ADR-0006: creating via the ORM
            fires sync/signals.py's post_save receiver exactly as the
            HTML UI would. TaskHistory.schedule() writes its row
            synchronously (common/models/tasks.py::th_schedule), so this
            is directly assertable without a live huey consumer.
        '''
        self.enable_bridge(MEDIANEST_BRIDGE_READ_ONLY='false')
        post_json(self.client, SOURCES_URL, self._valid_channel_body(), **self.auth_header())
        self.assertTrue(
            TaskHistory.objects.filter(name='sync.tasks.index_source').exists(),
        )

    def test_key_collision_returns_409_with_existing_uuid(self):
        self.enable_bridge(MEDIANEST_BRIDGE_READ_ONLY='false')
        first = post_json(self.client, SOURCES_URL, self._valid_channel_body(), **self.auth_header())
        existing_uuid = json.loads(first.content)['uuid']

        second = post_json(
            self.client, SOURCES_URL,
            self._valid_channel_body(name='Different Name', directory='different_dir'),
            **self.auth_header(),
        )
        self.assertEqual(second.status_code, 409)
        body = json.loads(second.content)
        self.assertEqual(body['code'], 'SOURCE_CONFLICT')
        self.assertEqual(body['existingSourceUuid'], existing_uuid)
        self.assertEqual(Source.objects.count(), 1)  # no second row created

    def test_name_collision_without_key_collision_returns_400_namespace_conflict(self):
        self.enable_bridge(MEDIANEST_BRIDGE_READ_ONLY='false')
        post_json(self.client, SOURCES_URL, self._valid_channel_body(), **self.auth_header())

        response = post_json(
            self.client, SOURCES_URL,
            self._valid_channel_body(
                canonicalKey='UCdifferentkeyabc12345', directory='another_dir',
            ),  # same name, different key/directory
            **self.auth_header(),
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(json.loads(response.content)['code'], 'SOURCE_NAMESPACE_CONFLICT')
        self.assertEqual(Source.objects.count(), 1)

    def test_directory_collision_without_key_collision_returns_400_namespace_conflict(self):
        self.enable_bridge(MEDIANEST_BRIDGE_READ_ONLY='false')
        post_json(self.client, SOURCES_URL, self._valid_channel_body(), **self.auth_header())

        response = post_json(
            self.client, SOURCES_URL,
            self._valid_channel_body(
                canonicalKey='UCdifferentkeyabc12345', name='A Totally Different Name',
            ),  # same directory, different key/name
            **self.auth_header(),
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(json.loads(response.content)['code'], 'SOURCE_NAMESPACE_CONFLICT')

    def test_directory_traversal_rejected(self):
        self.enable_bridge(MEDIANEST_BRIDGE_READ_ONLY='false')
        response = post_json(
            self.client, SOURCES_URL,
            self._valid_channel_body(directory='../../../etc'),
            **self.auth_header(),
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(json.loads(response.content)['code'], 'SOURCE_INVALID')
        self.assertEqual(Source.objects.count(), 0)

    def test_missing_required_field_returns_400(self):
        self.enable_bridge(MEDIANEST_BRIDGE_READ_ONLY='false')
        body = self._valid_channel_body()
        del body['directory']
        response = post_json(self.client, SOURCES_URL, body, **self.auth_header())
        self.assertEqual(response.status_code, 400)

    def test_unknown_field_returns_400(self):
        self.enable_bridge(MEDIANEST_BRIDGE_READ_ONLY='false')
        response = post_json(
            self.client, SOURCES_URL,
            self._valid_channel_body(notAContractField='x'),
            **self.auth_header(),
        )
        self.assertEqual(response.status_code, 400)

    def test_requires_auth(self):
        self.enable_bridge(MEDIANEST_BRIDGE_READ_ONLY='false')
        response = post_json(self.client, SOURCES_URL, self._valid_channel_body())
        self.assertEqual(response.status_code, 401)

    def test_get_still_works_on_same_route(self):
        '''T2's key-lookup GET on /sources must still work post-T3.'''
        self.enable_bridge()
        response = self.client.get(SOURCES_URL + '?key=nonexistent', **self.auth_header())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(response.content), {'data': []})


class SyncSourceEndpointTestCase(BridgeTestCase):

    def _make_source(self):
        return Source.objects.create(
            source_type='i', key='UCabc123', name='Test', directory='test',
        )

    def _sync_url(self, source):
        return f'{SOURCES_URL}/{source.pk}/sync'

    def test_blocked_by_default_read_only(self):
        source = self._make_source()
        TaskHistory.objects.all().delete()  # clear the create-time schedule
        self.enable_bridge()
        response = self.client.post(self._sync_url(source), **self.auth_header())
        self.assertEqual(response.status_code, 403)
        self.assertEqual(json.loads(response.content)['code'], 'PROVIDER_READ_ONLY')

    def test_valid_sync_schedules_task_and_returns_202(self):
        source = self._make_source()
        TaskHistory.objects.all().delete()
        self.enable_bridge(MEDIANEST_BRIDGE_READ_ONLY='false')
        response = self.client.post(self._sync_url(source), **self.auth_header())
        self.assertEqual(response.status_code, 202)
        body = json.loads(response.content)
        assert_matches_schema(self, body, 'Source')
        self.assertTrue(
            TaskHistory.objects.filter(name='sync.tasks.index_source').exists(),
        )

    def test_repeated_sync_does_not_duplicate_pending_task(self):
        '''
            TRACEABILITY obligation #1, scheduled-not-started half: proves
            the dedup predicate catches a task that has been scheduled but
            never picked up by a worker (start_at IS NULL), which
            get_source_index_task() alone (running() only) would miss --
            not just a coincidental "second call is a no-op" result.

            start_at IS NULL is the natural state here, not simulated:
            TaskHistory.schedule() writes its row synchronously
            (common/models/tasks.py::th_schedule) but never sets start_at
            itself -- only common/huey.py's EXECUTING signal handler does,
            and this test environment has no live huey consumer to fire
            that signal. Asserted explicitly below rather than left
            implicit, so this test cannot silently stop testing what it
            claims to if that environment assumption ever changes.
        '''
        source = self._make_source()
        TaskHistory.objects.all().delete()
        self.enable_bridge(MEDIANEST_BRIDGE_READ_ONLY='false')
        self.client.post(self._sync_url(source), **self.auth_header())
        pending = TaskHistory.objects.get(name='sync.tasks.index_source')
        self.assertIsNone(
            pending.start_at,
            'precondition for this test: the pending task must be '
            'scheduled-but-not-started (start_at IS NULL), not merely '
            'present, or this is not exercising the case '
            'get_source_index_task() alone would miss',
        )

        response = self.client.post(self._sync_url(source), **self.auth_header())
        self.assertEqual(response.status_code, 202)
        second_count = TaskHistory.objects.filter(name='sync.tasks.index_source').count()
        self.assertEqual(
            second_count, 1,
            'a second sync-now call while the first is still scheduled-'
            'but-not-started must not schedule a duplicate index_source task',
        )

    def test_create_then_immediate_sync_converges_to_one_task(self):
        '''
            The ADR-0006 double-indexing scenario, dynamically verified in
            this harness (not merely reasoned about): POST /sources alone
            schedules index_source with a 10-minute delay
            (source_post_save signal); POST /sources/{uuid}/sync uses a
            30-second delay. Calling sync-now immediately after create
            must converge to ONE index_source TaskHistory row, not two --
            the freshly-created 10-minute-delayed row is itself
            scheduled-not-started (start_at IS NULL), so the same dedup
            predicate that covers repeated sync-now calls must also cover
            this create-then-sync sequence.
        '''
        self.enable_bridge(MEDIANEST_BRIDGE_READ_ONLY='false')
        create_response = post_json(self.client, SOURCES_URL, {
            'sourceType': 'channel',
            'canonicalKey': 'UCconvergetest1234567',
            'canonicalUrl': 'https://www.youtube.com/channel/UCconvergetest1234567',
            'name': 'Converge Test',
            'directory': 'converge_test',
        }, **self.auth_header())
        self.assertEqual(create_response.status_code, 201)
        self.assertEqual(
            TaskHistory.objects.filter(name='sync.tasks.index_source').count(), 1,
            'create alone should schedule exactly one index_source task',
        )
        source_uuid = json.loads(create_response.content)['uuid']

        sync_response = self.client.post(
            f'{SOURCES_URL}/{source_uuid}/sync', **self.auth_header(),
        )
        self.assertEqual(sync_response.status_code, 202)
        self.assertEqual(
            TaskHistory.objects.filter(name='sync.tasks.index_source').count(), 1,
            'sync-now immediately after create must converge to one '
            'index_source task, not schedule a second (double-indexing) '
            'one alongside the 10-minute-delayed create-time task',
        )

    def test_nonexistent_source_returns_404(self):
        self.enable_bridge(MEDIANEST_BRIDGE_READ_ONLY='false')
        response = self.client.post(
            f'{SOURCES_URL}/{uuid.uuid4()}/sync', **self.auth_header(),
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(json.loads(response.content)['code'], 'SOURCE_NOT_FOUND')

    def test_malformed_uuid_returns_404(self):
        self.enable_bridge(MEDIANEST_BRIDGE_READ_ONLY='false')
        response = self.client.post(
            f'{SOURCES_URL}/not-a-uuid/sync', **self.auth_header(),
        )
        self.assertEqual(response.status_code, 404)

    def test_requires_auth(self):
        source = self._make_source()
        self.enable_bridge(MEDIANEST_BRIDGE_READ_ONLY='false')
        response = self.client.post(self._sync_url(source))
        self.assertEqual(response.status_code, 401)


class CapabilitiesReflectsT3TestCase(BridgeTestCase):

    def test_write_flags_are_true_except_update_disable_delete(self):
        self.enable_bridge()
        response = self.client.get(CAPS_URL, **self.auth_header())
        body = json.loads(response.content)
        self.assertTrue(body['validateChannelSource'])
        self.assertTrue(body['validatePlaylistSource'])
        self.assertTrue(body['createChannelSource'])
        self.assertTrue(body['createPlaylistSource'])
        self.assertTrue(body['syncSource'])
        self.assertFalse(body['updateSource'])
        self.assertFalse(body['disableSource'])
        self.assertFalse(body['deleteSource'])
        # No task/retry/skip/enable/plex endpoints exist anywhere in this app.
        for flag in (
            'retryMedia', 'skipMedia', 'enableMedia', 'readTasks',
            'cancelTask', 'retryTask', 'oneOffVideoDownload', 'pushEvents',
            'plexScan', 'plexCollectionCreation',
        ):
            self.assertFalse(body[flag], flag)


class BodySizeHardeningOnWriteRoutesTestCase(BridgeTestCase):
    '''
        T3 due obligation (T1 verifier LOW, carried through T2): the
        body-size gate now actually reads and counts bytes rather than
        trusting Content-Length alone (views.py::_read_body_is_oversized).
        These tests exercise it against a real write route with a real
        JSON body, not just the generic dispatch-level coverage in
        test_dispatch.py.
    '''

    def test_oversized_create_body_rejected_413_no_source_created(self):
        self.enable_bridge(
            MEDIANEST_BRIDGE_READ_ONLY='false',
            MEDIANEST_BRIDGE_MAX_BODY_BYTES='64',
        )
        big_body = {
            'sourceType': 'channel',
            'canonicalKey': 'UC' + 'x' * 200,
            'canonicalUrl': 'https://www.youtube.com/channel/UC' + 'x' * 200,
            'name': 'Test',
            'directory': 'test',
        }
        response = post_json(self.client, SOURCES_URL, big_body, **self.auth_header())
        self.assertEqual(response.status_code, 413)
        self.assertEqual(json.loads(response.content)['code'], 'REQUEST_TOO_LARGE')
        self.assertEqual(Source.objects.count(), 0)

    def test_body_within_limit_reaches_the_view(self):
        self.enable_bridge(
            MEDIANEST_BRIDGE_READ_ONLY='false',
            MEDIANEST_BRIDGE_MAX_BODY_BYTES='65536',
        )
        response = post_json(self.client, SOURCES_URL, {
            'sourceType': 'channel',
            'canonicalKey': 'UCabcdefghijklmnopqrstuv',
            'canonicalUrl': 'https://www.youtube.com/channel/UCabcdefghijklmnopqrstuv',
            'name': 'Test Channel',
            'directory': 'test_channel',
        }, **self.auth_header())
        self.assertEqual(response.status_code, 201)

    def test_oversized_validate_body_rejected_despite_read_only_exempt(self):
        '''
            read_only_exempt bypasses the read-only gate, not the
            body-size gate -- both run in BridgeView.dispatch() but are
            independent checks.
        '''
        self.enable_bridge(MEDIANEST_BRIDGE_MAX_BODY_BYTES='64')
        big_body = {
            'sourceType': 'channel',
            'canonicalKey': 'UC' + 'x' * 200,
            'canonicalUrl': 'https://www.youtube.com/channel/UC' + 'x' * 200,
        }
        response = post_json(self.client, VALIDATE_URL, big_body, **self.auth_header())
        self.assertEqual(response.status_code, 413)
