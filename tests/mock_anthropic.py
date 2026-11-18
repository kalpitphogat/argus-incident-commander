"""A tiny stand-in for the Anthropic Messages API.

It lets the test-suite drive the *real* LLM code path (langchain-anthropic ->
anthropic SDK -> HTTP) without an API key, and it validates every request
against the rules current Claude models enforce:

* no ``temperature`` / ``top_p`` / ``top_k``;
* no forced ``tool_choice`` (``any`` / ``tool``);
* ``output_config.effort`` is always set;
* structured output uses ``output_config.format`` (never the deprecated
  ``output_format``);
* assistant turns are replayed with their thinking blocks intact (append-only
  history, required for preserved thinking).

Responses are schema-driven and just smart enough to walk the whole graph.
"""

from __future__ import annotations

import json
import re
import socket
import threading
import time
from typing import Any

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse


class MockAnthropic:
    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.violations: list[str] = []
        self.app = FastAPI()
        self.app.post("/v1/messages")(self._messages)
        self.port = _free_port()
        self._server: uvicorn.Server | None = None

    # ------------------------------------------------------------------ server
    def start(self) -> "MockAnthropic":
        config = uvicorn.Config(self.app, host="127.0.0.1", port=self.port, log_level="warning")
        self._server = uvicorn.Server(config)
        threading.Thread(target=self._server.run, daemon=True).start()
        for _ in range(100):
            if self._server.started:
                break
            time.sleep(0.05)
        return self

    def stop(self) -> None:
        if self._server:
            self._server.should_exit = True

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    # -------------------------------------------------------------- validation
    def _check(self, body: dict, headers: dict) -> None:
        def bad(msg: str) -> None:
            self.violations.append(msg)

        if body.get("model") != "claude-opus-5-5":
            bad(f"unexpected model {body.get('model')}")
        for p in ("temperature", "top_p", "top_k"):
            if p in body:
                bad(f"sampling parameter sent: {p}")
        tc = body.get("tool_choice")
        if isinstance(tc, dict) and tc.get("type") in ("any", "tool"):
            bad(f"forced tool_choice sent: {tc}")
        if "output_format" in body:
            bad("deprecated output_format sent")
        oc = body.get("output_config") or {}
        if not oc.get("effort"):
            bad("output_config.effort missing")
        th = body.get("thinking")
        if th and th.get("type") in ("disabled", "enabled"):
            bad(f"invalid thinking config for this model: {th}")
        if body.get("fallbacks") != "default":
            bad("server-side fallbacks not enabled")
        beta = headers.get("anthropic-beta", "")
        if "server-side-fallback-2026-07-01" not in beta:
            bad("fallback beta header missing")
        for m in body.get("messages", []):
            if m["role"] == "assistant" and isinstance(m["content"], list):
                if any(b.get("type") == "tool_use" for b in m["content"]) and \
                        not any(b.get("type") == "thinking" for b in m["content"]):
                    bad("assistant turn replayed without its thinking block")

    # --------------------------------------------------------------- responses
    async def _messages(self, request: Request):
        body = await request.json()
        self.requests.append(body)
        self._check(body, {k.lower(): v for k, v in request.headers.items()})
        fmt = (body.get("output_config") or {}).get("format")
        if body.get("stream"):
            return StreamingResponse(self._stream(body), media_type="text/event-stream")
        if fmt:
            payload = _structured(fmt["schema"], _all_text(body))
            return JSONResponse(_message([_thinking(), {"type": "text", "text": json.dumps(payload)}], "end_turn"))
        if body.get("tools"):
            return JSONResponse(self._tool_turn(body))
        return JSONResponse(_message([{"type": "text", "text": "ok"}], "end_turn"))

    def _tool_turn(self, body: dict) -> dict:
        tools = [t["name"] for t in body["tools"]]
        results = sum(1 for m in body["messages"] if m["role"] == "user" and isinstance(m["content"], list)
                      and any(b.get("type") == "tool_result" for b in m["content"]))
        target = re.search(r"Target component: ([\w-]+)", _all_text(body))
        target = target.group(1) if target else "checkout-service"
        if results >= min(2, len(tools)):
            return _message([_thinking(), {"type": "text", "text": f"Conclusion: {target} reviewed."}], "end_turn")
        name = tools[results]
        args: dict[str, Any] = {}
        props = next(t for t in body["tools"] if t["name"] == name)["input_schema"].get("properties", {})
        for key in ("component", "service"):
            if key in props:
                args[key] = target
        if "metric" in props:
            args["metric"] = "error_rate_pct"
        return _message([_thinking(), {"type": "text", "text": f"Checking {name} on {target}."},
                         {"type": "tool_use", "id": f"toolu_{len(self.requests)}", "name": name, "input": args}],
                        "tool_use")

    def _stream(self, body: dict):
        text = ("# Postmortem: mock incident\n\n**Severity** SEV2\n\n## Summary\nA mock summary.\n\n"
                "## Customer status update\nWe fixed it.\n")
        msg = _message([], None)
        yield _sse("message_start", {"type": "message_start", "message": {**msg, "content": []}})
        yield _sse("content_block_start", {"type": "content_block_start", "index": 0,
                                           "content_block": {"type": "text", "text": ""}})
        for piece in re.findall(r".{1,24}", text, flags=re.S):
            yield _sse("content_block_delta", {"type": "content_block_delta", "index": 0,
                                               "delta": {"type": "text_delta", "text": piece}})
        yield _sse("content_block_stop", {"type": "content_block_stop", "index": 0})
        yield _sse("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn",
                                                                         "stop_sequence": None},
                                     "usage": {"output_tokens": 50}})
        yield _sse("message_stop", {"type": "message_stop"})


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


