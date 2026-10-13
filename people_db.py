"""
People database — SQLite store for all qualifying Telegram messages.

One row per unique sender in `people`, one row per message in `messages`,
one row per (sender, group) pair in `person_groups`.  All upserts are
idempotent so the backfill and the scanner can run repeatedly without
producing duplicates.
"""

import sqlite3

DB_PATH = "people.db"

_DDL = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS people (
    username        TEXT PRIMARY KEY,
    first_seen      TEXT NOT NULL,
    last_seen       TEXT NOT NULL,
    total_messages  INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS messages (
    message_key     TEXT PRIMARY KEY,
    username        TEXT NOT NULL,
    group_id        INTEGER NOT NULL,
    group_name      TEXT NOT NULL,
    message_id      INTEGER NOT NULL,
    telegram_link   TEXT NOT NULL,
    message_text    TEXT NOT NULL,
    sent_at         TEXT NOT NULL,
    FOREIGN KEY (username) REFERENCES people(username)
);

CREATE INDEX IF NOT EXISTS idx_messages_username ON messages(username);
CREATE INDEX IF NOT EXISTS idx_messages_sent_at  ON messages(sent_at);

CREATE TABLE IF NOT EXISTS person_groups (
    username        TEXT NOT NULL,
    group_id        INTEGER NOT NULL,
    group_name      TEXT NOT NULL,
    message_count   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (username, group_id)
);
"""


def init_db(path: str = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.executescript(_DDL)
    conn.commit()
    return conn


def upsert_message(
    conn: sqlite3.Connection,
    *,
    username: str,
    group_id: int,
    group_name: str,
    message_id: int,
    text: str,
    telegram_link: str,
    sent_at: str,          # ISO-8601 string, e.g. "2026-10-13T09:30:00"
) -> bool:
    """
    Insert a message and update the sender's record.  Returns True if the
    message was new, False if it was already in the database.
    """
    key = f"{group_id}_{message_id}"
    uname = username.lower()
    date_str = sent_at[:10]  # YYYY-MM-DD

    conn.execute(
        """
        INSERT OR IGNORE INTO messages
            (message_key, username, group_id, group_name, message_id,
             telegram_link, message_text, sent_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (key, uname, group_id, group_name, message_id, telegram_link, text, sent_at),
    )
    if conn.execute("SELECT changes()").fetchone()[0] == 0:
        return False  # already existed

    conn.execute(
        """
        INSERT INTO people (username, first_seen, last_seen, total_messages)
        VALUES (?, ?, ?, 1)
        ON CONFLICT(username) DO UPDATE SET
            first_seen     = MIN(first_seen, excluded.first_seen),
            last_seen      = MAX(last_seen,  excluded.last_seen),
            total_messages = total_messages + 1
        """,
        (uname, date_str, date_str),
    )

    conn.execute(
        """
        INSERT INTO person_groups (username, group_id, group_name, message_count)
        VALUES (?, ?, ?, 1)
        ON CONFLICT(username, group_id) DO UPDATE SET
            message_count = message_count + 1
        """,
        (uname, group_id, group_name),
    )

    return True


def stats(path: str = DB_PATH) -> dict:
    conn = sqlite3.connect(path)
    result = {
        "people":   conn.execute("SELECT COUNT(*) FROM people").fetchone()[0],
        "messages": conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0],
        "groups":   conn.execute("SELECT COUNT(DISTINCT group_id) FROM messages").fetchone()[0],
    }
    conn.close()
    return result
