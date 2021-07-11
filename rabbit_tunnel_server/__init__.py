from __future__ import annotations

import asyncio
import enum
import json
import logging
import time
from typing import TYPE_CHECKING

import async_timeout
import msgpack
import websockets.exceptions
from starlette.applications import Starlette
from starlette.responses import JSONResponse, Response
from starlette.websockets import WebSocketState

from .multiplexer import MultiplexedConnection, Multiplexer
from .tunnel_directory import InMemoryTunnelDirectory, NameConflictError

if TYPE_CHECKING:
    from starlette.requests import Request
    from starlette.websockets import WebSocket

__all__ = ['init_environment', 'Server']

logger = logging.getLogger(__name__)

_PORXY_CONNECTION_KEEPALIVE_INTERVAL = 30  # 30 seconds


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


class Server:
    def __init__(self, service_domain: str, multiplexer_host: str, multiplexer_port: int, debug: bool = False) -> None:
        if debug:
            _setup_debug_logger()

        self.service_domain = service_domain

        self.web_app = Starlette(
            on_startup=[self._app_startup],
            on_shutdown=[self._app_shutdown],
        )

        self.web_app.router.add_route('/', self._index_route, ['GET'])
        self.web_app.router.add_route('/stats', self._stats_route, ['GET'])
        self.web_app.router.add_websocket_route('/tunnel/{name:str}', self._tunnel_ws_route)

        self._tunnel_directory = InMemoryTunnelDirectory()
        self._multiplexer = Multiplexer(service_domain, multiplexer_host, multiplexer_port, self._tunnel_directory)

        self._c2s_puller_dict: dict[str, asyncio.Task] = {}

    async def _app_startup(self) -> None:
        await self._multiplexer.start()

    async def _app_shutdown(self) -> None:
        await self._multiplexer.stop()

    async def _index_route(self, request: Request) -> Response:
        return JSONResponse({'ok': True})

    async def _stats_route(self, request: Request) -> Response:
        return Response(
            json.dumps(
                {
                    'tunnel_directory': self._tunnel_directory.summary(),
                    'multiplexer': self._multiplexer.summary(),
                },
                indent=2,
            ),
            media_type='application/json',
        )

    async def _c2s_puller(
        self,
        tunnel_name: str,
        tunnel_uid: str,
        multiplexed_connection: MultiplexedConnection,
    ) -> None:
        multiplexed_connection.tunnel_name = tunnel_name
        multiplexed_connection.tunnel_uid = tunnel_uid

        async def _keepalive_checker() -> None:
            while True:
                await asyncio.sleep(_PORXY_CONNECTION_KEEPALIVE_INTERVAL)
                from_last_keepalive = time.monotonic() - multiplexed_connection.last_keepalive_ts
                if from_last_keepalive <= 3 * _PORXY_CONNECTION_KEEPALIVE_INTERVAL:
                    continue

                multiplexed_connection.c2s_queue.put_nowait({
                    'type': 'closed',
                    'reason': 'client-keepalive-timeout',
                })
                return

        keepalive_checker_task = asyncio.create_task(_keepalive_checker())

        try:
            while True:
                msg = await multiplexed_connection.c2s_queue.get()

                if msg['type'] == 'setup-ok':
                    if not (
                        not multiplexed_connection.connected.is_set()
                        and not multiplexed_connection.connected_ack.is_set()
                    ):
                        raise RuntimeError('Invalid state: duplicated setup-ok messages')

                    multiplexed_connection.connected.set()

                    try:
                        async with async_timeout.timeout(10):
                            await multiplexed_connection.connected_ack.wait()
                    except asyncio.TimeoutError:
                        # frontend is already closed
                        break

                    logger.debug(
                        'Connection %s is established for [%s] (UID: %s)',
                        multiplexed_connection.conn_uid,
                        tunnel_name,
                        tunnel_uid[:8],
                    )

                elif msg['type'] == 'data':
                    if not (
                        multiplexed_connection.connected.is_set()
                        and multiplexed_connection.connected_ack.is_set()
                    ):
                        raise RuntimeError('Invalid state: data message before setup-ok message')

                    multiplexed_connection.last_keepalive_ts = time.monotonic()

                    try:
                        multiplexed_connection.writer.write(msg['data'])
                        await multiplexed_connection.writer.drain()
                    except ConnectionResetError:
                        break

                elif msg['type'] == 'keepalive':
                    multiplexed_connection.last_keepalive_ts = time.monotonic()

                elif msg['type'] == 'closed':
                    logger.debug(
                        'Connection %s is closed for [%s] (UID: %s) : %s',
                        multiplexed_connection.conn_uid,
                        tunnel_name,
                        tunnel_uid[:8],
                        msg['reason'],
                    )
                    break

                else:
                    raise NotImplementedError()

                multiplexed_connection.c2s_queue.task_done()

        except asyncio.CancelledError:
            pass

        except Exception:
            logger.exception('Exception from client-to-server puller : %s', multiplexed_connection.conn_uid)

        finally:
            try:
                keepalive_checker_task.cancel()
                await keepalive_checker_task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception('Exception from tunnel proxy connection keepalive checker')

            multiplexed_connection.reader.feed_eof()
            del self._c2s_puller_dict[multiplexed_connection.conn_uid]

    async def _tunnel_ws_route(self, ws: WebSocket) -> None:
        await ws.accept()

        name: str = ws.path_params['name']

        if name.startswith('!'):
            force = True
            name = name[1:]
        else:
            force = False

        try:
            tunnel_entry = await self._tunnel_directory.acquire(name, ws, force)
        except NameConflictError:
            await ws.close(code=TunnelClosedCode.NameConflict)
            return

        logger.info('Tunnel [%s] (UID: %s) is connected', name, tunnel_entry.uid[:8])

        async def _eviction_checker() -> None:
            await self._tunnel_directory.wait_until_die(name, tunnel_entry.uid)

            logger.debug('Tunnel [%s] (UID: %s) is evicted', name, tunnel_entry.uid[:8])

            if ws.client_state != WebSocketState.DISCONNECTED:
                await ws.close(code=TunnelClosedCode.Evicted)

        eviction_checker_task = asyncio.create_task(_eviction_checker())

        try:
            await ws.send_bytes(
                msgpack.packb({
                    'type': 'welcome',
                    'conn_uid': None,
                    'domain': self.service_domain,
                })
            )

            while True:
                recv_msg = await ws.receive()
                if recv_msg['type'] == 'websocket.disconnect':
                    break

                msg = msgpack.unpackb(recv_msg['bytes'])
                if not (
                    isinstance(msg, dict)
                    and 'type' in msg
                    and 'conn_uid' in msg
                ):
                    # invalid msg format
                    continue

                multiplexed_connection = self._multiplexer.get_connection(msg['conn_uid'])

                if multiplexed_connection is None:
                    # expired connection
                    continue

                if msg['type'] == 'setup-ok':
                    if msg['conn_uid'] in self._c2s_puller_dict:
                        logger.warning('Invalid state: connection is already registered')
                        continue

                    self._c2s_puller_dict[msg['conn_uid']] = asyncio.create_task(
                        self._c2s_puller(name, tunnel_entry.uid, multiplexed_connection)
                    )

                await multiplexed_connection.c2s_queue.put(msg)

        except websockets.exceptions.ConnectionClosedOK:
            pass

        except Exception:
            logger.exception('Exception from tunnel WS connection')

        finally:
            for puller_task in self._c2s_puller_dict.values():
                puller_task.cancel()

            eviction_checker_task.cancel()
            try:
                await eviction_checker_task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.warning('Exception while canceling eviction_checker_task', exc_info=True)

            await self._tunnel_directory.release(name, tunnel_entry.uid)

        if ws.client_state != WebSocketState.DISCONNECTED:
            await ws.close()

        logger.info('Tunnel [%s] (UID: %s) is closed', name, tunnel_entry.uid[:8])
