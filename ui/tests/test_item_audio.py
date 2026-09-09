"""Per-item bulletin audio: ui.beacon_audio + the /items/{source}/{item_id}/audio
route + the dashboard's play button."""
import sqlite3

from adapters.storage import set_setting

from ui import beacon_audio


def _mk_item(conn: sqlite3.Connection, source: str, item_id: str) -> None:
    conn.execute(
        "INSERT INTO items (source, item_id, extracted_title, fetched_at, rawdata) "
        "VALUES (?, ?, 'Title', datetime('now'), '{}')",
        (source, item_id),
    )
    conn.commit()


def _point_wav_dir_at(conn: sqlite3.Connection, path) -> None:
    set_setting(conn, "BEACON_TTS_WAV_DIR", str(path))


# --- ui.beacon_audio unit tests ---


def test_latest_voice_clip_none_when_dir_missing(conn, tmp_path):
    _point_wav_dir_at(conn, tmp_path / "nope")
    assert beacon_audio.latest_voice_clip(conn, "csn", "2026-09-02T20:02:06") is None


def test_latest_voice_clip_picks_newest_and_ignores_frame_clips(conn, tmp_path):
    _point_wav_dir_at(conn, tmp_path)
    (tmp_path / "csn-item1-1788395938.wav").write_bytes(b"RIFF-old")
    newest = tmp_path / "csn-item1-1788399999.wav"
    newest.write_bytes(b"RIFF-new")
    # frame clip for the same item — must never be chosen
    (tmp_path / "csn-item1-0-1788400000.wav").write_bytes(b"RIFF-frame")
    # different item
    (tmp_path / "csn-item2-1788400001.wav").write_bytes(b"RIFF-other")

    assert beacon_audio.latest_voice_clip(conn, "csn", "item1") == newest


def test_latest_voice_clip_rejects_path_traversal(conn, tmp_path):
    _point_wav_dir_at(conn, tmp_path)
    assert beacon_audio.latest_voice_clip(conn, "..", "etc/passwd") is None
    assert beacon_audio.latest_voice_clip(conn, "csn", "../../secret") is None


# --- list_item_transmissions / item_clip_path (audit-driven) ---


def _tx_event(conn, source, item_id, event_type, details):
    import json as _json

    conn.execute(
        "INSERT INTO audit_log (event_type, actor, source, item_id, details, recorded_at) "
        "VALUES (?, 'beacon', ?, ?, ?, datetime('now'))",
        (event_type, source, item_id, _json.dumps(details)),
    )
    conn.commit()


def test_list_item_transmissions_newest_first_voice_and_frame(conn, tmp_path):
    _point_wav_dir_at(conn, tmp_path)
    (tmp_path / "csn-i1-100.wav").write_bytes(b"RIFF")
    (tmp_path / "csn-i1-0-200.wav").write_bytes(b"RIFF")
    _tx_event(conn, "csn", "i1", "beacon.voice.transmitted", {"truncated": True, "clip": "csn-i1-100.wav"})
    _tx_event(conn, "csn", "i1", "beacon.frame.transmitted",
              {"byte_length": 88, "ref": 0, "clip": "csn-i1-0-200.wav"})

    txs = beacon_audio.list_item_transmissions(conn, "csn", "i1")

    assert [t["kind"] for t in txs] == ["frame", "voice"]  # newest (id DESC) first
    assert txs[0]["ref"] == 0 and txs[0]["byte_length"] == 88
    assert txs[0]["url"] == "/items/csn/i1/audio/csn-i1-0-200.wav"
    assert txs[1]["truncated"] is True


def test_list_item_transmissions_skips_rows_whose_clip_is_gone(conn, tmp_path):
    _point_wav_dir_at(conn, tmp_path)
    (tmp_path / "csn-i1-100.wav").write_bytes(b"RIFF")
    _tx_event(conn, "csn", "i1", "beacon.voice.transmitted", {"clip": "csn-i1-100.wav"})
    _tx_event(conn, "csn", "i1", "beacon.voice.transmitted", {"clip": "csn-i1-999.wav"})  # no file

    txs = beacon_audio.list_item_transmissions(conn, "csn", "i1")
    assert [t["name"] for t in txs] == ["csn-i1-100.wav"]


def test_list_item_transmissions_ignores_other_items_clip_names(conn, tmp_path):
    _point_wav_dir_at(conn, tmp_path)
    (tmp_path / "csn-other-100.wav").write_bytes(b"RIFF")
    _tx_event(conn, "csn", "i1", "beacon.voice.transmitted", {"clip": "csn-other-100.wav"})
    assert beacon_audio.list_item_transmissions(conn, "csn", "i1") == []


