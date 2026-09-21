"""Isolated synchronous SQLite queue. No legacy module is imported or modified.

Call ``recover()`` once at application startup, BEFORE starting workers. Opening
an additional Store does not recover another worker's in-flight requests.
All mutations use BEGIN IMMEDIATE; the queue limit counts queued + running.
Idempotency keys are scoped separately to requests and jobs and never expire.
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Self

STATES = frozenset({"queued", "running", "done", "error", "unknown", "cancelled"})
TERMINAL = STATES - {"queued", "running"}


class IdempotencyConflict(ValueError):
    """An existing key was reused with different input."""


class QueueFull(ValueError):
    """The complete submission would exceed the pending queue limit."""


class ConversationConflict(ValueError):
    """An alias already identifies another upstream session."""


class InvalidTransition(ValueError):
    """A request state transition would replay or overwrite settled work."""


def _json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _hash(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _identifier(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a nonempty string")
    return value


class Store:
    """Store(path, queue_limit=1000); use a dedicated NEW v2 database path.

    Records are detached JSON-compatible dicts. Request records contain id,
    payload, status, job_id, index, key, payload_hash, timestamps and any result
    fields supplied to update_request. Jobs include request records as items.
    A cancelled job stays cancelled even if its active request later finishes.
    Running work is not interrupted by cancellation; only queued work is cancelled.
    """

    def __init__(self, path: str | Path, queue_limit: int = 1000):
        if type(queue_limit) is not int or not 1 <= queue_limit <= 1000:
            raise ValueError("queue_limit must be between 1 and 1000")
        self.queue_limit = queue_limit
        self._lock = threading.RLock()
        self._db = sqlite3.connect(
            str(path), timeout=30, isolation_level=None, check_same_thread=False
        )
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA foreign_keys=ON")
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, key TEXT UNIQUE, payload_hash TEXT NOT NULL,
                data TEXT NOT NULL, status TEXT NOT NULL,
                created_at REAL NOT NULL, updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS requests (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                id TEXT NOT NULL UNIQUE, key TEXT UNIQUE,
                payload_hash TEXT NOT NULL, payload TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN
                    ('queued','running','done','error','unknown','cancelled')),
                job_id TEXT REFERENCES jobs(id), item_index INTEGER,
                fields TEXT NOT NULL DEFAULT '{}',
                created_at REAL NOT NULL, updated_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS request_queue ON requests(status,seq);
            CREATE INDEX IF NOT EXISTS request_job ON requests(job_id,item_index);
            CREATE TABLE IF NOT EXISTS events (
                request_id TEXT NOT NULL REFERENCES requests(id),
                seq INTEGER NOT NULL, data TEXT NOT NULL,
                PRIMARY KEY(request_id,seq)
            );
            CREATE TABLE IF NOT EXISTS sessions (
                sid TEXT PRIMARY KEY, parent_idx INTEGER,
                created_at REAL NOT NULL, updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS aliases (
                cid TEXT PRIMARY KEY, sid TEXT NOT NULL REFERENCES sessions(sid)
            );
        """)

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @contextmanager
    def _transaction(self):
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                yield
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise

    def _existing(self, table: str, key: str | None, digest: str):
        if key is None:
            return None
        _identifier(key, "key")
        row = self._db.execute(f"SELECT * FROM {table} WHERE key=?", (key,)).fetchone()
        if row is not None and row["payload_hash"] != digest:
            raise IdempotencyConflict(f"{table} key already used with different input")
        return row

    def pending_count(self) -> int:
        with self._lock:
            return self._db.execute(
                "SELECT COUNT(*) FROM requests WHERE status IN ('queued','running')"
            ).fetchone()[0]

    def _capacity(self, count: int) -> None:
        if self.pending_count() + count > self.queue_limit:
            raise QueueFull(f"pending queue limit is {self.queue_limit}")

    def _insert_request(
        self,
        payload,
        key=None,
        job_id=None,
        index=None,
        status="queued",
        fields=None,
        rid=None,
    ):
        rid = rid or f"req_{uuid.uuid4().hex}"
        now = time.time()
        self._db.execute(
            """INSERT INTO requests
            (id,key,payload_hash,payload,status,job_id,item_index,fields,created_at,updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                rid,
                key,
                _hash(payload),
                _json(payload),
                status,
                job_id,
                index,
                _json(fields or {}),
                now,
                now,
            ),
        )
        return rid

    @staticmethod
    def _request(row):
        if row is None:
            return None
        return {
            **json.loads(row["fields"]),
            "id": row["id"],
            "payload": json.loads(row["payload"]),
            "key": row["key"],
            "payload_hash": row["payload_hash"],
            "status": row["status"],
            "job_id": row["job_id"],
            "index": row["item_index"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def create_request(self, payload, key=None, job_id=None) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise TypeError("payload must be an object")
        digest = _hash(payload)
        with self._transaction():
            old = self._existing("requests", key, digest)
            if old is not None:
                if old["job_id"] != job_id:
                    raise IdempotencyConflict("request key belongs to another job")
                existing = self._request(old)
                assert existing is not None
                return existing
            index = None
            if job_id is not None:
                job = self.get_job(job_id)
                if job is None or job["status"] not in {"queued", "running"}:
                    raise ValueError("job must exist and be active")
                index = len(job["items"])
            self._capacity(1)
            rid = self._insert_request(payload, key, job_id, index)
            record = self.get_request(rid)
            assert record is not None
            return record

    def get_request(self, id):
        with self._lock:
            return self._request(
                self._db.execute("SELECT * FROM requests WHERE id=?", (id,)).fetchone()
            )

    def request_status(self, id):
        """Cheap status-only read so pollers avoid re-parsing the event blob."""
        with self._lock:
            row = self._db.execute(
                "SELECT status FROM requests WHERE id=?", (id,)
            ).fetchone()
            return None if row is None else row["status"]

    def get_request_by_key(self, key):
        with self._lock:
            return self._request(
                self._db.execute(
                    "SELECT * FROM requests WHERE key=?", (key,)
                ).fetchone()
            )

    def unresolved_conversation(self, cid, sid):
        """Unknown submissions fence both named aliases and upstream sessions."""
        if not cid and not sid:
            return False
        with self._lock:
            for row in self._db.execute(
                "SELECT * FROM requests WHERE status='unknown'"
            ):
                record = self._request(row)
                assert record is not None
                payload = record["payload"]
                old_cid = payload.get("conversation") or payload.get("conversation_id")
                old_sid = payload.get("chat_session_id") or (
                    record.get("result") or {}
                ).get("chat_session_id")
                if old_cid and not old_sid:
                    mapping = self.get_conversation(old_cid)
                    old_sid = mapping["chat_session_id"] if mapping else None
                if (cid and cid == old_cid) or (sid and sid == old_sid):
                    return True
            return False

    def append_event(self, request_id, event):
        """Return the appended object with 1-based seq; missing request raises KeyError.

        Dedicated event rows are independent of the optional request.events
        snapshot field. Choose one persistence strategy per worker.
        """
        if not isinstance(event, dict):
            raise TypeError("event must be an object")
        _json(event)
        with self._transaction():
            if self.get_request(request_id) is None:
                raise KeyError(request_id)
            seq = self._db.execute(
                "SELECT COALESCE(MAX(seq),0)+1 FROM events WHERE request_id=?",
                (request_id,),
            ).fetchone()[0]
            value = {**event, "seq": seq}
            self._db.execute(
                "INSERT INTO events VALUES (?,?,?)", (request_id, seq, _json(value))
            )
            return value

    def list_events(self, request_id, after=0):
        """Return event objects with seq > after in sequence order."""
        if type(after) is not int or after < 0:
            raise ValueError("after must be a nonnegative integer")
        with self._lock:
            return [
                json.loads(r[0])
                for r in self._db.execute(
                    "SELECT data FROM events WHERE request_id=? AND seq>? ORDER BY seq",
                    (request_id, after),
                )
            ]

    def claim_next(self):
        with self._transaction():
            row = self._db.execute("""
                SELECT r.* FROM requests r LEFT JOIN jobs j ON j.id=r.job_id
                WHERE r.status='queued' AND (r.job_id IS NULL OR (
                    j.status IN ('queued','running') AND NOT EXISTS (
                        SELECT 1 FROM requests prev WHERE prev.job_id=r.job_id
                        AND ((prev.status='running' AND
                             (json_extract(j.data,'$.mode')='thread' OR
                              json_extract(j.data,'$.pause_seconds')>0)) OR
                            (json_extract(j.data,'$.mode')='thread'
                             AND prev.item_index < r.item_index AND prev.status!='done'))
                    )
                )) ORDER BY r.seq LIMIT 1
            """).fetchone()
            if row is None:
                return None
            self._db.execute(
                "UPDATE requests SET status='running',updated_at=? WHERE id=?",
                (time.time(), row["id"]),
            )
            self._refresh_job(row["job_id"])
            return self.get_request(row["id"])

    def _refresh_job(self, job_id):
        if job_id is None:
            return
        row = self._db.execute(
            "SELECT status FROM jobs WHERE id=?", (job_id,)
        ).fetchone()
        if row is None or row[0] == "cancelled":
            return
        states = {
            r[0]
            for r in self._db.execute(
                "SELECT status FROM requests WHERE job_id=?", (job_id,)
            )
        }
        # An uncertain/failed predecessor blocks a thread; it must never replay.
        status = next(
            (
                s
                for s in ("running", "unknown", "error", "queued", "cancelled")
                if s in states
            ),
            "done",
        )
        mode = self._db.execute(
            "SELECT data FROM jobs WHERE id=?", (job_id,)
        ).fetchone()
        if json.loads(mode[0])["mode"] == "independent" and "queued" in states:
            status = "running" if "running" in states else "queued"
        self._db.execute(
            "UPDATE jobs SET status=?,updated_at=? WHERE id=?",
            (status, time.time(), job_id),
        )

    def return_unsent(self, id):
        """Release a claim only when the live scheduler proves no submission.

        Never used by crash recovery or reconciliation. Observed upstream events
        independently forbid returning a potentially submitted request to queue,
        whether they live in the request.events snapshot or the dedicated rows.
        """
        with self._transaction():
            record = self.get_request(id)
            if record is None or record["status"] != "running":
                return
            observed = self._db.execute(
                "SELECT 1 FROM events WHERE request_id=? LIMIT 1", (id,)
            ).fetchone()
            if record.get("events") or record.get("chat_session_id") or observed:
                raise InvalidTransition("cannot requeue an observed upstream request")
            self._db.execute(
                "UPDATE requests SET status='queued',updated_at=? WHERE id=?",
                (time.time(), id),
            )
            self._refresh_job(record.get("job_id"))

    def update_request(self, id, status, **fields):
        if status not in STATES:
            raise ValueError("invalid request status")
        protected = {
            "id",
            "payload",
            "key",
            "payload_hash",
            "job_id",
            "index",
            "created_at",
            "updated_at",
            "status",
        }
        if protected.intersection(fields):
            raise ValueError("request identity and payload are immutable")
        _json(fields)
        with self._transaction():
            row = self._db.execute(
                "SELECT * FROM requests WHERE id=?", (id,)
            ).fetchone()
            if row is None:
                return None
            previous = row["status"]
            if previous in TERMINAL:
                if previous == status:
                    return self._request(row)
                raise InvalidTransition(f"cannot change {previous} to {status}")
            if status != previous and not (
                (previous == "queued" and status == "cancelled")
                or (previous == "running" and status in TERMINAL)
            ):
                raise InvalidTransition(
                    f"cannot change {previous} to {status}; use claim_next"
                )
            merged = {**json.loads(row["fields"]), **fields}
            if status in TERMINAL:
                merged.setdefault("finished_at", time.time())
            self._db.execute(
                "UPDATE requests SET status=?,fields=?,updated_at=? WHERE id=?",
                (status, _json(merged), time.time(), id),
            )
            self._refresh_job(row["job_id"])
            return self.get_request(id)

    def annotate_request(self, id, **fields):
        """Attach diagnostic fields without changing status, result or error.

        Lets the bridge record why a settled (often ``done``) request still
        failed action validation, or annotate a queued request before dispatch,
        without relaxing the replay/overwrite guards in ``update_request``.
        """
        if not fields:
            return self.get_request(id)
        immutable = {
            "id",
            "payload",
            "key",
            "payload_hash",
            "job_id",
            "index",
            "created_at",
            "updated_at",
            "status",
            "result",
            "error",
        }
        if immutable.intersection(fields):
            raise ValueError("status, result and request identity are immutable")
        _json(fields)
        with self._transaction():
            row = self._db.execute(
                "SELECT fields FROM requests WHERE id=?", (id,)
            ).fetchone()
            if row is None:
                return None
            merged = {**json.loads(row["fields"]), **fields}
            self._db.execute(
                "UPDATE requests SET fields=?,updated_at=? WHERE id=?",
                (_json(merged), time.time(), id),
            )
            return self.get_request(id)

    def put_conversation(self, cid, sid, parent_idx):
        with self._transaction():
            self._put_conversation(cid, sid, parent_idx)
            return self.get_conversation(cid)

    def reconcile_request(self, request_id, history):
        from .recovery import history_evidence

        with self._transaction():
            record = self.get_request(request_id)
            if record is None:
                raise KeyError(request_id)
            if record["status"] != "unknown":
                return {"status": record["status"], "reason": "not_unknown"}
            result, reason = history_evidence(record, history)
            if result is None:
                return {"status": "unknown", "reason": reason}
            payload = record["payload"]
            sid = result["chat_session_id"]
            cid = payload.get("conversation") or payload.get("conversation_id") or sid
            current = self.get_conversation(cid) or self.get_conversation(sid)
            if current and (
                current["chat_session_id"] != sid
                or current["parent_idx"]
                not in (
                    record.get("submitted_parent_idx", payload.get("parent_idx", -1)),
                    result["approach_msg_idx"],
                )
            ):
                return {"status": "unknown", "reason": "conversation_conflict"}
            self._put_conversation(cid, sid, result["approach_msg_idx"])
            row = self._db.execute(
                "SELECT fields FROM requests WHERE id=?", (request_id,)
            ).fetchone()
            fields = json.loads(row[0])
            fields.update(
                {
                    "result": {**result, "conversation": cid},
                    "error": None,
                    "reconciled_at": time.time(),
                    "observed_events": fields.get("events", []),
                    "events": [
                        {
                            "event": "start",
                            "chat_session_id": sid,
                            "approach_msg_idx": result["approach_msg_idx"],
                        },
                        {
                            "event": "msg",
                            "item": {"type": "text", "content": result["text"]},
                        },
                        {"event": "end", "status": "finished"},
                    ],
                }
            )
            self._db.execute(
                "UPDATE requests SET status='done',fields=?,updated_at=? WHERE id=?",
                (_json(fields), time.time(), request_id),
            )
            self._refresh_job(record["job_id"])
            return {"status": "done", "reason": "confirmed"}

    def _put_conversation(self, cid, sid, parent_idx, importing=False):
        _identifier(cid, "cid")
        _identifier(sid, "sid")
        if parent_idx is not None and (type(parent_idx) is not int or parent_idx < 0):
            raise ValueError("parent_idx must be a nonnegative integer or None")
        # Reserve the actual session ID as a canonical alias as well.
        for alias in (cid, sid):
            old = self._db.execute(
                "SELECT sid FROM aliases WHERE cid=?", (alias,)
            ).fetchone()
            if old is not None and old[0] != sid:
                raise ConversationConflict(
                    f"alias {alias!r} already maps to another session"
                )
        session = self._db.execute(
            "SELECT parent_idx FROM sessions WHERE sid=?", (sid,)
        ).fetchone()
        if importing and session is not None and session[0] != parent_idx:
            raise ConversationConflict(f"session {sid!r} has conflicting parent_idx")
        now = time.time()
        self._db.execute(
            """INSERT INTO sessions VALUES (?,?,?,?) ON CONFLICT(sid)
            DO UPDATE SET parent_idx=excluded.parent_idx,updated_at=excluded.updated_at""",
            (sid, parent_idx, now, now),
        )
        for alias in (cid, sid):
            self._db.execute("INSERT OR IGNORE INTO aliases VALUES (?,?)", (alias, sid))

    def get_conversation(self, cid):
        with self._lock:
            row = self._db.execute(
                """SELECT s.* FROM sessions s JOIN aliases a
                ON a.sid=s.sid WHERE a.cid=?""",
                (cid,),
            ).fetchone()
            if row is None:
                return None
            aliases = [
                r[0]
                for r in self._db.execute(
                    "SELECT cid FROM aliases WHERE sid=? ORDER BY cid", (row["sid"],)
                )
            ]
            return {
                "conversation_id": cid,
                "chat_session_id": row["sid"],
                "parent_idx": row["parent_idx"],
                "aliases": aliases,
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }

    def list_conversations(self):
        with self._lock:
            return [
                self.get_conversation(r[0])
                for r in self._db.execute(
                    "SELECT sid FROM sessions ORDER BY created_at,sid"
                ).fetchall()
            ]

    @staticmethod
    def _job_input(items, mode, conversation, approach_id, pause_seconds):
        if mode not in {"independent", "thread"}:
            raise ValueError("mode must be independent or thread")
        if not isinstance(items, list) or not items:
            raise ValueError("items must be a nonempty list")
        for item in items:
            if not isinstance(item, dict):
                raise TypeError("every item must be an object")
            message = item.get("message") or item.get("content")
            if not isinstance(message, str) or not message.strip():
                raise ValueError("every item must have a nonempty message or content")
        pause = float(pause_seconds)
        if not math.isfinite(pause) or pause < 0:
            raise ValueError("pause_seconds must be finite and nonnegative")
        return {
            "items": items,
            "mode": mode,
            "conversation": conversation,
            "approach_id": approach_id,
            "pause_seconds": pause,
        }

    def create_job(
        self,
        items,
        mode="independent",
        conversation=None,
        approach_id=None,
        pause_seconds: float = 20.0,
        key=None,
    ) -> dict[str, Any]:
        data = self._job_input(items, mode, conversation, approach_id, pause_seconds)
        digest = _hash(data)
        with self._transaction():
            old = self._existing("jobs", key, digest)
            if old is not None:
                existing = self.get_job(old["id"])
                assert existing is not None
                return existing
            self._capacity(len(items))
            jid = f"job_{uuid.uuid4().hex}"
            self._insert_job(jid, data, key, digest)
            job = self.get_job(jid)
            assert job is not None
            return job

    def _insert_job(self, jid, data, key, digest, legacy=False):
        now = time.time()
        self._db.execute(
            "INSERT INTO jobs VALUES (?,?,?,?,?,?,?)",
            (jid, key, digest, _json(data), "queued", now, now),
        )
        for index, raw in enumerate(data["items"]):
            payload = dict(raw)
            if legacy:
                for field in ("status", "text", "error", "chat_session_id", "index"):
                    payload.pop(field, None)
            payload["message"] = raw.get("message") or raw.get("content")
            payload["conversation"] = (
                raw.get("conversation")
                or data["conversation"]
                or (jid if data["mode"] == "thread" else f"{jid}-{index}")
            )
            payload["approach_id"] = raw.get("approach_id") or data["approach_id"]
            status = raw.get("status", "queued") if legacy else "queued"
            if status == "running":
                status = "unknown"
            if status not in STATES:
                raise ValueError(f"invalid legacy item status {status!r}")
            fields = {
                k: raw[k]
                for k in ("text", "error", "chat_session_id")
                if legacy and k in raw
            }
            self._insert_request(
                payload, job_id=jid, index=index, status=status, fields=fields
            )
        self._refresh_job(jid)

    def get_job(self, id):
        with self._lock:
            row = self._db.execute("SELECT * FROM jobs WHERE id=?", (id,)).fetchone()
            if row is None:
                return None
            data = json.loads(row["data"])
            items = [
                self._request(r)
                for r in self._db.execute(
                    "SELECT * FROM requests WHERE job_id=? ORDER BY item_index,seq",
                    (id,),
                )
            ]
            return {
                **data,
                "id": id,
                "key": row["key"],
                "payload_hash": row["payload_hash"],
                "status": row["status"],
                "items": items,
                "total": len(items),
                "done": sum(it["status"] == "done" for it in items if it is not None),
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }

    def list_jobs(self):
        with self._lock:
            return [
                self.get_job(r[0])
                for r in self._db.execute(
                    "SELECT id FROM jobs ORDER BY created_at DESC,id"
                ).fetchall()
            ]

    def cancel_job(self, id):
        with self._transaction():
            job = self.get_job(id)
            if job is None or job["status"] in {"done", "cancelled"}:
                return job
            self._db.execute(
                "UPDATE jobs SET status='cancelled',updated_at=? WHERE id=?",
                (time.time(), id),
            )
            self._db.execute(
                """UPDATE requests SET status='cancelled',updated_at=?
                WHERE job_id=? AND status='queued'""",
                (time.time(), id),
            )
            return self.get_job(id)

    def recover(self):
        """Mark interrupted running requests unknown, never queued; return count."""
        with self._transaction():
            jobs = [
                r[0]
                for r in self._db.execute(
                    "SELECT DISTINCT job_id FROM requests WHERE status='running'"
                )
            ]
            count = self._db.execute(
                """UPDATE requests SET status='unknown',updated_at=?
                WHERE status='running'""",
                (time.time(),),
            ).rowcount
            for jid in jobs:
                self._refresh_job(jid)
            return count

    def import_legacy(self, path):
        """Read one JSON file or recursively a directory; never write its contents.

        Return {imported: int, skipped: int, conflicts: list, invalids: list}.
        Each legacy job is atomic; each conversation/alias is independently
        validated. Equal existing jobs are skipped; conflicting data is retained
        in neither direction. Legacy running requests become unknown.
        """
        report = {"imported": 0, "skipped": 0, "conflicts": [], "invalids": []}
        source = Path(path)
        files = sorted(source.rglob("*.json")) if source.is_dir() else [source]
        for file in files:
            try:
                data = json.loads(file.read_text(encoding="utf-8-sig"))
                if not isinstance(data, dict):
                    raise TypeError("expected JSON object")
                if "threads" in data:
                    self._import_conversations(data, str(file), report)
                elif "items" in data and "id" in data:
                    with self._transaction():
                        self._import_job(data, report)
                else:
                    raise ValueError("unrecognized legacy document")
            except (IdempotencyConflict, ConversationConflict) as exc:
                report["conflicts"].append({"path": str(file), "error": str(exc)})
            except (ValueError, TypeError, OSError, sqlite3.IntegrityError) as exc:
                report["invalids"].append({"path": str(file), "error": str(exc)})
        return report

    def _import_job(self, raw, report):
        jid = _identifier(raw["id"], "job id")
        data = self._job_input(
            raw["items"],
            raw.get("mode", "independent"),
            raw.get("conversation"),
            raw.get("approach_id"),
            raw.get("pause_seconds", 20),
        )
        digest = _hash(raw)
        old = self._db.execute(
            "SELECT payload_hash FROM jobs WHERE id=?", (jid,)
        ).fetchone()
        if old is not None:
            if old[0] != digest:
                raise IdempotencyConflict(f"legacy job {jid!r} already differs")
            report["skipped"] += 1
            return
        status = raw.get("status", "queued")
        if status not in STATES | {"completed", "failed"}:
            raise ValueError("invalid legacy job status")
        if status == "cancelled":
            data = {
                **data,
                "items": [
                    dict(item, status="cancelled")
                    if item.get("status", "queued") == "queued"
                    else item
                    for item in data["items"]
                ],
            }
        self._capacity(
            sum(item.get("status", "queued") == "queued" for item in data["items"])
        )
        self._insert_job(jid, data, None, digest, legacy=True)
        if status == "cancelled":
            self._db.execute("UPDATE jobs SET status='cancelled' WHERE id=?", (jid,))
        report["imported"] += 1

    def _import_conversations(self, data, path, report):
        threads = data["threads"]
        if not isinstance(threads, dict):
            raise TypeError("threads must be an object")
        for cid, thread in threads.items():
            try:
                if not isinstance(thread, dict):
                    raise TypeError("thread must be an object")
                with self._transaction():
                    old = self.get_conversation(cid)
                    self._put_conversation(
                        cid,
                        thread.get("chat_session_id"),
                        thread.get("parent_idx"),
                        importing=True,
                    )
                report["skipped" if old else "imported"] += 1
            except (ValueError, TypeError) as exc:
                bucket = (
                    "conflicts" if isinstance(exc, ConversationConflict) else "invalids"
                )
                report[bucket].append({"path": path, "id": cid, "error": str(exc)})
        for namespace in ("openai_ids", "fingerprints"):
            aliases = data.get(namespace, {})
            if not isinstance(aliases, dict):
                report["invalids"].append(
                    {"path": path, "error": f"invalid {namespace}"}
                )
                continue
            for alias, cid in aliases.items():
                try:
                    if not isinstance(cid, str) or cid not in threads:
                        raise ValueError("alias refers to a missing thread")
                    source = threads[cid]
                    if not isinstance(source, dict):
                        raise TypeError("alias refers to invalid thread")
                    with self._transaction():
                        target = self.get_conversation(cid)
                        if (
                            target is None
                            or target["chat_session_id"]
                            != source.get("chat_session_id")
                            or target["parent_idx"] != source.get("parent_idx")
                        ):
                            raise ConversationConflict(
                                "alias target did not import consistently"
                            )
                        old = self.get_conversation(alias)
                        self._put_conversation(
                            alias,
                            target["chat_session_id"],
                            target["parent_idx"],
                            importing=True,
                        )
                    report["skipped" if old else "imported"] += 1
                except (ValueError, TypeError) as exc:
                    bucket = (
                        "conflicts"
                        if isinstance(exc, ConversationConflict)
                        else "invalids"
                    )
                    report[bucket].append(
                        {"path": path, "id": alias, "error": str(exc)}
                    )
