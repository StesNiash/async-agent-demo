from __future__ import annotations

import asyncio
import time

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

from app.engine import RunEngine
from app.main import create_app
from app.model import MockModel
from app.store import Store


async def wait_until(fn, timeout: float = 3.0, interval: float = 0.02):
    deadline = asyncio.get_running_loop().time() + timeout
    last = None
    while asyncio.get_running_loop().time() < deadline:
        last = fn()
        if last:
            return last
        await asyncio.sleep(interval)
    raise AssertionError(f"condition not met in {timeout}s; last={last!r}")


@pytest_asyncio.fixture
async def engine(tmp_path):
    store = Store(tmp_path / "demo.db")
    eng = RunEngine(store, MockModel(), heartbeat_interval=30.0, poll_interval=0.01)
    await eng.start()
    try:
        yield eng, store
    finally:
        await eng.stop()
        store.close()


@pytest.mark.asyncio
async def test_echo_round_trip(engine):
    eng, store = engine
    run_id = await eng.create_run("echo hello", heartbeat_interval=30)
    run = await eng.wait_for_run(run_id, timeout=3)
    assert run["status"] == "done", run
    assert "hello" in (run["result"] or "")
    actions = store.get_actions(run_id)
    assert actions and actions[0]["status"] == "done"
    roles = [m["role"] for m in store.get_memory(run_id)]
    assert roles[0] == "user" and "tool" in roles and roles[-1] == "assistant"


@pytest.mark.asyncio
async def test_manual_action_pauses_run_and_does_not_block_other_runs(engine):
    eng, store = engine
    run_id = await eng.create_run("manual", heartbeat_interval=30)

    run = await wait_until(lambda: store.get_run(run_id) if store.get_run(run_id)["status"] == "waiting" else None)
    assert run["status"] == "waiting"
    action = store.get_actions(run_id)[0]
    assert action["status"] == "running"
    assert run["messages"][-1]["role"] == "assistant"
    assert run["messages"][-1]["tool_calls"][0]["id"] == action["call_id"]

    # The worker is not blocked: another run can complete while this one waits.
    other_id = await eng.create_run("echo second", heartbeat_interval=30)
    other = await eng.wait_for_run(other_id, timeout=3)
    assert other["status"] == "done"
    assert store.get_run(run_id)["status"] == "waiting"

    outcome = await eng.complete_action(action["id"], result={"manual": "ok"})
    assert outcome["changed"] is True
    done = await eng.wait_for_run(run_id, timeout=3)
    assert done["status"] == "done"
    assert "manual" in (done["result"] or "")


@pytest.mark.asyncio
async def test_heartbeat_wakes_run_but_does_not_timeout_tool(engine):
    eng, store = engine
    run_id = await eng.create_run("manual", heartbeat_interval=0.08)

    await wait_until(lambda: [e for e in store.get_events(run_id) if e["type"] == "heartbeat"])
    action = store.get_actions(run_id)[0]
    assert action["status"] == "running"
    assert store.get_run(run_id)["status"] != "failed"

    # Give the heartbeat a chance to resume generation and wait again.
    await asyncio.sleep(0.2)
    assert store.get_action(action["id"])["status"] == "running"
    run = store.get_run(run_id)
    assert run["status"] in ("waiting", "ready", "generating")

    await eng.complete_action(action["id"], result={"manual": "after-heartbeat"})
    done = await eng.wait_for_run(run_id, timeout=3)
    assert done["status"] == "done"
    assert "after-heartbeat" in (done["result"] or "")
    assert any(e["type"] == "heartbeat" for e in store.get_events(run_id))


@pytest.mark.asyncio
async def test_notification_wakes_same_run_with_interim_and_late_result(engine):
    eng, store = engine
    run_id = await eng.create_run("manual", heartbeat_interval=30)
    await wait_until(lambda: store.get_run(run_id)["status"] == "waiting")

    run = await eng.notify_run(run_id, "peer ping")
    assert run is not None

    # The same run processes the notification and returns to waiting; the
    # transcript contains an interim tool result before the notification.
    await wait_until(
        lambda: (
            store.get_run(run_id)["status"] == "waiting"
            and any("peer ping" in m["content"] for m in store.get_memory(run_id))
        )
    )
    messages = store.get_run(run_id)["messages"]
    call_id = store.get_actions(run_id)[0]["call_id"]
    tool_messages = [
        m for m in messages if m.get("role") == "tool" and m.get("tool_call_id") == call_id
    ]
    assert len(tool_messages) == 1
    assert "in_progress" in tool_messages[0]["content"]
    assert any(m.get("role") == "user" and "peer ping" in m["content"] for m in messages)
    assert store.get_run(run_id)["pending_calls"] == {call_id: store.get_actions(run_id)[0]["id"]}

    action = store.get_actions(run_id)[0]
    await eng.complete_action(action["id"], result={"manual": "after-notification"})
    done = await eng.wait_for_run(run_id, timeout=3)
    assert done["status"] == "done"
    assert "after-notification" in (done["result"] or "")

    late = [
        m for m in done["messages"]
        if m.get("role") == "system" and "TOOL RESULT (late)" in m.get("content", "")
    ]
    assert late and "after-notification" in late[-1]["content"]
    # Still exactly one tool message for the call; the final result is independent
    # system text, not a duplicate tool result.
    assert len([
        m for m in done["messages"]
        if m.get("role") == "tool" and m.get("tool_call_id") == call_id
    ]) == 1


