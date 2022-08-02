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
_SETUP_HEADER_MAX_SIZE = 16 * 2**10  # 16 KB
_SETUP_CONNECTED_TIMEOUT = 10  # 10 seconds

_KEEPALIVE_INTERVAL = 30  # 30 seconds
_KEEPALIVE_COUNT = 3

logger = logging.getLogger(__name__)


def _date_header() -> str:
    stamp = mktime(datetime.datetime.now(datetime.timezone.utc).timetuple())
    return formatdate(timeval=stamp, localtime=False, usegmt=True)


class HTTPErrorHeader(enum.Enum):
    BAD_REQUEST = "400 Bad Request"
    INTERNAL_SERVER_ERROR = "500 Internal Server Error"
    GATEWAY_TIMEOUT = "504 Gateway Timeout"


class MultiplexErrorCode(enum.Enum):
    PARSE_ERROR = enum.auto()
    INSUFFICIENT_DATA = enum.auto()
    MAX_HEADER_SIZE_EXCEED = enum.auto()
    HEADER_TIMEOUT = enum.auto()
    NO_TARGET_HOST_HEADER = enum.auto()
    NO_TARGET_TUNNEL = enum.auto()
    UNRESPONSIVE = enum.auto()
    UNEXPECTED = enum.auto()


@dataclasses.dataclass
class MultiplexError(Exception):
    header: HTTPErrorHeader
    code: MultiplexErrorCode


@dataclasses.dataclass
class MConn:
    conn_uid: str
    writer: asyncio.StreamWriter
    setup_result: asyncio.Future[bool]
    closed_reason: str | None
    last_keepalive_ts: float

    @property
    def is_closed(self) -> bool:
        return self.closed_reason is not None

    def set_setup_result(self, result: bool) -> None:
        if self.setup_result.done():
            return
        self.setup_result.set_result(result)

    def set_closed_reason(self, reason: str) -> None:
        self.set_setup_result(False)

        if self.closed_reason is not None:
            return

        self.closed_reason = reason


def _generate_http_error(
    header: HTTPErrorHeader, code: MultiplexErrorCode
) -> bytes:
    return (
        f"HTTP/1.0 {header.value}\r\n"
        f"date: {_date_header()}\r\n"
        "server: rabbit-tunnel\r\n"
        "content-type: text/html\r\n"
        "content-length: 0\r\n"
        f"x-failed-code: {code.name}\r\n"
        "\r\n"
    ).encode()


