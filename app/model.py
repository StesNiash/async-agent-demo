"""A deterministic stand-in for an LLM.

It receives the exact `run.messages` transcript, so it exercises the same
resume contract a real model would: assistant tool_calls, then tool results,
then the next assistant turn.
"""

from __future__ import annotations

import asyncio
import json
import re
import uuid

import httpx

from .tools import openai_tools

SYSTEM_PROMPT = """You are an agent running inside a resumable execution loop.

When a task needs a tool, call one of the provided mock tools. A tool call may
pause the run for seconds or minutes; that is expected. If you receive an
interim tool result with status=in_progress, tell the user the tool is still
running instead of claiming it finished. When the final result arrives as
independent text (TOOL RESULT (late)), summarize it for the user.

Be concise and do not invent tool results."""


def _call(name: str, **args) -> dict:
    return {
        "id": "call_" + uuid.uuid4().hex[:10],
        "name": name,
        "args": args,
    }


def _tool_summary(messages: list[dict]) -> str:
    tools = [m for m in messages if m.get("role") == "tool"]
    if not tools:
        return ""
    parts = []
    for message in tools[-4:]:
        content = message.get("content") or ""
        try:
            data = json.loads(content)
        except (TypeError, ValueError):
            data = {"raw": content}
        if isinstance(data, dict) and data.get("error"):
            parts.append("ошибка: " + str(data["error"]))
        elif isinstance(data, dict) and data.get("tool") == "mock_compile":
            parts.append("компиляция: " + str(data.get("output", "ok")))
        else:
            parts.append(json.dumps(data, ensure_ascii=False))
    return "; ".join(parts)


def _call_parts(call: dict) -> tuple[str, str, dict]:
    """Accept both internal and already-serialized OpenAI tool call shapes."""

    function = call.get("function")
    if isinstance(function, dict):
        name = function.get("name")
        raw_args = function.get("arguments") or "{}"
        try:
            args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
        except (TypeError, ValueError):
            args = {"_raw_arguments": raw_args}
    else:
        name = call.get("name")
        args = call.get("args")
    if not name:
        raise ValueError(f"assistant tool_call has no function name: {call!r}")
    return (
        call.get("id") or "call_" + uuid.uuid4().hex[:10],
        name,
        args if isinstance(args, dict) else {"value": args},
    )


def _openai_messages(messages: list[dict]) -> list[dict]:
    out: list[dict] = []
    for message in messages:
        role = message.get("role")
        if role == "assistant":
            item: dict = {
                "role": "assistant",
                "content": message.get("content") or "",
            }
            if message.get("tool_calls"):
                item["tool_calls"] = [
                    {
                        "id": call_id,
                        "type": "function",
                        "function": {
                            "name": name,
                            "arguments": json.dumps(args, ensure_ascii=False),
                        },
                    }
                    for call_id, name, args in (
                        _call_parts(call) for call in message["tool_calls"]
                    )
                ]
            out.append(item)
        elif role == "tool":
            out.append({
                "role": "tool",
                "tool_call_id": message.get("tool_call_id"),
                "content": message.get("content") or "",
            })
        else:
            out.append({
                "role": role,
                "content": message.get("content") or "",
            })
    return out


class OpenAIModel:
    """Minimal client for any OpenAI-compatible chat completions endpoint."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        temperature: float = 0.2,
        timeout: float = 120.0,
        client: httpx.AsyncClient | None = None,
    ):
        url = base_url.rstrip("/")
        if not url.endswith("/chat/completions"):
            url += "/chat/completions"
        self.url = url
        self.api_key = api_key
        self.model = model
        self.temperature = temperature
        self._client = client or httpx.AsyncClient(timeout=timeout)

    async def close(self) -> None:
        await self._client.aclose()

    async def complete(self, messages: list[dict]) -> dict:
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                *_openai_messages(messages),
            ],
            "temperature": self.temperature,
            "tools": openai_tools(),
            "tool_choice": "auto",
        }
        response = await self._client.post(
            self.url,
            headers={"Authorization": f"Bearer {self.api_key}"},
            json=payload,
        )
        if response.status_code >= 400:
            raise RuntimeError(
                f"LLM HTTP {response.status_code}: {response.text[:500]}"
            )
        data = response.json()
        message = data["choices"][0]["message"]
        tool_calls = []
        for call in message.get("tool_calls") or []:
            function = call.get("function") or {}
            raw_args = function.get("arguments") or "{}"
            try:
                args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
            except (TypeError, ValueError):
                args = {"_raw_arguments": raw_args}
            tool_calls.append({
                "id": call.get("id") or "call_" + uuid.uuid4().hex[:10],
                "name": function.get("name"),
                "args": args or {},
            })
        return {
            "content": message.get("content") or "",
            "tool_calls": tool_calls,
        }


class MockModel:
    async def complete(self, messages: list[dict]) -> dict:
        await asyncio.sleep(0.05)
        last = messages[-1] if messages else {"role": "user", "content": ""}
        role = last.get("role")

        if role == "tool":
            summary = _tool_summary(messages)
            return {
                "content": f"Инструмент завершился: {summary}",
                "tool_calls": [],
            }

        if role == "system" and "HEARTBEAT" in (last.get("content") or ""):
            return {
                "content": "Ещё жду результат инструмента. Продолжаю ждать.",
                "tool_calls": [],
            }

        if role == "system" and "NOTIFICATION:" in (last.get("content") or ""):
            text = (last.get("content") or "").split("NOTIFICATION:", 1)[-1].strip()
            return {
                "content": "Принял уведомление во время ожидания: " + text,
                "tool_calls": [],
            }

        if role == "system" and "TOOL RESULT (late):" in (last.get("content") or ""):
            text = (last.get("content") or "").split("TOOL RESULT (late):", 1)[-1].strip()
            return {
                "content": "Получен поздний результат инструмента: " + text,
                "tool_calls": [],
            }

        text = ""
        for message in reversed(messages):
            if message.get("role") == "user":
                text = str(message.get("content") or "")
                break
        lower = text.lower()

        if "multi" in lower:
            return {
                "content": "",
                "tool_calls": [
                    _call("mock_echo", text="from-multi"),
                    _call("mock_manual", label="multi-manual"),
                ],
            }
        if "manual" in lower:
            return {
                "content": "",
                "tool_calls": [_call("mock_manual", label=text[:120])],
            }
        if "fail" in lower:
            return {"content": "", "tool_calls": [_call("mock_fail")]}
        if "compile" in lower:
            match = re.search(r"(\d+(?:\.\d+)?)", text)
            seconds = float(match.group(1)) if match else 5.0
            return {
                "content": "",
                "tool_calls": [_call("mock_compile", seconds=seconds)],
            }
        if "echo" in lower:
            echo_text = text.split("echo", 1)[-1].strip() or text
            return {
                "content": "",
                "tool_calls": [_call("mock_echo", text=echo_text)],
            }
        return {
            "content": "Принято. Это ответ без вызова инструментов: " + text,
            "tool_calls": [],
        }
