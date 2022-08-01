from __future__ import annotations

import asyncio
import enum
import hashlib
import hmac
import json
import logging
import os
import time
from typing import TYPE_CHECKING

import msgpack
import websockets.exceptions
from starlette.applications import Starlette
from starlette.responses import JSONResponse, Response
from starlette.websockets import WebSocketState

from .multiplexer import Multiplexer
from .tunnel_directory import InMemoryTunnelDirectory, NameConflictError

if TYPE_CHECKING:
    from starlette.requests import Request
    from starlette.websockets import WebSocket

try:
    from setuptools_scm import get_version

    __version__ = get_version(root="..", relative_to=__file__)
except LookupError:
    try:
        from ._version import version

        __version__ = version
    except ModuleNotFoundError:
        raise RuntimeError(
            "Cannot determine version: "
            "check whether git repository is initialized "
            "or _version.py file exists."
        )

__all__ = ["init_environment", "Server"]

logger = logging.getLogger(__name__)


class TunnelClosedCode(enum.IntEnum):
    NameConflict = 4900
    Evicted = 4901
    ServerTermination = 4902


def _setup_debug_logger() -> None:
    logger.setLevel(logging.DEBUG)
    if logger.handlers:
        return

    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter(logging.BASIC_FORMAT))
    logger.handlers.append(handler)


class ControlServer:
    def __init__(
        self,
        service_domain: str,
        multiplexer_host: str,
        multiplexer_port: int,
        secret_key: str,
        debug: bool = False,
    ) -> None:
        if debug:
            _setup_debug_logger()

        self.service_domain = service_domain
        self.secret_key = secret_key or os.getenv("SECRET_KEY", "")

        self.web_app = Starlette(
            on_startup=[self._app_startup],
            on_shutdown=[self._app_shutdown],
        )

        self.web_app.router.add_route("/", self._index_route, ["GET"])
        self.web_app.router.add_route("/stats", self._stats_route, ["GET"])
        self.web_app.router.add_websocket_route(
            "/tunnel/{name:str}", self._tunnel_ws_route
        )

        self._tunnel_directory = InMemoryTunnelDirectory()
        self._multiplexer = Multiplexer(
            service_domain,
            multiplexer_host,
            multiplexer_port,
            self._tunnel_directory,
        )

    async def _app_startup(self) -> None:
        await self._multiplexer.start()

    async def _app_shutdown(self) -> None:
        await self._multiplexer.stop()

    async def _index_route(self, _: Request) -> Response:
        return JSONResponse({"ok": True})

    async def _stats_route(self, _: Request) -> Response:
        # TODO : only details only if debug mode
        return Response(
            json.dumps(
                {
                    "tunnel_directory": self._tunnel_directory.summary(),
                    "multiplexer": self._multiplexer.summary(),
                },
                indent=2,
            ),
            media_type="application/json",
        )

    async def _tunnel_ws_route(self, ws: WebSocket) -> None:
        req_name_raw: str = ws.path_params["name"]

        if req_name_raw.startswith("!"):
            force = True
            req_name = req_name_raw[1:]
        else:
            force = False
            req_name = req_name_raw

        if self.secret_key:
            token: str = ws.query_params.get("token", "")
            try:
                token_digest, token_exp_str = token.split(":", maxsplit=1)

                token_exp = int(token_exp_str)
                if token_exp < time.time():
                    raise RuntimeError("expired token")

                if not hmac.compare_digest(
                    token_digest,
                    hmac.new(
                        key=hashlib.sha256(self.secret_key.encode()).digest(),
                        msg=f"{token_exp_str}:{req_name_raw}".encode(),
                        digestmod=hashlib.sha256,
                    ).hexdigest(),
                ):
                    raise RuntimeError("wrong token digest")

            except Exception:
                await ws.close()
                return

        await ws.accept()

        try:
            tunnel_entry = await self._tunnel_directory.acquire(
                req_name, ws, force
            )
        except NameConflictError:
            await ws.close(code=TunnelClosedCode.NameConflict)
            return

        logger.info(
            "Tunnel [%s] (UID: %s) is connected",
            tunnel_entry.name,
            tunnel_entry.uid[:8],
        )

        async def _eviction_checker() -> None:
            await self._tunnel_directory.wait_until_die(tunnel_entry)

            logger.debug(
                "Tunnel [%s] (UID: %s) is evicted",
                tunnel_entry.name,
                tunnel_entry.uid[:8],
            )

            if ws.client_state != WebSocketState.DISCONNECTED:
                await ws.close(code=TunnelClosedCode.Evicted)

        eviction_checker_task = asyncio.create_task(_eviction_checker())

        try:
            await ws.send_bytes(
                msgpack.packb(
                    {
                        "type": "welcome",
                        "conn_uid": None,
                        "name": tunnel_entry.name,
                        "domain": self.service_domain,
                    }
                )
            )

            while True:
                recv_msg = await ws.receive()
                if recv_msg["type"] == "websocket.disconnect":
                    break

                msg = msgpack.unpackb(recv_msg["bytes"])
                if not (
                    isinstance(msg, dict)
                    and "type" in msg
                    and "conn_uid" in msg
                ):
                    # invalid msg format
                    continue

                m_conn = self._multiplexer.get_connection(msg["conn_uid"])

                if m_conn is None:
                    # expired connection
                    continue

                if msg["type"] == "setup-ok":
                    if m_conn.setup_result.done():
                        logger.warning(
                            "Invalid state: connection setup is already called"
                        )
                        continue

                    m_conn.set_setup_result(True)

                elif msg["type"] == "data":
                    if m_conn.is_closed:
                        continue

                    m_conn.last_keepalive_ts = time.monotonic()

                    try:
                        m_conn.writer.write(msg["data"])
                        await m_conn.writer.drain()
                    except (RuntimeError, ConnectionError):
                        continue

                elif msg["type"] == "keepalive":
                    if m_conn.is_closed:
                        continue

                    m_conn.last_keepalive_ts = time.monotonic()

                elif msg["type"] == "closed":
                    if m_conn.is_closed:
                        continue

                    m_conn.set_closed_reason(msg["reason"])

                else:
                    logger.warning("Unexpected msg : %s", msg)

        except websockets.exceptions.ConnectionClosedOK:
            pass

        except Exception:
            logger.exception("Exception from tunnel WS connection")

        finally:
            eviction_checker_task.cancel()
            try:
                await eviction_checker_task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.warning(
                    "Exception while canceling eviction_checker_task",
                    exc_info=True,
                )

            await self._tunnel_directory.release(tunnel_entry)

            if ws.client_state != WebSocketState.DISCONNECTED:
                await ws.close()

            logger.info(
                "Tunnel [%s] (UID: %s) is closed",
                tunnel_entry.name,
                tunnel_entry.uid[:8],
            )
