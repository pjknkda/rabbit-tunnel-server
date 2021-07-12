from __future__ import annotations

import asyncio
import dataclasses
import datetime
import enum
import logging
import re
import time
from email.utils import formatdate
from time import mktime
from typing import Any

import async_timeout
import httptools
import msgpack
from websockets.exceptions import ConnectionClosed as WsConnectionClosed

from .tunnel_directory import TunnelDirectory

_READ_BUFFER = 4 * 2**10  # 4KB
_SETUP_HEADER_TIMEOUT = 10  # 10 seconds
_SETUP_CONNECTED_TIMEOUT = 10  # 10 seconds


logger = logging.getLogger(__name__)


def _date_header() -> str:
    stamp = mktime(datetime.datetime.now(datetime.timezone.utc).timetuple())
    return formatdate(timeval=stamp, localtime=False, usegmt=True)


class HTTPErrorHeader(enum.Enum):
    BAD_REQUEST = '400 Bad Request'
    INTERNAL_SERVER_ERROR = '500 Internal Server Error'
    GATEWAY_TIMEOUT = '504 Gateway Timeout'


class MultiplexErrorCode(enum.Enum):
    PARSE_ERROR = enum.auto()
    INSUFFICIENT_DATA = enum.auto()
    HEARDER_TIMEOUT = enum.auto()
    NO_TARGET_HOST_HEADER = enum.auto()
    NO_TARGET_TUNNEL = enum.auto()
    UNRESPONSIVE = enum.auto()
    UNEXPECTED = enum.auto()


class MultiplexError(Exception):
    def __init__(self, header: HTTPErrorHeader, code: MultiplexErrorCode) -> None:
        self.header = header
        self.code = code


@dataclasses.dataclass
class MultiplexedConnection:
    conn_uid: str
    connected: asyncio.Event
    connected_ack: asyncio.Event
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    c2s_queue: asyncio.Queue[dict[str, Any]]
    tunnel_name: str | None
    tunnel_uid: str | None
    last_keepalive_ts: float


def _generate_http_error(header: HTTPErrorHeader, code: MultiplexErrorCode) -> bytes:
    return (
        f'HTTP/1.0 {header.value}\r\n'
        f'date: {_date_header()}\r\n'
        'server: rabbit-tunnel\r\n'
        'content-type: text/html\r\n'
        'content-length: 0\r\n'
        f'x-failed-code: {code.name}\r\n'
        '\r\n'
    ).encode()


