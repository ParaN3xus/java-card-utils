#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ctypes
import logging
import socket
import struct
import sys
import time
from collections.abc import Callable, Generator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from enum import IntEnum
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Final, Protocol, cast

from smartcard.scard import (  # pyright: ignore[reportMissingTypeStubs]
    SCardBeginTransaction as _SCardBeginTransaction,  # pyright: ignore[reportUnknownVariableType]
)
from smartcard.scard import (  # pyright: ignore[reportMissingTypeStubs]
    SCardEndTransaction as _SCardEndTransaction,  # pyright: ignore[reportUnknownVariableType]
)
from smartcard.System import readers as _readers  # pyright: ignore[reportMissingTypeStubs, reportUnknownVariableType]

logger: Final[logging.Logger] = logging.getLogger("vpcd_bridge")


# region pyscard types

readers = cast(Callable[[], list[object]], _readers)
SCardBeginTransaction = cast(Callable[[int], int], _SCardBeginTransaction)
SCardEndTransaction = cast(Callable[[int, int], int], _SCardEndTransaction)

SCARD_LEAVE_CARD: Final[int] = 0


class PcscConnection(Protocol):
    """The slice of pyscard's `CardConnection` this bridge uses."""

    def connect(self) -> None: ...

    def getATR(self) -> list[int]: ...

    def transmit(self, command: list[int]) -> tuple[list[int], int, int]: ...

    def disconnect(self) -> None: ...


class PcscReader(Protocol):
    """The slice of pyscard's `Reader` this bridge uses."""

    def createConnection(self) -> PcscConnection: ...


# endregion

# region errors


class BridgeError(Exception):
    """Base class for the ways one relayed session can end."""


class CardUnavailable(BridgeError):
    """The card or the reader could not be used."""


class LinkLost(BridgeError):
    """The vpcd connection is gone, or the card vanished while it was up."""


# endregion

# region helpers


def hexbytes(data: bytes) -> str:
    return " ".join(f"{byte:02X}" for byte in data)


def is_elevated() -> bool | None:
    """True when the token holds BUILTIN\\Administrators; None if unknown."""
    if sys.platform != "win32":
        return None
    windll = getattr(ctypes, "windll", None)
    if windll is None:
        return None
    try:
        return bool(windll.shell32.IsUserAnAdmin())
    except OSError:
        return None


def find_reader(pattern: str) -> PcscReader | None:
    """First physical reader whose name contains `pattern` (virtual ones excluded)."""
    for candidate in readers():
        if "virtual" in str(candidate).lower():
            continue
        if pattern.lower() in str(candidate).lower():
            return cast(PcscReader, candidate)
    return None


def recv_bytes(sock: socket.socket, size: int) -> bytes:
    """Read exactly `size` bytes; a socket timeout is passed on to the caller."""
    buffer = bytearray()
    while len(buffer) < size:
        try:
            chunk = sock.recv(size - len(buffer))
        except TimeoutError:
            raise
        except OSError as exc:
            raise LinkLost(f"vpcd read failed: {exc}") from exc
        if not chunk:
            raise LinkLost("vpcd closed the connection")
        buffer += chunk
    return bytes(buffer)


def read_frame(sock: socket.socket, timeout: float | None = None) -> bytes | None:
    """Read one [length:2][payload] frame, or None if nothing came within `timeout`."""
    if timeout is not None:
        sock.settimeout(timeout)
    try:
        size = struct.unpack(">H", recv_bytes(sock, 2))[0]
    except TimeoutError:
        return None
    finally:
        sock.settimeout(None)
    return recv_bytes(sock, size)


def send_frame(sock: socket.socket, payload: bytes) -> None:
    """Write one [length][payload] frame, or raise LinkLost."""
    try:
        sock.sendall(struct.pack(">H", len(payload)) + payload)
    except OSError as exc:
        raise LinkLost(f"vpcd write failed: {exc}") from exc


