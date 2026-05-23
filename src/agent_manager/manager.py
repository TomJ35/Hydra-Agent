import asyncio
import json
import os
import re
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlparse, urlunparse

import websockets


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def sanitize_prefixed_value(raw: str, key: str) -> str:
    value = (raw or "").strip()
    prefix = f"{key}="
    while value.startswith(prefix):
        value = value[len(prefix) :].strip()
    return value


def sanitize_hub_url(raw: str) -> str:
    return sanitize_prefixed_value(raw, "HUB_URL")


def split_csv_values(raw: str) -> list[str]:
    return [part.strip() for part in (raw or "").split(",") if part.strip()]


def dedupe_preserve_order(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


def build_agent_ws_url(base_url: str, agent_id: str) -> str:
    url = sanitize_hub_url(base_url)
    if "{agent_id}" in url:
        return url.replace("{agent_id}", agent_id)

    parsed = urlparse(url)
    path = parsed.path.rstrip("/")
    if path.endswith("/ws/agent"):
        new_path = f"{path}/{agent_id}"
    elif path.endswith(f"/ws/agent/{agent_id}"):
        new_path = path
    else:
        new_path = path

    return urlunparse(parsed._replace(path=new_path))


@dataclass(frozen=True)
class AgentSpec:
    agent_id: str
    server_id: str
    llm: str
    api_name: str


def normalize_agent_id(raw: Any) -> str:
    return sanitize_prefixed_value(str(raw or ""), "AGENT_ID")


def normalize_server_id(raw: Any, fallback: str) -> str:
    return sanitize_prefixed_value(str(raw or ""), "SERVER_ID") or fallback


def normalize_llm(raw: Any, fallback: str) -> str:
    return sanitize_prefixed_value(str(raw or ""), "LLM") or fallback


def normalize_api_name(raw: Any, fallback: str) -> str:
    return sanitize_prefixed_value(str(raw or ""), "API_NAME") or fallback


def to_agent_spec(
    payload: dict[str, Any],
    *,
    fallback_server_id: str,
    fallback_llm: str,
    fallback_api_name: str,
    fallback_agent_id: str,
) -> AgentSpec:
    return AgentSpec(
        agent_id=normalize_agent_id(payload.get("agent_id")) or fallback_agent_id,
        server_id=normalize_server_id(payload.get("server_id"), fallback_server_id),
        llm=normalize_llm(payload.get("llm"), fallback_llm),
        api_name=normalize_api_name(payload.get("api_name"), fallback_api_name),
    )


def safe_container_suffix(agent_id: str) -> str:
    suffix = re.sub(r"[^a-zA-Z0-9_.-]+", "-", agent_id).strip("-").lower()
    return suffix or "agent"


def parse_env_bool(raw: str | None, default: bool) -> bool:
    if raw is None:
        return default

    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    return default


def resolve_docker_bin(preferred: str | None = None) -> str:
    raw = (preferred or "").strip()
    if raw:
        direct = Path(raw)
        if direct.exists():
            return str(direct)
        located = shutil.which(raw)
        if located:
            return located
        raise RuntimeError(f"DOCKER_BIN is set to '{raw}' but executable was not found")

    located = shutil.which("docker")
    if located:
        return located

    candidates = [
        Path("/usr/bin/docker"),
        Path("/usr/local/bin/docker"),
        Path("C:/Program Files/Docker/Docker/resources/bin/docker.exe"),
    ]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)

    raise RuntimeError(
        "Docker CLI not found. Install docker in the agent-manager runtime or set DOCKER_BIN to the docker executable path."
    )


