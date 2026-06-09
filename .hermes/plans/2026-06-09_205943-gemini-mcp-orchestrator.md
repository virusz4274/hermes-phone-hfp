# Generic Gemini MCP Orchestrator Implementation Plan

> **For Codex:** Implement this plan task-by-task using strict TDD. Do not work on the Hermes plugin/adapter path. Keep this feature generic to MCP clients and MCP servers.

**Goal:** Build a generic MCP-side orchestrator that polls Gemini Live tool requests from the HFP MCP server, executes allowed actions for real, verifies the result, and submits the actual result back to Gemini during the live call.

**Architecture:** Add a standalone Python module and CLI inside `hfp_mcp` that acts as a reference MCP client/orchestrator. It connects to the existing HFP MCP endpoint, polls `poll_gemini_live_requests` plus `get_gemini_live_pending_requests`, dispatches supported requests to safe handlers, and calls `submit_gemini_live_result` only after the action is really completed or explicitly rejected. The orchestrator must not depend on Hermes; Hermes can later reuse the same MCP request/result loop separately.

**Tech Stack:** Python 3.11+, existing `mcp>=1.27.0`, asyncio, stdlib `argparse`, stdlib `pathlib/json/re`, pytest + pytest-asyncio.

---

## Current Context

Relevant existing files:

- `src/hfp_mcp/gemini_live.py`
  - Gemini Live declares generic tool names:
    - `ask_mcp_client`
    - `notify_mcp_client`
    - `get_mcp_client_context`
    - `handoff_to_mcp_client`
  - Backward-compatible aliases remain:
    - `ask_hermes`
    - `notify_hermes`
    - `get_hermes_context`
    - `handoff_to_hermes`

- `src/hfp_mcp/server.py`
  - Existing MCP tools include:
    - `start_gemini_live_call`
    - `stop_gemini_live_call`
    - `get_gemini_live_status`
    - `send_gemini_live_text`
    - `poll_gemini_live_requests`
    - `get_gemini_live_pending_requests`
    - `submit_gemini_live_result`

- `tests/test_gemini_live.py`
  - Already tests that generic MCP tool declarations exist.
  - Already tests pending request recovery behavior.

Observed failure to fix:

- During a real call, Gemini requested:
  - create `~/hey_this_is_working.txt`
- The temporary polling loop received the request and submitted a canned success-like response.
- The file was not created during the call because no real action executor existed.

Acceptance criteria for this feature:

1. A generic non-Hermes CLI can run during a Gemini Live call and handle requests.
2. If Gemini asks to create a file in an allowed directory, the file is actually created before success is submitted.
3. If a request cannot be executed safely or is unsupported, the submitted result clearly says it failed/was unsupported.
4. The orchestrator never pretends success for actions it did not perform.
5. The orchestrator can recover requests that were already polled but not answered by reading `get_gemini_live_pending_requests`.
6. The implementation is covered by unit tests with no real phone call required.

Out of scope for this plan:

- Hermes platform/plugin integration.
- Full natural-language automation for every possible computer task.
- Unrestricted shell execution.
- Dialing real numbers in tests.

---

## High-Level Design

Create these new files:

- `src/hfp_mcp/gemini_orchestrator.py`
- `tests/test_gemini_orchestrator.py`

Modify:

- `pyproject.toml`
  - Add console script:
    - `hfp-mcp-gemini-orchestrator = "hfp_mcp.gemini_orchestrator:main"`

Core types/functions to add:

```python
@dataclass(frozen=True)
class GeminiToolRequest:
    request_id: str
    name: str
    arguments: dict[str, Any]
    created_at: float | None = None

@dataclass(frozen=True)
class ActionResult:
    ok: bool
    message: str
    data: dict[str, Any] = field(default_factory=dict)
```

Core components:

- `parse_request(raw: dict) -> GeminiToolRequest`
- `merge_requests(polled: list[dict], pending: list[dict]) -> list[GeminiToolRequest]`
- `LocalActionExecutor`
  - Executes safe built-in actions.
  - Initially supports:
    - status/context requests
    - notifications/handoff acknowledgement
    - safe file creation in allowed directories
  - Does not use Hermes.
