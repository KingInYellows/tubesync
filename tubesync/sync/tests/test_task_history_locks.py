'''
    MED-695: huey's task-history writes and save_media task bodies hit
    "database is locked" during the nightly crawl, because
    rename_all_media_for_source held the SQLite write lock (TubeSync opens
    transactions with BEGIN IMMEDIATE) for its whole run, and save_media
    held it through save_model()'s back-off sleep.
'''
import logging
import uuid
from unittest import mock, skipUnless

from django import db
from django.test import TransactionTestCase, override_settings
from huey import signals

from common.huey import historical_task, is_sqlite_locked_error
from common.models import TaskHistory
from sync import tasks as sync_tasks
from sync.tests.test_signals import make_media, make_source


class FakeTask:
    name = 'fake_task'
    priority = 0

    def __init__(self):
        self.id = str(uuid.uuid4())
        self.args = ('arg',)
        self.kwargs = dict()


class FakeHuey:
    name = 'fake_queue'

    def __init__(self):
        self.storage = dict()

    def get(self, key, peek=False):
        if peek:
            return self.storage.get(key)
        return self.storage.pop(key, None)

    def put(self, key, data):
        self.storage[key] = data

    def scheduled_at_from_task(self, task_obj):
        return None


class HistoricalTaskLockTestCase(TransactionTestCase):
    '''
        Runs outside a test transaction, as a huey worker thread does.
    '''

    def setUp(self):
        logging.disable(logging.CRITICAL)

    def tearDown(self):
        logging.disable(logging.NOTSET)

    def _signal(self, task_obj, signal_name=signals.SIGNAL_EXECUTING):
        historical_task(signal_name, task_obj, huey=FakeHuey())

    def test_is_sqlite_locked_error(self):
        self.assertTrue(is_sqlite_locked_error(db.OperationalError('database is locked')))
        self.assertTrue(is_sqlite_locked_error(db.OperationalError('database table is locked')))
        self.assertFalse(is_sqlite_locked_error(db.OperationalError('no such table: x')))

    def test_writes_history_row(self):
        task_obj = FakeTask()
        self._signal(task_obj)
        th = TaskHistory.objects.get(task_id=task_obj.id)
        self.assertEqual(th.attempts, 1)
        self.assertEqual(th.queue, 'fake_queue')

    def test_locked_database_is_skipped_with_a_warning(self):
        task_obj = FakeTask()
        locked = db.OperationalError('database is locked')
        with (
            mock.patch.object(TaskHistory.objects, 'get_or_create', side_effect=locked),
            mock.patch('common.logger.log.warning') as warning,
        ):
            self._signal(task_obj)
        warning.assert_called_once()
        self.assertIn('database is locked', str(warning.call_args))
        self.assertFalse(TaskHistory.objects.filter(task_id=task_obj.id).exists())

    def test_other_operational_errors_still_raise(self):
        broken = db.OperationalError('no such table: common_taskhistory')
        with (
            mock.patch.object(TaskHistory.objects, 'get_or_create', side_effect=broken),
            self.assertRaises(db.OperationalError),
        ):
            self._signal(FakeTask())

    def test_locked_wording_from_other_databases_still_raises(self):
        # MySQL reports a locked account as "Account is locked."
        locked = db.OperationalError('Account is locked.')
        with (
            mock.patch.object(TaskHistory.objects, 'get_or_create', side_effect=locked),
            mock.patch.object(db.connections['default'], 'vendor', 'mysql'),
            self.assertRaises(db.OperationalError),
        ):
            self._signal(FakeTask())

    def test_locked_error_inside_a_transaction_still_raises(self):
        locked = db.OperationalError('database is locked')
        with (
            mock.patch.object(TaskHistory.objects, 'get_or_create', side_effect=locked),
            self.assertRaises(db.OperationalError),
            db.transaction.atomic(),
        ):
            self._signal(FakeTask())


class FsTasksHoldNoOuterTransactionTestCase(TransactionTestCase):
    '''
        Runs outside a test transaction so `in_atomic_block` reflects only
        what the task itself opened.
    '''

    def setUp(self):
        logging.disable(logging.CRITICAL)

    def tearDown(self):
        logging.disable(logging.NOTSET)

    @override_settings(RENAME_ALL_SOURCES=True)
    def test_rename_all_media_for_source_commits_per_item(self):
        source = make_source(key='UC_rename', directory='rename')
        for n in range(3):
            make_media(source, key=f'rename{n}', downloaded=True)

        states = list()

        def recording_rename(*args, **kwargs):
            # A per-item transaction nested in an outer one would be a
            # savepoint; on its own it is the outermost block.
            states.append((
                db.connection.in_atomic_block,
                list(db.connection.savepoint_ids),
            ))

        with mock.patch('sync.models.Media.rename_files', recording_rename):
            sync_tasks.rename_all_media_for_source.call_local(str(source.pk))

        self.assertEqual(states, [(True, [])] * 3)

    @override_settings(RENAME_ALL_SOURCES=True)
    def test_rename_all_media_for_source_passes_over_items_that_stop_qualifying(self):
        source = make_source(key='UC_rename_skip', directory='rename_skip')
        media_list = [
            make_media(source, key=f'rskip{n}', downloaded=True)
            for n in range(3)
        ]
        keys = sorted(m.pk for m in media_list)

        renamed = list()

        def recording_rename(instance, *args, **kwargs):
            renamed.append(instance.pk)
            if 1 == len(renamed):
                # a user acts on the second item while the task runs
                sync_tasks.Media.objects.filter(pk=keys[1]).update(downloaded=False)

        with mock.patch('sync.models.Media.rename_files', recording_rename):
            sync_tasks.rename_all_media_for_source.call_local(str(source.pk))

        self.assertEqual(renamed, [keys[0], keys[2]])

    @skipUnless('sqlite' == db.connection.vendor, 'the back-off sleep is SQLite only')
    def test_save_media_sleeps_outside_a_transaction(self):
        source = make_source(key='UC_save', directory='save')
        media = make_media(source, key='save1')

        states = list()

        def recording_sleep(seconds):
            states.append(db.connection.in_atomic_block)

        with mock.patch.object(sync_tasks.time, 'sleep', recording_sleep):
            sync_tasks.save_media.call_local(str(media.pk))

        self.assertEqual(states, [False])

    def test_save_media_looks_up_and_saves_in_one_transaction(self):
        '''
            No other writer may commit between the lookup and the full-row
            save, or the save would overwrite its change.
        '''
        source = make_source(key='UC_save_tx', directory='save_tx')
        media = make_media(source, key='save2')

        seen = list()
        real_get = sync_tasks.Media.objects.get

        def recording_get(*args, **kwargs):
            seen.append(('get', db.connection.in_atomic_block))
            return real_get(*args, **kwargs)

        def recording_save(instance, *args, **kwargs):
            seen.append(('save', db.connection.in_atomic_block))

        with (
            mock.patch.object(sync_tasks.Media.objects, 'get', recording_get),
            mock.patch('sync.models.Media.save', recording_save),
            mock.patch.object(sync_tasks.time, 'sleep'),
        ):
            sync_tasks.save_media.call_local(str(media.pk))

        self.assertEqual(seen, [('get', True), ('save', True)])