def parse_agent_specs_from_env(manager_id: str) -> list[AgentSpec]:
    base_server_id = sanitize_prefixed_value(os.getenv("SERVER_ID", ""), "SERVER_ID") or manager_id
    base_llm = sanitize_prefixed_value(os.getenv("LLM", ""), "LLM")
    base_api_name = sanitize_prefixed_value(os.getenv("API_NAME", ""), "API_NAME")

    raw_agents_json = (os.getenv("AGENTS_CONFIG_JSON") or "").strip()
    if raw_agents_json:
        try:
            parsed = json.loads(raw_agents_json)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid AGENTS_CONFIG_JSON: {exc}") from exc

        if isinstance(parsed, dict):
            parsed = [parsed]
        if not isinstance(parsed, list):
            raise ValueError("AGENTS_CONFIG_JSON must be a JSON object or a list of objects")

        specs: list[AgentSpec] = []
        for index, item in enumerate(parsed, start=1):
            if not isinstance(item, dict):
                raise ValueError("AGENTS_CONFIG_JSON items must be JSON objects")
            specs.append(
                to_agent_spec(
                    item,
                    fallback_server_id=base_server_id,
                    fallback_llm=base_llm,
                    fallback_api_name=base_api_name,
                    fallback_agent_id=f"{manager_id}-agent-{index:02d}",
                )
            )

        unique_ids = dedupe_preserve_order([spec.agent_id for spec in specs])
        if len(unique_ids) != len(specs):
            raise ValueError("Duplicate agent_id values detected in AGENTS_CONFIG_JSON")
        return specs

    raw_agent_ids = split_csv_values(sanitize_prefixed_value(os.getenv("AGENT_IDS", ""), "AGENT_IDS"))
    if raw_agent_ids:
        agent_ids = dedupe_preserve_order(raw_agent_ids)
        server_ids = split_csv_values(sanitize_prefixed_value(os.getenv("SERVER_IDS", ""), "SERVER_IDS"))
        llm_values = split_csv_values(sanitize_prefixed_value(os.getenv("LLM_LIST", ""), "LLM_LIST"))
        api_values = split_csv_values(sanitize_prefixed_value(os.getenv("API_NAME_LIST", ""), "API_NAME_LIST"))

        specs = []
        for index, agent_id in enumerate(agent_ids):
            payload = {
                "agent_id": agent_id,
                "server_id": server_ids[index] if index < len(server_ids) else base_server_id,
                "llm": llm_values[index] if index < len(llm_values) else base_llm,
                "api_name": api_values[index] if index < len(api_values) else base_api_name,
            }
            specs.append(
                to_agent_spec(
                    payload,
                    fallback_server_id=base_server_id,
                    fallback_llm=base_llm,
                    fallback_api_name=base_api_name,
                    fallback_agent_id=f"{manager_id}-agent-{index + 1:02d}",
                )
            )
        return specs

    raw_agent_id = sanitize_prefixed_value(os.getenv("AGENT_ID", ""), "AGENT_ID")
    agent_id = raw_agent_id or f"{manager_id}-agent-01"
    return [
        AgentSpec(
            agent_id=agent_id,
            server_id=base_server_id,
            llm=base_llm,
            api_name=base_api_name,
        )
    ]