- `GeminiMCPOrchestrator`
  - Connects to HFP MCP through `mcp.client.streamable_http.streamablehttp_client`.
  - Polls `poll_gemini_live_requests`.
  - Reads `get_gemini_live_pending_requests` each loop.
  - Deduplicates request IDs.
  - Dispatches to executor.
  - Submits exact result text to `submit_gemini_live_result`.

Recommended CLI examples:

```bash
# Run until Gemini stops or Ctrl+C
hfp-mcp-gemini-orchestrator \
  --url http://127.0.0.1:8000/mcp \
  --allowed-dir /home/virusz4274 \
  --allow-file-create

# Run one iteration for testing/debugging
hfp-mcp-gemini-orchestrator \
  --url http://127.0.0.1:8000/mcp \
  --allowed-dir /home/virusz4274 \
  --allow-file-create \
  --once

# Safe mode: can answer status/context but cannot write files
hfp-mcp-gemini-orchestrator --url http://127.0.0.1:8000/mcp --once
```

---

## Task 1: Add request/result dataclasses and request normalization tests

**Objective:** Create a small, testable core model for Gemini MCP requests without connecting to a real MCP server.

**Files:**

- Create: `src/hfp_mcp/gemini_orchestrator.py`
- Create/modify: `tests/test_gemini_orchestrator.py`

**Step 1: Write failing tests**

Add `tests/test_gemini_orchestrator.py`:

```python
from hfp_mcp.gemini_orchestrator import GeminiToolRequest, parse_request


def test_parse_request_normalizes_valid_gemini_request():
    request = parse_request(
        {
            "request_id": "req-1",
            "name": "ask_mcp_client",
            "arguments": {"task": "Create a file", "urgency": "medium"},
            "created_at": 123.0,
        }
    )

    assert request == GeminiToolRequest(
        request_id="req-1",
        name="ask_mcp_client",
        arguments={"task": "Create a file", "urgency": "medium"},
        created_at=123.0,
    )


def test_parse_request_rejects_missing_request_id():
    try:
        parse_request({"name": "ask_mcp_client", "arguments": {}})
    except ValueError as exc:
        assert "request_id" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_parse_request_defaults_non_dict_arguments_to_empty_dict():
    request = parse_request(
        {"request_id": "req-1", "name": "notify_mcp_client", "arguments": "bad"}
    )

    assert request.arguments == {}
```

**Step 2: Verify RED**

Run:

```bash
cd /home/virusz4274/phone-bluetooth-hfp-mcp
.venv/bin/python -m pytest tests/test_gemini_orchestrator.py -q
```

Expected: FAIL because `hfp_mcp.gemini_orchestrator` does not exist.

**Step 3: Minimal implementation**

Create `src/hfp_mcp/gemini_orchestrator.py`:

```python
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class GeminiToolRequest:
    request_id: str
    name: str
    arguments: dict[str, Any]
    created_at: float | None = None


@dataclass(frozen=True)
class ActionResult:
    ok: bool
    message: str
    data: dict[str, Any] = field(default_factory=dict)

    def to_gemini_text(self) -> str:
        status = "succeeded" if self.ok else "failed"
        if self.data:
            return f"MCP client action {status}: {self.message}\nData: {self.data}"
        return f"MCP client action {status}: {self.message}"


def parse_request(raw: dict[str, Any]) -> GeminiToolRequest:
    request_id = str(raw.get("request_id") or "").strip()
    if not request_id:
        raise ValueError("Gemini request is missing request_id")
    name = str(raw.get("name") or "").strip() or "unknown"
    arguments = raw.get("arguments")
    if not isinstance(arguments, dict):
        arguments = {}
    created_at_raw = raw.get("created_at")
    created_at = float(created_at_raw) if isinstance(created_at_raw, int | float) else None
    return GeminiToolRequest(
        request_id=request_id,
        name=name,
        arguments=arguments,
        created_at=created_at,
    )
```

**Step 4: Verify GREEN**

