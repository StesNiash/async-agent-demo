"""SQLite persistence for the demo.

Runs own the exact in-flight LLM transcript (`messages`). Actions are separate
jobs. Events and memory are append-only read models for the UI and for debugging.
No compaction on purpose: this demo is about pause/resume and wakeups.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

TERMINAL_RUN_STATUSES = {"done", "failed", "cancelled"}
ACTIVE_ACTION_STATUSES = {"queued", "running"}


def _json(value: Any, default=None) -> Any:
    if value is None:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


class Store:
    def __init__(self, path: str | Path):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            str(path), check_same_thread=False, timeout=30, isolation_level=None
        )
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._init_schema()

    def _init_schema(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS runs (
                id TEXT PRIMARY KEY,
                thread_id TEXT NOT NULL DEFAULT 'main',
                status TEXT NOT NULL,
                kind TEXT NOT NULL DEFAULT 'user',
                parent_run_id TEXT,
                messages TEXT NOT NULL,
                pending_calls TEXT NOT NULL,
                wake_at REAL,
                heartbeat_interval REAL NOT NULL DEFAULT 2.0,
                heartbeat_count INTEGER NOT NULL DEFAULT 0,
                result TEXT,
                error TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                done_at REAL
            );

            CREATE TABLE IF NOT EXISTS actions (
                id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                call_id TEXT NOT NULL,
                tool TEXT NOT NULL,
                args TEXT NOT NULL,
                mode TEXT NOT NULL DEFAULT 'auto',
                status TEXT NOT NULL DEFAULT 'queued',
                result TEXT,
                progress TEXT,
                error TEXT,
                created_at REAL NOT NULL,
                started_at REAL,
                finished_at REAL
            );

            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                type TEXT NOT NULL,
                payload TEXT NOT NULL,
                created_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS memory (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at REAL NOT NULL
            );

            CREATE INDEX IF NOT EXISTS runs_status_idx ON runs (status, created_at);
            CREATE INDEX IF NOT EXISTS actions_run_idx ON actions (run_id, created_at);
            CREATE INDEX IF NOT EXISTS events_run_idx ON events (run_id, id);
            CREATE INDEX IF NOT EXISTS memory_run_idx ON memory (run_id, id);
            """
        )
        for column, ddl in (
            ("kind", "ALTER TABLE runs ADD COLUMN kind TEXT NOT NULL DEFAULT 'user'"),
            ("parent_run_id", "ALTER TABLE runs ADD COLUMN parent_run_id TEXT"),
            ("progress", "ALTER TABLE actions ADD COLUMN progress TEXT"),
        ):
            try:
                self._conn.execute(ddl)
            except sqlite3.OperationalError:
                pass

    # ----- runs ---------------------------------------------------------

    def create_run(
        self,
        run_id: str,
        text: str,
        heartbeat_interval: float,
        kind: str = "user",
        parent_run_id: str | None = None,
    ) -> dict:
        now = time.time()
        if kind == "notification":
            messages = [{"role": "system", "content": "NOTIFICATION: " + text}]
            memory_role = "notification"
            event_type = "notification_received"
        else:
            messages = [{"role": "user", "content": text}]
            memory_role = "user"
            event_type = "user_message"
        with self._lock:
            self._conn.execute(
                "INSERT INTO runs "
                "(id, status, kind, parent_run_id, messages, pending_calls, "
                "heartbeat_interval, created_at, updated_at) "
                "VALUES (?, 'ready', ?, ?, ?, '{}', ?, ?, ?)",
                (
                    run_id, kind, parent_run_id, json.dumps(messages, ensure_ascii=False),
                    heartbeat_interval, now, now,
                ),
            )
            self._append_memory_tx(run_id, memory_role, text, now)
            self._append_event_tx(
                run_id,
                "run_created",
                {
                    "heartbeat_interval": heartbeat_interval,
                    "kind": kind,
                    "parent_run_id": parent_run_id,
                },
                now,
            )
            self._append_event_tx(run_id, event_type, {"content": text}, now)
        return self.get_run(run_id)

    def get_run(self, run_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        return self._run_row(row) if row else None

    def list_runs(self, limit: int = 50) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM runs ORDER BY created_at DESC LIMIT ?", (int(limit),)
            ).fetchall()
        return [self._run_row(row) for row in rows]

    def claim_ready_run(self) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT id FROM runs WHERE status = 'ready' "
                "ORDER BY created_at ASC, id ASC LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            now = time.time()
            changed = self._conn.execute(
                "UPDATE runs SET status = 'generating', updated_at = ? "
                "WHERE id = ? AND status = 'ready'",
                (now, row["id"]),
            )
            if changed.rowcount == 0:
                return None
            run = self.get_run(row["id"])
        return run

    def update_run(
        self,
        run_id: str,
        *,
        messages: list[dict] | None = None,
        pending_calls: dict | None = None,
        status: str | None = None,
        wake_at: float | None | object = ...,
        result: str | None | object = ...,
        error: str | None | object = ...,
    ) -> dict | None:
        fields: list[str] = []
        values: list[Any] = []
        if messages is not None:
            fields.append("messages = ?")
            values.append(json.dumps(messages, ensure_ascii=False))
        if pending_calls is not None:
            fields.append("pending_calls = ?")
            values.append(json.dumps(pending_calls, ensure_ascii=False))
        if status is not None:
            fields.append("status = ?")
            values.append(status)
        if wake_at is not ...:
            fields.append("wake_at = ?")
            values.append(wake_at)
        if result is not ...:
            fields.append("result = ?")
            values.append(result)
        if error is not ...:
            fields.append("error = ?")
            values.append(error)
        fields.append("updated_at = ?")
        values.append(time.time())
        if status == "done":
            fields.append("done_at = ?")
            values.append(time.time())
        values.append(run_id)
        with self._lock:
            self._conn.execute(
                f"UPDATE runs SET {', '.join(fields)} WHERE id = ?", values
            )
        return self.get_run(run_id)

    def claim_due_heartbeats(self, now: float | None = None) -> list[dict]:
        """Wake every waiting run whose heartbeat is due.

        The wake is an in-place update of the same run. Pending calls first get
        an interim tool message; no synthetic heartbeat text is added to the
        transcript. The action itself is untouched: this is a timeout of the
        agent's wait, not of the tool.
        """
        now = time.time() if now is None else now
        woken: list[dict] = []
        with self._lock:
            rows = self._conn.execute(
                "SELECT id FROM runs WHERE status = 'waiting' "
                "AND wake_at IS NOT NULL AND wake_at <= ? ORDER BY wake_at ASC",
                (now,),
            ).fetchall()
            for row in rows:
                run = self.wake_run(row["id"], reason="heartbeat")
                if run is not None:
                    woken.append(run)
        return woken

    def cancel_run(self, run_id: str) -> dict | None:
        with self._lock:
            now = time.time()
            run = self.get_run(run_id)
            if run is None or run["status"] in TERMINAL_RUN_STATUSES:
                return run
            self._conn.execute(
                "UPDATE runs SET status = 'cancelled', wake_at = NULL, updated_at = ?, done_at = ? "
                "WHERE id = ?",
                (now, now, run_id),
            )
            self._conn.execute(
                "UPDATE actions SET status = 'cancelled', finished_at = ? "
                "WHERE run_id = ? AND status IN ('queued', 'running')",
                (now, run_id),
            )
            self._append_memory_tx(run_id, "system", "RUN CANCELLED", now)
            self._append_event_tx(run_id, "run_cancelled", {}, now)
        return self.get_run(run_id)

    def reset_generating(self) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE runs SET status = 'ready', updated_at = ? WHERE status = 'generating'",
                (time.time(),),
            )

    # ----- actions ------------------------------------------------------

    def create_action(
        self,
        action_id: str,
        run_id: str,
        call_id: str,
        tool: str,
        args: dict,
        mode: str,
    ) -> dict:
        now = time.time()
        with self._lock:
            self._conn.execute(
                "INSERT INTO actions "
                "(id, run_id, call_id, tool, args, mode, status, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 'queued', ?)",
                (
                    action_id, run_id, call_id, tool,
                    json.dumps(args, ensure_ascii=False), mode, now,
                ),
            )
            self._append_event_tx(
                run_id, "action_created",
                {"action_id": action_id, "call_id": call_id, "tool": tool, "args": args},
                now,
            )
        return self.get_action(action_id)

    def get_action(self, action_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM actions WHERE id = ?", (action_id,)
            ).fetchone()
        return self._action_row(row) if row else None

    def get_actions(self, run_id: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM actions WHERE run_id = ? ORDER BY created_at ASC, id ASC",
                (run_id,),
            ).fetchall()
        return [self._action_row(row) for row in rows]

    def mark_action_running(self, action_id: str) -> dict | None:
        with self._lock:
            now = time.time()
            changed = self._conn.execute(
                "UPDATE actions SET status = 'running', started_at = ? "
                "WHERE id = ? AND status = 'queued'",
                (now, action_id),
            )
            if changed.rowcount:
                action = self.get_action(action_id)
                self._append_event_tx(
                    action["run_id"], "action_started",
                    {"action_id": action_id, "tool": action["tool"]}, now,
                )
        return self.get_action(action_id)

    def set_action_progress(self, action_id: str, progress: dict) -> dict | None:
        with self._lock:
            action = self.get_action(action_id)
            if action is None or action["status"] not in ACTIVE_ACTION_STATUSES:
                return action
            now = time.time()
            self._conn.execute(
                "UPDATE actions SET progress = ? WHERE id = ?",
                (json.dumps(progress, ensure_ascii=False), action_id),
            )
            self._append_event_tx(
                action["run_id"],
                "action_progress",
                {"action_id": action_id, "progress": progress},
                now,
            )
        return self.get_action(action_id)

    def wake_run(
        self,
        run_id: str,
        *,
        notification: str | None = None,
        reason: str = "heartbeat",
    ) -> dict | None:
        """Wake a waiting run in place.

        Before waking for a heartbeat/notification, every pending tool call that
        has no result yet gets an *interim* tool message ("in progress", partial
        output). This keeps assistant.tool_calls followed by tool messages while
        still allowing the same run to react to a new event. The eventual result
        will be appended as independent text, not as a second tool result.
        """
        with self._lock:
            run = self.get_run(run_id)
            if run is None or run["status"] not in ("waiting", "ready"):
                return None
            now = time.time()
            messages = run["messages"]
            pending = dict(run["pending_calls"])
            text_by_call = {
                m.get("tool_call_id")
                for m in messages
                if m.get("role") == "tool"
            }
            for call_id, action_id in pending.items():
                if call_id in text_by_call:
                    continue
                action = self.get_action(action_id) or {}
                progress = action.get("progress") or {}
                interim = {
                    "status": "in_progress",
                    "action_id": action_id,
                    "tool": action.get("tool"),
                    "message": "tool is still running; this is not the final result",
                    "progress": progress,
                }
                content = json.dumps(interim, ensure_ascii=False)
                messages.append({
                    "role": "tool",
                    "tool_call_id": call_id,
                    "content": content,
                })
                self._append_memory_tx(run_id, "tool", content, now)
                self._append_event_tx(
                    run_id,
                    "tool_interim",
                    {"action_id": action_id, "call_id": call_id, "progress": progress},
                    now,
                )

            if notification is not None:
                content = notification
                messages.append({"role": "user", "content": content})
                self._append_memory_tx(run_id, "user", content, now)
                self._append_event_tx(
                    run_id, "notification_received",
                    {"content": content, "reason": reason}, now,
                )
            else:
                # Keep the heartbeat wording generic. The model should not need
                # to know which action is pending; it has the exact transcript.
                # The message also gives the model a fresh turn to speak instead
                # of silently re-running the last user request.
                waited = max(0.0, now - (run["wait_started_at"] or now))
                content = f"No new events in the last {waited:.1f}s."
                messages.append({"role": "system", "content": content})
                self._append_memory_tx(run_id, "system", content, now)
                self._append_event_tx(
                    run_id, "heartbeat",
                    {"waited_s": waited, "pending": len(pending), "reason": reason},
                    now,
                )

            self._conn.execute(
                "UPDATE runs SET status = 'ready', wake_at = NULL, messages = ?, "
                "heartbeat_count = heartbeat_count + ?, updated_at = ? WHERE id = ?",
                (
                    json.dumps(messages, ensure_ascii=False),
                    0 if notification is not None else 1,
                    now,
                    run_id,
                ),
            )
        return self.get_run(run_id)

    def finish_action(
        self,
        action_id: str,
        result: Any = None,
        error: str | None = None,
    ) -> dict:
        """Mark an action finished and append its result to the owning run.

        Returns {"changed": bool, "action": ..., "run": ...}. The run is resumed
        only when every call of the current assistant turn has a result.
        """
        with self._lock:
            now = time.time()
            action = self.get_action(action_id)
            if action is None:
                return {"changed": False, "action": None, "run": None}
            if action["status"] not in ACTIVE_ACTION_STATUSES:
                return {
                    "changed": False,
                    "action": action,
                    "run": self.get_run(action["run_id"]),
                }

            final_status = "failed" if error else "done"
            self._conn.execute(
                "UPDATE actions SET status = ?, result = ?, error = ?, finished_at = ? "
                "WHERE id = ?",
                (
                    final_status,
                    None if result is None else json.dumps(result, ensure_ascii=False),
                    error,
                    now,
                    action_id,
                ),
            )
            self._append_event_tx(
                action["run_id"],
                "action_failed" if error else "action_done",
                {
                    "action_id": action_id,
                    "call_id": action["call_id"],
                    "tool": action["tool"],
                    "result": result,
                    "error": error,
                },
                now,
            )

            run = self.get_run(action["run_id"])
            if run is None or run["status"] in TERMINAL_RUN_STATUSES:
                return {"changed": True, "action": self.get_action(action_id), "run": run}

            messages = run["messages"]
            pending = dict(run["pending_calls"])
            pending.pop(action["call_id"], None)
            tool_content = (
                json.dumps({"error": error}, ensure_ascii=False)
                if error
                else json.dumps(result, ensure_ascii=False)
            )
            already_answered = any(
                m.get("role") == "tool" and m.get("tool_call_id") == action["call_id"]
                for m in messages
            )
            if already_answered:
                # The call already got an interim tool result when a heartbeat or
                # notification woke this run. The late final result is independent
                # text, not a second tool result for the same call_id.
                late = "TOOL RESULT (late): " + tool_content
                messages.append({"role": "system", "content": late})
                self._append_memory_tx(run["id"], "system", late, now)
                self._append_event_tx(
                    run["id"], "tool_result_late",
                    {"action_id": action_id, "call_id": action["call_id"], "error": error},
                    now,
                )
            else:
                messages.append({
                    "role": "tool",
                    "tool_call_id": action["call_id"],
                    "content": tool_content,
                })
                self._append_memory_tx(run["id"], "tool", tool_content, now)

            if pending:
                status = "waiting"
                wake_at = now + run["heartbeat_interval"]
            else:
                status = "ready"
                wake_at = None
            self._conn.execute(
                "UPDATE runs SET messages = ?, pending_calls = ?, status = ?, wake_at = ?, "
                "updated_at = ? WHERE id = ?",
                (
                    json.dumps(messages, ensure_ascii=False),
                    json.dumps(pending, ensure_ascii=False),
                    status, wake_at, now, run["id"],
                ),
            )
            action = self.get_action(action_id)
            run = self.get_run(run["id"])
        return {"changed": True, "action": action, "run": run}

    # ----- events / memory ---------------------------------------------

    def append_event(self, run_id: str, type_: str, payload: dict) -> int:
        with self._lock:
            return self._append_event_tx(run_id, type_, payload, time.time())

    def append_memory(self, run_id: str, role: str, content: str) -> int:
        with self._lock:
            return self._append_memory_tx(run_id, role, content, time.time())

    def get_events(self, run_id: str, after_id: int = 0, limit: int = 500) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM events WHERE run_id = ? AND id > ? ORDER BY id ASC LIMIT ?",
                (run_id, int(after_id), int(limit)),
            ).fetchall()
        return [self._event_row(row) for row in rows]

    def get_memory(self, run_id: str | None = None, limit: int = 500) -> list[dict]:
        with self._lock:
            if run_id:
                rows = self._conn.execute(
                    "SELECT * FROM memory WHERE run_id = ? ORDER BY id ASC LIMIT ?",
                    (run_id, int(limit)),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM memory ORDER BY id ASC LIMIT ?", (int(limit),)
                ).fetchall()
        return [dict(row) for row in rows]

    # ----- internal helpers --------------------------------------------

    def _append_event_tx(self, run_id: str, type_: str, payload: dict, now: float) -> int:
        cur = self._conn.execute(
            "INSERT INTO events (run_id, type, payload, created_at) VALUES (?, ?, ?, ?)",
            (run_id, type_, json.dumps(payload, ensure_ascii=False), now),
        )
        return int(cur.lastrowid)

    def _append_memory_tx(self, run_id: str, role: str, content: str, now: float) -> int:
        cur = self._conn.execute(
            "INSERT INTO memory (run_id, role, content, created_at) VALUES (?, ?, ?, ?)",
            (run_id, role, content, now),
        )
        return int(cur.lastrowid)

    def _run_row(self, row: sqlite3.Row) -> dict:
        run = dict(row)
        run["messages"] = _json(run["messages"], [])
        run["pending_calls"] = _json(run["pending_calls"], {})
        run["wait_started_at"] = (
            (run["wake_at"] - run["heartbeat_interval"])
            if run["status"] == "waiting" and run["wake_at"] is not None
            else None
        )
        return run

    def _action_row(self, row: sqlite3.Row) -> dict:
        action = dict(row)
        action["args"] = _json(action["args"], {})
        action["result"] = _json(action["result"], None)
        action["progress"] = _json(action.get("progress"), None)
        return action

    def _event_row(self, row: sqlite3.Row) -> dict:
        event = dict(row)
        event["payload"] = _json(event["payload"], {})
        return event

    def close(self) -> None:
        with self._lock:
            self._conn.close()
