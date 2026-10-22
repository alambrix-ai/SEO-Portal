"""Safe image intake: storage backends, raster uploads and SVG sanitising.

Most of this runs without a database: :func:`clean_upload`,
:func:`normalise_raster` and :func:`sanitise_svg` are pure, and the quota
check is exercised against a stand-in session. The database tests at the end
prove the bytes round-trip and that another workspace cannot read them.
"""
from __future__ import annotations

import hashlib
import io
import subprocess
from typing import Any

import pytest
from PIL import Image

from app.core.config import settings
from app.core.exceptions import NotFoundError
from app.models.creative import FileKind, FileSource, StoredFile
from app.services import uploads
from app.services.creative import svg
from app.services.creative.svg import SvgRejected, sanitise_svg
from app.services.storage import (
    PostgresStorage,
    S3Storage,
    StorageNotConfigured,
    backend_named,
    get_storage,
)
from app.services.uploads import (
    StorageFull,
    UploadRejected,
    clean_upload,
    display_name_for,
    normalise_raster,
)
from tests.conftest import requires_db

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


# ── Helpers ────────────────────────────────────────────────────────────────
def _image_bytes(fmt: str, size: tuple[int, int] = (16, 12), mode: str = "RGB", **save: Any) -> bytes:  # noqa: ANN401
    out = io.BytesIO()
    Image.new(mode, size, "red" if mode != "1" else 1).save(out, format=fmt, **save)
    return out.getvalue()


def _animated(fmt: str) -> bytes:
    frames = [Image.new("RGB", (8, 8), colour) for colour in ("red", "blue")]
    out = io.BytesIO()
    frames[0].save(out, format=fmt, save_all=True, append_images=frames[1:], duration=100, loop=0)
    return out.getvalue()


def _reason(exc: pytest.ExceptionInfo[UploadRejected]) -> str:
    return exc.value.details["reason"]


SIMPLE_SVG = (
    b'<?xml version="1.0" encoding="UTF-8"?>\n'
    b'<svg xmlns="http://www.w3.org/2000/svg" width="200" height="100" viewBox="0 0 200 100">'
    b'<defs><linearGradient id="g"><stop offset="0" stop-color="#ff0000"/>'
    b'<stop offset="1" stop-color="#0000ff"/></linearGradient></defs>'
    b'<rect width="200" height="100" fill="url(#g)"/>'
    b'<circle cx="50" cy="50" r="20" fill="#00ff00"/></svg>'
)


def _svg(body: str, *, head: str = "") -> bytes:
    return (
        f'{head}<svg xmlns="http://www.w3.org/2000/svg" '
        f'xmlns:xlink="http://www.w3.org/1999/xlink" viewBox="0 0 10 10">{body}</svg>'
    ).encode()


# ── Raster uploads: happy path ─────────────────────────────────────────────
@pytest.mark.parametrize(
    ("filename", "fmt", "mime"),
    [
        ("logo.png", "PNG", "image/png"),
        ("photo.JPG", "JPEG", "image/jpeg"),
        ("photo.jpeg", "JPEG", "image/jpeg"),
        ("hero.webp", "WEBP", "image/webp"),
    ],
)
def test_a_real_image_is_accepted_and_hashed(filename: str, fmt: str, mime: str) -> None:
    cleaned = clean_upload(filename, _image_bytes(fmt))
    assert cleaned.mime == mime
    assert (cleaned.width, cleaned.height) == (16, 12)
    assert cleaned.sha256 == hashlib.sha256(cleaned.data).hexdigest()
    assert uploads.sniff(cleaned.data) == mime


def test_reencoding_strips_exif_gps_and_icc_profile() -> None:
    exif = Image.Exif()
    exif[0x010F] = "SecretCam"  # Make
    exif[0x8825] = {2: (51.0, 30.0, 0.0)}  # GPSInfo: latitude
    icc = b"\x00" * 128
    original = _image_bytes("JPEG", exif=exif.tobytes(), icc_profile=icc)
    with Image.open(io.BytesIO(original)) as before:
        assert before.info.get("exif") and before.info.get("icc_profile")

    cleaned = clean_upload("photo.jpg", original)
    with Image.open(io.BytesIO(cleaned.data)) as after:
        assert "exif" not in after.info
        assert "icc_profile" not in after.info
        assert len(after.getexif()) == 0
    assert b"SecretCam" not in cleaned.data


