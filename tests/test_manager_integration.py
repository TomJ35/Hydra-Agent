from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "src"))

from agent_manager.manager import (  # noqa: E402
    AgentSpec,
    ManagedAgent,
    build_agent_ws_url,
    parse_agent_specs_from_env,
    safe_container_suffix,
)


class FakeAgentWebSocket:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.closed = False

    async def send(self, payload: str) -> None:
        self.sent.append(payload)

    async def close(self) -> None:
        self.closed = True


class DockerProbeAgent(ManagedAgent):
    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.commands: list[list[str]] = []

    async def run_docker_command(self, args: list[str]):
        self.commands.append(args)
        return SimpleNamespace(returncode=0, stdout="container-id", stderr="")


def make_agent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **env) -> DockerProbeAgent:
    for key, value in env.items():
        monkeypatch.setenv(key, str(value))
    monkeypatch.setenv("AGENT_RUNTIME_ROOT", str(tmp_path))

    return DockerProbeAgent(
        hub_url="ws://hub.local/ws/agent",
        manager_id="manager-01",
        heartbeat_interval=10,
        reconnect_delay=1,
        spec=AgentSpec(
            agent_id="agent-control",
            server_id="render-01",
            llm="groq",
            api_name="GEMINI",
        ),
    )


def test_parse_agent_specs_prefers_json_config(monkeypatch):
    monkeypatch.setenv("SERVER_ID", "SERVER_ID=render-default")
    monkeypatch.setenv("LLM", "LLM=groq")
    monkeypatch.setenv("API_NAME", "API_NAME=GEMINI")
    monkeypatch.setenv(
        "AGENTS_CONFIG_JSON",
        json.dumps(
            [
                {"agent_id": "AGENT_ID=agent-01", "llm": "mistral"},
                {"agent_id": "agent-02", "server_id": "render-02"},
            ]
        ),
    )

    specs = parse_agent_specs_from_env("manager-01")

    assert specs == [
        AgentSpec(agent_id="agent-01", server_id="render-default", llm="mistral", api_name="GEMINI"),
        AgentSpec(agent_id="agent-02", server_id="render-02", llm="groq", api_name="GEMINI"),
    ]


def test_parse_agent_specs_rejects_duplicate_json_ids(monkeypatch):
    monkeypatch.setenv(
        "AGENTS_CONFIG_JSON",
        '[{"agent_id":"agent-01"},{"agent_id":"AGENT_ID=agent-01"}]',
    )

    with pytest.raises(ValueError, match="Duplicate agent_id"):
        parse_agent_specs_from_env("manager-01")


def test_parse_agent_specs_maps_csv_values(monkeypatch):
    monkeypatch.delenv("AGENTS_CONFIG_JSON", raising=False)
    monkeypatch.setenv("AGENT_IDS", "agent-01, agent-02, agent-01")
    monkeypatch.setenv("SERVER_IDS", "render-01,render-02")
    monkeypatch.setenv("LLM_LIST", "groq,mistral")
    monkeypatch.setenv("API_NAME_LIST", "GEMINI,OPENROUTER")

    specs = parse_agent_specs_from_env("manager-01")

    assert specs == [
        AgentSpec(agent_id="agent-01", server_id="render-01", llm="groq", api_name="GEMINI"),
        AgentSpec(agent_id="agent-02", server_id="render-02", llm="mistral", api_name="OPENROUTER"),
    ]


def test_build_agent_ws_url_handles_templates_and_agent_suffix():
    assert build_agent_ws_url("wss://hub.local/ws/agent/{agent_id}", "agent-01") == "wss://hub.local/ws/agent/agent-01"
    assert build_agent_ws_url("wss://hub.local/ws/agent", "agent-01") == "wss://hub.local/ws/agent/agent-01"
    assert build_agent_ws_url("wss://hub.local/ws/agent/agent-01", "agent-01") == "wss://hub.local/ws/agent/agent-01"