class ManagedAgent:
    def __init__(
        self,
        *,
        hub_url: str,
        manager_id: str,
        heartbeat_interval: int,
        reconnect_delay: int,
        spec: AgentSpec,
    ) -> None:
        self.hub_url = hub_url
        self.manager_id = manager_id
        self.heartbeat_interval = heartbeat_interval
        self.reconnect_delay = reconnect_delay
        self.spec = spec

        self.ws = None
        self.current_ticket_id: str | None = None

        runtime_root_raw = sanitize_prefixed_value(os.getenv("AGENT_RUNTIME_ROOT", os.getcwd()), "AGENT_RUNTIME_ROOT")
        self.runtime_root = Path(runtime_root_raw).resolve()

        env_file_raw = sanitize_prefixed_value(os.getenv("DOCKER_AGENT_ENV_FILE", ""), "DOCKER_AGENT_ENV_FILE")
        if env_file_raw:
            env_path = Path(env_file_raw)
            if not env_path.is_absolute():
                env_path = self.runtime_root / env_path
            self.docker_env_file = env_path.resolve()
        else:
            self.docker_env_file = (self.runtime_root / ".env").resolve()

        mount_source_raw = sanitize_prefixed_value(os.getenv("DOCKER_AGENT_MOUNT_SOURCE", ""), "DOCKER_AGENT_MOUNT_SOURCE")
        self.docker_mount_source = str(Path(mount_source_raw).resolve()) if mount_source_raw else ""
        self.docker_mount_target = sanitize_prefixed_value(os.getenv("DOCKER_AGENT_MOUNT_TARGET", "/app"), "DOCKER_AGENT_MOUNT_TARGET") or "/app"
        self.running_inside_docker = Path("/.dockerenv").exists()
        self.docker_manager_container_ref = sanitize_prefixed_value(
            os.getenv("DOCKER_MANAGER_CONTAINER_REF", os.getenv("HOSTNAME", "")),
            "DOCKER_MANAGER_CONTAINER_REF",
        )
        use_volumes_from_default = self.running_inside_docker and not self.docker_mount_source
        self.docker_use_volumes_from_manager = parse_env_bool(
            sanitize_prefixed_value(os.getenv("DOCKER_AGENT_USE_VOLUMES_FROM_MANAGER", ""), "DOCKER_AGENT_USE_VOLUMES_FROM_MANAGER"),
            use_volumes_from_default,
        )

        self.docker_agent_image = sanitize_prefixed_value(os.getenv("DOCKER_AGENT_IMAGE", "python:3.11-slim"), "DOCKER_AGENT_IMAGE") or "python:3.11-slim"
        self.docker_agent_command = (
            sanitize_prefixed_value(
                os.getenv("DOCKER_AGENT_COMMAND", "pip install --no-cache-dir websockets && python -u src/agent_manager/manager.py"),
                "DOCKER_AGENT_COMMAND",
            )
            or "pip install --no-cache-dir websockets && python -u src/agent_manager/manager.py"
        )
        self.docker_container_prefix = sanitize_prefixed_value(os.getenv("DOCKER_AGENT_CONTAINER_PREFIX", "hydra-agent"), "DOCKER_AGENT_CONTAINER_PREFIX") or "hydra-agent"
        self.docker_extra_args = shlex.split(sanitize_prefixed_value(os.getenv("DOCKER_AGENT_EXTRA_ARGS", ""), "DOCKER_AGENT_EXTRA_ARGS"))
        self.docker_bin_pref = sanitize_prefixed_value(os.getenv("DOCKER_BIN", ""), "DOCKER_BIN")
        self.agent_role = sanitize_prefixed_value(os.getenv("AGENT_ROLE", "agent"), "AGENT_ROLE") or "agent"

    async def run(self) -> None:
        while True:
            try:
                await self.connect()
                await self.listen()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                print(
                    f"[agent {self.spec.agent_id}] disconnected: {exc} "
                    f"-> reconnect in {self.reconnect_delay}s"
                )
            finally:
                await self.close_connection()

            await asyncio.sleep(self.reconnect_delay)

    async def connect(self) -> None:
        connect_url = build_agent_ws_url(self.hub_url, self.spec.agent_id)
        query: dict[str, str] = {
            "manager_id": self.manager_id,
            "server_id": self.spec.server_id,
            "role": self.agent_role,
        }
        if self.spec.llm:
            query["llm"] = self.spec.llm
        if self.spec.api_name:
            query["api"] = self.spec.api_name
        separator = "&" if "?" in connect_url else "?"
        connect_url = f"{connect_url}{separator}{urlencode(query)}"

        print(
            f"[agent {self.spec.agent_id}] connecting to {connect_url} "
            f"(manager_id={self.manager_id}, server_id={self.spec.server_id}, llm={self.spec.llm or '-'}, api={self.spec.api_name or '-'})"
        )
        self.ws = await websockets.connect(
            connect_url,
            ping_interval=20,
            ping_timeout=20,
            close_timeout=10,
        )
        await self.send_heartbeat(status="idle")
        print(f"[agent {self.spec.agent_id}] connected")

    async def listen(self) -> None:
        async with asyncio.TaskGroup() as task_group:
            task_group.create_task(self.heartbeat_loop())
            task_group.create_task(self.receive_loop())

    async def heartbeat_loop(self) -> None:
        while True:
            status = "working" if self.current_ticket_id else "idle"
            await self.send_heartbeat(status=status)
            await asyncio.sleep(self.heartbeat_interval)

    async def send_heartbeat(self, status: str) -> None:
        payload = {
            "type": "heartbeat",
            "status": status,
            "current_ticket": self.current_ticket_id,
            "server_id": self.spec.server_id,
            "manager_id": self.manager_id,
            "agent_role": self.agent_role,
            "ts": utc_now_iso(),
        }
        await self.send(payload)

    async def receive_loop(self) -> None:
        if self.ws is None:
            raise RuntimeError("WebSocket is not connected")

        async for raw in self.ws:
            print(f"[ws recv {self.spec.agent_id}] {raw}")
            try:
                message = json.loads(raw)
            except json.JSONDecodeError:
                print(f"[agent {self.spec.agent_id}] invalid JSON received: {raw!r}")
                continue

            await self.handle_message(message)

    async def handle_message(self, message: dict[str, Any]) -> None:
        message_type = str(message.get("type", "")).lower()

        if message_type == "heartbeat_ack":
            print(
                f"[heartbeat ack {self.spec.agent_id}] "
                f"agent_id={message.get('agent_id')} ts={message.get('ts')}"
            )
            return

        if message_type == "create_agent":
            await self.handle_create_agent(message)
            return

        if message_type == "delete_agent":
            await self.handle_delete_agent(message)
            return

        if message_type == "assign":
            await self.handle_assign(message)
            return

        if message_type == "kill":
            await self.handle_kill()
            return

        print(f"[agent {self.spec.agent_id}] ignored message type={message_type!r}")

    async def handle_create_agent(self, message: dict[str, Any]) -> None:
        request_id = str(message.get("request_id") or "").strip()
        payload = message.get("agent") if isinstance(message.get("agent"), dict) else {}

        target_spec = to_agent_spec(
            payload,
            fallback_server_id=self.spec.server_id,
            fallback_llm=self.spec.llm,
            fallback_api_name=self.spec.api_name,
            fallback_agent_id=f"{self.manager_id}-agent-{int(datetime.now(timezone.utc).timestamp())}",
        )

        ok = False
        error = ""
        container_name = ""
        try:
            container_name = await self.create_runtime_agent_container(target_spec)
            ok = True
            print(
                f"[agent {self.spec.agent_id}] create_agent OK -> "
                f"agent_id={target_spec.agent_id} container={container_name}"
            )
        except Exception as exc:
            error = str(exc)
            print(
                f"[agent {self.spec.agent_id}] create_agent FAIL -> "
                f"agent_id={target_spec.agent_id} error={error}"
            )

        await self.send(
            {
                "type": "create_agent_result",
                "request_id": request_id,
                "ok": ok,
                "agent_id": target_spec.agent_id,
                "server_id": target_spec.server_id,
                "container_name": container_name,
                "error": error,
                "ts": utc_now_iso(),
            }
        )

    async def handle_delete_agent(self, message: dict[str, Any]) -> None:
        request_id = str(message.get("request_id") or "").strip()
        target_agent_id = normalize_agent_id(message.get("agent_id"))

        ok = False
        error = ""
        container_name = ""

        if not target_agent_id:
            error = "agent_id is required"
        elif target_agent_id == self.spec.agent_id:
            error = "Refusing to delete the control websocket agent container"
        else:
            try:
                container_name = await self.delete_runtime_agent_container(target_agent_id)
                ok = True
                print(
                    f"[agent {self.spec.agent_id}] delete_agent OK -> "
                    f"agent_id={target_agent_id} container={container_name}"
                )
            except Exception as exc:
                error = str(exc)
                print(
                    f"[agent {self.spec.agent_id}] delete_agent FAIL -> "
                    f"agent_id={target_agent_id} error={error}"
                )

        await self.send(
            {
                "type": "delete_agent_result",
                "request_id": request_id,
                "ok": ok,
                "agent_id": target_agent_id,
                "server_id": self.spec.server_id,
                "container_name": container_name,
                "error": error,
                "ts": utc_now_iso(),
            }
        )

    async def run_docker_command(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        docker_bin = resolve_docker_bin(self.docker_bin_pref)
        cmd = [docker_bin, *args]
        try:
            return await asyncio.to_thread(
                subprocess.run,
                cmd,
                capture_output=True,
                text=True,
                cwd=str(self.runtime_root),
            )
        except FileNotFoundError as exc:
            raise RuntimeError(f"Docker CLI executable not found: {docker_bin}") from exc

    async def create_runtime_agent_container(self, target_spec: AgentSpec) -> str:
        container_name = f"{self.docker_container_prefix}-{safe_container_suffix(target_spec.agent_id)}"
        cmd = [
            "run",
            "-d",
            "--restart",
            "unless-stopped",
            "--name",
            container_name,
        ]

        if self.docker_use_volumes_from_manager:
            if not self.docker_manager_container_ref:
                raise RuntimeError(
                    "DOCKER_AGENT_USE_VOLUMES_FROM_MANAGER is enabled but no manager container ref is available. "
                    "Set DOCKER_MANAGER_CONTAINER_REF."
                )
            cmd.extend(["--volumes-from", self.docker_manager_container_ref])
        elif self.docker_mount_source:
            cmd.extend(["-v", f"{self.docker_mount_source}:{self.docker_mount_target}"])
        else:
            raise RuntimeError(
                "No mount strategy configured for spawned agents. "
                "Set DOCKER_AGENT_MOUNT_SOURCE or enable DOCKER_AGENT_USE_VOLUMES_FROM_MANAGER."
            )

        cmd.extend(["-w", self.docker_mount_target])

        if self.docker_env_file.exists():
            cmd.extend(["--env-file", str(self.docker_env_file)])

        overrides = {
            "HUB_URL": self.hub_url,
            "MANAGER_ID": self.manager_id,
            "AGENT_ID": target_spec.agent_id,
            "AGENT_ROLE": "agent",
            "SERVER_ID": target_spec.server_id,
            "LLM": target_spec.llm,
            "API_NAME": target_spec.api_name,
            "HEARTBEAT_INTERVAL": str(self.heartbeat_interval),
            "RECONNECT_DELAY": str(self.reconnect_delay),
            # Force single-agent mode in spawned containers.
            "AGENTS_CONFIG_JSON": "",
            "AGENT_IDS": "",
            "SERVER_IDS": "",
            "LLM_LIST": "",
            "API_NAME_LIST": "",
        }
        for key, value in overrides.items():
            cmd.extend(["-e", f"{key}={value}"])

        if self.docker_extra_args:
            cmd.extend(self.docker_extra_args)

        cmd.append(self.docker_agent_image)
        cmd.extend(["sh", "-c", self.docker_agent_command])

        proc = await self.run_docker_command(cmd)

        if proc.returncode != 0:
            stderr = (proc.stderr or "").strip()
            stdout = (proc.stdout or "").strip()
            details = stderr or stdout or f"docker exited with code {proc.returncode}"
            raise RuntimeError(details)

        return container_name

    async def delete_runtime_agent_container(self, target_agent_id: str) -> str:
        container_name = f"{self.docker_container_prefix}-{safe_container_suffix(target_agent_id)}"
        proc = await self.run_docker_command(["rm", "-f", container_name])

        if proc.returncode != 0:
            stderr = (proc.stderr or "").strip()
            stdout = (proc.stdout or "").strip()
            details = (stderr or stdout or f"docker exited with code {proc.returncode}").lower()
            if "no such container" not in details:
                raise RuntimeError(stderr or stdout or f"docker exited with code {proc.returncode}")

        return container_name

    async def handle_assign(self, message: dict[str, Any]) -> None:
        ticket = message.get("ticket", {})
        ticket_id = str(ticket.get("id", "unknown-ticket"))

        self.current_ticket_id = ticket_id
        print(f"[agent {self.spec.agent_id}] assign received: {ticket_id}")

        await self.send(
            {
                "type": "log",
                "agent_id": self.spec.agent_id,
                "ticket_id": ticket_id,
                "level": "info",
                "message": "Ticket assigned to manager (worker not implemented yet).",
                "ts": utc_now_iso(),
            }
        )

        self.current_ticket_id = None

    async def handle_kill(self) -> None:
        print(f"[agent {self.spec.agent_id}] kill received, closing connection")
        await self.close_connection()
        raise ConnectionError("Kill received")

    async def send(self, payload: dict[str, Any]) -> None:
        if self.ws is None:
            raise RuntimeError("WebSocket is not connected")

        print(f"[ws send {self.spec.agent_id}] {json.dumps(payload, ensure_ascii=False)}")
        await self.ws.send(json.dumps(payload))

    async def close_connection(self) -> None:
        if self.ws is not None:
            try:
                await self.ws.close()
            finally:
                self.ws = None


class AgentManager:
    def __init__(self) -> None:
        self.hub_url = sanitize_hub_url(os.getenv("HUB_URL", "ws://localhost:8000/ws/agent"))
        self.manager_id = sanitize_prefixed_value(os.getenv("MANAGER_ID", "manager-01"), "MANAGER_ID") or "manager-01"
        self.heartbeat_interval = int(os.getenv("HEARTBEAT_INTERVAL", "10"))
        self.reconnect_delay = int(os.getenv("RECONNECT_DELAY", "5"))
        self.agent_specs = parse_agent_specs_from_env(self.manager_id)

    async def run(self) -> None:
        print(f"[manager] boot with {len(self.agent_specs)} agent(s) for manager_id={self.manager_id}")
        for spec in self.agent_specs:
            print(
                f"[manager] agent={spec.agent_id} "
                f"server_id={spec.server_id} llm={spec.llm or '-'} api={spec.api_name or '-'}"
            )

        async with asyncio.TaskGroup() as task_group:
            for spec in self.agent_specs:
                agent = ManagedAgent(
                    hub_url=self.hub_url,
                    manager_id=self.manager_id,
                    heartbeat_interval=self.heartbeat_interval,
                    reconnect_delay=self.reconnect_delay,
                    spec=spec,
                )
                task_group.create_task(agent.run())


async def main() -> None:
    manager = AgentManager()
    await manager.run()


if __name__ == "__main__":
    asyncio.run(main())