def test_item_transmission_counts_grouped(conn, tmp_path):
    _point_wav_dir_at(conn, tmp_path)
    _tx_event(conn, "csn", "a", "beacon.voice.transmitted", {"clip": "csn-a-1.wav"})
    _tx_event(conn, "csn", "a", "beacon.voice.transmitted", {"clip": "csn-a-2.wav"})
    _tx_event(conn, "csn", "b", "beacon.frame.transmitted", {"clip": "csn-b-0-1.wav"})

    counts = beacon_audio.item_transmission_counts(
        conn, [{"source": "csn", "item_id": "a"}, {"source": "csn", "item_id": "b"}]
    )
    assert counts == {("csn", "a"): 2, ("csn", "b"): 1}


def test_item_clip_path_rejects_traversal_and_wrong_item(conn, tmp_path):
    _point_wav_dir_at(conn, tmp_path)
    (tmp_path / "csn-i1-100.wav").write_bytes(b"RIFF")
    assert beacon_audio.item_clip_path(conn, "csn", "i1", "csn-i1-100.wav") is not None
    assert beacon_audio.item_clip_path(conn, "csn", "i1", "../secret.wav") is None
    assert beacon_audio.item_clip_path(conn, "csn", "i1", "csn-other-100.wav") is None
    assert beacon_audio.item_clip_path(conn, "csn", "i1", "csn-i1-100.txt") is None


# --- route tests ---


def test_clip_audio_route_streams_and_404s(client, conn, tmp_path):
    _mk_item(conn, "csn", "i1")
    _point_wav_dir_at(conn, tmp_path)
    (tmp_path / "csn-i1-0-200.wav").write_bytes(b"RIFF-frame-bytes")

    ok = client.get("/items/csn/i1/audio/csn-i1-0-200.wav")
    assert ok.status_code == 200 and ok.content == b"RIFF-frame-bytes"
    assert ok.headers["content-type"] == "audio/wav"

    assert client.get("/items/csn/i1/audio/csn-i1-0-999.wav").status_code == 404
    assert client.get("/items/csn/i1/audio/..%2Fx.wav").status_code in (404, 400)


def test_item_page_shows_transmissions_section(client, conn, tmp_path):
    _mk_item(conn, "csn", "i1")
    _point_wav_dir_at(conn, tmp_path)
    (tmp_path / "csn-i1-100.wav").write_bytes(b"RIFF")
    _tx_event(conn, "csn", "i1", "beacon.voice.transmitted", {"clip": "csn-i1-100.wav"})

    body = client.get("/items/csn/i1").text
    assert 'data-audio-url="/items/csn/i1/audio/csn-i1-100.wav"' in body
    assert 'id="beacon-audio"' in body


def test_audio_route_404_without_clip(client, conn):
    _mk_item(conn, "csn", "item1")
    resp = client.get("/items/csn/item1/audio")
    assert resp.status_code == 404


def test_audio_route_streams_wav(client, conn, tmp_path):
    _mk_item(conn, "csn", "item1")
    _point_wav_dir_at(conn, tmp_path)
    (tmp_path / "csn-item1-1788400001.wav").write_bytes(b"RIFFfake-wav-bytes")

    resp = client.get("/items/csn/item1/audio")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "audio/wav"
    assert resp.content == b"RIFFfake-wav-bytes"


def test_dashboard_play_button_and_xN_disclosure(client, conn, tmp_path):
    _mk_item(conn, "csn", "aired")
    _mk_item(conn, "csn", "silent")
    _point_wav_dir_at(conn, tmp_path)
    (tmp_path / "csn-aired-1.wav").write_bytes(b"RIFF")
    (tmp_path / "csn-aired-2.wav").write_bytes(b"RIFF")
    _tx_event(conn, "csn", "aired", "beacon.voice.transmitted", {"clip": "csn-aired-1.wav"})
    _tx_event(conn, "csn", "aired", "beacon.voice.transmitted", {"clip": "csn-aired-2.wav"})

    body = client.get("/").text
    assert 'data-audio-url="/items/csn/aired/audio"' in body  # primary button
    assert 'data-audio-url="/items/csn/aired/audio/csn-aired-2.wav"' in body  # per-airing
    assert "&times;2" in body  # the disclosure chip
    assert 'data-audio-url="/items/csn/silent/audio"' not in body

