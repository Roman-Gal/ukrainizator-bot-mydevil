"""PostgreSQL inbox. One advisory-locked process per database; no SQLite runtime files."""
import atexit
import hashlib
import os
import secrets
import time
import json

_test_schemas = {}


class Record(dict):
    def __getitem__(self, key):
        return tuple(self.values())[key] if isinstance(key, int) else super().__getitem__(key)


def record_row(cursor):
    columns = [column.name for column in cursor.description] if cursor.description else []
    return lambda values: Record(zip(columns, values))


def cleanup_test_schemas():
    if not _test_schemas:
        return
    import psycopg
    from psycopg import sql
    try:
        with psycopg.connect(os.environ['TRANSLATOR_TEST_DATABASE_URL'], autocommit=True) as db:
            for schema in _test_schemas.values():
                db.execute(sql.SQL('DROP SCHEMA IF EXISTS {} CASCADE').format(sql.Identifier(schema)))
    except Exception:
        pass  # Test launcher verifies no schemas remain; never log a DSN.


atexit.register(cleanup_test_schemas)


class PostgresStore:
    def __init__(self, url, *, test_path=None):
        import psycopg
        from psycopg import sql
        self.db = psycopg.connect(url, autocommit=True, connect_timeout=10, row_factory=record_row)
        self.db.execute("SET statement_timeout='5s'; SET lock_timeout='5s'")
        schema = None
        if test_path is not None:
            key = secrets.token_hex(12) if test_path == ':memory:' else hashlib.sha256(test_path.encode()).hexdigest()
            schema = _test_schemas.setdefault(key, 'translator_test_' + secrets.token_hex(10))
            self.db.execute(sql.SQL('CREATE SCHEMA IF NOT EXISTS {}').format(sql.Identifier(schema)))
            self.db.execute(sql.SQL('SET search_path TO {}').format(sql.Identifier(schema)))
        lock = self.db.execute('SELECT pg_try_advisory_lock(hashtext(%s)) AS acquired', (schema or 'ukrainizator-worker',)).fetchone()
        if not lock['acquired']:
            self.db.close()
            raise RuntimeError('Another translator already holds this database.')
        self.db.execute('''
            CREATE TABLE IF NOT EXISTS jobs (
                id BIGINT PRIMARY KEY, body TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending', plan TEXT,
                sent INTEGER NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0,
                ready_at DOUBLE PRECISION NOT NULL DEFAULT 0, created_at DOUBLE PRECISION NOT NULL
            );
            CREATE INDEX IF NOT EXISTS ready_jobs ON jobs(status, ready_at, id);
            CREATE TABLE IF NOT EXISTS joke_times (
                chat BIGINT NOT NULL, sender BIGINT NOT NULL, at DOUBLE PRECISION NOT NULL,
                PRIMARY KEY(chat, sender)
            );
        ''')

    def enqueue(self, update):
        self.db.execute('INSERT INTO jobs(id,body,created_at) VALUES (%s,%s,%s) ON CONFLICT(id) DO NOTHING',
                        (update['update_id'], json.dumps(update, ensure_ascii=False), time.time()))

    def next(self):
        return self.db.execute("SELECT * FROM jobs WHERE status='pending' AND ready_at<=%s ORDER BY id LIMIT 1", (time.time(),)).fetchone()

    def plan(self, job_id, payloads):
        self.db.execute("UPDATE jobs SET plan=%s, body='{}' WHERE id=%s", (json.dumps(payloads, ensure_ascii=False), job_id))

    def sent(self, job_id, count):
        self.db.execute('UPDATE jobs SET sent=%s WHERE id=%s', (count, job_id))

    def finish(self, job_id, status='done'):
        self.db.execute("UPDATE jobs SET status=%s, body='{}', plan=NULL WHERE id=%s", (status, job_id))

    def retry(self, job_id, attempts, delay):
        self.db.execute('UPDATE jobs SET attempts=%s, ready_at=%s WHERE id=%s', (attempts, time.time() + delay, job_id))

    def claim_joke(self, chat, sender, cooldown):
        now = time.time()
        row = self.db.execute('''INSERT INTO joke_times(chat,sender,at) VALUES (%s,%s,%s)
            ON CONFLICT(chat,sender) DO UPDATE SET at=excluded.at
            WHERE joke_times.at<=%s RETURNING at''', (chat, sender, now, now - cooldown)).fetchone()
        return row is not None

    def close(self):
        self.db.close()