def test_png_text_chunks_are_not_carried_over() -> None:
    from PIL import PngImagePlugin

    meta = PngImagePlugin.PngInfo()
    meta.add_text("Author", "private-name")
    cleaned = clean_upload("logo.png", _image_bytes("PNG", pnginfo=meta))
    assert b"private-name" not in cleaned.data


def test_a_phone_jpeg_with_a_preview_frame_is_accepted() -> None:
    frames = [Image.new("RGB", (8, 8), "red"), Image.new("RGB", (4, 4), "blue")]
    out = io.BytesIO()
    frames[0].save(out, format="MPO", save_all=True, append_images=frames[1:])
    cleaned = clean_upload("phone.jpg", out.getvalue())
    assert cleaned.mime == "image/jpeg"
    assert (cleaned.width, cleaned.height) == (8, 8)


# ── Raster uploads: refusals ───────────────────────────────────────────────
def test_a_png_named_jpg_is_refused_as_a_mismatch() -> None:
    with pytest.raises(UploadRejected) as exc:
        clean_upload("photo.jpg", _image_bytes("PNG"))
    assert _reason(exc) == "mismatch"


def test_a_script_named_png_is_refused() -> None:
    with pytest.raises(UploadRejected) as exc:
        clean_upload("logo.png", b"#!/bin/sh\nrm -rf /\n")
    assert _reason(exc) == "unsupported"


def test_an_svg_named_png_is_refused_as_a_mismatch() -> None:
    with pytest.raises(UploadRejected) as exc:
        clean_upload("logo.png", SIMPLE_SVG)
    assert _reason(exc) == "mismatch"


@pytest.mark.parametrize("filename", ["anim.gif", "anim.png"])
def test_a_gif_is_refused_whatever_it_is_called(filename: str) -> None:
    with pytest.raises(UploadRejected) as exc:
        clean_upload(filename, _image_bytes("GIF"))
    assert _reason(exc) == "gif"


@pytest.mark.parametrize(("filename", "fmt"), [("anim.png", "PNG"), ("anim.webp", "WEBP")])
def test_an_animated_image_is_refused(filename: str, fmt: str) -> None:
    with pytest.raises(UploadRejected) as exc:
        clean_upload(filename, _animated(fmt))
    assert _reason(exc) == "animated"


