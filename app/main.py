"""HTTP surface for the async-agent demo.

Externally a caller can still create a run and wait for an answer. Internally
the worker does not block on the tool: the run pauses and is resumed by an
action result or a heartbeat.
"""

from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel, Field

from .config import build_model
from .engine import RunEngine
from .store import Store
from .tools import describe_tools


class RunRequest(BaseModel):
    message: str = Field(min_length=1)
    heartbeat_interval: float | None = None


class CompleteRequest(BaseModel):
    result: Any = None
    error: str | None = None


class MessageRequest(BaseModel):
    message: str = Field(min_length=1)


class NotificationRequest(MessageRequest):
    run_id: str


class ProgressRequest(BaseModel):
    progress: dict


def create_app(
    db_path: str | Path | None = None,
    heartbeat_interval: float = 2.0,
    model=None,
) -> FastAPI:
    if db_path is None:
        db_path = os.getenv("DEMO_DB", "data/demo.db")
    store = Store(db_path)
    model = model or build_model()
    engine = RunEngine(store, model, heartbeat_interval=heartbeat_interval)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await engine.start()
        try:
            yield
        finally:
            await engine.stop()
            if hasattr(model, "close"):
                await model.close()
            store.close()

    app = FastAPI(title="async-agent-demo", lifespan=lifespan)
    app.state.store = store
    app.state.engine = engine
    app.state.model = model

    @app.get("/health")
    async def health():
        return {
            "status": "ok",
            "db": str(db_path),
            "model": type(model).__name__,
            "model_name": getattr(model, "model", None),
        }

    @app.get("/tools")
    async def tools():
        return {"tools": describe_tools()}

    @app.post("/runs")
    async def create_run(req: RunRequest):
        run_id = await engine.create_run(req.message, req.heartbeat_interval)
        return {"run_id": run_id, "status": "ready"}

    @app.post("/notifications")
    async def notify_run(req: NotificationRequest):
        run = await engine.notify_run(req.run_id, req.message)
        if run is None:
            raise HTTPException(
                status_code=409,
                detail="run does not exist or is not waiting/ready",
            )
        return {"run": run}

    @app.post("/runs/{run_id}/notify")
    async def notify_this_run(run_id: str, req: MessageRequest):
        run = await engine.notify_run(run_id, req.message)
        if run is None:
            raise HTTPException(
                status_code=409,
                detail="run does not exist or is not waiting/ready",
            )
        return {"run": run}

    @app.get("/runs")
    async def list_runs(limit: int = 50):
        return {"runs": store.list_runs(limit=limit)}

    @app.get("/runs/{run_id}")
    async def get_run(run_id: str):
        snapshot = engine.snapshot(run_id)
        if snapshot is None:
            raise HTTPException(status_code=404, detail="run not found")
        return snapshot

    @app.post("/runs/{run_id}/wait")
    async def wait_run(run_id: str, timeout: float = 30.0):
        run = await engine.wait_for_run(run_id, timeout=timeout)
        if run is None:
            raise HTTPException(status_code=404, detail="run not found")
        snapshot = engine.snapshot(run_id)
        snapshot["waited"] = run["status"] not in ("done", "failed", "cancelled")
        return snapshot

    @app.post("/runs/{run_id}/cancel")
    async def cancel_run(run_id: str):
        run = await engine.cancel_run(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="run not found")
        return {"run": run}

    @app.post("/actions/{action_id}/complete")
    async def complete_action(action_id: str, req: CompleteRequest):
        action = store.get_action(action_id)
        if action is None:
            raise HTTPException(status_code=404, detail="action not found")
        outcome = await engine.complete_action(
            action_id, result=req.result, error=req.error
        )
        if not outcome.get("changed"):
            raise HTTPException(
                status_code=409,
                detail=f"action is not active (status={outcome['action']['status']})",
            )
        return outcome

    @app.post("/actions/{action_id}/progress")
    async def progress(action_id: str, req: ProgressRequest):
        action = await engine.set_action_progress(action_id, req.progress)
        if action is None:
            raise HTTPException(status_code=404, detail="action not found")
        return {"action": action}

    @app.get("/runs/{run_id}/events")
    async def events(run_id: str, after: int = 0):
        if store.get_run(run_id) is None:
            raise HTTPException(status_code=404, detail="run not found")

        async def generate():
            last = int(after)
            idle = 0
            while True:
                batch = store.get_events(run_id, after_id=last, limit=200)
                for event in batch:
                    last = event["id"]
                    idle = 0
                    yield "data: " + json.dumps(event, ensure_ascii=False) + "\n\n"
                if not batch:
                    run = store.get_run(run_id)
                    if run and run["status"] in ("done", "failed", "cancelled"):
                        break
                    idle += 1
                    if idle % 15 == 0:
                        yield ": keepalive\n\n"
                await asyncio.sleep(0.2)

        return StreamingResponse(generate(), media_type="text/event-stream")

    @app.get("/memory")
    async def memory(run_id: str | None = None, limit: int = 500):
        return {"memory": store.get_memory(run_id, limit=limit)}

    @app.get("/", response_class=HTMLResponse)
    async def index():
        return HTML

    return app