Run:

```bash
.venv/bin/python -m pytest tests/test_gemini_orchestrator.py -q
```

Expected: PASS.

---

## Task 2: Add request merge/deduplication behavior

**Objective:** Ensure the orchestrator handles both newly polled and already-pending Gemini requests without double-submitting results.

**Files:**

- Modify: `src/hfp_mcp/gemini_orchestrator.py`
- Modify: `tests/test_gemini_orchestrator.py`

**Step 1: Write failing tests**

Append:

```python
from hfp_mcp.gemini_orchestrator import merge_requests


def test_merge_requests_deduplicates_by_request_id_preserving_order():
    merged = merge_requests(
        [
            {"request_id": "req-1", "name": "ask_mcp_client", "arguments": {"task": "one"}},
            {"request_id": "req-2", "name": "ask_mcp_client", "arguments": {"task": "two"}},
        ],
        [
            {"request_id": "req-1", "name": "ask_mcp_client", "arguments": {"task": "one duplicate"}},
            {"request_id": "req-3", "name": "notify_mcp_client", "arguments": {"event": "three"}},
        ],
    )

    assert [item.request_id for item in merged] == ["req-1", "req-2", "req-3"]
    assert merged[0].arguments["task"] == "one"
```

**Step 2: Verify RED**

Run:

```bash
.venv/bin/python -m pytest tests/test_gemini_orchestrator.py::test_merge_requests_deduplicates_by_request_id_preserving_order -q
```

Expected: FAIL because `merge_requests` does not exist.

**Step 3: Implement**

Add:

```python
def merge_requests(
    polled: list[dict[str, Any]], pending: list[dict[str, Any]]
) -> list[GeminiToolRequest]:
    seen: set[str] = set()
    merged: list[GeminiToolRequest] = []
    for raw in [*polled, *pending]:
        request = parse_request(raw)
        if request.request_id in seen:
            continue
        seen.add(request.request_id)
        merged.append(request)
    return merged
```

**Step 4: Verify GREEN**

Run:

```bash
.venv/bin/python -m pytest tests/test_gemini_orchestrator.py -q
```

Expected: PASS.

---

## Task 3: Add safe local file-create executor

**Objective:** Implement the exact behavior that failed in the real call: when Gemini asks to create a simple file inside an allowed directory, actually create it and verify it exists before reporting success.

**Files:**

- Modify: `src/hfp_mcp/gemini_orchestrator.py`
- Modify: `tests/test_gemini_orchestrator.py`

**Security requirements:**

- File writes are disabled unless `allow_file_create=True`.
- File writes must stay inside one of the configured `allowed_dirs`.
- Reject absolute paths outside allowed dirs.
- Reject path traversal such as `../secret.txt`.
- Do not execute shell commands.
- If the task is unsupported, return `ok=False` with an honest message.

**Step 1: Write failing tests**

Append:

```python
from pathlib import Path

from hfp_mcp.gemini_orchestrator import LocalActionExecutor


def test_executor_creates_requested_file_in_allowed_home_folder(tmp_path):
    executor = LocalActionExecutor(allowed_dirs=[tmp_path], allow_file_create=True)
    request = GeminiToolRequest(
        request_id="req-1",
        name="ask_mcp_client",
        arguments={
            "task": 'Create a file named "hey_this_is_working.txt" in the user home folder.',
            "context": "test request",
        },
    )

    result = executor.execute(request)

    created = tmp_path / "hey_this_is_working.txt"
    assert result.ok is True
    assert created.exists()
    assert "hey_this_is_working.txt" in result.message
    assert result.data["path"] == str(created)


def test_executor_refuses_file_create_when_not_enabled(tmp_path):
    executor = LocalActionExecutor(allowed_dirs=[tmp_path], allow_file_create=False)
    request = GeminiToolRequest(
        request_id="req-1",
        name="ask_mcp_client",
        arguments={"task": 'Create a file named "blocked.txt" in the user home folder.'},
    )

    result = executor.execute(request)

    assert result.ok is False
    assert not (tmp_path / "blocked.txt").exists()
    assert "not enabled" in result.message.lower()


def test_executor_rejects_path_traversal(tmp_path):
    executor = LocalActionExecutor(allowed_dirs=[tmp_path], allow_file_create=True)
    request = GeminiToolRequest(
        request_id="req-1",
        name="ask_mcp_client",
        arguments={"task": 'Create a file named "../escape.txt" in the user home folder.'},
    )

    result = executor.execute(request)

    assert result.ok is False
    assert not (tmp_path.parent / "escape.txt").exists()
    assert "outside allowed" in result.message.lower() or "invalid" in result.message.lower()


def test_executor_reports_unsupported_task_without_claiming_success(tmp_path):
    executor = LocalActionExecutor(allowed_dirs=[tmp_path], allow_file_create=True)
    request = GeminiToolRequest(
        request_id="req-1",
        name="ask_mcp_client",
        arguments={"task": "Book a flight to Tokyo."},
    )

    result = executor.execute(request)

    assert result.ok is False
    assert "unsupported" in result.message.lower()
```

