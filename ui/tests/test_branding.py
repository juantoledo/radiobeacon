"""ui.branding (validate/normalize/store) + routers/branding.py (HTTP)."""
import io
import sqlite3

import pytest
from adapters.storage import get_brand_asset
from fastapi import Request
from fastapi.testclient import TestClient
from PIL import Image

from ui import branding


def _png_bytes(size=(64, 48), mode="RGBA", color=(200, 30, 30, 128)):
    buf = io.BytesIO()
    Image.new(mode, size, color).save(buf, format="PNG")
    return buf.getvalue()


def _jpeg_bytes(size=(64, 48)):
    buf = io.BytesIO()
    Image.new("RGB", size, (10, 20, 30)).save(buf, format="JPEG")
    return buf.getvalue()


def _webp_bytes(size=(64, 48)):
    buf = io.BytesIO()
    Image.new("RGBA", size, (5, 5, 5, 200)).save(buf, format="WEBP")
    return buf.getvalue()


@pytest.fixture
def real_login_client(conn: sqlite3.Connection):
    """A TestClient with only get_db overridden — get_current_user runs for
    real (no session cookie set), so this proves a route is reachable with
    genuinely no auth, the same idiom test_auth_routes.py's own
    real_login_client uses."""
    from ui.app import app
    from ui.db import get_db

    def _override(request: Request):
        request.state.db_conn = conn
        yield conn

    app.dependency_overrides[get_db] = _override
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(get_db, None)


# --- ui.branding._normalize / save_logo (no DB round-trip) ---


def test_save_logo_stores_png_with_correct_dimensions(conn):
    branding.save_logo(conn, _png_bytes(size=(64, 48)), actor="test")

    asset = get_brand_asset(conn, "logo")
    assert asset is not None
    assert asset["content_type"] == "image/png"
    assert asset["width"] == 64
    assert asset["height"] == 48
    assert Image.open(io.BytesIO(asset["content"])).format == "PNG"


def test_save_logo_accepts_jpeg_and_webp_reencoding_to_png(conn):
    branding.save_logo(conn, _jpeg_bytes(), actor="test")
    assert get_brand_asset(conn, "logo")["content_type"] == "image/png"

    branding.save_logo(conn, _webp_bytes(), actor="test")
    assert get_brand_asset(conn, "logo")["content_type"] == "image/png"


def test_save_logo_downscales_oversized_dimensions_preserving_aspect_ratio(conn):
    branding.save_logo(conn, _png_bytes(size=(1200, 600)), actor="test")

    asset = get_brand_asset(conn, "logo")
    assert asset["width"] <= branding.MAX_DIMENSION
    assert asset["height"] <= branding.MAX_DIMENSION
    assert asset["width"] / asset["height"] == pytest.approx(1200 / 600, rel=0.02)


def test_save_logo_preserves_transparency(conn):
    branding.save_logo(conn, _png_bytes(mode="RGBA", color=(1, 2, 3, 40)), actor="test")

    asset = get_brand_asset(conn, "logo")
    decoded = Image.open(io.BytesIO(asset["content"]))
    assert decoded.mode == "RGBA"
    assert decoded.getpixel((0, 0))[3] == 40


def test_save_logo_rejects_oversized_upload(conn):
    huge = b"\x00" * (branding.MAX_UPLOAD_BYTES + 1)

    with pytest.raises(branding.LogoUploadError, match="too large"):
        branding.save_logo(conn, huge, actor="test")

    assert get_brand_asset(conn, "logo") is None


def test_save_logo_rejects_non_image_bytes(conn):
    with pytest.raises(branding.LogoUploadError, match="valid image"):
        branding.save_logo(conn, b"not an image, just plain text bytes", actor="test")


def test_save_logo_rejects_svg(conn):
    svg = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'

    with pytest.raises(branding.LogoUploadError, match="unsupported|valid image"):
        branding.save_logo(conn, svg, actor="test")

    assert get_brand_asset(conn, "logo") is None


def test_save_logo_re_upload_changes_checksum(conn):
    branding.save_logo(conn, _png_bytes(color=(10, 10, 10, 255)), actor="test")
    first = get_brand_asset(conn, "logo")["checksum"]

    branding.save_logo(conn, _png_bytes(color=(200, 200, 200, 255)), actor="test")
    second = get_brand_asset(conn, "logo")["checksum"]

    assert first != second


def test_remove_logo(conn):
    branding.save_logo(conn, _png_bytes(), actor="test")
    assert get_brand_asset(conn, "logo") is not None

    removed = branding.remove_logo(conn, actor="test")

    assert removed is True
    assert get_brand_asset(conn, "logo") is None


