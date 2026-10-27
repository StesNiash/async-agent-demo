from __future__ import annotations

import json

import httpx
import pytest

from app.engine import RunEngine
from app.model import OpenAIModel
from app.store import Store


@pytest.mark.asyncio
async def test_openai_model_converts_tool_calls_and_messages():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["payload"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "choices": [{
                    "message": {
                        "content": "",
                        "tool_calls": [{
                            "id": "call_test",
                            "type": "function",
                            "function": {
                                "name": "mock_echo",
                                "arguments": "{\"text\": \"hi\"}",
                            },
                        }],
                    }
                }]
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    model = OpenAIModel(
        base_url="https://llm.example/v1",
        api_key="test-key",
        model="test-model",
        client=client,
    )
    try:
        result = await model.complete([{"role": "user", "content": "echo hi"}])
    finally:
        await model.close()

    assert captured["url"] == "https://llm.example/v1/chat/completions"
    assert captured["payload"]["model"] == "test-model"
    assert captured["payload"]["messages"][0]["role"] == "system"
    assert captured["payload"]["tools"][0]["function"]["name"] == "mock_echo"
    assert result["content"] == ""
    assert result["tool_calls"] == [{
        "id": "call_test",
        "name": "mock_echo",
        "args": {"text": "hi"},
    }]


@pytest.mark.asyncio
async def test_openai_model_replays_assistant_tool_calls_as_openai_shape():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "done", "tool_calls": []}}]},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    model = OpenAIModel(
        base_url="https://llm.example/v1",
        api_key="test-key",
        model="test-model",
        client=client,
    )
    try:
        result = await model.complete([
            {"role": "user", "content": "echo hi"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{
                    "id": "call_1",
                    "name": "mock_echo",
                    "args": {"text": "hi"},
                }],
            },
            {
                "role": "tool",
                "tool_call_id": "call_1",
                "content": "{\"echo\": \"hi\"}",
            },
        ])
    finally:
        await model.close()

    replay = captured["payload"]["messages"][2]
    assert replay["role"] == "assistant"
    assert replay["tool_calls"][0]["id"] == "call_1"
    assert replay["tool_calls"][0]["function"]["name"] == "mock_echo"
    assert json.loads(replay["tool_calls"][0]["function"]["arguments"]) == {"text": "hi"}
    assert captured["payload"]["messages"][3] == {
        "role": "tool",
        "tool_call_id": "call_1",
        "content": "{\"echo\": \"hi\"}",
    }
    assert result["content"] == "done"


@pytest.mark.asyncio
async def test_openai_model_drives_engine_tool_call(tmp_path):
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(
                200,
                json={
                    "choices": [{
                        "message": {
                            "content": "",
                            "tool_calls": [{
                                "id": "call_engine",
                                "type": "function",
                                "function": {
                                    "name": "mock_echo",
                                    "arguments": "{\"text\": \"from-real-shape\"}",
                                },
                            }],
                        }
                    }]
                },
            )
        payload = json.loads(request.content)
        assistant = next(m for m in payload["messages"] if m["role"] == "assistant")
        assert assistant["tool_calls"][0]["function"]["name"] == "mock_echo"
        assert json.loads(
            assistant["tool_calls"][0]["function"]["arguments"]
        ) == {"text": "from-real-shape"}
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "echo finished", "tool_calls": []}}]},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    model = OpenAIModel(
        base_url="https://llm.example/v1",
        api_key="test-key",
        model="test-model",
        client=client,
    )
    store = Store(tmp_path / "openai-engine.db")
    engine = RunEngine(store, model, heartbeat_interval=30, poll_interval=0.01)
    await engine.start()
    try:
        run_id = await engine.create_run("echo hi", heartbeat_interval=30)
        run = await engine.wait_for_run(run_id, timeout=3)
        action_status = store.get_actions(run_id)[0]["status"]
    finally:
        await engine.stop()
        await model.close()
        store.close()

    assert run["status"] == "done"
    assert run["result"] == "echo finished"
    assert action_status == "done"
