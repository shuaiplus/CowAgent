"""Regression coverage for the Codex provider's stdio and CowAgent routing."""

import json
import subprocess
import sys

from bridge.agent_bridge import AgentLLMModel
from config import conf
from models.codex import app_server
from models.codex.codex_bot import _tool_alias

# The subprocess emits the real protocol shapes without needing an account.
_FAKE_SERVER = r"""
import json
import sys

def send(message):
    print(json.dumps(message), flush=True)

history = []
for line in sys.stdin:
    request = json.loads(line)
    method, params = request.get("method"), request.get("params", {})
    if method == "initialized":
        continue
    if method == "initialize":
        assert params["capabilities"]["experimentalApi"]
        result = {}
    elif method == "account/read":
        result = {"account": {"type": sys.argv[1], "planType": "plus"}}
    elif method == "model/list":
        assert params["modelProvider"] == "openai"
        if not params["cursor"]:
            result = {"data": [{"model": "codex-test"}, {"model": "hidden", "hidden": True}],
                      "nextCursor": "page-2"}
        else:
            result = {"data": [{"model": "codex-test"}, {"model": "codex-other"}]}
    elif method == "thread/start":
        assert params["environments"] == [] and params["sandbox"] == "read-only"
        assert params["ephemeral"] and params["model"] == "codex-test"
        namespaces = params["dynamicTools"]
        if namespaces:
            assert namespaces[0]["name"] == "cowagent"
        tools = namespaces[0]["tools"] if namespaces else []
        result = {"thread": {"id": "thread"}, "model": "codex-test"}
    elif method == "thread/inject_items":
        history = params["items"]
        result = {}
    elif method == "turn/start":
        address = {"threadId": "thread", "turnId": "turn"}
        if tools and not history:
            send({"method": "item/tool/call", "id": "tool-request", "params": {
                **address, "tool": tools[0]["name"], "namespace": "cowagent", "callId": "upstream-call",
                "arguments": {"key": "value"},
            }})
        else:
            if tools:
                assert [item["type"] for item in history] == [
                    "message", "function_call", "function_call_output",
                ]
                assert history[1]["name"] == tools[0]["name"] and history[1]["namespace"] == "cowagent"
                assert history[1]["call_id"] == history[2]["call_id"]
                assert history[2]["output"] == "42"
            send({"method": "item/agentMessage/delta", "params": {
                **address, "itemId": "message", "delta": "The answer is ",
            }})
            send({"method": "item/agentMessage/delta", "params": {
                **address, "itemId": "message", "delta": "42.",
            }})
            send({"method": "item/completed", "params": {**address, "item": {
                "id": "message", "type": "agentMessage", "text": "The answer is 42.",
            }}})
            send({"method": "thread/tokenUsage/updated", "params": {**address, "tokenUsage": {
                "total": {"inputTokens": 4, "outputTokens": 2, "totalTokens": 6, "cachedInputTokens": 1},
            }}})
            send({"method": "turn/completed", "params": {"threadId": "thread", "turn": {
                "id": "turn", "status": "completed",
            }}})
        result = {"turn": {"id": "turn"}}
    else:
        raise AssertionError(request)
    send({"id": request["id"], "result": result})
"""


def _fake_server(monkeypatch, tmp_path, account_type="chatgpt"):
    real_popen = subprocess.Popen
    children = []
    monkeypatch.setattr(app_server, "codex_home", lambda: tmp_path)
    monkeypatch.setattr(app_server, "find_codex", lambda home: sys.executable)
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-enter-codex")
    monkeypatch.setenv("OPENAI_BASE_URL", "must-not-enter-codex")
    monkeypatch.setenv("CODEX_HOME", "must-not-use-personal-codex-home")

    def launch(args, **kwargs):
        assert "OPENAI_API_KEY" not in kwargs["env"]
        assert "OPENAI_BASE_URL" not in kwargs["env"]
        assert kwargs["env"]["CODEX_HOME"] == str(tmp_path.resolve())
        child = real_popen([sys.executable, "-u", "-c", _FAKE_SERVER, account_type], **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(app_server.subprocess, "Popen", launch)
    return children


def _routed_bot(monkeypatch):
    monkeypatch.setitem(conf(), "bot_type", "codex")
    monkeypatch.setitem(conf(), "model", "codex-test")
    monkeypatch.setitem(conf(), "use_linkai", False)
    return AgentLLMModel(None).bot


def test_codex_continues_with_cowagent_tool_results(monkeypatch, tmp_path):
    children = _fake_server(monkeypatch, tmp_path)
    bot = _routed_bot(monkeypatch)
    tools = [{"name": "lookup", "description": "Look up a value", "input_schema": {"type": "object"}}]
    messages = [{"role": "user", "content": [{"type": "text", "text": "What is the value?"}]}]

    first = bot.call_with_tools(messages, tools)
    call = first["choices"][0]["message"]["tool_calls"][0]
    assert first["choices"][0]["finish_reason"] == "tool_calls"
    assert call["function"]["name"] == "lookup"
    assert json.loads(call["function"]["arguments"]) == {"key": "value"}
    assert children[0].poll() is not None

    messages.extend([
        {"role": "assistant", "content": [{
            "type": "tool_use", "id": call["id"], "name": "lookup", "input": {"key": "value"},
        }]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": call["id"], "content": "42"}]},
    ])
    chunks = list(bot.call_with_tools(messages, tools, stream=True))
    assert [chunk["choices"][0]["delta"].get("content") for chunk in chunks[:-1]] == [
        "The answer is ", "42.",
    ]
    assert chunks[-1]["choices"][0]["finish_reason"] == "stop"
    assert chunks[-1]["usage"]["total_tokens"] == 6
    assert children[1].poll() is not None
    assert len(_tool_alias("mcp:tool/name" * 10)) <= 64


def test_codex_rejects_api_key_accounts(monkeypatch, tmp_path):
    children = _fake_server(monkeypatch, tmp_path, account_type="apiKey")
    result = _routed_bot(monkeypatch).call_with_tools([{"role": "user", "content": "Hi"}])
    assert result["error"] and result["status_code"] == 401
    assert "cow codex login" in result["message"]
    assert children[0].poll() is not None


def test_model_list_reaches_web_and_session_pickers(monkeypatch, tmp_path):
    _fake_server(monkeypatch, tmp_path)
    from channel.web.api.models import ModelsHandler
    from channel.web.api.sessions import _session_model_catalog

    (tmp_path / "auth.json").touch()
    assert app_server.refresh_models() == ["codex-test", "codex-other"]
    card = next(row for row in ModelsHandler._provider_overview() if row["id"] == "codex")
    assert card["configured"] and not card["api_key_masked"]
    entry = next(row for row in _session_model_catalog() if row["id"] == "codex")
    assert entry["models"] == ["codex-test", "codex-other"]
    (tmp_path / "auth.json").unlink()
    assert not any(row["id"] == "codex" for row in _session_model_catalog())
