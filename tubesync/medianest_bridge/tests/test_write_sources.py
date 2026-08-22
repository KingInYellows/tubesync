'''
    T3 endpoint tests: POST /sources/validate, POST /sources,
    POST /sources/{sourceUuid}/sync -- the first mutation surface.
'''
import json
import uuid
from datetime import timedelta
from unittest.mock import patch

from django.db import IntegrityError
from django.utils import timezone

from common.models import TaskHistory
from sync.models import Media, Source

from .base import BridgeTestCase, BridgeTransactionTestCase
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

    def test_url_key_mismatch_with_canonical_key_returns_400(self):
        self.enable_bridge()
        response = post_json(self.client, VALIDATE_URL, {
            'sourceType': 'channel',
            'canonicalKey': 'UCAAAAAAAAAAAAAAAAAAAA',
            'canonicalUrl': 'https://www.youtube.com/channel/UCBBBBBBBBBBBBBBBBBB',
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
        '''
            Per the original T3 brief: validate must never persist
            anything, checked against every model a create would touch
            -- not just Source (the obvious one), but also Media (never
            relevant to validate, checked for completeness) and
            TaskHistory (the side effect a real create triggers via
            source_post_save -- its absence here is what actually proves
            validate never goes anywhere near Source.save()).
        '''
        self.enable_bridge()
        before = (Source.objects.count(), Media.objects.count(), TaskHistory.objects.count())
        post_json(self.client, VALIDATE_URL, {
            'sourceType': 'channel',
            'canonicalKey': 'UCabcdefghijklmnopqrstuv',
            'canonicalUrl': 'https://www.youtube.com/channel/UCabcdefghijklmnopqrstuv',
        }, **self.auth_header())
        after = (Source.objects.count(), Media.objects.count(), TaskHistory.objects.count())
        self.assertEqual(after, before)

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

    def test_invalid_canonical_url_returns_400(self):
        self.enable_bridge(MEDIANEST_BRIDGE_READ_ONLY='false')
        response = post_json(
            self.client, SOURCES_URL,
            self._valid_channel_body(canonicalUrl='x'),
            **self.auth_header(),
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(json.loads(response.content)['code'], 'SOURCE_INVALID')
        self.assertEqual(Source.objects.count(), 0)

    def test_url_key_mismatch_returns_400(self):
        self.enable_bridge(MEDIANEST_BRIDGE_READ_ONLY='false')
        response = post_json(
            self.client, SOURCES_URL,
            self._valid_channel_body(
                canonicalKey='UCAAAAAAAAAAAAAAAAAAAA',
                canonicalUrl='https://www.youtube.com/channel/UCBBBBBBBBBBBBBBBBBB',
            ),
            **self.auth_header(),
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(json.loads(response.content)['code'], 'SOURCE_INVALID')
        self.assertEqual(Source.objects.count(), 0)

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
                canonicalKey='UCdifferentkeyabc12345',
                canonicalUrl='https://www.youtube.com/channel/UCdifferentkeyabc12345',
                directory='another_dir',
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
                canonicalKey='UCdifferentkeyabc12345',
                canonicalUrl='https://www.youtube.com/channel/UCdifferentkeyabc12345',
                name='A Totally Different Name',
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


class CreateSourceRaceConditionTestCase(BridgeTransactionTestCase):
    '''
        T3 verifier MEDIUM: the pre-checks in CreateSourceView (SELECT
        for an existing key, SELECT/exists for name/directory) only
        narrow the common case -- the real convergence mechanism is the
        try/except IntegrityError around form.save(). Every other
        conflict test in this file only exercises the pre-check path
        (the pre-check finds the conflict and returns before ever
        calling form.save()); none of them touch the except branch
        itself.

        These tests make the except branch execute for real, without
        sleeping or threading (deterministic, not a timing race):
        Source.save() is patched so that, at the exact moment our
        request's own save() would run, it first creates the
        "concurrent" row itself -- simulating a second request's commit
        landing in the gap between our pre-check/form validation (which
        both ran against a table that didn't have the conflicting row
        yet, so both legitimately passed) and our own INSERT. The
        resulting IntegrityError is a real one raised by the DB's own
        UNIQUE constraint, not a mocked exception, so the except
        branch's own re-query logic is exercised exactly as it would be
        against a genuine concurrent conflict.

        Uses BridgeTransactionTestCase, not BridgeTestCase: nesting two
        real Model.save() calls (the injected "concurrent" row, then the
        request's own row) inside Django's TestCase-level savepoint
        wrapping raised "database table is locked" on sqlite instead of
        the intended IntegrityError -- confirmed empirically, not
        assumed. TransactionTestCase gives each of these tests a real,
        unwrapped connection where two independently-committing writes
        against the same table behave the way they would in production.
    '''

    def _post_with_injected_concurrent_row(self, body, *, concurrent_source_kwargs):
        '''
            Patches Source.save so the first call (this request's own
            save, triggered by form.save() inside CreateSourceView)
            creates `concurrent_source_kwargs` as a real row via the
            *unpatched* save method immediately beforehand, then proceeds
            with the real (also unpatched) save for the request's own
            instance -- which now genuinely violates whichever unique
            constraint the concurrent row shares with it.
        '''
        original_save = Source.save
        created = {}

        def save_with_injected_race(instance, *args, **kwargs):
            if 'concurrent' not in created:
                concurrent = Source(**concurrent_source_kwargs)
                original_save(concurrent, *args, **kwargs)
                created['concurrent'] = concurrent
            return original_save(instance, *args, **kwargs)

        self.enable_bridge(MEDIANEST_BRIDGE_READ_ONLY='false')
        with patch.object(Source, 'save', save_with_injected_race):
            response = post_json(self.client, SOURCES_URL, body, **self.auth_header())
        return response, created.get('concurrent')

    def test_key_collision_race_returns_409_with_true_winners_uuid(self):
        response, concurrent = self._post_with_injected_concurrent_row(
            {
                'sourceType': 'channel',
                'canonicalKey': 'UCracekeytest1234567',
                'canonicalUrl': 'https://www.youtube.com/channel/UCracekeytest1234567',
                'name': 'Our Name',
                'directory': 'our_dir',
            },
            concurrent_source_kwargs=dict(
                source_type='i', key='UCracekeytest1234567',
                name='Concurrent Winner', directory='concurrent_winner_dir',
            ),
        )
        self.assertEqual(response.status_code, 409)
        body = json.loads(response.content)
        self.assertEqual(body['code'], 'SOURCE_CONFLICT')
        self.assertEqual(body['existingSourceUuid'], str(concurrent.pk))
        # Only the concurrent winner exists -- our own row must not have
        # been left half-created or adopted as a second row.
        self.assertEqual(Source.objects.filter(key='UCracekeytest1234567').count(), 1)

    def test_name_collision_race_returns_400_namespace_conflict_not_adopted(self):
        response, concurrent = self._post_with_injected_concurrent_row(
            {
                'sourceType': 'channel',
                'canonicalKey': 'UCracenametest123456',
                'canonicalUrl': 'https://www.youtube.com/channel/UCracenametest123456',
                'name': 'Shared Race Name',
                'directory': 'our_own_dir',
            },
            concurrent_source_kwargs=dict(
                source_type='i', key='UCdifferentkeyforrace',
                name='Shared Race Name', directory='concurrent_dir',
            ),
        )
        self.assertEqual(response.status_code, 400)
        body = json.loads(response.content)
        self.assertEqual(body['code'], 'SOURCE_NAMESPACE_CONFLICT')
        self.assertNotIn(
            'existingSourceUuid', body,
            'a namespace conflict must never carry an adoptable uuid',
        )
        self.assertEqual(concurrent.name, 'Shared Race Name')
        self.assertFalse(Source.objects.filter(key='UCracenametest123456').exists())

    def test_unrecognized_integrity_error_reraised_as_sanitized_500(self):
        '''
            No concurrent row is created at all here -- the except
            block's re-query for both key and name/directory conflicts
            will find nothing, so it must re-raise rather than silently
            swallow an IntegrityError it cannot explain, and
            BridgeView.dispatch()'s catch-all must turn that into a
            sanitized 500 with no raw exception text or traceback in the
            response body.
        '''
        self.enable_bridge(MEDIANEST_BRIDGE_READ_ONLY='false')

        def always_raise(instance, *args, **kwargs):
            raise IntegrityError('simulated unrecognized constraint violation')

        with patch.object(Source, 'save', always_raise):
            response = post_json(self.client, SOURCES_URL, {
                'sourceType': 'channel',
                'canonicalKey': 'UCunrecognizedrace123',
                'canonicalUrl': 'https://www.youtube.com/channel/UCunrecognizedrace123',
                'name': 'Unrecognized Race',
                'directory': 'unrecognized_race_dir',
            }, **self.auth_header())

        self.assertEqual(response.status_code, 500)
        body = json.loads(response.content)
        self.assertEqual(body['code'], 'INTERNAL_PROVIDER_ERROR')
        self.assertNotIn('simulated unrecognized constraint violation', response.content.decode())
        self.assertNotIn('Traceback', response.content.decode())
        self.assertNotIn('IntegrityError', response.content.decode())
        self.assertFalse(Source.objects.filter(key='UCunrecognizedrace123').exists())


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
            and must advance the slower create-time schedule to sync-now's
            shorter delay rather than leaving the 10-minute wait intact.
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
        before_sync = TaskHistory.objects.get(name='sync.tasks.index_source')
        self.assertGreater(
            before_sync.scheduled_at,
            timezone.now() + timedelta(seconds=120),
            'create-time index_source should use the long post-save delay',
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
        after_sync = TaskHistory.objects.get(name='sync.tasks.index_source')
        self.assertLess(
            after_sync.scheduled_at,
            timezone.now() + timedelta(seconds=90),
            'sync-now must advance the pending create-time task to its '
            'shorter delay, not leave the 10-minute schedule intact',
        )

    def test_revoked_pending_task_does_not_block_sync(self):
        '''
            A terminal revoked scheduled-but-never-started row (marked
            [revoked] by RevokeTaskView / huey reschedule) must not be
            treated as a live pending index_source task.
        '''
        source = self._make_source()
        TaskHistory.objects.all().delete()
        stale = TaskHistory.objects.create(
            name='sync.tasks.index_source',
            task_id=str(uuid.uuid4()),
            task_params=[['{}'.format(source.pk)], ''],
            verbose_name='[revoked] Index media from source "x" once',
            scheduled_at=timezone.now() + timedelta(seconds=600),
            end_at=timezone.now(),
        )
        self.enable_bridge(MEDIANEST_BRIDGE_READ_ONLY='false')
        response = self.client.post(self._sync_url(source), **self.auth_header())
        self.assertEqual(response.status_code, 202)
        self.assertEqual(
            TaskHistory.objects.filter(
                name='sync.tasks.index_source',
            ).exclude(pk=stale.pk).count(),
            1,
            'sync-now must schedule a fresh task when only a revoked row exists',
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