**Step 2: Verify RED**

Run:

```bash
.venv/bin/python -m pytest tests/test_gemini_orchestrator.py -q
```

Expected: FAIL because `LocalActionExecutor` does not exist.

**Step 3: Implement minimal safe executor**

Add to `src/hfp_mcp/gemini_orchestrator.py`:

```python
import re
from pathlib import Path


_CREATE_FILE_RE = re.compile(
    r"create\s+(?:a\s+)?file\s+(?:named|called)?\s*[\"']?([^\"'\n]+?)[\"']?(?:\s+in\s+|\s*$)",
    re.IGNORECASE,
)


class LocalActionExecutor:
    def __init__(self, allowed_dirs: list[Path], allow_file_create: bool = False):
        self.allowed_dirs = [Path(item).expanduser().resolve() for item in allowed_dirs]
        self.allow_file_create = allow_file_create

    def execute(self, request: GeminiToolRequest) -> ActionResult:
        name = request.name
        args = request.arguments
        if name in {"notify_mcp_client", "notify_hermes"}:
            event = str(args.get("event") or "notification")
            return ActionResult(True, f"Received notification: {event}")
        if name in {"handoff_to_mcp_client", "handoff_to_hermes"}:
            reason = str(args.get("reason") or "handoff requested")
            return ActionResult(True, f"Received handoff request: {reason}")
        if name in {"get_mcp_client_context", "get_hermes_context"}:
            topic = str(args.get("topic") or "general")
            return ActionResult(True, f"MCP client context is available for topic: {topic}")
        if name not in {"ask_mcp_client", "ask_hermes"}:
            return ActionResult(False, f"Unsupported Gemini tool request: {name}")

        task = str(args.get("task") or "")
        create_match = _CREATE_FILE_RE.search(task)
        if create_match:
            return self._create_file(create_match.group(1).strip())
        return ActionResult(False, f"Unsupported MCP client task: {task or '(empty task)'}")

    def _create_file(self, filename: str) -> ActionResult:
        if not self.allow_file_create:
            return ActionResult(False, "File creation is not enabled for this orchestrator")
        if not self.allowed_dirs:
            return ActionResult(False, "No allowed directories configured for file creation")
        if not filename or filename in {".", ".."}:
            return ActionResult(False, f"Invalid filename: {filename!r}")
        relative = Path(filename)
        if relative.is_absolute() or ".." in relative.parts:
            return ActionResult(False, f"Invalid or outside allowed directory filename: {filename}")

        base = self.allowed_dirs[0]
        target = (base / relative).resolve()
        if not self._is_allowed(target):
            return ActionResult(False, f"Refusing to write outside allowed directories: {target}")

        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            target.write_text(
                "Created by hfp-mcp Gemini MCP orchestrator.\n",
                encoding="utf-8",
            )
        if not target.exists():
            return ActionResult(False, f"File creation did not complete: {target}")
        return ActionResult(True, f"Created file: {target}", {"path": str(target)})

    def _is_allowed(self, target: Path) -> bool:
        for allowed in self.allowed_dirs:
            try:
                target.relative_to(allowed)
                return True
            except ValueError:
                continue
        return False
```

