from __future__ import annotations

import abc
import asyncio
import threading
import uuid
from typing import TYPE_CHECKING, Any, NamedTuple

if TYPE_CHECKING:
    from starlette.websockets import WebSocket


class TunnelEntry(NamedTuple):
    name: str
    uid: str
    ws: WebSocket


class NameConflictError(Exception):
    pass


class TunnelDirectory(abc.ABC):
    @abc.abstractmethod
    async def get(self, name: str) -> TunnelEntry | None:
        raise NotImplementedError()

    @abc.abstractmethod
    async def acquire(
        self, name: str, ws: WebSocket, force: bool = False
    ) -> TunnelEntry:
        raise NotImplementedError()

    @abc.abstractmethod
    async def release(self, entry: TunnelEntry) -> None:
        raise NotImplementedError()

    @abc.abstractmethod
    async def wait_until_die(self, entry: TunnelEntry) -> None:
        raise NotImplementedError()

    @abc.abstractmethod
    def summary(self) -> dict[str, Any]:
        raise NotImplementedError()


class InMemoryTunnelDirectory(TunnelDirectory):
    def __init__(self) -> None:
        self._name_to_entry: dict[str, TunnelEntry] = {}
        self._lock = threading.RLock()

    async def get(self, name: str) -> TunnelEntry | None:
        return self._name_to_entry.get(name.lower())

    async def acquire(
        self, name: str, ws: WebSocket, force: bool = False
    ) -> TunnelEntry:
        name_lower = name.lower()

        with self._lock:
            if name_lower in self._name_to_entry and not force:
                raise NameConflictError()

            self._name_to_entry[name_lower] = TunnelEntry(
                name=name_lower,
                uid=uuid.uuid4().hex,
                ws=ws,
            )

            return self._name_to_entry[name_lower]

    async def release(self, entry: TunnelEntry) -> None:
        with self._lock:
            active_entry = self._name_to_entry.get(entry.name)

            if active_entry is None or active_entry.uid != entry.uid:
                return

            del self._name_to_entry[entry.name]

    async def wait_until_die(self, entry: TunnelEntry) -> None:
        while True:
            await asyncio.sleep(0.5)

            with self._lock:
                active_entry = self._name_to_entry.get(entry.name)

                if active_entry is None or active_entry.uid != entry.uid:
                    break

    def summary(self) -> dict[str, Any]:
        return {
            "num_tunnels": len(self._name_to_entry),
        }