def test_a_truncated_image_is_refused() -> None:
    # Noise so the pixel data cannot compress into the first few bytes.
    noisy = Image.frombytes("RGB", (200, 200), bytes(range(256)) * 468 + bytes(192))
    out = io.BytesIO()
    noisy.save(out, format="PNG")
    whole = out.getvalue()
    with pytest.raises(UploadRejected) as exc:
        clean_upload("logo.png", whole[: len(whole) // 2])
    assert _reason(exc) == "damaged"


def test_a_decompression_bomb_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """A tiny file declaring far more pixels than the cap (Pillow's hard error)."""
    monkeypatch.setattr(settings, "creative_upload_max_pixels", 1_000_000)
    bomb = _image_bytes("PNG", size=(5000, 5000), mode="1")
    assert len(bomb) < 100_000
    with pytest.raises(UploadRejected) as exc:
        clean_upload("bomb.png", bomb)
    assert _reason(exc) == "too_many_pixels"


def test_an_image_just_over_the_pixel_cap_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """Between 1x and 2x the cap Pillow only warns; the warning is an error here."""
    monkeypatch.setattr(settings, "creative_upload_max_pixels", 1_000_000)
    with pytest.raises(UploadRejected) as exc:
        clean_upload("big.png", _image_bytes("PNG", size=(1100, 1000), mode="1"))
    assert _reason(exc) == "too_many_pixels"


def test_a_file_over_the_byte_cap_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    data = _image_bytes("PNG")
    monkeypatch.setattr(settings, "creative_upload_max_bytes", len(data) - 1)
    with pytest.raises(UploadRejected) as exc:
        clean_upload("logo.png", data)
    assert _reason(exc) == "too_large"
    assert "MB" in exc.value.message


@pytest.mark.parametrize("filename", ["logo.bmp", "logo", "logo.png.exe", ".png"])
def test_an_unsupported_extension_is_refused(filename: str) -> None:
    with pytest.raises(UploadRejected) as exc:
        clean_upload(filename, _image_bytes("PNG"))
    assert _reason(exc) == "unsupported"


def test_an_empty_file_and_an_overlong_name_are_refused() -> None:
    with pytest.raises(UploadRejected) as exc:
        clean_upload("logo.png", b"")
    assert _reason(exc) == "empty"
    with pytest.raises(UploadRejected) as exc:
        clean_upload("a" * 300 + ".png", _image_bytes("PNG"))
    assert _reason(exc) == "unsupported"


def test_refusal_messages_are_plain_and_carry_no_internals() -> None:
    with pytest.raises(UploadRejected) as exc:
        clean_upload("logo.png", _image_bytes("PNG")[:40])
    message = exc.value.message
    assert exc.value.status_code == 422
    for word in ("Pillow", "PIL", "Traceback", "OSError", "truncated", "Postgres"):
        assert word not in message


# ── Generated images ───────────────────────────────────────────────────────
def test_normalise_raster_cleans_a_generated_png() -> None:
    cleaned = normalise_raster(_image_bytes("PNG", size=(32, 32), mode="RGBA"))
    assert cleaned.mime == "image/png"
    assert cleaned.data.startswith(PNG_MAGIC)
    with Image.open(io.BytesIO(cleaned.data)) as image:
        assert image.mode == "RGBA"


@pytest.mark.parametrize(
    "data",
    [_image_bytes("GIF"), SIMPLE_SVG, b"<html>not an image</html>", b""],
)
def test_normalise_raster_refuses_anything_but_png_jpeg_webp(data: bytes) -> None:
    with pytest.raises(UploadRejected):
        normalise_raster(data)


def test_normalise_raster_applies_the_bomb_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "creative_upload_max_pixels", 1_000_000)
    with pytest.raises(UploadRejected) as exc:
        normalise_raster(_image_bytes("PNG", size=(5000, 5000), mode="1"))
    assert _reason(exc) == "too_many_pixels"


# ── Display names ──────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("filename", "mime", "expected"),
    [
        ("../../etc/passwd\x00.png", "image/png", "passwd.png"),
        ("C:\\Users\\me\\logo.svg", "image/png", "logo.png"),
        ("evil\u202egnp.jpg", "image/jpeg", "evilgnp.jpg"),
        ("...", "image/png", "image.png"),
    ],
)
def test_display_names_lose_paths_and_control_characters(
    filename: str, mime: str, expected: str
) -> None:
    assert display_name_for(filename, mime) == expected


# ── SVG: happy path ────────────────────────────────────────────────────────
def test_an_svg_upload_comes_back_as_png() -> None:
    cleaned = clean_upload("logo.svg", SIMPLE_SVG)
    assert cleaned.mime == "image/png"
    assert cleaned.data.startswith(PNG_MAGIC)
    # Fitted inside 1024x1024, aspect ratio kept.
    assert (cleaned.width, cleaned.height) == (1024, 512)
    with Image.open(io.BytesIO(cleaned.data)) as image:
        # The gradient survived sanitising: the left edge is red, not black.
        r, g, b = image.convert("RGB").getpixel((2, 256))
        assert r > 200 and b < 60


def test_sanitised_svg_keeps_allowed_content_and_drops_the_unknown() -> None:
    markup = sanitise_svg(
        _svg(
            '<metadata><rdf>editor data</rdf></metadata>'
            '<style>rect { fill: red }</style>'
            '<g id="a" class="x" data-foo="1"><rect width="5" height="5" fill="#123456"/></g>'
            '<use href="#a" x="5"/>'
            '<rect width="1" height="1" style="fill:url(#a)"/>'
            '<rect width="1" height="1" style="fill:#fff"/>'
        )
    )
    assert "metadata" not in markup and "<style" not in markup
    assert "class=" not in markup and "data-foo" not in markup
    assert 'href="#a"' in markup
    assert "url(" not in markup
    assert 'style="fill:#fff"' in markup
    assert markup.startswith('<svg xmlns="http://www.w3.org/2000/svg"')