**Step 4: Verify GREEN**

Run:

```bash
.venv/bin/python -m pytest tests/test_gemini_orchestrator.py -q
```

Expected: PASS.

**Implementation note:** The regex only needs to support simple file-create requests for now. Do not build a broad natural-language command parser. Unsupported requests must fail honestly.

---

## Task 4: Add a testable MCP client abstraction

**Objective:** Make the orchestrator loop testable without running a real MCP server.

**Files:**

- Modify: `src/hfp_mcp/gemini_orchestrator.py`
- Modify: `tests/test_gemini_orchestrator.py`

**Step 1: Write failing tests with a fake client**

Append:

```python
import pytest

from hfp_mcp.gemini_orchestrator import GeminiMCPOrchestrator


class FakeGeminiClient:
    def __init__(self):
        self.submitted = []
        self.polled = [
            {
                "request_id": "req-1",
                "name": "ask_mcp_client",
                "arguments": {"task": 'Create a file named "during_call.txt" in the user home folder.'},
            }
        ]
        self.pending = []

    async def poll_requests(self, timeout_seconds: float):
        requests, self.polled = self.polled, []
        return requests

    async def pending_requests(self):
        return self.pending

    async def submit_result(self, request_id: str, result: str):
        self.submitted.append((request_id, result))
        return {"ok": True}

    async def gemini_status(self):
        return {"running": True}


@pytest.mark.asyncio
async def test_orchestrator_handles_one_request_and_submits_real_result(tmp_path):
    client = FakeGeminiClient()
    executor = LocalActionExecutor(allowed_dirs=[tmp_path], allow_file_create=True)
    orchestrator = GeminiMCPOrchestrator(client=client, executor=executor)

    handled = await orchestrator.run_once()

    assert handled == 1
    assert (tmp_path / "during_call.txt").exists()
    assert client.submitted
    request_id, result_text = client.submitted[0]
    assert request_id == "req-1"
    assert "succeeded" in result_text
    assert "during_call.txt" in result_text


@pytest.mark.asyncio
async def test_orchestrator_does_not_resubmit_handled_request(tmp_path):
    client = FakeGeminiClient()
    client.polled = []
    client.pending = [
        {
            "request_id": "req-1",
            "name": "ask_mcp_client",
            "arguments": {"task": 'Create a file named "once.txt" in the user home folder.'},
        }
    ]
    executor = LocalActionExecutor(allowed_dirs=[tmp_path], allow_file_create=True)
    orchestrator = GeminiMCPOrchestrator(client=client, executor=executor)

    assert await orchestrator.run_once() == 1
    assert await orchestrator.run_once() == 0
    assert len(client.submitted) == 1
```

**Step 2: Verify RED**

Run:

```bash
.venv/bin/python -m pytest tests/test_gemini_orchestrator.py::test_orchestrator_handles_one_request_and_submits_real_result -q
```

Expected: FAIL because `GeminiMCPOrchestrator` does not exist.

**Step 3: Implement orchestrator class against a protocol-like client**

Add:

```python
class GeminiMCPOrchestrator:
    def __init__(self, client: Any, executor: LocalActionExecutor):
        self.client = client
        self.executor = executor
        self.handled_request_ids: set[str] = set()

    async def run_once(self, timeout_seconds: float = 2.0) -> int:
        polled = await self.client.poll_requests(timeout_seconds=timeout_seconds)
        pending = await self.client.pending_requests()
        requests = merge_requests(polled, pending)
        handled = 0
        for request in requests:
            if request.request_id in self.handled_request_ids:
                continue
            result = self.executor.execute(request)
            await self.client.submit_result(request.request_id, result.to_gemini_text())
            self.handled_request_ids.add(request.request_id)
            handled += 1
        return handled

    async def run_forever(self, poll_seconds: float = 2.0, sleep_seconds: float = 0.2) -> None:
        import asyncio

        while True:
            await self.run_once(timeout_seconds=poll_seconds)
            status = await self.client.gemini_status()
            if not status.get("running"):
                return
            await asyncio.sleep(sleep_seconds)
```

