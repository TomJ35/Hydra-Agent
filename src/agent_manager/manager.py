import asyncio
import json
import os
from datetime import datetime, timezone
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


class AgentManager:
    def __init__(self) -> None:
        self.hub_url = sanitize_hub_url(os.getenv("HUB_URL", "ws://localhost:8000/ws/agent"))
        self.manager_id = sanitize_prefixed_value(os.getenv("MANAGER_ID", "manager-01"), "MANAGER_ID") or "manager-01"
        raw_agent_id = sanitize_prefixed_value(os.getenv("AGENT_ID", ""), "AGENT_ID")
        self.agent_id = raw_agent_id or f"{self.manager_id}-agent-01"
        self.heartbeat_interval = int(os.getenv("HEARTBEAT_INTERVAL", "10"))
        self.reconnect_delay = int(os.getenv("RECONNECT_DELAY", "5"))
        raw_server_id = sanitize_prefixed_value(os.getenv("SERVER_ID", ""), "SERVER_ID")
        self.server_id = raw_server_id or self.manager_id
        self.llm = sanitize_prefixed_value(os.getenv("LLM", ""), "LLM")
        self.api_name = sanitize_prefixed_value(os.getenv("API_NAME", ""), "API_NAME")

        self.ws = None
        self.current_ticket_id: str | None = None

    async def run(self) -> None:
        while True:
            try:
                await self.connect()
                await self.listen()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                print(f"[manager] disconnected: {exc} -> reconnect in {self.reconnect_delay}s")
            finally:
                await self.close_connection()

            await asyncio.sleep(self.reconnect_delay)

    async def connect(self) -> None:
        connect_url = build_agent_ws_url(self.hub_url, self.agent_id)
        query: dict[str, str] = {"server_id": self.server_id}
        if self.llm:
            query["llm"] = self.llm
        if self.api_name:
            query["api"] = self.api_name
        if query:
            separator = "&" if "?" in connect_url else "?"
            connect_url = f"{connect_url}{separator}{urlencode(query)}"

        print(
            f"[manager] connecting to {connect_url} "
            f"(manager_id={self.manager_id}, server_id={self.server_id}, agent_id={self.agent_id})"
        )
        self.ws = await websockets.connect(
            connect_url,
            ping_interval=20,
            ping_timeout=20,
            close_timeout=10,
        )
        await self.send_heartbeat(status="idle")
        print("[manager] connected")

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
            "server_id": self.server_id,
            "manager_id": self.manager_id,
            "ts": utc_now_iso(),
        }
        await self.send(payload)

    async def receive_loop(self) -> None:
        if self.ws is None:
            raise RuntimeError("WebSocket is not connected")

        async for raw in self.ws:
            print(f"[ws recv] {raw}")
            try:
                message = json.loads(raw)
            except json.JSONDecodeError:
                print(f"[manager] invalid JSON received: {raw!r}")
                continue

            await self.handle_message(message)

    async def handle_message(self, message: dict[str, Any]) -> None:
        message_type = str(message.get("type", "")).lower()

        if message_type == "heartbeat_ack":
            print(f"[heartbeat ack] agent_id={message.get('agent_id')} ts={message.get('ts')}")
            return

        if message_type == "assign":
            await self.handle_assign(message)
            return

        if message_type == "kill":
            await self.handle_kill()
            return

        print(f"[manager] ignored message type={message_type!r}")

    async def handle_assign(self, message: dict[str, Any]) -> None:
        ticket = message.get("ticket", {})
        ticket_id = str(ticket.get("id", "unknown-ticket"))

        self.current_ticket_id = ticket_id
        print(f"[manager] assign received: {ticket_id}")

        await self.send(
            {
                "type": "log",
                "agent_id": self.agent_id,
                "ticket_id": ticket_id,
                "level": "info",
                "message": "Ticket assigned to manager (worker not implemented yet).",
                "ts": utc_now_iso(),
            }
        )

        self.current_ticket_id = None

    async def handle_kill(self) -> None:
        print("[manager] kill received, closing connection")
        await self.close_connection()
        raise ConnectionError("Kill received")

    async def send(self, payload: dict[str, Any]) -> None:
        if self.ws is None:
            raise RuntimeError("WebSocket is not connected")

        print(f"[ws send] {json.dumps(payload, ensure_ascii=False)}")
        await self.ws.send(json.dumps(payload))

    async def close_connection(self) -> None:
        if self.ws is not None:
            try:
                await self.ws.close()
            finally:
                self.ws = None


async def main() -> None:
    manager = AgentManager()
    await manager.run()


if __name__ == "__main__":
    asyncio.run(main())