# ── SVG: abuse cases (research R2) ─────────────────────────────────────────
HOSTILE_SVGS: dict[str, bytes] = {
    "image_local_file": _svg('<image href="/etc/passwd" width="10" height="10"/>'),
    "image_file_url": _svg('<image xlink:href="file:///etc/passwd" width="10" height="10"/>'),
    "image_http": _svg('<image href="http://169.254.169.254/latest" width="10" height="10"/>'),
    "use_http": _svg('<use href="https://evil.example/x.svg#a"/>'),
    "image_svg_data": _svg(
        '<image href="data:image/svg+xml;base64,PHN2Zz48L3N2Zz4=" width="1" height="1"/>'
    ),
    "script": _svg("<script>alert(1)</script>"),
    "script_in_dropped_element": _svg("<a><script>alert(1)</script></a>"),
    "foreign_object": _svg("<foreignObject><div>x</div></foreignObject>"),
    "onload": _svg('<rect width="1" height="1" onload="alert(1)"/>').replace(
        b"<svg ", b'<svg onload="alert(1)" '
    ),
    "onclick_mixed_case": _svg('<rect width="1" height="1" OnClick="x()"/>'),
    "link_href_http": _svg('<a href="https://evil.example"><rect width="1" height="1"/></a>'),
    "style_url_http": _svg('<rect width="1" height="1" style="fill:url(http://evil.example/a)"/>'),
    "style_import": _svg('<rect width="1" height="1" style="@import \'http://evil\'"/>'),
    "fill_url_http": _svg('<rect width="1" height="1" fill="url(https://evil.example/#g)"/>'),
    "style_element_url": _svg("<style>rect{fill:url(http://evil.example/a)}</style>"),
}


@pytest.mark.parametrize("name", sorted(HOSTILE_SVGS))
def test_hostile_svg_content_is_refused(name: str) -> None:
    with pytest.raises(SvgRejected) as exc:
        sanitise_svg(HOSTILE_SVGS[name])
    assert exc.value.reason == "unsafe"


@pytest.mark.parametrize("name", ["script", "image_local_file", "image_http"])
def test_hostile_svg_uploads_are_refused_in_plain_english(name: str) -> None:
    with pytest.raises(UploadRejected) as exc:
        clean_upload("logo.svg", HOSTILE_SVGS[name])
    assert _reason(exc) == "svg_invalid"
    assert "PNG" in exc.value.message


def test_an_xxe_doctype_is_refused() -> None:
    xxe = (
        b'<?xml version="1.0"?>\n'
        b'<!DOCTYPE svg [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>\n'
        b'<svg xmlns="http://www.w3.org/2000/svg"><text>&xxe;</text></svg>'
    )
    with pytest.raises(SvgRejected) as exc:
        sanitise_svg(xxe)
    assert exc.value.reason == "unsafe"


def test_billion_laughs_is_refused() -> None:
    laughs = ['<!ENTITY lol "lol">'] + [
        f'<!ENTITY lol{i} "{("&lol" + (str(i - 1) if i > 1 else "") + ";") * 10}">'
        for i in range(1, 10)
    ]
    bomb = (
        '<?xml version="1.0"?>\n<!DOCTYPE svg [' + "".join(laughs) + "]>\n"
        '<svg xmlns="http://www.w3.org/2000/svg"><text>&lol9;</text></svg>'
    ).encode()
    with pytest.raises(SvgRejected) as exc:
        sanitise_svg(bomb)
    assert exc.value.reason == "unsafe"


def test_a_plain_doctype_is_refused() -> None:
    with pytest.raises(SvgRejected):
        sanitise_svg(
            b'<?xml version="1.0"?><!DOCTYPE svg PUBLIC "-//W3C//DTD SVG 1.1//EN" '
            b'"http://www.w3.org/Graphics/SVG/1.1/DTD/svg11.dtd">'
            b'<svg xmlns="http://www.w3.org/2000/svg"/>'
        )


@pytest.mark.parametrize(
    "body",
    [
        # A use inside the group it references.
        '<g id="a"><use href="#a"/></g>',
        # Two groups referencing each other.
        '<g id="a"><use href="#b"/></g><g id="b"><use xlink:href="#a"/></g>',
        # A use referencing itself.
        '<use id="u" href="#u"/>',
    ],
)
def test_recursive_use_is_refused(body: str) -> None:
    with pytest.raises(SvgRejected) as exc:
        sanitise_svg(_svg(body))
    assert exc.value.reason == "too_complex"


def test_use_fan_out_is_capped() -> None:
    """Ten uses of ten uses of ... stays under 50 uses but explodes when expanded."""
    groups = ['<g id="g0">' + '<rect width="1" height="1"/>' * 40 + "</g>"]
    for level in range(1, 5):
        groups.append(f'<g id="g{level}">' + f'<use href="#g{level - 1}"/>' * 10 + "</g>")
    with pytest.raises(SvgRejected) as exc:
        sanitise_svg(_svg("<defs>" + "".join(groups) + "</defs>"))
    assert exc.value.reason == "too_complex"