**Step 4: Verify GREEN**

Run:

```bash
.venv/bin/python -m pytest tests/test_gemini_orchestrator.py -q
```

Expected: PASS.

---

## Task 5: Implement real MCP HTTP client wrapper

**Objective:** Connect the orchestrator to the real HFP MCP server endpoint.

**Files:**

- Modify: `src/hfp_mcp/gemini_orchestrator.py`
- Modify: `tests/test_gemini_orchestrator.py` if helpful, but keep network tests mocked/fake.

**Step 1: Add a wrapper class**

Implement an async context manager:

```python
class HfpMCPGeminiClient:
    def __init__(self, url: str):
        self.url = url
        self._stream_ctx = None
        self._session_ctx = None
        self._session = None

    async def __aenter__(self):
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        self._stream_ctx = streamablehttp_client(self.url)
        streams = await self._stream_ctx.__aenter__()
        self._session_ctx = ClientSession(streams[0], streams[1])
        self._session = await self._session_ctx.__aenter__()
        await self._session.initialize()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if self._session_ctx is not None:
            await self._session_ctx.__aexit__(exc_type, exc, tb)
        if self._stream_ctx is not None:
            await self._stream_ctx.__aexit__(exc_type, exc, tb)

    async def _call_tool(self, name: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
        if self._session is None:
            raise RuntimeError("MCP client is not connected")
        result = await self._session.call_tool(name, args or {})
        structured = getattr(result, "structuredContent", None) or getattr(result, "structured_content", None)
        if isinstance(structured, dict):
            return structured
        content = getattr(result, "content", []) or []
        for item in content:
            text = getattr(item, "text", None)
            if not text:
                continue
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                return {"ok": True, "text": text}
            if isinstance(parsed, dict):
                return parsed
        return {"ok": False, "error": f"Tool {name} returned no structured content"}

    async def poll_requests(self, timeout_seconds: float):
        result = await self._call_tool("poll_gemini_live_requests", {"timeout_seconds": timeout_seconds})
        return list(result.get("requests") or [])

    async def pending_requests(self):
        result = await self._call_tool("get_gemini_live_pending_requests")
        return list(result.get("requests") or [])

    async def submit_result(self, request_id: str, result: str):
        return await self._call_tool(
            "submit_gemini_live_result",
            {"request_id": request_id, "result": result},
        )

    async def gemini_status(self):
        return await self._call_tool("get_gemini_live_status")
```

**Step 2: Add unit tests for `_call_tool` parsing if simple**

If Codex can easily mock a result object, add tests that structured content and text JSON content parse correctly. If mocking becomes noisy, keep this wrapper thin and validate by integration command in Task 7.

**Step 3: Run tests**

```bash
.venv/bin/python -m pytest tests/test_gemini_orchestrator.py -q
```

Expected: PASS.

---

## Task 6: Add CLI entrypoint

**Objective:** Provide a generic command that users can run during calls without Hermes.

**Files:**

- Modify: `src/hfp_mcp/gemini_orchestrator.py`
- Modify: `pyproject.toml`
- Modify: `tests/test_gemini_orchestrator.py`

**Step 1: Write failing CLI parser tests**

Append:

```python
from hfp_mcp.gemini_orchestrator import build_parser


def test_cli_parser_accepts_allowed_dir_and_file_create_flag(tmp_path):
    parser = build_parser()
    args = parser.parse_args(
        [
            "--url",
            "http://127.0.0.1:8000/mcp",
            "--allowed-dir",
            str(tmp_path),
            "--allow-file-create",
            "--once",
        ]
    )

    assert args.url == "http://127.0.0.1:8000/mcp"
    assert args.allowed_dir == [str(tmp_path)]
    assert args.allow_file_create is True
    assert args.once is True
```

**Step 2: Verify RED**

Run:

