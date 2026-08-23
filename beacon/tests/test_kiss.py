import socket
import threading
import time

import pytest

from beacon.kiss import (
    FEND,
    FESC,
    KissTcpClient,
    build_ui_frame,
    encode_callsign_address,
    kiss_escape,
    kiss_frame,
    kiss_unescape,
    tnc2_line,
)

# --- KISS escaping ---


def test_kiss_escape_passes_through_ordinary_bytes():
    assert kiss_escape(b"hello") == b"hello"


def test_kiss_escape_fend_byte():
    assert kiss_escape(bytes([FEND])) == bytes([FESC, 0xDC])


def test_kiss_escape_fesc_byte():
    assert kiss_escape(bytes([FESC])) == bytes([FESC, 0xDD])


def test_kiss_escape_unescape_roundtrip():
    original = bytes([0x01, FEND, 0x02, FESC, 0x03, FEND, FESC])
    assert kiss_unescape(kiss_escape(original)) == original


def test_kiss_frame_starts_and_ends_with_fend():
    frame = kiss_frame(b"payload", port=0)
    assert frame[0] == FEND
    assert frame[-1] == FEND


def test_kiss_frame_command_byte_encodes_port():
    frame = kiss_frame(b"x", port=2)
    # byte after the leading FEND is the command byte: high nibble = port
    assert frame[1] == (2 << 4)


def test_kiss_frame_escapes_payload_containing_fend():
    frame = kiss_frame(bytes([FEND]), port=0)
    inner = frame[2:-1]  # strip leading FEND+command byte and trailing FEND
    assert inner == bytes([FESC, 0xDC])


# --- AX.25 address encoding — reference byte sequences computed
# independently of the implementation (not by calling it) ---


def test_encode_callsign_address_matches_reference_bytes_not_last():
    assert encode_callsign_address("TEST-1", is_last=False).hex() == "a88aa6a84040e2"


def test_encode_callsign_address_matches_reference_bytes_last():
    assert encode_callsign_address("TEST-1", is_last=True).hex() == "a88aa6a84040e3"


def test_encode_callsign_address_default_ssid_zero():
    assert encode_callsign_address("WXALRT", is_last=True).hex() == "aeb08298a4a8e1"


def test_encode_callsign_address_ssid_15_single_char_callsign():
    assert encode_callsign_address("X-15", is_last=True).hex() == "b04040404040ff"


def test_encode_callsign_address_seven_bytes_long():
    assert len(encode_callsign_address("CD3DXZ-1", is_last=False)) == 7


def test_encode_callsign_address_rejects_ssid_out_of_range():
    with pytest.raises(ValueError):
        encode_callsign_address("TEST-16", is_last=True)


def test_encode_callsign_address_rejects_callsign_too_long():
    with pytest.raises(ValueError):
        encode_callsign_address("TOOLONGCALL", is_last=True)


def test_encode_callsign_address_uppercases_input():
    assert encode_callsign_address("test-1", is_last=True) == encode_callsign_address(
        "TEST-1", is_last=True
    )


# --- UI frame assembly ---


def test_build_ui_frame_structure():
    frame = build_ui_frame(source_callsign="CD3DXZ-1", dest_callsign="WXALRT", info=b"hello")

    dest_addr = frame[0:7]
    src_addr = frame[7:14]
    control = frame[14]
    pid = frame[15]
    info = frame[16:]

    assert dest_addr == encode_callsign_address("WXALRT", is_last=False)
    assert src_addr == encode_callsign_address("CD3DXZ-1", is_last=True)
    assert control == 0x03
    assert pid == 0xF0
    assert info == b"hello"


def test_tnc2_line_format():
    assert tnc2_line(source_callsign="CD3DXZ-1", dest_callsign="WXALRT", info="hola") == (
        "CD3DXZ-1>WXALRT:hola"
    )


# --- KissTcpClient against a real fake TCP server ---


class _FakeKissServer:
    """A real TCP server on 127.0.0.1 (OS-assigned port) standing in for
    Direwolf's KISS port — no real Direwolf needed."""

    def __init__(self):
        self.received: list[bytes] = []
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(1)
        self.port = self._sock.getsockname()[1]
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._thread.start()

    def _accept_loop(self):
        try:
            conn, _addr = self._sock.accept()
        except OSError:
            return
        with conn:
            conn.settimeout(2)
            while True:
                try:
                    chunk = conn.recv(4096)
                except OSError:
                    break
                if not chunk:
                    break
                self.received.append(chunk)

    def close(self):
        self._sock.close()


@pytest.fixture
def fake_server():
    server = _FakeKissServer()
    yield server
    server.close()


def test_send_ui_frame_delivers_expected_bytes(fake_server):
    client = KissTcpClient("127.0.0.1", fake_server.port, connect_timeout=2)

    ok = client.send_ui_frame(source_callsign="CD3DXZ-1", dest_callsign="WXALRT", info=b"test payload")
    client.close()
    time.sleep(0.1)  # let the server thread's recv() catch up

    assert ok is True
    received = b"".join(fake_server.received)
    assert received[0] == FEND
    assert received[-1] == FEND
    inner = kiss_unescape(received[2:-1])
    expected_frame = build_ui_frame(
        source_callsign="CD3DXZ-1", dest_callsign="WXALRT", info=b"test payload"
    )
    assert inner == expected_frame


def test_send_ui_frame_reuses_cached_connection(fake_server):
    client = KissTcpClient("127.0.0.1", fake_server.port, connect_timeout=2)

    client.send_ui_frame(source_callsign="A", dest_callsign="B", info=b"1")
    sock_after_first = client._sock
    client.send_ui_frame(source_callsign="A", dest_callsign="B", info=b"2")

    assert client._sock is sock_after_first
    client.close()


def test_send_ui_frame_returns_false_on_connection_refused():
    # Nothing listening on this port -- connection should fail cleanly.
    client = KissTcpClient("127.0.0.1", 1, connect_timeout=1)

    ok = client.send_ui_frame(source_callsign="A", dest_callsign="B", info=b"x")

    assert ok is False


def test_send_ui_frame_never_raises_on_failure():
    client = KissTcpClient("127.0.0.1", 1, connect_timeout=1)
    # Should not raise even after a failure -- caller relies on this.
    client.send_ui_frame(source_callsign="A", dest_callsign="B", info=b"x")
    client.send_ui_frame(source_callsign="A", dest_callsign="B", info=b"y")
