"""Resumable run engine.

The unit of execution is a run, not a task. A run pauses after emitting tool
calls, persists its exact transcript, and is woken by either an action result or
a heartbeat. The worker never waits on a tool.

Terms:
  run      — resumable generation transcript
  action   — an asynchronous tool execution owned by a run
  wake_at  — when to wake the run because its *wait* timed out (not the tool)
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from typing import Any

from .store import TERMINAL_RUN_STATUSES, Store
from .tools import action_spec


class RunEngine:
    def __init__(
        self,
        store: Store,
        model,
        heartbeat_interval: float = 2.0,
        poll_interval: float = 0.03,
    ):
        self.store = store
        self.model = model
        self.heartbeat_interval = max(0.05, float(heartbeat_interval))
        self.poll_interval = max(0.01, float(poll_interval))
        self._wake = asyncio.Event()
        self._stopping = False
        self._worker_task: asyncio.Task | None = None
        self._heartbeat_task: asyncio.Task | None = None
        self._action_tasks: set[asyncio.Task] = set()
        self._run_locks: dict[str, asyncio.Lock] = {}

    async def start(self) -> None:
        self.store.reset_generating()
        self._worker_task = asyncio.create_task(self._worker_loop(), name="run-worker")
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop(), name="heartbeat")

    async def stop(self) -> None:
        self._stopping = True
        self._wake.set()
        for task in [self._worker_task, self._heartbeat_task, *self._action_tasks]:
            if task is not None:
                task.cancel()
        await asyncio.gather(
            *[task for task in [self._worker_task, self._heartbeat_task, *self._action_tasks]
              if task is not None],
            return_exceptions=True,
        )
        self._worker_task = None
        self._heartbeat_task = None
        self._action_tasks.clear()

    async def create_run(self, text: str, heartbeat_interval: float | None = None) -> str:
        run_id = uuid.uuid4().hex[:10]
        interval = self.heartbeat_interval if heartbeat_interval is None else heartbeat_interval
        self.store.create_run(run_id, text.strip(), max(0.05, float(interval)))
        self._wake.set()
        return run_id

    async def notify_run(self, run_id: str, text: str) -> dict | None:
        """Inject a notification into *this* run and wake it in place.

        If the run is waiting on tools, each pending call first gets an interim
        tool message. The notification is then appended as a user message. The
        final action result is appended later as independent system text.
        """
        lock = self._run_lock(run_id)
        async with lock:
            run = self.store.wake_run(
                run_id, notification=text.strip(), reason="notification"
            )
        if run is not None:
            self._wake.set()
        return run

    async def set_action_progress(self, action_id: str, progress: dict) -> dict | None:
        action = self.store.get_action(action_id)
        if action is None:
            return None
        lock = self._run_lock(action["run_id"])
        async with lock:
            action = self.store.set_action_progress(action_id, progress)
        return action

    async def cancel_run(self, run_id: str) -> dict | None:
        lock = self._run_lock(run_id)
        async with lock:
            run = self.store.cancel_run(run_id)
        self._wake.set()
        return run

    async def complete_action(
        self,
        action_id: str,
        result: Any = None,
        error: str | None = None,
    ) -> dict:
        action = self.store.get_action(action_id)
        if action is None:
            return {"changed": False, "action": None, "run": None}
        lock = self._run_lock(action["run_id"])
        async with lock:
            outcome = self.store.finish_action(action_id, result=result, error=error)
        if outcome.get("changed"):
            self.store.append_event(
                outcome["run"]["id"],
                "action_result_delivered",
                {"action_id": action_id, "error": error},
            )
        self._wake.set()
        return outcome

    async def wait_for_run(self, run_id: str, timeout: float = 30.0) -> dict | None:
        deadline = asyncio.get_running_loop().time() + max(0.0, float(timeout))
        while True:
            run = self.store.get_run(run_id)
            if run is None or run["status"] in TERMINAL_RUN_STATUSES:
                return run
            if asyncio.get_running_loop().time() >= deadline:
                return run
            await asyncio.sleep(0.03)

    def snapshot(self, run_id: str, event_limit: int = 200) -> dict | None:
        run = self.store.get_run(run_id)
        if run is None:
            return None
        return {
            "run": run,
            "actions": self.store.get_actions(run_id),
            "events": self.store.get_events(run_id, limit=event_limit),
            "memory": self.store.get_memory(run_id, limit=event_limit),
        }

    # ----- worker -------------------------------------------------------

    async def _worker_loop(self) -> None:
        while not self._stopping:
            try:
                run = self.store.claim_ready_run()
                if run is not None:
                    try:
                        await self._step(run)
                    except Exception as exc:  # noqa: BLE001 - a run failure must not kill the worker
                        self.store.update_run(
                            run["id"], status="failed", error=str(exc), wake_at=None
                        )
                        self.store.append_event(
                            run["id"], "run_failed", {"error": str(exc)}
                        )
                    continue
                self._wake.clear()
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=self.poll_interval)
                except asyncio.TimeoutError:
                    pass
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - keep the demo worker alive
                await asyncio.sleep(0.1)

    async def _step(self, run: dict) -> None:
        run_id = run["id"]
        lock = self._run_lock(run_id)
        async with lock:
            current = self.store.get_run(run_id)
            if current is None or current["status"] != "generating":
                return
            messages = current["messages"]
            self.store.append_event(
                run_id, "llm_started", {"message_count": len(messages)}
            )
            response = await self.model.complete(messages)

            current = self.store.get_run(run_id)
            if current is None or current["status"] != "generating":
                return

            content = response.get("content") or ""
            tool_calls = response.get("tool_calls") or []

            pending_actions = [
                action
                for action in (
                    self.store.get_action(action_id)
                    for action_id in current["pending_calls"].values()
                )
                if action is not None
            ]

            def args_key(value) -> str:
                return json.dumps(value or {}, sort_keys=True, ensure_ascii=False)

            accepted: list[dict] = []
            duplicates: list[tuple[dict, dict]] = []
            for call in tool_calls:
                match = next(
                    (
                        action
                        for action in pending_actions
                        if action["tool"] == call["name"]
                        and args_key(action["args"]) == args_key(call.get("args"))
                    ),
                    None,
                )
                if match is None:
                    accepted.append(call)
                else:
                    duplicates.append((call, match))

            for call, action in duplicates:
                self.store.append_event(
                    run_id,
                    "duplicate_tool_call_ignored",
                    {
                        "call_id": call["id"],
                        "tool": call["name"],
                        "action_id": action["id"],
                    },
                )

            if duplicates and not accepted:
                # The model re-requested work that is already pending. Treat it
                # as progress, not as a second action.
                progress = content or (
                    "The requested tool is already running; waiting for its result."
                )
                messages.append({"role": "assistant", "content": progress})
                self.store.append_memory(run_id, "assistant", progress)
                self.store.append_event(
                    run_id, "assistant_progress", {"content": progress}
                )
                self.store.update_run(
                    run_id,
                    messages=messages,
                    status="waiting",
                    wake_at=time.time() + current["heartbeat_interval"],
                )
                return

            tool_calls = accepted
            assistant = {"role": "assistant", "content": content}
            if tool_calls:
                assistant["tool_calls"] = [
                    {
                        "id": call["id"],
                        "type": "function",
                        "function": {
                            "name": call["name"],
                            "arguments": json.dumps(
                                call.get("args") or {}, ensure_ascii=False
                            ),
                        },
                    }
                    for call in tool_calls
                ]
            messages.append(assistant)
            self.store.append_memory(
                run_id,
                "assistant",
                content or ("tool_calls: " + ", ".join(c["name"] for c in tool_calls)),
            )
            self.store.append_event(
                run_id,
                "assistant_message",
                {
                    "content": content,
                    "tool_calls": [
                        {"id": c["id"], "name": c["name"], "args": c.get("args") or {}}
                        for c in tool_calls
                    ],
                },
            )

            if tool_calls:
                pending = dict(current["pending_calls"])
                started: list[tuple[str, dict]] = []
                for call in tool_calls:
                    spec = action_spec(call["name"], call.get("args") or {})
                    action_id = uuid.uuid4().hex[:12]
                    self.store.create_action(
                        action_id,
                        run_id,
                        call["id"],
                        call["name"],
                        call.get("args") or {},
                        spec["mode"],
                    )
                    pending[call["id"]] = action_id
                    started.append((action_id, spec))
                self.store.update_run(
                    run_id,
                    messages=messages,
                    pending_calls=pending,
                    status="waiting",
                    wake_at=time.time() + current["heartbeat_interval"],
                )
                self.store.append_event(
                    run_id,
                    "run_waiting",
                    {"action_ids": [a for a, _ in started]},
                )
                for action_id, spec in started:
                    self._start_action(action_id, spec)
                return

            if current["pending_calls"]:
                # Heartbeat resume while a tool is still running. This is progress,
                # not the final answer: keep the run waiting for the result.
                self.store.update_run(
                    run_id,
                    messages=messages,
                    status="waiting",
                    wake_at=time.time() + current["heartbeat_interval"],
                )
                self.store.append_event(
                    run_id, "assistant_progress", {"content": content}
                )
                return

            self.store.update_run(
                run_id,
                messages=messages,
                status="done",
                wake_at=None,
                result=content,
                error=None,
            )
            self.store.append_event(run_id, "run_done", {"content": content})

    # ----- actions ------------------------------------------------------

    def _start_action(self, action_id: str, spec: dict) -> None:
        action = self.store.mark_action_running(action_id)
        if action is None or action["status"] != "running":
            return
        self.store.set_action_progress(
            action_id,
            {"partial_output": f"{action['tool']} started", "elapsed_s": 0},
        )
        if spec["mode"] == "manual":
            return
        task = asyncio.create_task(
            self._auto_action(action_id, spec), name=f"action-{action_id[:8]}"
        )
        self._action_tasks.add(task)
        task.add_done_callback(self._action_tasks.discard)

    async def _auto_action(self, action_id: str, spec: dict) -> None:
        delay = float(spec.get("delay") or 0.0)
        await asyncio.sleep(delay)
        if self._stopping:
            return
        if spec.get("error"):
            await self.complete_action(action_id, error=spec["error"])
        else:
            await self.complete_action(action_id, result=spec.get("result") or {})

    # ----- heartbeat ----------------------------------------------------

    async def _heartbeat_loop(self) -> None:
        while not self._stopping:
            try:
                woken = self.store.claim_due_heartbeats()
                if woken:
                    self._wake.set()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - keep the heartbeat alive
                pass
            await asyncio.sleep(self.poll_interval)

    # ----- helpers ------------------------------------------------------

    def _run_lock(self, run_id: str) -> asyncio.Lock:
        lock = self._run_locks.get(run_id)
        if lock is None:
            lock = asyncio.Lock()
            self._run_locks[run_id] = lock
        return lock
