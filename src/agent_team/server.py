from __future__ import annotations

import asyncio
import contextlib
import fcntl
import json
import os
import secrets
import tempfile
from pathlib import Path

from .chatroom import ChatRoom
from .config import TeamConfig, valid_name
from .engine import Room
from .store import Store
from .streams import readline


def encode(event: dict) -> bytes:
    return (json.dumps(event, ensure_ascii=False) + "\n").encode()


async def receive(reader: asyncio.StreamReader) -> dict:
    line = await readline(reader)
    if not line:
        raise EOFError
    event = json.loads(line)
    if not isinstance(event, dict):
        raise ValueError("Requests must be JSON objects")
    return event


class Server:
    def __init__(self, config: TeamConfig, session: Path) -> None:
        self.config, self.session = config, session.resolve()
        self.token = secrets.token_urlsafe(32)
        self.clients: dict[str, tuple[asyncio.Queue, asyncio.StreamWriter]] = {}
        self.handlers: set[asyncio.Task] = set()
        self.stopping = False
        self.listener: asyncio.Server | None = None
        self.lease = None
        self.workspace_lease = None
        self.store: Store | None = None
        self.room: Room | None = None

    async def start(self) -> None:
        self.session.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lease = os.fdopen(
            os.open(self.session / "room.lock", os.O_CREAT | os.O_RDWR, 0o600), "w"
        )
        try:
            fcntl.flock(self.lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self.lease.close()
            self.lease = None
            raise ValueError("This session is already running; use agent-team join") from exc
        try:
            if self.config.workflow == "build":
                lock_dir = self.config.workspace / ".agent-team"
                lock_dir.mkdir(mode=0o700, exist_ok=True)
                self.workspace_lease = os.fdopen(
                    os.open(lock_dir / "workspace.lock", os.O_CREAT | os.O_RDWR, 0o600),
                    "w",
                )
                try:
                    fcntl.flock(self.workspace_lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as exc:
                    raise ValueError(
                        "This workspace already has a team; join it or use another workspace"
                    ) from exc
            self.store = Store(self.session / "events.sqlite3")
            room_type = ChatRoom if self.config.interaction_mode == "chatroom" else Room
            self.room = room_type(self.config, self.store, self.broadcast)
            self.listener = await asyncio.start_server(self.handle, "127.0.0.1", 0)
            info = {
                "host": "127.0.0.1",
                "port": self.listener.sockets[0].getsockname()[1],
                "token": self.token,
                "pid": os.getpid(),
                "protocol": 1,
            }
            with tempfile.NamedTemporaryFile("w", dir=self.session, delete=False) as handle:
                temp_path = Path(handle.name)
                json.dump(info, handle)
            try:
                # Startup has no active clients; keep publication atomic with the lease.
                temp_path.replace(self.session / "connection.json")  # noqa: ASYNC240
            finally:
                temp_path.unlink(missing_ok=True)  # noqa: ASYNC240
            self.room.start()
        except BaseException:
            await self.close()
            raise

    def broadcast(self, event: dict) -> None:
        for queue, writer in list(self.clients.values()):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                # A slow reader cannot hold up agents or other participants.
                writer.close()

    async def send_loop(
        self, queue: asyncio.Queue, writer: asyncio.StreamWriter, replay: list[dict]
    ) -> None:
        try:
            for event in replay:
                writer.write(encode(event))
                await asyncio.wait_for(writer.drain(), 10)
            while True:
                writer.write(encode(await queue.get()))
                await asyncio.wait_for(writer.drain(), 10)
        finally:
            writer.close()

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        self.handlers.add(task)
        name = None
        sender = None
        try:
            hello = await asyncio.wait_for(receive(reader), 5)
            token = hello.get("token")
            if (
                hello.get("type") != "join"
                or not isinstance(token, str)
                or not secrets.compare_digest(token.encode(), self.token.encode())
            ):
                raise ValueError("Authentication failed")
            candidate = hello.get("name")
            if not valid_name(candidate) or candidate == "system":
                raise ValueError(
                    "Names require 1-32 letters, digits, CJK characters, underscores or hyphens"
                )
            if candidate in self.clients or candidate in {a.name for a in self.config.agents}:
                raise ValueError("Name already in use; choose another --name")
            name = candidate
            queue: asyncio.Queue = asyncio.Queue(maxsize=512)
            self.clients[name] = (queue, writer)
            assert self.room and self.store
            # Snapshot and subscription occur without an await, so no event can fall between them.
            replay = [{"type": "welcome", "name": name, "state": self.room.status()}]
            replay.extend({**message, "replay": True} for message in self.room.messages)
            sender = asyncio.create_task(self.send_loop(queue, writer, replay))
            self.room.emit("presence", durable=False, names=list(self.clients))
            while True:
                request = await receive(reader)
                try:
                    kind = request.get("type")
                    if kind == "say":
                        self.room.say(name, request.get("text"))
                    elif kind == "redirect":
                        if isinstance(self.room, ChatRoom):
                            self.room.redirect(name, request.get("text"))
                        else:
                            self.room.say(name, request.get("text"))
                    elif kind == "control":
                        self.room.control(request.get("action"), request.get("target"))
                    elif kind == "status":
                        queue.put_nowait({"type": "state", **self.room.status()})
                    elif kind == "sessions":
                        queue.put_nowait({"type": "session.state", **self.room.status()})
                    elif kind == "workflow":
                        queue.put_nowait(
                            {"type": "workflow", "workflow": self.room.status()["workflow"]}
                        )
                    elif kind == "history":
                        after = request.get("after", 0)
                        if type(after) is not int or after < 0:
                            raise ValueError("after must be a nonnegative integer")
                        batch = [m for m in self.room.messages if m["id"] > after][:100]
                        for message in batch:
                            queue.put_nowait({**message, "replay": True})
                        queue.put_nowait(
                            {
                                "type": "history.end",
                                "count": len(batch),
                                "next_after": batch[-1]["id"] if batch else after,
                            }
                        )
                    else:
                        raise ValueError("Unknown request type")
                except (ValueError, TypeError) as exc:
                    queue.put_nowait({"type": "error", "speaker": "system", "text": str(exc)})
        except (EOFError, ConnectionError, asyncio.CancelledError):
            pass
        except (ValueError, TimeoutError, asyncio.QueueFull) as exc:
            with contextlib.suppress(ConnectionError, TimeoutError):
                writer.write(encode({"type": "error", "speaker": "system", "text": str(exc)}))
                await asyncio.wait_for(writer.drain(), 1)
        finally:
            if sender:
                sender.cancel()
                await asyncio.gather(sender, return_exceptions=True)
            if name:
                self.clients.pop(name, None)
                if not self.stopping and self.room:
                    # Presence is observational; even the last human leaving does not
                    # revoke the floor or pause unattended work.
                    self.room.emit("presence", durable=False, names=list(self.clients))
            writer.close()
            with contextlib.suppress(ConnectionError):
                await writer.wait_closed()
            self.handlers.discard(task)

    async def close(self) -> None:
        self.stopping = True
        if self.listener:
            self.listener.close()
            await self.listener.wait_closed()
        if self.room and not self.room.closed:
            await self.room.close()
        for task in list(self.handlers):
            task.cancel()
        await asyncio.gather(*self.handlers, return_exceptions=True)
        if self.store:
            self.store.close()
            self.store = None
        if self.lease:
            (self.session / "connection.json").unlink(missing_ok=True)
            self.lease.close()
            self.lease = None
        if self.workspace_lease:
            self.workspace_lease.close()
            self.workspace_lease = None


async def connect(session: Path, name: str) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    try:
        info = json.loads((session / "connection.json").read_text())
    except FileNotFoundError as exc:
        raise ValueError("Session is not running; use agent-team start or serve first") from exc
    if info.get("host") != "127.0.0.1" or info.get("protocol") != 1:
        raise ValueError("Unsupported session address or protocol")
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(info["host"], info["port"]), 5
        )
    except OSError as exc:
        raise ValueError("Cannot connect; the server may have exited, restart it") from exc
    writer.write(encode({"type": "join", "name": name, "token": info["token"]}))
    await writer.drain()
    return reader, writer