def test_too_many_use_elements_are_refused() -> None:
    body = '<rect id="r" width="1" height="1"/>' + '<use href="#r"/>' * (svg.MAX_USE + 1)
    with pytest.raises(SvgRejected) as exc:
        sanitise_svg(_svg(body))
    assert exc.value.reason == "too_complex"


def test_too_many_elements_are_refused() -> None:
    body = '<rect width="1" height="1"/>' * svg.MAX_ELEMENTS
    with pytest.raises(SvgRejected) as exc:
        sanitise_svg(_svg(body))
    assert exc.value.reason == "too_complex"


def test_nesting_deeper_than_the_cap_is_refused() -> None:
    body = "<g>" * svg.MAX_DEPTH + "</g>" * svg.MAX_DEPTH
    with pytest.raises(SvgRejected) as exc:
        sanitise_svg(_svg(body))
    assert exc.value.reason == "too_complex"


@pytest.mark.parametrize("size", ['width="5000"', 'height="5000px"', 'width="60in"', 'width="1e400"'])
def test_oversized_declared_dimensions_are_refused(size: str) -> None:
    data = f'<svg xmlns="http://www.w3.org/2000/svg" {size}><rect/></svg>'.encode()
    with pytest.raises(SvgRejected):
        sanitise_svg(data)


def test_an_svg_over_the_size_cap_is_refused() -> None:
    padding = "<!--" + "x" * svg.SVG_MAX_BYTES + "-->"
    data = _svg(padding)
    with pytest.raises(SvgRejected) as exc:
        sanitise_svg(data)
    assert exc.value.reason == "too_large"
    with pytest.raises(UploadRejected) as upload_exc:
        clean_upload("logo.svg", data)
    assert _reason(upload_exc) == "svg_too_large"


def test_the_svg_cap_setting_can_lower_but_not_raise_the_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "creative_svg_max_bytes", 10 * 1024 * 1024)
    assert svg.svg_byte_limit() == svg.SVG_MAX_BYTES
    monkeypatch.setattr(settings, "creative_svg_max_bytes", 100)
    with pytest.raises(SvgRejected):
        sanitise_svg(SIMPLE_SVG)


@pytest.mark.parametrize(
    "data",
    [
        b"<html><svg xmlns='http://www.w3.org/2000/svg'/></html>",
        b"<?xml version='1.0'?><html xmlns='http://www.w3.org/1999/xhtml'/>",
        b"<svg xmlns='http://www.w3.org/2000/svg'><rect",
    ],
)
def test_malformed_or_non_svg_markup_is_refused(data: bytes) -> None:
    with pytest.raises(SvgRejected) as exc:
        sanitise_svg(data)
    assert exc.value.reason == "invalid"


def test_an_inline_png_image_is_kept() -> None:
    import base64

    encoded = base64.b64encode(_image_bytes("PNG", size=(2, 2))).decode()
    markup = sanitise_svg(_svg(f'<image href="data:image/png;base64,{encoded}" width="10" height="10"/>'))
    assert f"data:image/png;base64,{encoded}" in markup


# ── SVG: the rasteriser process ────────────────────────────────────────────
def test_resvg_is_never_given_a_path_or_resource_directory() -> None:
    assert "svg_path" not in svg._RENDER_SCRIPT
    assert "resources_dir" not in svg._RENDER_SCRIPT
    assert "skip_system_fonts=True" in svg._RENDER_SCRIPT
    assert "RLIMIT_AS" in svg._RENDER_SCRIPT


def test_the_render_process_inherits_no_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-not-real")
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pass@host/db")
    env = svg._child_env()
    assert "ANTHROPIC_API_KEY" not in env and "DATABASE_URL" not in env
    assert set(env) <= {"SYSTEMROOT", "PATH"}


def test_a_render_that_runs_too_long_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    def slow(*_args: Any, **kwargs: Any) -> subprocess.CompletedProcess[bytes]:  # noqa: ANN401
        assert kwargs["timeout"] == svg.RENDER_TIMEOUT_SECONDS
        raise subprocess.TimeoutExpired(cmd="render", timeout=kwargs["timeout"])

    monkeypatch.setattr(svg.subprocess, "run", slow)
    with pytest.raises(SvgRejected) as exc:
        svg.rasterise_svg(sanitise_svg(SIMPLE_SVG))
    assert exc.value.reason == "too_complex"


