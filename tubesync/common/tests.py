import os.path
from django.conf import settings
from django.test import TestCase, Client
from .testutils import prevent_request_warnings
from .utils import (
    clean_filename,
    parse_database_connection_string,
    sqlite_init_command,
    sqlite_journal_mode,
)
from .errors import DatabaseConnectionError


class ErrorPageTestCase(TestCase):

    @prevent_request_warnings
    def test_error_403(self):
        c = Client()
        response = c.get('/error403')
        self.assertEqual(response.status_code, 403)

    @prevent_request_warnings
    def test_error_404(self):
        c = Client()
        response = c.get('/error404')
        self.assertEqual(response.status_code, 404)

    @prevent_request_warnings
    def test_error_500(self):
        c = Client()
        response = c.get('/error500')
        self.assertEqual(response.status_code, 500)


class HealthcheckTestCase(TestCase):

    def test_healthcheck(self):
        c = Client()
        response = c.get('/healthcheck')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content.decode(), 'ok')


class CommonStaticTestCase(TestCase):

    def test_robots(self):
        response = self.client.get('/robots.txt')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content.decode(), settings.ROBOTS)

    def test_favicon(self):
        # /favicon.ico should be a redirect to the real icon somewhere in STATIC_FILES
        response = self.client.get('/favicon.ico')
        self.assertEqual(response.status_code, 302)
        # Given tests run with DEBUG=False calls to files in /static/ will fail, check
        # the file exists on disk in common/static/ manually
        root = settings.STATIC_ROOT
        root_parts = str(root).split(os.sep)
        url = response.url
        if url.startswith('/'):
            url = url[1:]
        url_parts = url.split(os.sep)
        if url_parts[0] == root_parts[-1]:
            del root_parts[-1]
            del url_parts[0]
        root_parts.append('common')
        root_parts.append('static')
        favicon_real_path = os.path.join(os.sep.join(root_parts),
                                         os.sep.join(url_parts))
        self.assertTrue(os.path.exists(favicon_real_path))


class UtilsTestCase(TestCase):

    def test_parse_database_connection_string(self):
        database_dict = parse_database_connection_string(
            'postgresql://tubesync:password@localhost:5432/tubesync')
        self.assertEqual(database_dict,
            {
                'DRIVER': 'postgresql',
                'ENGINE': 'django.db.backends.postgresql',
                'USER': 'tubesync',
                'PASSWORD': 'password',
                'HOST': 'localhost',
                'PORT': 5432,
                'NAME': 'tubesync',
                'CONN_HEALTH_CHECKS': True,
                'CONN_MAX_AGE': 0,
                'OPTIONS': dict(pool={
                    'max_size': 10,
                    'min_size': 3,
                    'num_workers': 2,
                    'timeout': 180,
                }),
            }
        )
        database_dict = parse_database_connection_string(
            'mysql://tubesync:password@localhost:3306/tubesync')
        self.assertEqual(database_dict,
            {
                'DRIVER': 'mysql',
                'ENGINE': 'django.db.backends.mysql',
                'USER': 'tubesync',
                'PASSWORD': 'password',
                'HOST': 'localhost',
                'PORT': 3306,
                'NAME': 'tubesync',
                'CONN_HEALTH_CHECKS': True,
                'CONN_MAX_AGE': 300,
                'OPTIONS': {'charset': 'utf8mb4'}
            }
        )
        # Invalid driver
        with self.assertRaises(DatabaseConnectionError):
            parse_database_connection_string(
                'test://tubesync:password@localhost:5432/tubesync')
        # No username
        with self.assertRaises(DatabaseConnectionError):
            parse_database_connection_string(
                'postgresql://password@localhost:5432/tubesync')
        # No database name
        with self.assertRaises(DatabaseConnectionError):
            parse_database_connection_string(
                'postgresql://tubesync:password@5432')
        # Invalid port
        with self.assertRaises(DatabaseConnectionError):
            parse_database_connection_string(
                'postgresql://tubesync:password@localhost:test/tubesync')
        # Invalid port
        with self.assertRaises(DatabaseConnectionError):
            parse_database_connection_string(
                'postgresql://tubesync:password@localhost:65537/tubesync')
        # Invalid username or password
        with self.assertRaises(DatabaseConnectionError):
            parse_database_connection_string(
                'postgresql://tubesync:password:test@localhost:5432/tubesync')
        # Invalid database name
        with self.assertRaises(DatabaseConnectionError):
            parse_database_connection_string(
                'postgresql://tubesync:password@localhost:5432/tubesync/test')

    def test_clean_filename(self):
        self.assertEqual(clean_filename('a'), 'a')
        self.assertEqual(clean_filename('a\t'), 'a')
        self.assertEqual(clean_filename('a\n'), 'a')
        self.assertEqual(clean_filename('a a'), 'a a')
        self.assertEqual(clean_filename('a  a'), 'a  a')
        self.assertEqual(clean_filename('a\t\t\ta'), 'a   a')
        self.assertEqual(clean_filename('a\t\t\ta\t\t\t'), 'a   a')


