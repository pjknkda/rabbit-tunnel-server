from __future__ import annotations

import abc
import asyncio
import threading
import uuid
from typing import TYPE_CHECKING, Any, NamedTuple

if TYPE_CHECKING:
    from starlette.websockets import WebSocket


class TunnelEntry(NamedTuple):
    uid: str
    ws: WebSocket


class NameConflictError(Exception):
    pass


class TunnelDirectory(abc.ABC):
    @abc.abstractmethod
    async def get(self, name: str) -> TunnelEntry | None:
        raise NotImplementedError()

    @abc.abstractmethod
    async def acquire(self, name: str, ws: WebSocket, force: bool = False) -> TunnelEntry:
        raise NotImplementedError()

    @abc.abstractmethod
    async def release(self, name: str, tunnel_uid: str) -> None:
        raise NotImplementedError()

    @abc.abstractmethod
    async def wait_until_die(self, name: str, tunnel_uid: str) -> None:
        raise NotImplementedError()

    @abc.abstractmethod
    def summary(self) -> dict[str, Any]:
        raise NotImplementedError()


class InMemoryTunnelDirectory(TunnelDirectory):
    def __init__(self) -> None:
        self._name_to_entry: dict[str, TunnelEntry] = {}
        self._lock = threading.RLock()

    async def get(self, name: str) -> TunnelEntry | None:
        return self._name_to_entry.get(name)

    async def acquire(self, name: str, ws: WebSocket, force: bool = False) -> TunnelEntry:
        with self._lock:
            if (
                name in self._name_to_entry
                and not force
            ):
                raise NameConflictError()

            self._name_to_entry[name] = TunnelEntry(
                uid=uuid.uuid4().hex,
                ws=ws,
            )

            return self._name_to_entry[name]

    async def release(self, name: str, tunnel_uid: str) -> None:
        with self._lock:
            entry = self._name_to_entry.get(name)

            if (
                entry is None
                or entry.uid != tunnel_uid
            ):
                return

            del self._name_to_entry[name]

    async def wait_until_die(self, name: str, tunnel_uid: str) -> None:
        while True:
            await asyncio.sleep(1)

            with self._lock:
                entry = self._name_to_entry.get(name)

                if (
                    entry is None
                    or entry.uid != tunnel_uid
                ):
                    break

    def summary(self) -> dict[str, Any]:
        return {
            'num_tunnels': len(self._name_to_entry),
        }
