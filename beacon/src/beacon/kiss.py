"""KISS framing + a hand-built AX.25 UI-frame encoder + a TCP client for
Direwolf's KISS port (default 8001, per CONTEXT.md) — no third-party AX.25
library. Fully unit-testable against a real localhost TCP socket standing
in for Direwolf (see beacon/tests/test_kiss.py); the address encoding is
tested against manually-worked-out reference byte sequences.

Not fully field-validated: CONTEXT.md's own AX.25 example was confirmed
working via `kissutil`, which does its own TNC2-text-to-bytes translation
— this module bypasses kissutil and talks to Direwolf's raw KISS socket
with hand-built bytes instead, so its byte-level correctness against a
*real* TNC has not been proven by anything in this repo. One real
on-air/lab smoke test (send a frame, confirm reception) is recommended
before first live use, in addition to — not instead of — the unit suite.

Command-bit convention: both the destination and source address SSID
bytes set the C bit high (0x80), matching the common APRS/beacon-software
convention for UI frames sent without a formal connection (no single
universally-followed spec value exists for this case) — worth
double-checking against whatever the receiving station's software expects
if interop with someone else's decoder is important."""
import logging
import socket

logger = logging.getLogger(__name__)

FEND = 0xC0
FESC = 0xDB
TFEND = 0xDC
TFESC = 0xDD


def kiss_escape(data: bytes) -> bytes:
    out = bytearray()
    for b in data:
        if b == FEND:
            out.append(FESC)
            out.append(TFEND)
        elif b == FESC:
            out.append(FESC)
            out.append(TFESC)
        else:
            out.append(b)
    return bytes(out)


def kiss_unescape(data: bytes) -> bytes:
    """Only used by tests, to decode what a fake TCP server received back
    into the original bytes for assertions — the real KissTcpClient never
    needs to unescape anything (it only ever sends)."""
    out = bytearray()
    i = 0
    while i < len(data):
        b = data[i]
        if b == FESC and i + 1 < len(data) and data[i + 1] in (TFEND, TFESC):
            out.append(FEND if data[i + 1] == TFEND else FESC)
            i += 2
            continue
        out.append(b)
        i += 1
    return bytes(out)


def kiss_frame(data: bytes, *, port: int = 0) -> bytes:
    """FEND, command byte (high nibble = port, low nibble 0x0 = "data
    frame"), escaped payload, FEND."""
    command_byte = (port & 0x0F) << 4
    return bytes([FEND, command_byte]) + kiss_escape(data) + bytes([FEND])


def encode_callsign_address(callsign_ssid: str, *, is_last: bool) -> bytes:
    """The standard 7-byte AX.25 address field: 6 space-padded, uppercased,
    left-shifted-by-1 ASCII characters, followed by one SSID/control byte
    (C bit | 2 reserved bits set high | 4-bit SSID shifted left 1 |
    extension bit — 1 means this is the last address field, i.e. no
    digipeater path follows, matching this project's direct/simplex
    single-hop design per CONTEXT.md). callsign_ssid like "CD3DXZ-1" (SSID
    1) or "WXALRT" (SSID 0 implied)."""
    if "-" in callsign_ssid:
        call, ssid_str = callsign_ssid.split("-", 1)
        try:
            ssid = int(ssid_str)
        except ValueError:
            raise ValueError(f"invalid SSID in {callsign_ssid!r}") from None
    else:
        call, ssid = callsign_ssid, 0
    if not (0 <= ssid <= 15):
        raise ValueError(f"SSID must be 0-15, got {ssid} (from {callsign_ssid!r})")
    call = call.upper()
    if not call or len(call) > 6:
        raise ValueError(f"callsign {call!r} must be 1-6 characters")

    addr = bytearray(ord(c) << 1 for c in call.ljust(6))
    ssid_byte = 0x80 | 0x60 | (ssid << 1) | (0x01 if is_last else 0x00)
    addr.append(ssid_byte)
    return bytes(addr)


def build_ui_frame(*, source_callsign: str, dest_callsign: str, info: bytes) -> bytes:
    """dest address, source address (no digipeater path), control 0x03
    (UI frame, no poll/final), PID 0xF0 (no layer-3 protocol), info."""
    dest_addr = encode_callsign_address(dest_callsign, is_last=False)
    src_addr = encode_callsign_address(source_callsign, is_last=True)
    return dest_addr + src_addr + bytes([0x03, 0xF0]) + info


def tnc2_line(*, source_callsign: str, dest_callsign: str, info: str) -> str:
    """The human-readable ORIGEN>DESTINO:contenido form — used only for
    logging/audit-event readability, matching what CONTEXT.md field-
    validated via kissutil; not what actually goes on the wire (that's
    build_ui_frame's raw bytes)."""
    return f"{source_callsign}>{dest_callsign}:{info}"


class KissTcpClient:
    """Lazy-connect, cached socket — mirrors dispatcher.mq_publisher.
    _get_client's cache-and-reconnect-on-failure idiom. send_ui_frame
    never raises: logs and returns False on any socket error, and resets
    the cached connection so the next call reconnects fresh."""

    def __init__(self, host: str, port: int, *, connect_timeout: float = 5.0, kiss_port: int = 0):
        self._host = host
        self._port = port
        self._connect_timeout = connect_timeout
        self._kiss_port = kiss_port
        self._sock: socket.socket | None = None

    def _get_socket(self) -> socket.socket:
        if self._sock is not None:
            return self._sock
        sock = socket.create_connection((self._host, self._port), timeout=self._connect_timeout)
        self._sock = sock
        return sock

    def send_ui_frame(self, *, source_callsign: str, dest_callsign: str, info: bytes) -> bool:
        try:
            frame = build_ui_frame(
                source_callsign=source_callsign, dest_callsign=dest_callsign, info=info
            )
            payload = kiss_frame(frame, port=self._kiss_port)
            sock = self._get_socket()
            sock.sendall(payload)
            return True
        except OSError:
            logger.error(
                "kiss: failed to send frame to %s:%d", self._host, self._port, exc_info=True
            )
            self._reset()
            return False

    def _reset(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    def close(self) -> None:
        self._reset()
