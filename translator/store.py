import json
import os
import sqlite3
import time
from pathlib import Path


class SqliteTestStore:
    """Durable inbox and delivery checkpoints. Run exactly one worker per database."""

    def __init__(self, path):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS jobs (
                id INTEGER PRIMARY KEY, body TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending', plan TEXT,
                sent INTEGER NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0,
                ready_at REAL NOT NULL DEFAULT 0, created_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS ready_jobs ON jobs(status, ready_at, id);
            CREATE TABLE IF NOT EXISTS joke_times (
                chat INTEGER NOT NULL, sender INTEGER NOT NULL, at REAL NOT NULL,
                PRIMARY KEY(chat, sender)
            );
        """)

    def enqueue(self, update):
        with self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO jobs(id, body, created_at) VALUES (?, ?, ?)",
                (update["update_id"], json.dumps(update, ensure_ascii=False), time.time()),
            )

    def next(self):
        return self.db.execute(
            "SELECT * FROM jobs WHERE status='pending' AND ready_at<=? ORDER BY id LIMIT 1",
            (time.time(),),
        ).fetchone()

    def plan(self, job_id, payloads):
        with self.db:
            self.db.execute(
                "UPDATE jobs SET plan=?, body='{}' WHERE id=?",
                (json.dumps(payloads, ensure_ascii=False), job_id),
            )

    def sent(self, job_id, count):
        with self.db:
            self.db.execute("UPDATE jobs SET sent=? WHERE id=?", (count, job_id))

    def finish(self, job_id, status="done"):
        with self.db:
            # Keep only deduplication IDs after processing; remove chat contents.
            self.db.execute(
                "UPDATE jobs SET status=?, body='{}', plan=NULL WHERE id=?", (status, job_id)
            )

    def retry(self, job_id, attempts, delay):
        with self.db:
            self.db.execute(
                "UPDATE jobs SET attempts=?, ready_at=? WHERE id=?",
                (attempts, time.time() + delay, job_id),
            )

    def claim_joke(self, chat, sender, cooldown):
        now = time.time()
        row = self.db.execute(
            "SELECT at FROM joke_times WHERE chat=? AND sender=?", (chat, sender)
        ).fetchone()
        if row and now - row[0] < cooldown:
            return False
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO joke_times VALUES (?, ?, ?)", (chat, sender, now)
            )
        return True

    def close(self):
        self.db.close()


def Store(path):
    from .postgres import PostgresStore
    if os.getenv('TRANSLATOR_TEST_DATABASE_URL'):
        return PostgresStore(os.environ['TRANSLATOR_TEST_DATABASE_URL'], test_path=path)
    if path.startswith(('postgresql://', 'postgres://')):
        return PostgresStore(path)
    # Compatibility fixtures only; the hosting CLI requires a PostgreSQL URL.
    return SqliteTestStore(path)
