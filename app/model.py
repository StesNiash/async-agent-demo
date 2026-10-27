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