```bash
.venv/bin/python -m pytest tests/test_gemini_orchestrator.py::test_cli_parser_accepts_allowed_dir_and_file_create_flag -q
```

Expected: FAIL because `build_parser` does not exist.

**Step 3: Implement parser and async main**

Add:

```python
import argparse
import asyncio


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generic MCP client orchestrator for Gemini Live HFP calls"
    )
    parser.add_argument("--url", default="http://127.0.0.1:8000/mcp")
    parser.add_argument("--allowed-dir", action="append", default=[])
    parser.add_argument("--allow-file-create", action="store_true")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--poll-seconds", type=float, default=2.0)
    parser.add_argument("--sleep-seconds", type=float, default=0.2)
    return parser


async def async_main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    allowed_dirs = [Path(item) for item in args.allowed_dir]
    executor = LocalActionExecutor(
        allowed_dirs=allowed_dirs,
        allow_file_create=bool(args.allow_file_create),
    )
    async with HfpMCPGeminiClient(args.url) as client:
        orchestrator = GeminiMCPOrchestrator(client=client, executor=executor)
        if args.once:
            handled = await orchestrator.run_once(timeout_seconds=args.poll_seconds)
            print(f"Handled {handled} Gemini MCP request(s)")
            return 0
        await orchestrator.run_forever(
            poll_seconds=args.poll_seconds,
            sleep_seconds=args.sleep_seconds,
        )
    return 0


def main(argv: list[str] | None = None) -> None:
    raise SystemExit(asyncio.run(async_main(argv)))
```

**Step 4: Modify `pyproject.toml`**

Change:

```toml
[project.scripts]
hfp-mcp-server = "hfp_mcp.server:main"
```

To:

```toml
[project.scripts]
hfp-mcp-server = "hfp_mcp.server:main"
hfp-mcp-gemini-orchestrator = "hfp_mcp.gemini_orchestrator:main"
```

**Step 5: Verify GREEN**

Run:

```bash
.venv/bin/python -m pytest tests/test_gemini_orchestrator.py -q
```

Expected: PASS.

---

## Task 7: Add documentation and operational examples

**Objective:** Make it clear to non-Hermes MCP users how to use the orchestrator.

**Files:**

- Modify: `README.md`
- Optional create: `docs/gemini-mcp-orchestrator.md` if README is already large.

**Step 1: Add docs section**

Add a section titled:

```markdown
## Gemini Live generic MCP orchestrator
```

Include:

- What problem it solves:
  - Gemini Live can call `ask_mcp_client`, but an MCP client must execute the request.
- How to run:

```bash
hfp-mcp-gemini-orchestrator \
  --url http://127.0.0.1:8000/mcp \
  --allowed-dir "$HOME" \
  --allow-file-create
```

- Safety note:
  - File creation is disabled unless `--allow-file-create` is passed.
  - Writes are limited to `--allowed-dir`.
  - Unsupported requests return failure instead of fake success.

- Test-call workflow:

```bash
# Terminal 1: server
hfp-mcp-server --transport streamable-http --host 127.0.0.1 --port 8000

# Terminal 2: start/answer call + Gemini Live using any MCP client
# Terminal 3: orchestrator
hfp-mcp-gemini-orchestrator --url http://127.0.0.1:8000/mcp --allowed-dir "$HOME" --allow-file-create
```

- Example Gemini prompt during call:

```text
Create a file named hey_this_is_working.txt in my home folder.
```

- Expected outcome:
  - File exists during the call before Gemini reports success.

**Step 2: No code verification needed beyond tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_gemini_orchestrator.py tests/test_gemini_live.py -q
```

Expected: PASS.

---

## Task 8: Run full targeted test suite

**Objective:** Ensure new orchestrator does not break existing Gemini Live or HFP plugin tests.

Run:

```bash
cd /home/virusz4274/phone-bluetooth-hfp-mcp
.venv/bin/python -m pytest tests/test_gemini_orchestrator.py tests/test_gemini_live.py tests/test_hfp_phone_plugin.py -q
```

Expected:

- All tests pass.
- Existing `tests/test_gemini_live.py` and `tests/test_hfp_phone_plugin.py` continue passing.

Then run broader tests if time allows:

```bash
.venv/bin/python -m pytest -q
```

Expected:

- Entire test suite passes.

---

## Task 9: Manual verification without placing a real call

**Objective:** Verify the CLI can connect to the running MCP server and safely run one polling pass.

Prerequisites:

- HFP MCP service running at `http://127.0.0.1:8000/mcp`.
- No real phone call required for this check.