@contextmanager
def card_transaction(connection: PcscConnection) -> Generator[None, None, None]:
    """Hold the card for one relayed APDU, so another PC/SC client (gpg) cannot
    splice its commands into it, and its transaction cannot swallow ours."""
    inner = getattr(
        connection, "component", connection
    )  # pyscard hides hcard one decorator deep
    handle = getattr(inner, "hcard", None)
    if not isinstance(handle, int):
        yield
        return
    hresult = SCardBeginTransaction(handle)
    if hresult != 0:
        logger.warning("SCardBeginTransaction failed: 0x%08X", hresult)
        yield
        return
    try:
        yield
    finally:
        # 0 means do nothing when tx finished
        SCardEndTransaction(handle, 0)


# endregion


class CardSession:
    """One PC/SC session on the physical reader."""

    def __init__(self, reader: PcscReader) -> None:
        self._reader = reader
        self._conn: PcscConnection | None = None
        self._atr = b""

    @property
    def atr(self) -> bytes:
        """ATR read when the session was opened."""
        return self._atr

    @property
    def is_open(self) -> bool:
        return self._conn is not None

    def open(self) -> bool:
        """Power the card up and keep the session; False when there is no card."""
        self.close()
        connection: PcscConnection | None = None
        try:
            connection = self._reader.createConnection()
            connection.connect()
            self._atr = bytes(connection.getATR())
        except Exception as exc:  # pyscard raises a type per failure mode
            logger.debug("reader %s: no usable card: %s", self._reader, exc)
            if connection is not None:
                try:
                    connection.disconnect()
                except Exception:
                    logger.debug(
                        "disconnect after failed connect failed", exc_info=True
                    )
            return False
        self._conn = connection
        return True

    def close(self) -> None:
        """Close the session if any; a failure here is logged, never raised."""
        connection, self._conn = self._conn, None
        if connection is None:
            return
        try:
            connection.disconnect()
        except Exception:
            logger.debug("SCardDisconnect failed", exc_info=True)

    def current_atr(self) -> bytes | None:
        """ATR as the driver sees it right now, or None when the card is gone."""
        connection = self._conn
        if connection is None:
            return None
        try:
            return bytes(connection.getATR())
        except Exception:  # pyscard raises a type per failure mode
            logger.debug("SCardStatus failed", exc_info=True)
            return None

    def transmit(self, apdu: bytes) -> bytes:
        """Send one APDU, return the card's reply as data + SW1 SW2."""
        connection = self._conn
        if connection is None:
            raise CardUnavailable("no PC/SC session")
        try:
            with card_transaction(connection):
                data, sw1, sw2 = connection.transmit(list(apdu))
        except Exception as exc:  # pyscard raises a type per failure mode
            self.close()
            raise CardUnavailable(f"SCardTransmit failed: {exc}") from exc
        return bytes(data) + bytes((sw1, sw2))


# region relay


class Control(IntEnum):
    """vpcd control frames; only ATR is answered."""

    OFF = 0
    ON = 1
    RESET = 2
    ATR = 4


def handle_control(session: CardSession, sock: socket.socket, command: Control) -> None:
    match command:
        case Control.ATR:
            if not session.is_open and not session.open():
                raise LinkLost("vpcd asked for the ATR but no card could be powered up")
            atr = session.current_atr()
            if atr is None:
                raise LinkLost("vpcd asked for the ATR but the card is gone")
            logger.debug("ctrl ATR -> %s", hexbytes(atr))
            send_frame(sock, atr)
        case Control.RESET:
            logger.info("vpcd reset the card")
            if not session.open():
                raise LinkLost("card disappeared while it was being reset")
            logger.debug("ctrl RESET done, ATR %s", hexbytes(session.atr))
        case Control.OFF:
            logger.info("vpcd reports the card as off, powering it down")
            session.close()
        case Control.ON:
            logger.debug("ctrl ON (the card is powered by this session)")


