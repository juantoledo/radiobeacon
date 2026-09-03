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


def test_items_with_voice_clips_one_listing(conn, tmp_path):
    _point_wav_dir_at(conn, tmp_path)
    (tmp_path / "csn-has-audio-1788400001.wav").write_bytes(b"RIFF")
    (tmp_path / "csn-frame-only-0-1788400002.wav").write_bytes(b"RIFF")

    items = [
        {"source": "csn", "item_id": "has-audio"},
        {"source": "csn", "item_id": "frame-only"},
        {"source": "csn", "item_id": "nothing"},
    ]
    assert beacon_audio.items_with_voice_clips(conn, items) == {("csn", "has-audio")}


# --- route tests ---


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


def test_dashboard_shows_play_button_only_for_items_with_audio(client, conn, tmp_path):
    _mk_item(conn, "csn", "with-audio")
    _mk_item(conn, "csn", "without-audio")
    _point_wav_dir_at(conn, tmp_path)
    (tmp_path / "csn-with-audio-1788400001.wav").write_bytes(b"RIFF")

    body = client.get("/").text
    assert 'data-audio-url="/items/csn/with-audio/audio"' in body
    assert 'data-audio-url="/items/csn/without-audio/audio"' not in body