HTML = """<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<title>async-agent-demo</title>
<style>
  :root { color-scheme: dark; --bg:#101318; --panel:#171c24; --line:#2a3341;
          --text:#dce4ef; --muted:#8694a8; --accent:#7cc4ff; --ok:#75d69a; --warn:#ffc46b; }
  * { box-sizing:border-box; }
  body { margin:0; font:14px/1.45 ui-monospace,SFMono-Regular,Consolas,monospace;
         background:var(--bg); color:var(--text); }
  main { max-width:1180px; margin:0 auto; padding:24px; }
  h1 { font-size:20px; margin:0 0 4px; }
  p { color:var(--muted); margin:0 0 18px; }
  .row { display:flex; gap:8px; flex-wrap:wrap; align-items:center; }
  input, textarea, button { font:inherit; color:var(--text); background:#0d1117;
         border:1px solid var(--line); border-radius:7px; padding:8px 10px; }
  input[type=text] { flex:1; min-width:260px; }
  button { cursor:pointer; background:#1b2634; }
  button:hover { border-color:var(--accent); }
  button.ok { background:#16301f; border-color:#2f6b48; }
  button:disabled { opacity:.45; cursor:default; }
  .grid { display:grid; grid-template-columns:1.1fr .9fr; gap:16px; margin-top:18px; }
  .panel { background:var(--panel); border:1px solid var(--line); border-radius:10px; padding:14px; }
  .panel h2 { font-size:13px; color:var(--muted); margin:0 0 10px; text-transform:uppercase; letter-spacing:.08em; }
  .scroll { max-height:62vh; overflow:auto; }
  .msg { padding:9px 0; border-bottom:1px solid #202936; }
  .msg:last-child { border-bottom:0; }
  .role { color:var(--accent); font-size:12px; }
  pre { white-space:pre-wrap; margin:4px 0; }
  .badge { display:inline-block; border:1px solid var(--line); border-radius:20px; padding:1px 7px; font-size:12px; color:var(--muted); }
  .ok { color:var(--ok); } .warn { color:var(--warn); }
  .action { border:1px solid var(--line); border-radius:8px; padding:8px; margin:8px 0; }
  .action .id { color:var(--muted); font-size:11px; }
  .hidden { display:none; }
  @media (max-width:900px) { .grid { grid-template-columns:1fr; } }
</style>
</head>
<body><main>
  <h1>async-agent-demo</h1>
  <p>Run pauses after a tool call. Result or heartbeat resumes the exact transcript. Tools have no timeout.</p>

  <div class="row">
    <input id="message" type="text" value="compile 4" placeholder="compile 4 | echo hello | manual | fail | multi">
    <button id="create">Создать run</button>
    <button id="refresh" disabled>Обновить</button>
  </div>
  <div class="row" style="margin-top:10px">
    <input id="notification" type="text" placeholder="уведомление во время выполнения, например: peer ping">
    <button id="notify" disabled>Кинуть уведомление</button>
  </div>
  <div class="row" style="margin-top:10px">
    <span class="badge">run: <b id="run-id">—</b></span>
    <span class="badge">status: <b id="run-status">—</b></span>
    <span class="badge">heartbeats: <b id="hb-count">0</b></span>
  </div>

  <div class="grid">
    <section class="panel">
      <h2>Run transcript / memory</h2>
      <div id="transcript" class="scroll"></div>
    </section>
    <section class="panel">
      <h2>Actions</h2>
      <div id="actions" class="scroll"></div>
      <h2 style="margin-top:16px">Events</h2>
      <div id="events" class="scroll" style="max-height:28vh"></div>
    </section>
  </div>
</main>
<script>
let current = null;
const $ = s => document.querySelector(s);

async function api(path, opts) {
  const r = await fetch(path, opts);
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(body.detail || r.statusText);
  return body;
}
function esc(v) { return String(v ?? "").replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
function roleClass(r) { return r === "tool" ? "warn" : r === "system" ? "warn" : ""; }

async function createRun() {
  const message = $("#message").value.trim();
  if (!message) return;
  const d = await api("/runs", {
    method: "POST", headers: {"Content-Type":"application/json"},
    body: JSON.stringify({message, heartbeat_interval: 1.5})
  });
  current = d.run_id;
  $("#run-id").textContent = current;
  $("#refresh").disabled = false;
  $("#notify").disabled = false;
  await refresh();
}
async function sendNotification() {
  const message = $("#notification").value.trim();
  if (!message || !current) return;
  await api("/runs/" + encodeURIComponent(current) + "/notify", {
    method:"POST", headers:{"Content-Type":"application/json"},
    body: JSON.stringify({message})
  });
  $("#notification").value = "";
  await refresh();
}
async function refresh() {
  if (!current) return;
  const d = await api("/runs/" + encodeURIComponent(current));
  const run = d.run;
  $("#run-status").textContent = run.status;
  $("#run-status").className = run.status === "done" ? "ok" : run.status === "failed" ? "warn" : "";
  $("#hb-count").textContent = run.heartbeat_count;
  $("#transcript").innerHTML = (d.memory || []).map(m =>
    `<div class="msg"><div class="role ${roleClass(m.role)}">${esc(m.role)} #${m.id}</div><pre>${esc(m.content)}</pre></div>`
  ).join("") || "<p>пусто</p>";
  $("#actions").innerHTML = (d.actions || []).map(a => {
    const done = ["done","failed","cancelled"].includes(a.status);
    return `<div class="action"><div><b>${esc(a.tool)}</b> <span class="badge">${esc(a.status)}</span></div>
      <div class="id">action ${esc(a.id)} · call ${esc(a.call_id)} · mode ${esc(a.mode)}</div>
      <pre>${esc(JSON.stringify(a.result ?? a.error ?? a.args, null, 2))}</pre>
      ${a.status === "running" || a.status === "queued"
        ? `<button class="ok" onclick="completeAction('${esc(a.id)}')">Завершить вручную</button>` : ""}</div>`;
  }).join("") || "<p>нет действий</p>";
  $("#events").innerHTML = (d.events || []).slice().reverse().map(e =>
    `<div class="msg"><div class="role">${esc(e.type)} #${e.id}</div><pre>${esc(JSON.stringify(e.payload, null, 2))}</pre></div>`
  ).join("") || "<p>нет событий</p>";
}
async function completeAction(id) {
  await api("/actions/" + encodeURIComponent(id) + "/complete", {
    method:"POST", headers:{"Content-Type":"application/json"},
    body: JSON.stringify({result: {manual: "completed from UI"}})
  });
  await refresh();
}
$("#create").onclick = () => createRun().catch(e => alert(e.message));
$("#refresh").onclick = () => refresh().catch(e => alert(e.message));
$("#notify").onclick = () => sendNotification().catch(e => alert(e.message));
setInterval(() => { if (current) refresh().catch(() => {}); }, 700);
</script>
</body></html>
"""


app = create_app()