@pytest.mark.parametrize(
    ("returncode", "stdout"),
    [(0, b"GIF89a not a png"), (1, PNG_MAGIC + b"partial"), (0, b"")],
)
def test_render_output_that_is_not_a_png_is_refused(
    monkeypatch: pytest.MonkeyPatch, returncode: int, stdout: bytes
) -> None:
    def fake(*args: Any, **_kwargs: Any) -> subprocess.CompletedProcess[bytes]:  # noqa: ANN401
        return subprocess.CompletedProcess(args[0], returncode, stdout=stdout, stderr=b"")

    monkeypatch.setattr(svg.subprocess, "run", fake)
    with pytest.raises(SvgRejected) as exc:
        svg.rasterise_svg(sanitise_svg(SIMPLE_SVG))
    assert exc.value.reason == "render_failed"


# ── Storage backends ───────────────────────────────────────────────────────
def test_postgres_is_the_default_backend() -> None:
    assert isinstance(get_storage(), PostgresStorage)


def test_the_s3_stub_refuses_every_call_in_plain_english() -> None:
    stub = S3Storage()
    stored = StoredFile(tenant_id="t", kind="logo", source="upload", mime="image/png",
                        size_bytes=1, sha256="0" * 64)
    for call in (
        lambda: stub.put(None, stored, b"x"),  # type: ignore[arg-type]
        lambda: stub.get(None, tenant_id="t", file_id="f"),  # type: ignore[arg-type]
        lambda: stub.delete(None, tenant_id="t", file_id="f"),  # type: ignore[arg-type]
    ):
        with pytest.raises(StorageNotConfigured) as exc:
            call()
        assert "not set up" in exc.value.message
        assert "S3" not in exc.value.message


def test_an_unknown_backend_name_is_refused() -> None:
    with pytest.raises(StorageNotConfigured):
        backend_named("ftp")


# ── Storing and the tenant cap (stand-in session) ──────────────────────────
class _Dialect:
    name = "sqlite"


class _Bind:
    dialect = _Dialect()


class _FakeSession:
    def __init__(self) -> None:
        self.added: list[StoredFile] = []

    def get_bind(self) -> _Bind:
        return _Bind()

    def add(self, row: StoredFile) -> None:
        self.added.append(row)

    def flush(self) -> None:
        return None


def _use(monkeypatch: pytest.MonkeyPatch, used: int, cap: int) -> None:
    monkeypatch.setattr(uploads, "tenant_usage_bytes", lambda _db, _tenant: used)
    monkeypatch.setattr(settings, "creative_storage_max_bytes_per_tenant", cap)


def test_accept_image_records_provenance(monkeypatch: pytest.MonkeyPatch) -> None:
    _use(monkeypatch, used=0, cap=10 * 1024 * 1024)
    db = _FakeSession()
    stored = uploads.accept_image(
        db, tenant_id="tenant-1", user_id="user-1", filename="../My Logo.svg",  # type: ignore[arg-type]
        data=SIMPLE_SVG, kind=FileKind.LOGO,
    )
    assert db.added == [stored]
    assert stored.tenant_id == "tenant-1"
    assert stored.created_by == "user-1"
    assert stored.source == FileSource.UPLOAD
    assert stored.mime == "image/png"
    assert stored.display_name == "My Logo.png"
    assert stored.data is not None and stored.data.startswith(PNG_MAGIC)
    assert stored.sha256 == hashlib.sha256(stored.data).hexdigest()
    assert stored.size_bytes == len(stored.data)


def test_an_upload_over_the_tenant_storage_cap_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    data = _image_bytes("PNG")
    _use(monkeypatch, used=1000, cap=1000 + 10)
    db = _FakeSession()
    with pytest.raises(StorageFull) as exc:
        uploads.accept_image(
            db, tenant_id="t", user_id="u", filename="logo.png", data=data,  # type: ignore[arg-type]
            kind=FileKind.LOGO,
        )
    assert exc.value.status_code == 409
    assert db.added == []