def _thinking() -> dict:
    return {"type": "thinking", "thinking": "", "signature": "c2lnbmF0dXJl"}


def _message(content: list, stop: str | None) -> dict:
    return {"id": f"msg_{int(time.time() * 1e6)}", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
            "content": content, "stop_reason": stop, "stop_sequence": None,
            "usage": {"input_tokens": 100, "output_tokens": 40}}


def _all_text(body: dict) -> str:
    parts = []
    sys = body.get("system")
    if isinstance(sys, str):
        parts.append(sys)
    elif isinstance(sys, list):
        parts += [b.get("text", "") for b in sys]
    for m in body.get("messages", []):
        c = m["content"]
        if isinstance(c, str):
            parts.append(c)
        else:
            parts += [b.get("text", "") or json.dumps(b.get("content", "")) for b in c]
    return "\n".join(parts)


def _structured(schema: dict, text: str) -> Any:
    title = schema.get("title", "")
    svc = re.search(r'"service": "([\w-]+)"', text) or re.search(r"ALERT ([\w-]+)", text)
    svc = svc.group(1) if svc else "checkout-service"
    if title == "TriageDraft":
        return {"severity": "SEV2", "title": f"{svc} degraded", "summary": "Mock triage.", "customer_impact": "Some.",
                "suspected_area": svc,
                "initial_tasks": [{"analyst": a, "target": svc, "objective": f"{a} check", "window_minutes": 60,
                                   "focus_time": None} for a in ("metrics", "logs", "changes", "dependencies")]}
    if title == "HypothesisSet":
        return {"hypotheses": [{"id": "H1", "title": f"Deploy of {svc} caused a regression", "category": "bad_deploy",
                                "component": svc, "change_id": None, "confidence": 0.9,
                                "supporting": ["[changes] deploy right before onset"], "contradicting": [],
                                "reasoning": "Mock reasoning."}], "summary": "Mock synthesis."}
    if title == "Critique":
        return {"verdict": "sufficient", "reasoning": "Mock critic: enough evidence.", "gaps": [],
                "follow_up_tasks": []}
    if title == "PlanDraft":
        comp = re.search(r'"component": "([\w-]+)"', text)
        comp = comp.group(1) if comp else svc
        return {"summary": f"Roll back {comp}.", "actions": [{
            "action": "rollback_deploy",
            "params": {"service": comp, "to_version": None, "replicas": None, "datastore": None,
                       "max_connections": None, "flag": None, "enabled": None, "certificate": None,
                       "dependency": None},
            "rationale": "Deploy is the trigger.", "risk": "low", "expected_effect": "Recovery."}],
            "runbook_ids": ["RB-101"], "fallback": "Escalate.", "customer_message": "We are fixing it."}
    return _fill(schema, schema.get("$defs", {}))


def _fill(schema: dict, defs: dict) -> Any:
    if "$ref" in schema:
        return _fill(defs[schema["$ref"].split("/")[-1]], defs)
    if "anyOf" in schema:
        return _fill(schema["anyOf"][0], defs)
    if "enum" in schema:
        return schema["enum"][0]
    t = schema.get("type")
    if t == "object":
        return {k: _fill(v, defs) for k, v in schema.get("properties", {}).items()}
    if t == "array":
        return []
    if t == "string":
        return "mock"
    if t in ("number", "integer"):
        return 0.5 if t == "number" else 1
    if t == "boolean":
        return False
    return None


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
