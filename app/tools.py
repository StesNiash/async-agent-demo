"""Mock tools for the async demo.

There is deliberately no tool timeout. `auto` tools finish on their own; `manual`
tools stay running until the API completes them. Heartbeats belong to the run,
not to these tools.
"""

from __future__ import annotations

from typing import Any

TOOL_SPECS = {
    "mock_echo": {"mode": "auto", "default_delay": 0.2},
    "mock_compile": {"mode": "auto", "default_delay": 5.0},
    "mock_manual": {"mode": "manual", "default_delay": None},
    "mock_fail": {"mode": "auto", "default_delay": 0.3},
}


def describe_tools() -> dict[str, dict]:
    return {
        "mock_echo": {
            "description": "Return the supplied text after a short mock delay.",
            "mode": "auto",
        },
        "mock_compile": {
            "description": "Simulate a Java compile. args.seconds controls duration.",
            "mode": "auto",
        },
        "mock_manual": {
            "description": "Never completes on its own; complete it through the API.",
            "mode": "manual",
        },
        "mock_fail": {
            "description": "Simulate a tool failure.",
            "mode": "auto",
        },
    }


def action_spec(name: str, args: dict | None) -> dict:
    args = args or {}
    if name == "mock_compile":
        try:
            seconds = float(args.get("seconds", 5.0))
        except (TypeError, ValueError):
            seconds = 5.0
        seconds = max(0.0, min(seconds, 60.0))
        return {
            "mode": "auto",
            "delay": seconds,
            "result": {
                "tool": name,
                "status": "ok",
                "output": "Build finished: demo.jar",
                "duration_s": seconds,
            },
            "error": None,
        }
    if name == "mock_echo":
        return {
            "mode": "auto",
            "delay": 0.2,
            "result": {"tool": name, "echo": str(args.get("text", ""))},
            "error": None,
        }
    if name == "mock_manual":
        return {
            "mode": "manual",
            "delay": None,
            "result": {"tool": name, "manual": str(args.get("label", "manual"))},
            "error": None,
        }
    if name == "mock_fail":
        return {
            "mode": "auto",
            "delay": 0.3,
            "result": None,
            "error": "mock compile failed: cannot find symbol",
        }
    return {
        "mode": "auto",
        "delay": 0.1,
        "result": None,
        "error": f"unknown mock tool: {name}",
    }


def auto_result(spec: dict, args: dict | None = None) -> dict[str, Any]:
    if spec.get("error"):
        return {}
    return spec.get("result") or {}