class SqliteJournalModeTestCase(TestCase):

    def test_default_is_truncate(self):
        for value in (None, '', '   '):
            self.assertEqual(sqlite_journal_mode(value), ('TRUNCATE', None))

    def test_supported_modes_are_normalised(self):
        self.assertEqual(sqlite_journal_mode(' wal '), ('WAL', None))
        self.assertEqual(sqlite_journal_mode('Delete'), ('DELETE', None))
        self.assertEqual(sqlite_journal_mode('TRUNCATE'), ('TRUNCATE', None))

    def test_unsupported_mode_falls_back(self):
        for value in ('memory', 'off', 'persist', 'wall'):
            mode, msg = sqlite_journal_mode(value)
            self.assertEqual(mode, 'TRUNCATE')
            self.assertIn(value.upper(), msg)

    def test_truncate_init_command_matches_upstream(self):
        cmd = sqlite_init_command()
        self.assertIn('PRAGMA journal_mode = TRUNCATE;', cmd)
        self.assertIn('PRAGMA locking_mode = NORMAL;', cmd)
        self.assertIn('PRAGMA journal_size_limit = 67108864;', cmd)
        self.assertNotIn('synchronous', cmd)
        self.assertNotIn('WAL', cmd)

    def test_truncate_init_command_keeps_incremental_vacuum(self):
        self.assertIn('PRAGMA incremental_vacuum(100);', sqlite_init_command())

    def test_wal_init_command(self):
        cmd = sqlite_init_command('wal')
        self.assertIn('PRAGMA journal_mode = WAL;', cmd)
        self.assertIn('PRAGMA synchronous = NORMAL;', cmd)
        self.assertIn('PRAGMA locking_mode = NORMAL;', cmd)
        self.assertNotIn('EXCLUSIVE', cmd)
        self.assertNotIn('incremental_vacuum', cmd)

    def test_wal_reader_connects_while_writer_holds_lock(self):
        import sqlite3
        import tempfile
        cmd = sqlite_init_command('WAL')
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'db.sqlite3')
            setup = sqlite3.connect(path, isolation_level=None)
            setup.executescript(cmd)
            setup.execute('CREATE TABLE t (x INTEGER)')
            setup.execute('INSERT INTO t VALUES (1)')
            setup.close()
            writer = sqlite3.connect(path, isolation_level=None, timeout=0.2)
            reader = None
            try:
                writer.executescript(cmd)
                writer.execute('BEGIN IMMEDIATE')
                writer.execute('INSERT INTO t VALUES (2)')
                reader = sqlite3.connect(path, isolation_level=None, timeout=0.2)
                # Same order Django uses: each statement on its own.
                for statement in cmd.split(';'):
                    if statement.strip():
                        reader.execute(statement)
                count = reader.execute('SELECT COUNT(*) FROM t').fetchone()[0]
                self.assertEqual(count, 1)
            finally:
                if reader is not None:
                    reader.close()
                writer.execute('ROLLBACK')
                writer.close()

    def test_init_command_applies_to_a_real_database(self):
        import sqlite3
        import tempfile
        for mode, expected in (('TRUNCATE', 'truncate'), ('WAL', 'wal')):
            with tempfile.TemporaryDirectory() as tmp:
                db = sqlite3.connect(os.path.join(tmp, 'db.sqlite3'))
                try:
                    db.executescript(sqlite_init_command(mode))
                    got = db.execute('PRAGMA journal_mode').fetchone()[0]
                    self.assertEqual(got, expected)
                    if 'WAL' == mode:
                        sync = db.execute('PRAGMA synchronous').fetchone()[0]
                        self.assertEqual(sync, 1)  # NORMAL
                finally:
                    db.close()

    def _container_databases(self, env_value):
        import runpy
        from pathlib import Path
        from unittest import mock
        path = Path(settings.BASE_DIR) / 'tubesync' / 'local_settings.py.container'
        env = {'DATABASE_CONNECTION': ''}
        if env_value is not None:
            env['TUBESYNC_SQLITE_JOURNAL_MODE'] = env_value
        with mock.patch.dict(os.environ, env, clear=False):
            if env_value is None:
                os.environ.pop('TUBESYNC_SQLITE_JOURNAL_MODE', None)
            return runpy.run_path(str(path))['DATABASES']['default']

    def test_container_settings_default_is_truncate(self):
        db = self._container_databases(None)
        cmd = db['OPTIONS']['init_command']
        self.assertIn('PRAGMA journal_mode = TRUNCATE;', cmd)
        self.assertEqual(db['OPTIONS']['timeout'], 10)
        self.assertIn(('Using SQLite journal mode: TRUNCATE',), db['_msgs'])

    def test_container_settings_wal(self):
        db = self._container_databases('WAL')
        cmd = db['OPTIONS']['init_command']
        self.assertIn('PRAGMA journal_mode = WAL;', cmd)
        self.assertIn('PRAGMA synchronous = NORMAL;', cmd)
        self.assertIn(('Using SQLite journal mode: WAL',), db['_msgs'])