class Multiplexer:
    def __init__(self, service_domain: str, host: str, port: int, tunnel_directory: TunnelDirectory) -> None:
        self.service_domain = service_domain
        self.host = host
        self.port = port
        self.tunnel_directory = tunnel_directory

        self._server: asyncio.AbstractServer | None = None
        self._conn_uid_to_conn: dict[str, MultiplexedConnection] = {}

        self._conn_counter = 0

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._on_connection, self.host, self.port)
        logger.info('TCP multiplexer is running on %s:%d', self.host, self.port)

    async def stop(self) -> None:
        if self._server is None:
            return

        self._server.close()
        await self._server.wait_closed()

    def get_connection(self, conn_uid: str) -> MultiplexedConnection | None:
        return self._conn_uid_to_conn.get(conn_uid)

    def summary(self) -> dict[str, Any]:
        current_ts = time.monotonic()

        return {
            'active_conns': [
                {
                    'uid': conn.conn_uid,
                    'tunnel_name': conn.tunnel_name,
                    'tunnel_uid': conn.tunnel_uid,
                    'from_last_keepalive': current_ts - conn.last_keepalive_ts,
                }
                for conn in self._conn_uid_to_conn.values()
            ],
            'conn_counter': self._conn_counter,
        }

    async def _on_connection(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        self._conn_counter += 1
        conn_uid = str(self._conn_counter)

        logger.debug('Connection %s is opened', conn_uid)

        multiplexed_connection = MultiplexedConnection(
            conn_uid=conn_uid,
            connected=asyncio.Event(),
            connected_ack=asyncio.Event(),
            reader=reader,
            writer=writer,
            c2s_queue=asyncio.Queue(),
            tunnel_name=None,
            tunnel_uid=None,
            last_keepalive_ts=time.monotonic(),
        )

        self._conn_uid_to_conn[conn_uid] = multiplexed_connection

        tunnel_entry = None
        is_something_writed = False

        try:
            conn_setup = MultiplexedConnectionSetup(self.service_domain, reader)
            try:
                async with async_timeout.timeout(_SETUP_HEADER_TIMEOUT):
                    tunnel_name, setup_bytes = await conn_setup.process()
            except asyncio.TimeoutError:
                raise MultiplexError(HTTPErrorHeader.BAD_REQUEST, MultiplexErrorCode.HEARDER_TIMEOUT)

            tunnel_entry = await self.tunnel_directory.get(tunnel_name)
            if tunnel_entry is None:
                raise MultiplexError(HTTPErrorHeader.BAD_REQUEST, MultiplexErrorCode.NO_TARGET_TUNNEL)

            try:
                await tunnel_entry.ws.send_bytes(
                    msgpack.packb({
                        'type': 'setup',
                        'conn_uid': conn_uid,
                    })
                )
            except (RuntimeError, WsConnectionClosed):
                return

            try:
                async with async_timeout.timeout(_SETUP_CONNECTED_TIMEOUT):
                    await multiplexed_connection.connected.wait()
            except asyncio.TimeoutError:
                raise MultiplexError(HTTPErrorHeader.GATEWAY_TIMEOUT, MultiplexErrorCode.UNRESPONSIVE)

            is_something_writed = True

            multiplexed_connection.connected_ack.set()

            try:
                await tunnel_entry.ws.send_bytes(
                    msgpack.packb({
                        'type': 'data',
                        'conn_uid': conn_uid,
                        'data': setup_bytes,
                    })
                )
            except (RuntimeError, WsConnectionClosed):
                return

            try:
                while True:
                    buf = await reader.read(_READ_BUFFER)
                    if not buf:
                        break

                    await tunnel_entry.ws.send_bytes(
                        msgpack.packb({
                            'type': 'data',
                            'conn_uid': conn_uid,
                            'data': buf,
                        })
                    )
            except (RuntimeError, ConnectionResetError, WsConnectionClosed):
                return

        except MultiplexError as err:
            if not is_something_writed:
                writer.write(_generate_http_error(err.header, err.code))

            logger.debug('Connection %s is closed by error : %s / %s', conn_uid, err.header.name, err.code.name)

        except Exception:
            logger.exception('Exception from multiplexed TCP connection')

            if not is_something_writed:
                writer.write(_generate_http_error(HTTPErrorHeader.INTERNAL_SERVER_ERROR, MultiplexErrorCode.UNEXPECTED))

            logger.debug('Connection %s is closed by exception', conn_uid)

        finally:
            if tunnel_entry is not None:
                try:
                    await tunnel_entry.ws.send_bytes(
                        msgpack.packb({
                            'type': 'closed',
                            'conn_uid': conn_uid,
                            'reason': 'remote-connection-closed',
                        })
                    )
                except (RuntimeError, WsConnectionClosed):
                    pass
                except Exception:
                    logger.warning('Failed to send closed message', exc_info=True)

            try:
                await writer.drain()
            except ConnectionResetError:
                pass
            except Exception:
                logger.warning('Failed to drain writer', exc_info=True)

            del self._conn_uid_to_conn[conn_uid]
            writer.close()

            multiplexed_connection.c2s_queue.put_nowait({
                'type': 'closed',
                'reason': 'server-connection-closed',
            })


class MultiplexedConnectionSetup:
    def __init__(
        self,
        service_domain: str,
        reader: asyncio.StreamReader,
    ) -> None:
        self.reader = reader

        self._host_regex = r'^([^\.\/\s]+)\.' + re.escape(service_domain) + '$'

        self._headers: list[tuple[bytes, bytes]] = []
        self._is_header_completed = False

    async def process(self) -> tuple[str, bytes]:
        http_parser = httptools.HttpRequestParser(self)  # type: ignore

        setup_buf = bytearray()

        while not self._is_header_completed:
            buf = await self.reader.read(_READ_BUFFER)
            if not buf:
                # early termination
                raise MultiplexError(HTTPErrorHeader.BAD_REQUEST, MultiplexErrorCode.INSUFFICIENT_DATA)

            setup_buf.extend(buf)

            try:
                http_parser.feed_data(buf)
            except httptools.HttpParserError:
                raise MultiplexError(HTTPErrorHeader.BAD_REQUEST, MultiplexErrorCode.PARSE_ERROR)
            except httptools.HttpParserUpgrade:
                break

        if not self._is_header_completed:
            raise MultiplexError(HTTPErrorHeader.BAD_REQUEST, MultiplexErrorCode.NO_TARGET_HOST_HEADER)

        host_found: str | None = None
        for header_name, header_value in self._headers:
            if header_name == b'host':
                m = re.match(self._host_regex, header_value.decode(), re.IGNORECASE)
                if m is not None:
                    host_found = m.group(1)
                    break

        if host_found is None:
            raise MultiplexError(HTTPErrorHeader.BAD_REQUEST, MultiplexErrorCode.NO_TARGET_HOST_HEADER)

        return host_found, bytes(setup_buf)

    def on_header(self, name: bytes, value: bytes) -> None:
        self._headers.append((name.lower(), value))

    def on_headers_complete(self) -> None:
        self._is_header_completed = True