def test_create_runtime_agent_container_builds_docker_run_command(tmp_path, monkeypatch):
    mount_source = tmp_path / "workspace"
    mount_source.mkdir()
    (tmp_path / ".env").write_text("EXISTING=1\n", encoding="utf-8")
    agent = make_agent(
        tmp_path,
        monkeypatch,
        DOCKER_AGENT_MOUNT_SOURCE=mount_source,
        DOCKER_AGENT_MOUNT_TARGET="/workspace",
        DOCKER_AGENT_IMAGE="python:test",
        DOCKER_AGENT_COMMAND="python -m agent",
        DOCKER_AGENT_EXTRA_ARGS="--network hydra_net",
    )

    container_name = asyncio.run(
        agent.create_runtime_agent_container(
            AgentSpec(
                agent_id="Agent 01!/Prod",
                server_id="render-02",
                llm="mistral",
                api_name="OPENROUTER",
            )
        )
    )

    command = agent.commands[0]
    assert container_name == "hydra-agent-agent-01-prod"
    assert command[:5] == ["run", "-d", "--restart", "unless-stopped", "--name"]
    assert "hydra-agent-agent-01-prod" in command
    assert ["-v", f"{mount_source.resolve()}:/workspace"] == command[command.index("-v"): command.index("-v") + 2]
    assert ["--env-file", str((tmp_path / ".env").resolve())] == command[
        command.index("--env-file"): command.index("--env-file") + 2
    ]
    assert "-e" in command
    assert "AGENT_ID=Agent 01!/Prod" in command
    assert "AGENT_ROLE=agent" in command
    assert "SERVER_ID=render-02" in command
    assert "LLM=mistral" in command
    assert "API_NAME=OPENROUTER" in command
    assert "--network" in command
    assert command[-4:] == ["python:test", "sh", "-c", "python -m agent"]


def test_create_runtime_agent_container_requires_mount_strategy(tmp_path, monkeypatch):
    agent = make_agent(
        tmp_path,
        monkeypatch,
        DOCKER_AGENT_USE_VOLUMES_FROM_MANAGER="0",
        DOCKER_AGENT_MOUNT_SOURCE="",
    )

    with pytest.raises(RuntimeError, match="No mount strategy configured"):
        asyncio.run(
            agent.create_runtime_agent_container(
                AgentSpec(agent_id="agent-02", server_id="render-01", llm="groq", api_name="GEMINI")
            )
        )


def test_delete_runtime_agent_container_uses_sanitized_name(tmp_path, monkeypatch):
    agent = make_agent(tmp_path, monkeypatch, DOCKER_AGENT_MOUNT_SOURCE=tmp_path)

    container_name = asyncio.run(agent.delete_runtime_agent_container("Agent 02!/Prod"))

    assert container_name == "hydra-agent-agent-02-prod"
    assert agent.commands == [["rm", "-f", "hydra-agent-agent-02-prod"]]


def test_handle_create_and_delete_agent_messages_send_control_results(tmp_path, monkeypatch):
    agent = make_agent(tmp_path, monkeypatch, DOCKER_AGENT_MOUNT_SOURCE=tmp_path)
    websocket = FakeAgentWebSocket()
    agent.ws = websocket

    asyncio.run(
        agent.handle_create_agent(
            {
                "request_id": "create-1",
                "agent": {"agent_id": "agent-02", "server_id": "render-02", "llm": "mistral"},
            }
        )
    )

    create_result = json.loads(websocket.sent[-1])
    assert create_result["type"] == "create_agent_result"
    assert create_result["request_id"] == "create-1"
    assert create_result["ok"] is True
    assert create_result["agent_id"] == "agent-02"
    assert create_result["container_name"] == "hydra-agent-agent-02"

    asyncio.run(agent.handle_delete_agent({"request_id": "delete-self", "agent_id": "agent-control"}))
    delete_result = json.loads(websocket.sent[-1])

    assert delete_result["type"] == "delete_agent_result"
    assert delete_result["ok"] is False
    assert delete_result["error"] == "Refusing to delete the control websocket agent container"


def test_handle_assign_keeps_current_ticket_and_logs(tmp_path, monkeypatch):
    agent = make_agent(tmp_path, monkeypatch, DOCKER_AGENT_MOUNT_SOURCE=tmp_path)
    websocket = FakeAgentWebSocket()
    agent.ws = websocket

    asyncio.run(agent.handle_assign({"ticket": {"id": "TICK-A000", "title": "Ready task"}}))

    heartbeat = json.loads(websocket.sent[0])
    log = json.loads(websocket.sent[1])
    assert agent.current_ticket_id == "TICK-A000"
    assert heartbeat["type"] == "heartbeat"
    assert heartbeat["status"] == "working"
    assert heartbeat["current_ticket"] == "TICK-A000"
    assert log["type"] == "log"
    assert log["ticket_id"] == "TICK-A000"


def test_safe_container_suffix_keeps_docker_name_stable():
    assert safe_container_suffix("Agent 01!/Prod") == "agent-01-prod"
    assert safe_container_suffix("!!!") == "agent"