class Multiplexer:
    def __init__(
        self,
        service_domain: str,
        host: str,
        port: int,
        tunnel_directory: TunnelDirectory,
    ) -> None:
        self.service_domain = service_domain
        self.host = host
        self.port = port
        self.tunnel_directory = tunnel_directory

        self._server: asyncio.AbstractServer | None = None
        self._conn_uid_to_conn: dict[str, MConn] = {}

        self._conn_counter = 0

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._on_connection, self.host, self.port
        )
        logger.info("TCP multiplexer is running on %s:%d", self.host, self.port)

    async def stop(self) -> None:
        if self._server is None:
            return

        self._server.close()
        await self._server.wait_closed()

    def get_connection(self, conn_uid: str) -> MConn | None:
        return self._conn_uid_to_conn.get(conn_uid)

    def summary(self) -> dict[str, Any]:
        current_ts = time.monotonic()

        return {
            "active_conns": [
                {
                    "uid": conn.conn_uid,
                    "from_last_keepalive": current_ts - conn.last_keepalive_ts,
                }
                for conn in self._conn_uid_to_conn.values()
            ],
            "conn_counter": self._conn_counter,
        }

    async def _on_connection(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        loop = asyncio.get_event_loop()

        self._conn_counter += 1
        conn_uid = str(self._conn_counter)

        logger.debug("Connection %s is opened", conn_uid)

        try:
            conn_setup = MConnSetup(self.service_domain, reader)
            try:
                async with async_timeout.timeout(_SETUP_HEADER_TIMEOUT):
                    tunnel_name, setup_bytes = await conn_setup.process()
            except asyncio.TimeoutError:
                raise MultiplexError(
                    HTTPErrorHeader.BAD_REQUEST,
                    MultiplexErrorCode.HEADER_TIMEOUT,
                )

            tunnel_entry = await self.tunnel_directory.get(tunnel_name)
            if tunnel_entry is None:
                raise MultiplexError(
                    HTTPErrorHeader.BAD_REQUEST,
                    MultiplexErrorCode.NO_TARGET_TUNNEL,
                )

        except MultiplexError as err:
            logger.debug(
                "Multiplexing error : %s / %s (UID: %s)",
                err.header.name,
                err.code.name,
                conn_uid,
            )
            writer.write(_generate_http_error(err.header, err.code))
            return

        except Exception:
            logger.exception("Exception from m_conn setup (UID: %s)", conn_uid)
            writer.write(
                _generate_http_error(
                    HTTPErrorHeader.INTERNAL_SERVER_ERROR,
                    MultiplexErrorCode.UNEXPECTED,
                )
            )
            return

        m_conn = MConn(
            conn_uid=conn_uid,
            writer=writer,
            setup_result=loop.create_future(),
            closed_reason=None,
            last_keepalive_ts=time.monotonic(),
        )

        is_something_written = False
        keepalive_checker_task = None

        self._conn_uid_to_conn[conn_uid] = m_conn

        try:
            try:
                await tunnel_entry.ws.send_bytes(
                    msgpack.packb(
                        {
                            "type": "setup",
                            "conn_uid": conn_uid,
                        }
                    )
                )
            except (RuntimeError, WsConnectionClosed):
                return

            setup_ok = False
            try:
                async with async_timeout.timeout(_SETUP_CONNECTED_TIMEOUT):
                    setup_ok = await m_conn.setup_result
            except asyncio.TimeoutError:
                raise MultiplexError(
                    HTTPErrorHeader.GATEWAY_TIMEOUT,
                    MultiplexErrorCode.UNRESPONSIVE,
                )

            if not setup_ok:
                raise MultiplexError(
                    HTTPErrorHeader.GATEWAY_TIMEOUT,
                    MultiplexErrorCode.UNRESPONSIVE,
                )

            logger.debug(
                "Connection %s is established for [%s] (UID: %s)",
                m_conn.conn_uid,
                tunnel_entry.name,
                tunnel_entry.uid[:8],
            )

            async def _keepalive_checker() -> None:
                assert m_conn is not None
                while not m_conn.is_closed:
                    await asyncio.sleep(_KEEPALIVE_INTERVAL)
                    if (
                        time.monotonic() - m_conn.last_keepalive_ts
                        <= _KEEPALIVE_COUNT * _KEEPALIVE_INTERVAL
                    ):
                        continue
                    m_conn.set_closed_reason("client-keepalive-timeout")

            keepalive_checker_task = asyncio.create_task(_keepalive_checker())

            is_something_written = True
            try:
                await tunnel_entry.ws.send_bytes(
                    msgpack.packb(
                        {
                            "type": "data",
                            "conn_uid": conn_uid,
                            "data": setup_bytes,
                        }
                    )
                )

                while not m_conn.is_closed:
                    try:
                        async with async_timeout.timeout(0.1):
                            buf = await reader.read(_READ_BUFFER)
                    except asyncio.TimeoutError:
                        continue

                    if not buf:
                        break

                    await tunnel_entry.ws.send_bytes(
                        msgpack.packb(
                            {
                                "type": "data",
                                "conn_uid": conn_uid,
                                "data": buf,
                            }
                        )
                    )
            except (RuntimeError, ConnectionError, WsConnectionClosed):
                return

        except MultiplexError as err:
            logger.debug(
                "Multiplexing error : %s / %s (UID: %s)",
                err.header.name,
                err.code.name,
                conn_uid,
            )

            if not is_something_written:
                writer.write(_generate_http_error(err.header, err.code))

        except Exception:
            logger.exception("Exception from m_conn (UID: %s)", conn_uid)

            if not is_something_written:
                writer.write(
                    _generate_http_error(
                        HTTPErrorHeader.INTERNAL_SERVER_ERROR,
                        MultiplexErrorCode.UNEXPECTED,
                    )
                )

        finally:
            if keepalive_checker_task is not None:
                keepalive_checker_task.cancel()
                try:
                    await keepalive_checker_task
                except asyncio.CancelledError:
                    pass
                except Exception:
                    logger.warning(
                        "Exception from keepalive_checker_task", exc_info=True
                    )

            try:
                await writer.drain()
                writer.close()
                await writer.wait_closed()
            except ConnectionError:
                pass
            except Exception:
                logger.warning(
                    "Failed to drain and close writer", exc_info=True
                )

            try:
                await tunnel_entry.ws.send_bytes(
                    msgpack.packb(
                        {
                            "type": "closed",
                            "conn_uid": conn_uid,
                            "reason": "remote-connection-closed",
                        }
                    )
                )
            except (RuntimeError, WsConnectionClosed):
                pass
            except Exception:
                logger.warning("Failed to send closed message", exc_info=True)

            del self._conn_uid_to_conn[conn_uid]

            m_conn.set_closed_reason("remote-connection-closed")

            logger.debug(
                "Connection %s is closed for [%s] (UID: %s) : %s",
                m_conn.conn_uid,
                tunnel_entry.name,
                tunnel_entry.uid[:8],
                m_conn.closed_reason,
            )


class MConnSetup:
    def __init__(
        self,
        service_domain: str,
        reader: asyncio.StreamReader,
    ) -> None:
        self.reader = reader

        self._host_regex = r"^([^\.\/\s]+)\." + re.escape(service_domain) + "$"

        self._headers: list[tuple[bytes, bytes]] = []
        self._is_header_completed = False

    async def process(self) -> tuple[str, bytes]:
        http_parser = httptools.HttpRequestParser(self)

        setup_buf = bytearray()

        while not self._is_header_completed:
            buf = await self.reader.read(
                min(
                    _READ_BUFFER,
                    max(0, _SETUP_HEADER_MAX_SIZE - len(setup_buf)),
                )
            )

            if not buf:
                if _SETUP_HEADER_MAX_SIZE <= len(setup_buf):
                    raise MultiplexError(
                        HTTPErrorHeader.BAD_REQUEST,
                        MultiplexErrorCode.MAX_HEADER_SIZE_EXCEED,
                    )
                else:
                    raise MultiplexError(
                        HTTPErrorHeader.BAD_REQUEST,
                        MultiplexErrorCode.INSUFFICIENT_DATA,
                    )

            setup_buf.extend(buf)

            try:
                http_parser.feed_data(buf)
            except httptools.HttpParserError:
                raise MultiplexError(
                    HTTPErrorHeader.BAD_REQUEST, MultiplexErrorCode.PARSE_ERROR
                )
            except httptools.HttpParserUpgrade:
                break

        if not self._is_header_completed:
            raise MultiplexError(
                HTTPErrorHeader.BAD_REQUEST,
                MultiplexErrorCode.NO_TARGET_HOST_HEADER,
            )

        host_found: str | None = None
        for header_name, header_value in self._headers:
            if header_name == b"host":
                m = re.match(
                    self._host_regex, header_value.decode(), re.IGNORECASE
                )
                if m is not None:
                    host_found = m.group(1)
                    break

        if host_found is None:
            raise MultiplexError(
                HTTPErrorHeader.BAD_REQUEST,
                MultiplexErrorCode.NO_TARGET_HOST_HEADER,
            )

        return host_found, bytes(setup_buf)

    def on_header(self, name: bytes, value: bytes) -> None:
        self._headers.append((name.lower(), value))

    def on_headers_complete(self) -> None:
        self._is_header_completed = True
