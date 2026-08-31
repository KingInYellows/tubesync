'''
    Shared test scaffolding: a temp token file plus env-var helpers so each
    test controls the bridge's fail-closed configuration explicitly rather
    than relying on whatever happens to be in the process environment.
'''
import os
import tempfile
from contextlib import contextmanager

from django.test import TestCase, TransactionTestCase

BRIDGE_TOKEN = 'unit-test-bridge-token-do-not-use-in-prod'
BRIDGE_ENV_VARS = (
    'MEDIANEST_BRIDGE_TOKEN_FILE',
    'MEDIANEST_BRIDGE_READ_ONLY',
    'MEDIANEST_BRIDGE_MAX_BODY_BYTES',
    'MEDIANEST_BRIDGE_ALLOWED_CIDRS',
    'MEDIANEST_BRIDGE_UPSTREAM_SHA',
)


@contextmanager
def env_override(**kwargs):
    previous = {key: os.environ.get(key) for key in kwargs}
    try:
        for key, value in kwargs.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


class BridgeTestMixin:
    '''
        Clears every bridge env var before each test (so no test can leak
        configuration into another) and provides self.enable_bridge() to
        opt back in with a real token file. Shared between BridgeTestCase
        (the normal case, wrapped in a transaction/savepoint per test --
        fast, and correct for anything that doesn't need a real,
        independently-committing DB write mid-test) and
        BridgeTransactionTestCase (no such wrapping -- required for tests
        that deliberately trigger a real UNIQUE constraint violation via a
        second, genuinely separate write against the same table, which
        Django's TestCase-level savepoint nesting cannot support: nesting
        a real Model.save() inside another one, both hitting sqlite
        mid-transaction, raises "database table is locked", not the
        IntegrityError the test is actually trying to provoke).
    '''

    def setUp(self):
        super().setUp()
        self._env_ctx = env_override(**dict.fromkeys(BRIDGE_ENV_VARS))
        self._env_ctx.__enter__()
        self._token_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._env_ctx.__exit__, None, None, None)
        self.addCleanup(self._token_dir.cleanup)

    def enable_bridge(self, token=BRIDGE_TOKEN, **extra_env):
        token_path = os.path.join(self._token_dir.name, 'token.txt')
        with open(token_path, 'w', encoding='utf-8') as handle:
            handle.write(token)
        os.environ['MEDIANEST_BRIDGE_TOKEN_FILE'] = token_path
        for key, value in extra_env.items():
            os.environ[key] = value
        return token_path

    def auth_header(self, token=BRIDGE_TOKEN):
        return {'HTTP_AUTHORIZATION': f'Bearer {token}'}


class BridgeTestCase(BridgeTestMixin, TestCase):
    pass


class BridgeTransactionTestCase(BridgeTestMixin, TransactionTestCase):
    '''
        See BridgeTestMixin's docstring. Slower than BridgeTestCase (no
        shared savepoint to roll back -- each test's data is cleaned up
        by TransactionTestCase truncating tables afterward instead), so
        use this only when a test genuinely needs a real,
        independently-committing nested write.
    '''