def test_a_storage_cap_of_zero_allows_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    _use(monkeypatch, used=0, cap=0)
    with pytest.raises(StorageFull):
        uploads.accept_image(
            _FakeSession(), tenant_id="t", user_id="u", filename="logo.png",  # type: ignore[arg-type]
            data=_image_bytes("PNG"), kind=FileKind.LOGO,
        )


@pytest.mark.parametrize("kind", [FileKind.RENDER, FileKind.PREVIEW, "../../etc"])
def test_only_upload_kinds_can_be_uploaded(kind: str) -> None:
    with pytest.raises(UploadRejected) as exc:
        uploads.accept_image(
            _FakeSession(), tenant_id="t", user_id="u", filename="logo.png",  # type: ignore[arg-type]
            data=_image_bytes("PNG"), kind=kind,
        )
    assert _reason(exc) == "bad_kind"


def test_store_file_refuses_svg_and_a_wrong_hash(monkeypatch: pytest.MonkeyPatch) -> None:
    _use(monkeypatch, used=0, cap=10_000_000)
    with pytest.raises(UploadRejected):
        uploads.store_file(
            _FakeSession(), tenant_id="t", data=SIMPLE_SVG, mime="image/svg+xml",  # type: ignore[arg-type]
            kind=FileKind.LOGO, source=FileSource.UPLOAD,
            sha256=hashlib.sha256(SIMPLE_SVG).hexdigest(),
        )
    with pytest.raises(UploadRejected):
        uploads.store_file(
            _FakeSession(), tenant_id="t", data=b"abc", mime="image/png",  # type: ignore[arg-type]
            kind=FileKind.RENDER, source=FileSource.RENDERED, sha256="0" * 64,
        )


def test_a_generated_background_is_stored_without_an_uploader(monkeypatch: pytest.MonkeyPatch) -> None:
    _use(monkeypatch, used=0, cap=10_000_000)
    stored = uploads.store_generated_image(
        _FakeSession(), tenant_id="t", data=_image_bytes("PNG"), job_id="job-1",  # type: ignore[arg-type]
    )
    assert stored.source == FileSource.GENERATED
    assert stored.kind == FileKind.BACKGROUND
    assert stored.created_by == "" and stored.job_id == "job-1"


# ── Database ───────────────────────────────────────────────────────────────
@requires_db
def test_a_stored_svg_upload_round_trips_as_png(clean_db, org) -> None:  # noqa: ANN001
    from app.db.session import bind_tenant

    bind_tenant(clean_db, org.id)
    stored = uploads.accept_image(
        clean_db, tenant_id=org.id, user_id="user-1", filename="logo.svg",
        data=SIMPLE_SVG, kind=FileKind.LOGO,
    )
    clean_db.commit()
    data = PostgresStorage().get(clean_db, tenant_id=org.id, file_id=stored.id)
    assert data.startswith(PNG_MAGIC)
    assert hashlib.sha256(data).hexdigest() == stored.sha256


@requires_db
def test_another_workspace_cannot_read_a_stored_file(clean_db, org, second_org) -> None:  # noqa: ANN001
    from app.db.session import bind_tenant

    bind_tenant(clean_db, org.id)
    stored = uploads.accept_image(
        clean_db, tenant_id=org.id, user_id="user-1", filename="logo.png",
        data=_image_bytes("PNG"), kind=FileKind.LOGO,
    )
    clean_db.commit()
    with pytest.raises(NotFoundError):
        PostgresStorage().get(clean_db, tenant_id=second_org.id, file_id=stored.id)
    PostgresStorage().delete(clean_db, tenant_id=second_org.id, file_id=stored.id)
    assert PostgresStorage().get(clean_db, tenant_id=org.id, file_id=stored.id)


@requires_db
def test_the_tenant_cap_counts_what_is_already_stored(
    clean_db, org, monkeypatch: pytest.MonkeyPatch  # noqa: ANN001
) -> None:
    from app.db.session import bind_tenant

    bind_tenant(clean_db, org.id)
    first = uploads.accept_image(
        clean_db, tenant_id=org.id, user_id="u", filename="a.png",
        data=_image_bytes("PNG"), kind=FileKind.LOGO,
    )
    monkeypatch.setattr(settings, "creative_storage_max_bytes_per_tenant", first.size_bytes + 1)
    with pytest.raises(StorageFull):
        uploads.accept_image(
            clean_db, tenant_id=org.id, user_id="u", filename="b.png",
            data=_image_bytes("PNG", size=(20, 20)), kind=FileKind.LOGO,
        )