@pytest.mark.asyncio
async def test_failed_action_resumes_with_error(engine):
    eng, store = engine
    run_id = await eng.create_run("fail", heartbeat_interval=30)
    run = await eng.wait_for_run(run_id, timeout=3)
    assert run["status"] == "done"
    assert "ошиб" in (run["result"] or "").lower()
    assert store.get_actions(run_id)[0]["status"] == "failed"


@pytest.mark.asyncio
async def test_multiple_pending_actions_wait_for_all(engine):
    eng, store = engine
    run_id = await eng.create_run("multi", heartbeat_interval=30)
    await wait_until(lambda: len(store.get_actions(run_id)) == 2)

    actions = store.get_actions(run_id)
    manual = next(a for a in actions if a["mode"] == "manual")
    echo = next(a for a in actions if a["mode"] == "auto")

    await wait_until(lambda: store.get_action(echo["id"])["status"] == "done")
    assert store.get_run(run_id)["status"] == "waiting"

    await eng.complete_action(manual["id"], result={"manual": "second"})
    done = await eng.wait_for_run(run_id, timeout=3)
    assert done["status"] == "done"
    assert "second" in (done["result"] or "")


@pytest.mark.asyncio
async def test_memory_is_append_only(engine):
    eng, store = engine
    run_id = await eng.create_run("echo memory", heartbeat_interval=30)
    await eng.wait_for_run(run_id, timeout=3)
    first = store.get_memory(run_id)
    ids = [m["id"] for m in first]
    assert ids == sorted(ids)
    assert ids[0] == 1

    # Reading or resuming does not rewrite old records.
    await eng.create_run("echo more", heartbeat_interval=30)
    second = store.get_memory(run_id)
    assert [m["id"] for m in second] == ids


def test_http_api_manual_completion(tmp_path):
    app = create_app(tmp_path / "api.db", heartbeat_interval=0.1)
    with TestClient(app) as client:
        response = client.post("/runs", json={"message": "manual", "heartbeat_interval": 30})
        assert response.status_code == 200
        run_id = response.json()["run_id"]

        deadline = time.time() + 3
        action_id = None
        while time.time() < deadline:
            snapshot = client.get(f"/runs/{run_id}").json()
            actions = snapshot["actions"]
            if actions:
                action_id = actions[0]["id"]
                break
            time.sleep(0.03)
        assert action_id

        response = client.post(
            f"/actions/{action_id}/complete",
            json={"result": {"manual": "via-http"}},
        )
        assert response.status_code == 200

        response = client.post(f"/runs/{run_id}/wait?timeout=3")
        assert response.status_code == 200
        run = response.json()["run"]
        assert run["status"] == "done"
        assert "via-http" in (run["result"] or "")


def test_http_api_notification_stays_in_same_run(tmp_path):
    app = create_app(tmp_path / "notify.db", heartbeat_interval=0.1)
    with TestClient(app) as client:
        run_id = client.post(
            "/runs", json={"message": "manual", "heartbeat_interval": 30}
        ).json()["run_id"]

        deadline = time.time() + 3
        while time.time() < deadline:
            snapshot = client.get(f"/runs/{run_id}").json()
            if snapshot["actions"] and snapshot["run"]["status"] == "waiting":
                break
            time.sleep(0.03)
        action = snapshot["actions"][0]

        response = client.post(
            f"/runs/{run_id}/notify", json={"message": "peer ping"}
        )
        assert response.status_code == 200

        deadline = time.time() + 3
        while time.time() < deadline:
            snapshot = client.get(f"/runs/{run_id}").json()
            if snapshot["run"]["status"] == "waiting" and any(
                "peer ping" in m["content"] for m in snapshot["memory"]
            ):
                break
            time.sleep(0.03)
        assert snapshot["run"]["status"] == "waiting"
        assert any(
            m["role"] == "tool" and "in_progress" in m["content"]
            for m in snapshot["run"]["messages"]
        )

        client.post(
            f"/actions/{action['id']}/complete",
            json={"result": {"manual": "http-late"}},
        )
        done = client.post(f"/runs/{run_id}/wait?timeout=3").json()["run"]
        assert done["status"] == "done"
        assert any(
            "TOOL RESULT (late)" in m.get("content", "")
            for m in done["messages"]
        )
