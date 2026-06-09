import types
from pathlib import Path

import pytest

from hfp_mcp.gemini_orchestrator import (
    GeminiMCPOrchestrator,
    GeminiToolRequest,
    LocalActionExecutor,
    build_parser,
    merge_requests,
    parse_request,
)


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
    with pytest.raises(ValueError, match="request_id"):
        parse_request({"name": "ask_mcp_client", "arguments": {}})


def test_parse_request_defaults_non_dict_arguments_to_empty_dict():
    request = parse_request(
        {"request_id": "req-1", "name": "notify_mcp_client", "arguments": "bad"}
    )

    assert request.arguments == {}


def test_merge_requests_deduplicates_by_request_id_preserving_order():
    merged = merge_requests(
        [
            {"request_id": "req-1", "name": "ask_mcp_client", "arguments": {"task": "one"}},
            {"request_id": "req-2", "name": "ask_mcp_client", "arguments": {"task": "two"}},
        ],
        [
            {"request_id": "req-1", "name": "ask_mcp_client", "arguments": {"task": "dupe"}},
            {"request_id": "req-3", "name": "notify_mcp_client", "arguments": {"event": "three"}},
        ],
    )

    assert [item.request_id for item in merged] == ["req-1", "req-2", "req-3"]
    assert merged[0].arguments["task"] == "one"


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


def test_executor_creates_requested_tilde_path_inside_allowed_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    executor = LocalActionExecutor(allowed_dirs=[tmp_path], allow_file_create=True)
    request = GeminiToolRequest(
        request_id="req-1",
        name="ask_mcp_client",
        arguments={"task": "Create file ~/hey_this_is_working.txt"},
    )

    result = executor.execute(request)

    assert result.ok is True
    assert (tmp_path / "hey_this_is_working.txt").exists()


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
    assert "outside allowed" in result.message.lower()


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


class FakeGeminiClient:
    def __init__(self):
        self.submitted = []
        self.polled = [
            {
                "request_id": "req-1",
                "name": "ask_mcp_client",
                "arguments": {
                    "task": 'Create a file named "during_call.txt" in the user home folder.'
                },
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


def test_coerce_tool_result_accepts_structured_content():
    from hfp_mcp.gemini_orchestrator import _coerce_tool_result

    result = types.SimpleNamespace(structuredContent={"ok": True, "requests": []})

    assert _coerce_tool_result("tool", result) == {"ok": True, "requests": []}


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
