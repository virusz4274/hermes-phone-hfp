"""Generic MCP client orchestrator for Gemini Live tool requests."""

from __future__ import annotations

import argparse
import asyncio
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


GENERIC_ASK_TOOLS = {"ask_mcp_client"}
GENERIC_NOTIFY_TOOLS = {"notify_mcp_client"}
GENERIC_CONTEXT_TOOLS = {"get_mcp_client_context"}
GENERIC_HANDOFF_TOOLS = {"handoff_to_mcp_client"}


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
            return f"MCP client action {status}: {self.message}\nData: {json.dumps(self.data, sort_keys=True)}"
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
    created_at = (
        float(created_at_raw)
        if isinstance(created_at_raw, int | float)
        else None
    )
    return GeminiToolRequest(
        request_id=request_id,
        name=name,
        arguments=dict(arguments),
        created_at=created_at,
    )


def merge_requests(
    polled: list[dict[str, Any]],
    pending: list[dict[str, Any]],
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


_CREATE_FILE_NAMED_RE = re.compile(
    r"create\s+(?:a\s+)?file\s+(?:named|called)\s+[\"']([^\"'\n]+)[\"']",
    re.IGNORECASE,
)
_CREATE_FILE_NAMED_UNQUOTED_RE = re.compile(
    r"create\s+(?:a\s+)?file\s+(?:named|called)\s+(.+?)(?:\s+in\s+|\s+under\s+|\s+inside\s+|[.!?]?$)",
    re.IGNORECASE,
)
_CREATE_FILE_PATH_RE = re.compile(
    r"create\s+(?:a\s+)?file\s+((?:~|/)[^\s\"',;]+|[A-Za-z0-9_.-]+\.[A-Za-z0-9_.-]+)",
    re.IGNORECASE,
)


class LocalActionExecutor:
    def __init__(
        self,
        *,
        allowed_dirs: list[Path],
        allow_file_create: bool = False,
    ) -> None:
        self.allowed_dirs = [
            Path(item).expanduser().resolve() for item in allowed_dirs
        ]
        self.allow_file_create = allow_file_create

    def execute(self, request: GeminiToolRequest) -> ActionResult:
        args = request.arguments
        name = request.name

        if name in GENERIC_NOTIFY_TOOLS:
            event = str(args.get("event") or "notification").strip()
            return ActionResult(True, f"Received notification: {event}")
        if name in GENERIC_CONTEXT_TOOLS:
            topic = str(args.get("topic") or "general").strip()
            return ActionResult(True, f"MCP client context is available for topic: {topic}")
        if name in GENERIC_HANDOFF_TOOLS:
            reason = str(args.get("reason") or "handoff requested").strip()
            return ActionResult(True, f"Received handoff request: {reason}")
        if name not in GENERIC_ASK_TOOLS:
            return ActionResult(False, f"Unsupported Gemini tool request: {name}")

        task = str(args.get("task") or "").strip()
        requested_path = self._requested_file_path(args, task)
        if requested_path is not None:
            content = args.get("content")
            return self._create_file(requested_path, content if isinstance(content, str) else None)
        return ActionResult(False, f"Unsupported MCP client task: {task or '(empty task)'}")

    def _requested_file_path(self, args: dict[str, Any], task: str) -> str | None:
        for key in ("path", "file_path", "filename", "file_name"):
            value = args.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()

        if "create" not in task.lower() or "file" not in task.lower():
            return None
        for pattern in (
            _CREATE_FILE_NAMED_RE,
            _CREATE_FILE_NAMED_UNQUOTED_RE,
            _CREATE_FILE_PATH_RE,
        ):
            match = pattern.search(task)
            if match:
                return match.group(1).strip().strip("\"'")
        return None

    def _create_file(self, requested_path: str, content: str | None) -> ActionResult:
        if not self.allow_file_create:
            return ActionResult(False, "File creation is not enabled for this orchestrator")
        if not self.allowed_dirs:
            return ActionResult(False, "No allowed directories configured for file creation")

        requested = requested_path.strip()
        if not requested or requested in {".", ".."}:
            return ActionResult(False, f"Invalid file path: {requested_path!r}")

        candidate = Path(requested).expanduser()
        if not candidate.is_absolute():
            candidate = self.allowed_dirs[0] / candidate
        target = candidate.resolve()
        if not self._is_allowed(target):
            return ActionResult(False, f"Refusing to write outside allowed directories: {target}")
        if target.is_dir():
            return ActionResult(False, f"Refusing to create a file over a directory: {target}")

        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            target.write_text(
                content if content is not None else "Created by hfp-mcp Gemini MCP orchestrator.\n",
                encoding="utf-8",
            )
        if not target.exists() or not target.is_file():
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


class GeminiMCPOrchestrator:
    def __init__(self, *, client: Any, executor: LocalActionExecutor) -> None:
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
            await self.client.submit_result(
                request.request_id,
                result.to_gemini_text(),
            )
            self.handled_request_ids.add(request.request_id)
            handled += 1
        return handled

    async def run_forever(
        self,
        *,
        poll_seconds: float = 2.0,
        sleep_seconds: float = 0.2,
    ) -> None:
        while True:
            await self.run_once(timeout_seconds=poll_seconds)
            status = await self.client.gemini_status()
            if not status.get("running"):
                return
            await asyncio.sleep(sleep_seconds)


class HfpMCPGeminiClient:
    def __init__(self, url: str) -> None:
        self.url = url
        self._stream_ctx = None
        self._session_ctx = None
        self._session = None

    async def __aenter__(self) -> "HfpMCPGeminiClient":
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        self._stream_ctx = streamablehttp_client(self.url)
        streams = await self._stream_ctx.__aenter__()
        self._session_ctx = ClientSession(streams[0], streams[1])
        self._session = await self._session_ctx.__aenter__()
        await self._session.initialize()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        try:
            if self._session_ctx is not None:
                await self._session_ctx.__aexit__(exc_type, exc, tb)
        finally:
            if self._stream_ctx is not None:
                await self._stream_ctx.__aexit__(exc_type, exc, tb)

    async def _call_tool(
        self,
        name: str,
        args: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if self._session is None:
            raise RuntimeError("MCP client is not connected")
        result = await self._session.call_tool(name, args or {})
        return _coerce_tool_result(name, result)

    async def poll_requests(self, timeout_seconds: float) -> list[dict[str, Any]]:
        result = await self._call_tool(
            "poll_gemini_live_requests",
            {"timeout_seconds": timeout_seconds},
        )
        return list(result.get("requests") or [])

    async def pending_requests(self) -> list[dict[str, Any]]:
        result = await self._call_tool("get_gemini_live_pending_requests")
        return list(result.get("requests") or [])

    async def submit_result(self, request_id: str, result: str) -> dict[str, Any]:
        return await self._call_tool(
            "submit_gemini_live_result",
            {"request_id": request_id, "result": result},
        )

    async def gemini_status(self) -> dict[str, Any]:
        return await self._call_tool("get_gemini_live_status")


def _coerce_tool_result(name: str, result: Any) -> dict[str, Any]:
    structured = (
        getattr(result, "structuredContent", None)
        or getattr(result, "structured_content", None)
    )
    if isinstance(structured, dict):
        return structured

    content = getattr(result, "content", None) or []
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
    executor = LocalActionExecutor(
        allowed_dirs=[Path(item) for item in args.allowed_dir],
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