def test_remove_logo_when_none_set_is_a_no_op(conn):
    assert branding.remove_logo(conn, actor="test") is False


# --- HTTP routes ---


def test_get_brand_logo_404s_when_unset(client):
    response = client.get("/brand/logo")

    assert response.status_code == 404


def test_get_brand_logo_serves_stored_image(client, conn):
    branding.save_logo(conn, _png_bytes(size=(32, 32)), actor="test")

    response = client.get("/brand/logo")

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert Image.open(io.BytesIO(response.content)).size == (32, 32)


def test_get_brand_logo_reachable_with_no_auth_at_all(real_login_client, conn):
    """The whole point of the public/admin router split: this must not
    redirect to /login or 401/403 — the login page itself needs this
    route before anyone is authenticated."""
    branding.save_logo(conn, _png_bytes(), actor="test")

    response = real_login_client.get("/brand/logo", follow_redirects=False)

    assert response.status_code == 200


def test_upload_action_requires_admin(user_client):
    response = user_client.post(
        "/config/branding",
        files={"logo": ("logo.png", _png_bytes(), "image/png")},
        follow_redirects=False,
    )

    assert response.status_code == 403


def test_upload_action_stores_and_redirects(client, conn):
    response = client.post(
        "/config/branding",
        files={"logo": ("logo.png", _png_bytes(), "image/png")},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/config/branding?msg=logo+updated"
    assert get_brand_asset(conn, "logo") is not None


def test_upload_action_rejects_invalid_image_without_storing(client, conn):
    response = client.post(
        "/config/branding",
        files={"logo": ("not-a-logo.txt", b"hello", "text/plain")},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert "error=" in response.headers["location"]
    assert get_brand_asset(conn, "logo") is None


def test_remove_action_requires_admin(user_client):
    response = user_client.post("/config/branding/remove", follow_redirects=False)

    assert response.status_code == 403


def test_remove_action_clears_stored_logo(client, conn):
    branding.save_logo(conn, _png_bytes(), actor="test")

    response = client.post("/config/branding/remove", follow_redirects=False)

    assert response.status_code == 303
    assert get_brand_asset(conn, "logo") is None


def test_branding_page_shows_upload_form(client):
    response = client.get("/config/branding")

    assert response.status_code == 200
    assert 'enctype="multipart/form-data"' in response.text
    assert 'name="logo"' in response.text


def test_base_template_renders_logo_img_when_set(client, conn):
    branding.save_logo(conn, _png_bytes(), actor="test")

    response = client.get("/items")

    assert 'class="brand-logo"' in response.text
    assert "/brand/logo?v=" in response.text


def test_base_template_falls_back_to_dot_when_unset(client):
    response = client.get("/items")

    assert '<span class="dot"></span>radiobeacon' in response.text
    assert "brand-logo" not in response.text


def test_dashboard_shows_watermark_when_logo_set(client, conn):
    branding.save_logo(conn, _png_bytes(), actor="test")

    response = client.get("/")

    assert 'class="dashboard-watermark"' in response.text
    assert "/brand/logo?v=" in response.text


def test_dashboard_omits_watermark_when_no_logo_set(client):
    response = client.get("/")

    assert "dashboard-watermark" not in response.text


def test_watermark_does_not_appear_on_other_pages(client, conn):
    branding.save_logo(conn, _png_bytes(), actor="test")

    response = client.get("/items")

    assert "dashboard-watermark" not in response.text


def test_setup_wizard_branding_step_is_last_and_optional(fresh_install_client):
    response = fresh_install_client.get("/setup/branding")

    assert response.status_code == 200
    assert "Branding" in response.text
    assert 'href="/setup/finish"' in response.text  # the skip link's target


def test_setup_wizard_branding_upload_advances_to_finish_and_takes_effect(
    fresh_install_client,
):
    response = fresh_install_client.post(
        "/setup/branding",
        files={"logo": ("logo.png", _png_bytes(size=(200, 100)), "image/png")},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/setup/finish"

    dashboard = fresh_install_client.get("/")
    assert 'class="brand-logo"' in dashboard.text


def test_branding_page_includes_crop_editor(client):
    response = client.get("/config/branding")

    assert "data-logo-editor" in response.text
    assert 'class="logo-editor-canvas"' in response.text
    assert 'class="logo-editor-zoom"' in response.text
    assert "logo-editor.js" in response.text


def test_setup_wizard_branding_step_includes_crop_editor(fresh_install_client):
    response = fresh_install_client.get("/setup/branding")

    assert "data-logo-editor" in response.text
    assert 'class="logo-editor-canvas"' in response.text
    assert "logo-editor.js" in response.text
