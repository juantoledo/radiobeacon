"""Validate, normalize, and store an admin-uploaded brand logo.

Unlike every other admin-supplied binary in this repo (e.g. beacon TTS WAV
clips, which live on disk with only a filename in the DB — see
ui.beacon_audio), the logo is stored as a DB blob via
adapters.storage.brand_assets: there's only ever one row, so the usual
"don't put large binaries in the hot WAL path" concern that shapes the
audio-clip convention doesn't apply here, and it means a DB backup
captures branding automatically.

No in-browser crop/resize editor — this module does the only
normalization the feature needs: validate it's really a raster image,
cap+downscale its dimensions, and re-encode to a single consistent format
(PNG, so transparency survives when the source has it). SVG uploads are
rejected outright, not because SVG can't be branded with, but because
Pillow can't decode/validate/resize a vector format, and hand-rolling SVG
sanitization to safely accept arbitrary operator-supplied markup is a real
XSS surface not worth opening for this feature.
"""
import hashlib
import io

from PIL import Image, UnidentifiedImageError
from starlette.responses import Response

MAX_UPLOAD_BYTES = 2 * 1024 * 1024  # 2 MB, checked before any decode
MAX_DIMENSION = 512  # longest side, after downscaling; keeps the DB row small
_ALLOWED_FORMATS = {"PNG", "JPEG", "WEBP"}


class LogoUploadError(Exception):
    """A human-readable reason the upload was rejected — every branch below
    raises this instead of letting a Pillow/OS exception surface raw, so
    the route handler can show the operator something actionable."""


def _normalize(raw_bytes: bytes) -> tuple[bytes, int, int]:
    """Decodes, validates, downscales, and re-encodes `raw_bytes` to PNG.
    Returns (png_bytes, width, height). Raises LogoUploadError for anything
    that isn't a real, decodable PNG/JPEG/WEBP image."""
    if len(raw_bytes) > MAX_UPLOAD_BYTES:
        raise LogoUploadError(
            f"image is too large ({len(raw_bytes) // 1024} KB) — the limit is "
            f"{MAX_UPLOAD_BYTES // 1024} KB"
        )
    try:
        image = Image.open(io.BytesIO(raw_bytes))
        image.load()  # Image.open is lazy; this is where a truncated/corrupt file actually fails
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise LogoUploadError("that doesn't look like a valid image file") from exc

    if image.format not in _ALLOWED_FORMATS:
        raise LogoUploadError(
            f"unsupported image format {image.format!r} — upload a PNG, JPEG, or WebP "
            "(SVG isn't supported)"
        )

    # Preserve transparency when the source has it; anything else (e.g. a
    # plain JPEG) flattens to opaque RGB, which is correct, not a bug.
    if image.mode not in ("RGBA", "RGB"):
        image = image.convert("RGBA" if "A" in image.getbands() else "RGB")

    if image.width > MAX_DIMENSION or image.height > MAX_DIMENSION:
        image.thumbnail((MAX_DIMENSION, MAX_DIMENSION), Image.LANCZOS)

    out = io.BytesIO()
    image.save(out, format="PNG")  # re-encoding also strips EXIF/metadata
    return out.getvalue(), image.width, image.height


def save_logo(conn, raw_bytes: bytes, *, actor: str) -> None:
    """Validates and stores `raw_bytes` as the instance's logo. Raises
    LogoUploadError (safe to show the operator directly) on anything
    invalid; does nothing else on failure — the previous logo, if any,
    is left untouched."""
    from adapters.storage import set_brand_asset

    png_bytes, width, height = _normalize(raw_bytes)
    checksum = hashlib.sha256(png_bytes).hexdigest()
    set_brand_asset(
        conn,
        "logo",
        content=png_bytes,
        content_type="image/png",
        width=width,
        height=height,
        checksum=checksum,
        actor=actor,
    )


def remove_logo(conn, *, actor: str) -> bool:
    from adapters.storage import delete_brand_asset

    return delete_brand_asset(conn, "logo", actor=actor)


def logo_response(conn) -> Response | None:
    """The stored logo as an HTTP response, or None if unset (the caller
    turns that into a 404 — see routers/branding.py). Cache-Control is
    long-lived because the URL itself is checksum-versioned (see
    templating.logo_url): a re-upload changes the query string, so a
    stale cached response for the old URL is never served as the new
    logo."""
    from adapters.storage import get_brand_asset

    asset = get_brand_asset(conn, "logo")
    if asset is None:
        return None
    return Response(
        asset["content"],
        media_type=asset["content_type"],
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )
