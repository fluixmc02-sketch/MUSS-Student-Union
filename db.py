"""SQLite storage for the student union bot.

Anonymity rules enforced here:
- No names, usernames or phone numbers are ever stored.
- A ticket keeps the student's chat id only so the team's reply can reach them;
  that route is erased after ROUTE_RETENTION_DAYS or when the student uses /forgetme.
- Poll votes are stored as a keyed hash, never as a user id.
"""

import hashlib
import hmac
import secrets
import sqlite3
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS users (
    chat_id INTEGER PRIMARY KEY,
    joined_at INTEGER NOT NULL,
    blocked INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS tickets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    category TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'new',
    created_at INTEGER NOT NULL,
    chat_id INTEGER
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id INTEGER NOT NULL,
    sender TEXT NOT NULL,          -- 'student' or 'team'
    text TEXT,
    photo_id TEXT,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS links (
    chat_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    ticket_id INTEGER NOT NULL,
    side TEXT NOT NULL,            -- 'admin' or 'student'
    created_at INTEGER NOT NULL,
    PRIMARY KEY (chat_id, message_id)
);
CREATE TABLE IF NOT EXISTS polls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    question TEXT NOT NULL,
    options TEXT NOT NULL,         -- options separated by \\n
    open INTEGER NOT NULL DEFAULT 1,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS votes (
    poll_id INTEGER NOT NULL,
    voter TEXT NOT NULL,
    option INTEGER NOT NULL,
    PRIMARY KEY (poll_id, voter)
);
"""


class DB:
    def __init__(self, path: str):
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()
        self._secret = self._get_or_create_secret()

    def _get_or_create_secret(self) -> bytes:
        row = self.conn.execute("SELECT value FROM meta WHERE key='vote_secret'").fetchone()
        if row:
            return bytes.fromhex(row["value"])
        value = secrets.token_bytes(32)
        self.conn.execute("INSERT INTO meta VALUES ('vote_secret', ?)", (value.hex(),))
        self.conn.commit()
        return value

    # ---------- users ----------
    def add_user(self, chat_id: int) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO users (chat_id, joined_at) VALUES (?, ?)",
            (chat_id, int(time.time())),
        )
        self.conn.commit()

    def remove_user(self, chat_id: int) -> None:
        self.conn.execute("DELETE FROM users WHERE chat_id=? AND blocked=0", (chat_id,))
        self.conn.commit()

    def is_blocked(self, chat_id: int) -> bool:
        row = self.conn.execute("SELECT blocked FROM users WHERE chat_id=?", (chat_id,)).fetchone()
        return bool(row and row["blocked"])

    def set_blocked(self, chat_id: int, blocked: bool) -> None:
        self.add_user(chat_id)
        self.conn.execute("UPDATE users SET blocked=? WHERE chat_id=?", (int(blocked), chat_id))
        self.conn.commit()

    def active_users(self) -> list[int]:
        return [r["chat_id"] for r in self.conn.execute("SELECT chat_id FROM users WHERE blocked=0")]

    def forget(self, chat_id: int) -> None:
        """Erase every link between this chat and its tickets."""
        self.conn.execute("UPDATE tickets SET chat_id=NULL WHERE chat_id=?", (chat_id,))
        self.conn.execute("DELETE FROM links WHERE chat_id=?", (chat_id,))
        self.remove_user(chat_id)
        self.conn.commit()

    # ---------- tickets ----------
    def create_ticket(self, chat_id: int, category: str, text: str | None, photo_id: str | None) -> int:
        now = int(time.time())
        cur = self.conn.execute(
            "INSERT INTO tickets (category, created_at, chat_id) VALUES (?, ?, ?)",
            (category, now, chat_id),
        )
        ticket_id = cur.lastrowid
        self.conn.execute(
            "INSERT INTO messages (ticket_id, sender, text, photo_id, created_at) VALUES (?, 'student', ?, ?, ?)",
            (ticket_id, text, photo_id, now),
        )
        self.conn.commit()
        return ticket_id

    def add_message(self, ticket_id: int, sender: str, text: str | None, photo_id: str | None) -> None:
        self.conn.execute(
            "INSERT INTO messages (ticket_id, sender, text, photo_id, created_at) VALUES (?, ?, ?, ?, ?)",
            (ticket_id, sender, text, photo_id, int(time.time())),
        )
        self.conn.commit()

    def get_ticket(self, ticket_id: int):
        return self.conn.execute("SELECT * FROM tickets WHERE id=?", (ticket_id,)).fetchone()

    def ticket_messages(self, ticket_id: int):
        return self.conn.execute(
            "SELECT * FROM messages WHERE ticket_id=? ORDER BY id", (ticket_id,)
        ).fetchall()

    def set_status(self, ticket_id: int, status: str) -> None:
        self.conn.execute("UPDATE tickets SET status=? WHERE id=?", (status, ticket_id))
        self.conn.commit()

    def all_tickets(self):
        return self.conn.execute(
            """SELECT t.id, t.category, t.status, t.created_at,
                      (SELECT text FROM messages m WHERE m.ticket_id=t.id ORDER BY m.id LIMIT 1) AS text,
                      (SELECT COUNT(*) FROM messages m WHERE m.ticket_id=t.id AND m.sender='team') AS replies
               FROM tickets t ORDER BY t.id"""
        ).fetchall()

    # ---------- message links (for anonymous reply threads) ----------
    def add_link(self, chat_id: int, message_id: int, ticket_id: int, side: str) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO links VALUES (?, ?, ?, ?, ?)",
            (chat_id, message_id, ticket_id, side, int(time.time())),
        )
        self.conn.commit()

    def find_link(self, chat_id: int, message_id: int, side: str) -> int | None:
        row = self.conn.execute(
            "SELECT ticket_id FROM links WHERE chat_id=? AND message_id=? AND side=?",
            (chat_id, message_id, side),
        ).fetchone()
        return row["ticket_id"] if row else None

    def purge_old_routes(self, days: int) -> None:
        cutoff = int(time.time()) - days * 86400
        self.conn.execute("UPDATE tickets SET chat_id=NULL WHERE created_at < ?", (cutoff,))
        self.conn.execute("DELETE FROM links WHERE side='student' AND created_at < ?", (cutoff,))
        self.conn.commit()

    # ---------- polls ----------
    def create_poll(self, question: str, options: list[str]) -> int:
        cur = self.conn.execute(
            "INSERT INTO polls (question, options, created_at) VALUES (?, ?, ?)",
            (question, "\n".join(options), int(time.time())),
        )
        self.conn.commit()
        return cur.lastrowid

    def get_poll(self, poll_id: int):
        return self.conn.execute("SELECT * FROM polls WHERE id=?", (poll_id,)).fetchone()

    def list_polls(self, only_open: bool = False):
        sql = "SELECT * FROM polls" + (" WHERE open=1" if only_open else "") + " ORDER BY id DESC"
        return self.conn.execute(sql).fetchall()

    def close_poll(self, poll_id: int) -> None:
        self.conn.execute("UPDATE polls SET open=0 WHERE id=?", (poll_id,))
        self.conn.commit()

    def _voter_hash(self, poll_id: int, user_id: int) -> str:
        return hmac.new(self._secret, f"{poll_id}:{user_id}".encode(), hashlib.sha256).hexdigest()

    def vote(self, poll_id: int, user_id: int, option: int) -> bool:
        """Returns False if this person already voted in this poll."""
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO votes VALUES (?, ?, ?)",
            (poll_id, self._voter_hash(poll_id, user_id), option),
        )
        self.conn.commit()
        return cur.rowcount == 1

    def has_voted(self, poll_id: int, user_id: int) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM votes WHERE poll_id=? AND voter=?",
            (poll_id, self._voter_hash(poll_id, user_id)),
        ).fetchone() is not None

    def poll_results(self, poll_id: int) -> dict[int, int]:
        rows = self.conn.execute(
            "SELECT option, COUNT(*) AS n FROM votes WHERE poll_id=? GROUP BY option", (poll_id,)
        ).fetchall()
        return {r["option"]: r["n"] for r in rows}

    # ---------- stats ----------
    def stats(self) -> dict:
        q = lambda sql: self.conn.execute(sql).fetchone()[0]  # noqa: E731
        by_cat = self.conn.execute("SELECT category, COUNT(*) FROM tickets GROUP BY category").fetchall()
        by_status = self.conn.execute("SELECT status, COUNT(*) FROM tickets GROUP BY status").fetchall()
        return {
            "users": q("SELECT COUNT(*) FROM users WHERE blocked=0"),
            "tickets": q("SELECT COUNT(*) FROM tickets"),
            "by_category": {r[0]: r[1] for r in by_cat},
            "by_status": {r[0]: r[1] for r in by_status},
            "open_polls": q("SELECT COUNT(*) FROM polls WHERE open=1"),
        }