def relay_frame(session: CardSession, sock: socket.socket, frame: bytes) -> None:
    if not frame:
        raise LinkLost("vpcd sent an empty frame, unreachable")
    if len(frame) == 1:
        # A one-byte payload can only be a control frame
        try:
            command = Control(frame[0])
        except ValueError:
            raise LinkLost(f"unknown vpcd control byte 0x{frame[0]:02X}") from None
        handle_control(session, sock, command)
        return

    logger.debug("-> %s", hexbytes(frame))
    reply = session.transmit(frame)
    logger.debug("<- %s", hexbytes(reply))
    send_frame(sock, reply)


# endregion


@dataclass(frozen=True)
class Settings:
    host: str
    port: int
    reader_pattern: str
    poll_interval: float
    connect_timeout: float


def serve(session: CardSession, settings: Settings) -> None:
    """Relay one vpcd connection until the link or the card goes away."""
    try:
        sock = socket.create_connection(
            (settings.host, settings.port), timeout=settings.connect_timeout
        )
    except OSError as exc:
        raise CardUnavailable(f"vpcd {settings.host}:{settings.port}: {exc}") from exc

    with sock:
        logger.info("connected to vpcd %s:%d", settings.host, settings.port)
        while True:
            frame = read_frame(sock, settings.poll_interval)
            if frame is None:
                # vpcd only speaks while something asks it about the virtual card,
                # so silence can last forever, and we need to check by ourselves.
                if session.is_open and session.current_atr() is None:
                    raise LinkLost("card went away while the vpcd link was idle")
                continue
            relay_frame(session, sock, frame)


def run(settings: Settings) -> int:
    logger.info(
        "bridge: reader %r -> vpcd %s:%d (poll %.2fs)",
        settings.reader_pattern,
        settings.host,
        settings.port,
        settings.poll_interval,
    )
    if is_elevated() is False:
        logger.error(
            "not elevated: Windows refuses SELECT of the FIDO AID over WinSCard "
            "with SCARD_E_NO_ACCESS (0x80100027); run this bridge as administrator"
        )
        return 2

    while True:
        reader = find_reader(settings.reader_pattern)
        if reader is None:
            logger.debug(
                "no reader matching %r, staying off vpcd", settings.reader_pattern
            )
            time.sleep(settings.poll_interval)
            continue

        session = CardSession(reader)
        if not session.open():
            logger.debug("reader %s has no usable card, staying off vpcd", reader)
            time.sleep(settings.poll_interval)
            continue

        logger.info("card present in %s, ATR %s", reader, hexbytes(session.atr))
        try:
            serve(session, settings)
        except CardUnavailable as exc:
            logger.error("card link lost, dropping vpcd: %s", exc)
        except LinkLost as exc:
            logger.info("vpcd link closed: %s", exc)
        finally:
            session.close()
        logger.info("off vpcd again, watching the reader")
        time.sleep(settings.poll_interval)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Transparent PC/SC <-> vpcd bridge")
    parser.add_argument("--host", default="127.0.0.1", help="vpcd address")
    parser.add_argument("--port", type=int, default=35963, help="vpcd port")
    parser.add_argument(
        "--reader", default="", help="substring of the physical reader name"
    )
    parser.add_argument(
        "--poll", type=float, default=0.5, help="seconds between card checks"
    )
    parser.add_argument(
        "--connect-timeout", type=float, default=20.0, help="seconds to wait for vpcd"
    )
    parser.add_argument(
        "--log-file",
        default="",
        help="also append the log to this file",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="log every APDU and reply"
    )
    args = parser.parse_args(argv)

    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if args.log_file:
        log_path = Path(args.log_file).expanduser()
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(
            RotatingFileHandler(
                log_path, maxBytes=1_000_000, backupCount=3, encoding="utf-8"
            )
        )
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        handlers=handlers,
    )
    logger.debug("APDU logging is on")

    settings = Settings(
        host=str(args.host),
        port=int(args.port),
        reader_pattern=str(args.reader),
        poll_interval=float(args.poll),
        connect_timeout=float(args.connect_timeout),
    )
    try:
        return run(settings)
    except KeyboardInterrupt:
        logger.info("interrupted")
        return 1


if __name__ == "__main__":
    sys.exit(main())
