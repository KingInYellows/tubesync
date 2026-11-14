from contextlib import nullcontext

from django.db import connections, transaction, DEFAULT_DB_ALIAS


def read_atomic(using=None):
    '''
        A transaction for code that only reads.

        TubeSync sets the SQLite `transaction_mode` to IMMEDIATE, so every
        `transaction.atomic()` block opened outside another one starts with
        `BEGIN IMMEDIATE` and takes the database write lock, even when the
        block only runs SELECTs. A read then waits behind every writer for
        up to the SQLite timeout, and while it holds the lock every writer
        waits behind it. That is what made the bridge's GET /sources/:id
        (through `resolve_show_title`) and the dashboard stall for 30 s
        during the nightly crawl, and MediaNest time out (MED-636).

        On SQLite outside an atomic block this returns a no-op context, so
        each query runs in autocommit. With WAL a plain read never waits for
        a writer. Inside an atomic block it returns `transaction.atomic()`,
        a savepoint on a transaction that already holds the lock, which
        costs nothing extra. Other databases always get
        `transaction.atomic()`, which there is a savepoint or a normal
        transaction that takes no write lock for reads.
    '''
    alias = using or DEFAULT_DB_ALIAS
    connection = connections[alias]
    if 'sqlite' == connection.vendor and not connection.in_atomic_block:
        return nullcontext()
    return transaction.atomic(using=alias)