Run:

```bash
cd /home/virusz4274/phone-bluetooth-hfp-mcp
.venv/bin/hfp-mcp-gemini-orchestrator \
  --url http://127.0.0.1:8000/mcp \
  --allowed-dir /home/virusz4274 \
  --allow-file-create \
  --once
```

Expected when no Gemini request is pending:

```text
Handled 0 Gemini MCP request(s)
```

If there is a pending request, expected:

- It handles exactly the pending count.
- It submits results.
- It does not crash.

---

## Task 10: Manual verification with real call, only after user explicitly requests dialing

**Objective:** Confirm the original failure is fixed during a live call.

Do not dial automatically in tests. Real calls require explicit user direction.

When user explicitly asks for the real test:

1. Start or answer a call using the existing MCP workflow.
2. Start Gemini Live.
3. Start orchestrator in another terminal:

```bash
cd /home/virusz4274/phone-bluetooth-hfp-mcp
.venv/bin/hfp-mcp-gemini-orchestrator \
  --url http://127.0.0.1:8000/mcp \
  --allowed-dir /home/virusz4274 \
  --allow-file-create
```

4. Ask Gemini during the call:

```text
Create a file named hey_this_is_working.txt in my home folder.
```

5. While call is still active, verify from shell:

```bash
test -f /home/virusz4274/hey_this_is_working.txt && echo CREATED
```

Expected:

```text
CREATED
```

6. Gemini should report success only after the orchestrator actually created and verified the file.

---

## Risks and Tradeoffs

1. Natural-language parsing is intentionally limited.
   - This plan supports simple file-create requests.
   - Unsupported tasks must return honest failure, not fake success.

2. File writes are dangerous if unrestricted.
   - Keep `--allow-file-create` opt-in.
   - Keep writes constrained to `--allowed-dir`.
   - Reject absolute paths and `..` traversal.

3. The orchestrator is a reference implementation, not a full agent.
   - It proves the generic MCP request/result loop and can safely execute a small set of actions.
   - Later, additional handlers can be added for other MCP servers or tools.

4. Do not add Hermes-specific imports or assumptions.
   - This feature should work for any MCP-compatible client/application.

---

## Future Extension: Downstream MCP Tool Routing

After the basic safe orchestrator works, add configurable downstream MCP routing. This should be a separate PR/task.

Possible config shape:

```json
{
  "downstream_servers": {
    "filesystem": {
      "url": "http://127.0.0.1:8010/mcp"
    }
  },
  "routes": [
    {
      "match_tool": "ask_mcp_client",
      "match_keywords": ["file", "folder", "create"],
      "server": "filesystem",
      "tool": "create_file"
    }
  ]
}
```

This would let the HFP MCP app remain generic while allowing other MCP servers to do the actual work.

Do not build this extension until the local safe executor is tested and working.

---

## Final Verification Checklist for Codex

Before finishing, Codex should report:

- [ ] `tests/test_gemini_orchestrator.py` added.
- [ ] `src/hfp_mcp/gemini_orchestrator.py` added.
- [ ] `pyproject.toml` console script added.
- [ ] README/docs updated.
- [ ] File creation handler creates a real file only inside allowed dirs.
- [ ] Unsupported tasks return `ok=False` and are submitted to Gemini as failure/unsupported.
- [ ] No Hermes plugin code modified.
- [ ] Targeted tests pass:

```bash
.venv/bin/python -m pytest tests/test_gemini_orchestrator.py tests/test_gemini_live.py tests/test_hfp_phone_plugin.py -q
```

- [ ] Optional full tests pass:

```bash
.venv/bin/python -m pytest -q
```
