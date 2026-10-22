"""Creative rendering: fonts, templates, the Chromium sandbox and the fallback.

Most of these are refusals. The renderer draws text a model wrote into an
HTML page inside a real browser, so the tests prove the page cannot run
script, cannot reach the network (a local HTTP server must record zero hits)
and cannot be steered through colours, fonts or images.

Chromium tests skip with a reason when the browser is not installed; they
run in the production image (``docker compose run --rm api python -m pytest
tests/test_creative_render.py``).
"""
from __future__ import annotations

import html as html_lib
import io
import re
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from PIL import Image

from app.core.config import settings
from app.services.creative import compose, fonts
from app.services.creative import render as render_api
from app.services.creative.compose import (
    ComposeError,
    CreativeInputs,
    InlineImage,
    build_layout,
    render_html,
)
from app.services.creative.formats import FORMATS, get_format
from app.services.creative.qa import safe_area
from app.services.creative.render.base import RenderBusy, RenderError, RendererUnavailable
from app.services.creative.render.chromium import ChromiumRenderer, launch_args
from app.services.creative.render.pillow_fallback import PillowRenderer
from app.services.creative.schema import ComposedCopy, Palette

LOGO_RGB = (200, 30, 30)
XSS = "<script>document.body.style.background='red'</script><img src=x onerror=alert(1)>"


def _png(size: tuple[int, int], colour: tuple[int, int, int]) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, colour).save(buf, "PNG")
    return buf.getvalue()


PALETTE = Palette(background="#ffffff", text="#111111", accent="#0055ff", on_accent="#ffffff")


def _inputs(format_id: str = "banner_300x250", *, headline: str = "Run further on every trail",
            logo: bool = True, background: bool = False) -> CreativeInputs:
    copy = ComposedCopy(
        headline=headline,
        body="Lightweight grip with thirty day returns." if get_format(format_id).limits.body_chars else "",
        cta="Shop now",
        claims=("Free delivery on every order",),
        disclaimer="Terms apply.",
    )
    return CreativeInputs(
        format_id=format_id,
        copy=copy,
        palette=PALETTE,
        heading_font="playfair_display",
        body_font="inter",
        logo=InlineImage.from_bytes(_png((400, 120), LOGO_RGB)) if logo else None,
        background=InlineImage.from_bytes(_png((300, 300), (30, 90, 200))) if background else None,
    )


def _media_boxes(pdf: bytes) -> list[tuple[float, float]]:
    boxes = re.findall(rb"/MediaBox\s*\[\s*0\s+0\s+([\d.]+)\s+([\d.]+)\s*\]", pdf)
    return [(float(w), float(h)) for w, h in boxes]


def _mm_to_pt(mm: float) -> float:
    return mm / 25.4 * 72


# ── Fonts (research R5) ────────────────────────────────────────────────────
LOCK = fonts.FONT_DIR / "fonts.lock"


def test_fonts_lock_pins_every_registry_file_at_the_pinned_commit() -> None:
    pins = fonts.parse_lock(LOCK.read_text(encoding="utf-8"))
    assert set(pins) == set(fonts.manifest())
    for url in fonts.manifest().values():
        assert url.startswith(f"https://raw.githubusercontent.com/google/fonts/{fonts.FONTS_COMMIT}/ofl/")


@pytest.mark.parametrize("path", sorted(fonts.manifest()))
def test_shipped_font_files_match_their_pinned_hash(path: str) -> None:
    pins = fonts.parse_lock(LOCK.read_text(encoding="utf-8"))
    file = fonts.FONT_DIR / path
    assert file.is_file(), f"{path} is not shipped"
    data = file.read_bytes()
    fonts.verify_font_bytes(path, data, pins)
    if path.endswith(".ttf"):
        assert len(data) > 10_000  # a font, not a Git LFS pointer


def test_every_family_ships_its_ofl_licence() -> None:
    for spec in fonts.FONTS.values():
        licence = (fonts.FONT_DIR / spec.directory / fonts.LICENCE_FILE).read_text(encoding="utf-8")
        assert "SIL OPEN FONT LICENSE" in licence.upper()


def test_a_tampered_font_is_refused() -> None:
    pins = fonts.parse_lock(LOCK.read_text(encoding="utf-8"))
    path = "lato/Lato-Regular.ttf"
    data = bytearray((fonts.FONT_DIR / path).read_bytes())
    data[-1] ^= 0xFF
    with pytest.raises(fonts.FontIntegrityError):
        fonts.verify_font_bytes(path, bytes(data), pins)
    with pytest.raises(fonts.FontIntegrityError):
        fonts.verify_font_bytes(path, b"version https://git-lfs.github.com/spec/v1\n", pins)


def test_pillow_fallback_uses_static_files_only() -> None:
    for font_id in fonts.FONTS:
        path = fonts.pillow_font_path(font_id, 700)
        assert path.is_file()
        assert "[" not in path.name  # variable fonts are named Family[axes].ttf


# ── Templates (research R3) ────────────────────────────────────────────────
def test_template_environment_is_sandboxed_and_strict() -> None:
    from jinja2 import StrictUndefined
    from jinja2.sandbox import SandboxedEnvironment

    env = compose._env
    assert isinstance(env, SandboxedEnvironment)
    assert env.autoescape is True
    assert env.undefined is StrictUndefined
    assert env.extensions == {}
    assert env.loader.searchpath == [str(compose.TEMPLATE_DIR)]  # type: ignore[union-attr]


def test_templates_never_mark_text_safe() -> None:
    sources = [p.read_text(encoding="utf-8") for p in compose.TEMPLATE_DIR.glob("*.j2")]
    assert len(sources) == 6
    for source in sources:
        source = re.sub(r"\{#.*?#\}", "", source, flags=re.DOTALL)  # comments may name the rule
        assert "|safe" not in source.replace(" ", "")
        assert "Markup" not in source
        assert "http:" not in source and "https:" not in source
    code = Path(compose.__file__).read_text(encoding="utf-8")
    assert "from_string" not in code and "Markup(" not in code


def test_every_format_has_a_template() -> None:
    names = {compose.template_for(spec) for spec in FORMATS.values()}
    assert names == {"banner.html.j2", "social.html.j2", "email_header.html.j2",
                     "flyer.html.j2", "brochure.html.j2"}


def test_script_in_copy_is_escaped_in_the_html() -> None:
    html = render_html(build_layout(_inputs(headline=XSS[:60]), measure="pillow"), output="png")
    assert "<script>document" not in html
    assert "&lt;script&gt;document" in html
    assert "<img src=x" not in html


def test_document_carries_the_csp_and_only_data_uris() -> None:
    html = render_html(build_layout(_inputs(background=True), measure="chromium"), output="png")
    assert f'content="{compose.DOCUMENT_CSP}"' in html_lib.unescape(html)
    assert "default-src 'none'" in compose.DOCUMENT_CSP
    assert "http:" not in html and "https:" not in html
    urls = re.findall(r'(?:src="|url\()([^")]+)', html)
    assert urls and all(u.startswith("data:") for u in urls)


def test_a_hand_built_layout_cannot_smuggle_css(monkeypatch: pytest.MonkeyPatch) -> None:
    layout = build_layout(_inputs(), measure="pillow")
    page = layout.pages[0]
    bad_text = compose.TextBlock(**{**{f: getattr(page.texts[0], f) for f in
                                       compose.TextBlock.__dataclass_fields__},
                                    "colour": "red;background:url(http://x)"})
    bad = compose.Layout(layout.spec, layout.palette, layout.heading_font, layout.body_font,
                         (compose.Page(texts=(bad_text,)),), "pillow")
    with pytest.raises(ValueError):
        render_html(bad, output="png")
    bad_font = compose.TextBlock(**{**{f: getattr(page.texts[0], f) for f in
                                       compose.TextBlock.__dataclass_fields__},
                                    "font_id": "x;}body{background:url(http://x)"})
    bad = compose.Layout(layout.spec, layout.palette, layout.heading_font, layout.body_font,
                         (compose.Page(texts=(bad_font,)),), "pillow")
    with pytest.raises(ValueError):
        render_html(bad, output="png")


def test_invalid_colour_is_refused() -> None:
    with pytest.raises(ValueError):
        Palette(background="#fff;}", text="#111111", accent="#0055ff", on_accent="#ffffff")


def test_unknown_font_is_refused() -> None:
    with pytest.raises(ValueError):
        CreativeInputs("banner_300x250", _inputs().copy, PALETTE, "comic_sans", "inter")


@pytest.mark.parametrize("data", [
    b"GIF89a" + b"\x00" * 64,
    b"<svg xmlns='http://www.w3.org/2000/svg'/>",
    b"\x89PNG\r\n\x1a\n" + b"not really a png",
    b"",
])
def test_inline_images_must_be_real_png_jpeg_or_webp(data: bytes) -> None:
    with pytest.raises(ComposeError):
        InlineImage.from_bytes(data)


def test_oversized_html_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(compose, "MAX_HTML_BYTES", 1000)
    with pytest.raises(ComposeError):
        render_html(build_layout(_inputs(), measure="pillow"), output="png")


def test_layout_respects_safe_zones_and_bleed() -> None:
    for format_id in ("pinterest_pin", "flyer_a4", "instagram_story"):
        spec = get_format(format_id)
        layout = build_layout(_inputs(format_id), measure="pillow")
        area = safe_area(spec)
        for box in (*layout.text_boxes, *layout.logo_boxes):
            assert box.within(area), format_id


# ── Pillow fallback ────────────────────────────────────────────────────────
@pytest.mark.parametrize("format_id", ["banner_300x250", "banner_728x90", "instagram_portrait",
                                       "email_header"])
def test_pillow_png_has_the_format_size(format_id: str) -> None:
    spec = get_format(format_id)
    result = PillowRenderer().render(build_layout(_inputs(format_id), measure="pillow"),
                                     output="png")
    assert result.renderer == "pillow" and result.mime == "image/png"
    with Image.open(io.BytesIO(result.data)) as img:
        assert img.size == (spec.width_px, spec.height_px)


@pytest.mark.parametrize("format_id,pages", [("flyer_a4", 1), ("brochure_trifold", 2)])
def test_pillow_pdf_page_includes_bleed(format_id: str, pages: int) -> None:
    spec = get_format(format_id)
    result = PillowRenderer().render(build_layout(_inputs(format_id), measure="pillow"),
                                     output="pdf")
    boxes = _media_boxes(result.data)
    assert len(boxes) == pages == result.pages
    page_w, page_h = spec.page_mm  # type: ignore[misc]
    assert page_w == spec.trim_mm[0] + 6  # type: ignore[index]
    for w, h in boxes:
        assert abs(w - _mm_to_pt(page_w)) < 1 and abs(h - _mm_to_pt(page_h)) < 1


def test_pillow_refuses_a_layout_measured_for_chromium() -> None:
    with pytest.raises(RenderError):
        PillowRenderer().render(build_layout(_inputs(), measure="chromium"), output="png")


def test_configured_pillow_renderer_is_used(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "creative_renderer", "pillow")
    result = render_api.render_creative(_inputs())
    assert result.renderer == "pillow"
    assert render_api.renderer_status() == "pillow"


def test_chromium_that_will_not_launch_falls_back_to_pillow(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def broken(self: ChromiumRenderer) -> None:
        raise RendererUnavailable()

    monkeypatch.setattr(settings, "creative_renderer", "chromium")
    monkeypatch.setattr(render_api, "_chromium", ChromiumRenderer())
    monkeypatch.setattr(render_api, "_degraded", False)
    monkeypatch.setattr(ChromiumRenderer, "start", broken)
    with caplog.at_level("ERROR"):
        result = render_api.render_creative(_inputs())
    assert result.renderer == "pillow"
    assert render_api.renderer_status() == "degraded"
    assert any(r.levelname == "ERROR" for r in caplog.records)
    assert "Run further" not in caplog.text  # no copy in logs


# ── Chromium configuration (no browser needed) ─────────────────────────────
def test_launch_args_block_dns_and_sandbox_follows_settings() -> None:
    assert "--host-resolver-rules=MAP * ~NOTFOUND" in launch_args(sandbox=True)
    assert "--no-sandbox" not in launch_args(sandbox=True)
    assert "--no-sandbox" in launch_args(sandbox=False)


def test_render_slots_are_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "creative_render_timeout_seconds", 1)
    renderer = ChromiumRenderer(max_concurrent=1)
    assert renderer._slots.acquire(timeout=1)  # another render holds the only slot
    with pytest.raises(RenderBusy):
        renderer.render(build_layout(_inputs(), measure="chromium"), output="png")


def test_chromium_refuses_oversized_html() -> None:
    from app.services.creative.render.base import RenderInputError

    renderer = ChromiumRenderer()
    layout = build_layout(_inputs(), measure="chromium")
    with pytest.raises(RenderInputError):
        renderer.render_html("x" * (compose.MAX_HTML_BYTES + 1), layout, output="png")


# ── Chromium (skipped when the browser is absent) ──────────────────────────
@pytest.fixture(scope="module")
def chromium() -> Iterator[ChromiumRenderer]:
    renderer = ChromiumRenderer(max_concurrent=1)
    try:
        renderer.start()
    except RendererUnavailable:
        pytest.skip("Chromium is not installed here; these run in the production image")
    yield renderer
    renderer.shutdown()


class _Recorder(BaseHTTPRequestHandler):
    hits: list[str] = []

    def do_GET(self) -> None:  # noqa: N802 - stdlib name
        type(self).hits.append(self.path)
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.end_headers()
        self.wfile.write(_png((4, 4), (0, 255, 0)))

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - stdlib name
        return


@pytest.fixture()
def http_server() -> Iterator[tuple[str, list[str]]]:
    _Recorder.hits = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Recorder)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"127.0.0.1:{server.server_address[1]}", _Recorder.hits
    finally:
        server.shutdown()
        server.server_close()


def _pixel(png: bytes, xy: tuple[int, int]) -> tuple[int, int, int]:
    with Image.open(io.BytesIO(png)) as img:
        return img.convert("RGB").getpixel(xy)  # type: ignore[return-value]


def test_chromium_draws_a_data_image(chromium: ChromiumRenderer) -> None:
    layout = build_layout(_inputs(), measure="chromium")
    result = chromium.render(layout, output="png")
    logo = layout.logo_boxes[0]
    centre = (int(logo.x + logo.width / 2), int(logo.y + logo.height / 2))
    assert _pixel(result.data, centre) == LOGO_RGB


@pytest.mark.parametrize("format_id", ["banner_300x250", "banner_320x50", "x_post",
                                       "instagram_story", "email_header"])
def test_chromium_png_has_the_format_size(chromium: ChromiumRenderer, format_id: str) -> None:
    spec = get_format(format_id)
    result = chromium.render(build_layout(_inputs(format_id, background=True),
                                          measure="chromium"), output="png")
    assert (result.width, result.height) == (spec.width_px, spec.height_px)
    with Image.open(io.BytesIO(result.data)) as img:
        assert img.size == (spec.width_px, spec.height_px)


@pytest.mark.parametrize("format_id,pages", [("flyer_a4", 1), ("flyer_letter", 1),
                                             ("brochure_bifold", 2)])
def test_chromium_pdf_page_includes_bleed(chromium: ChromiumRenderer, format_id: str,
                                          pages: int) -> None:
    spec = get_format(format_id)
    result = chromium.render(build_layout(_inputs(format_id), measure="chromium"), output="pdf")
    boxes = _media_boxes(result.data)
    assert len(boxes) == pages
    page_w, page_h = spec.page_mm  # type: ignore[misc]
    for w, h in boxes:
        # Chromium rounds the page to whole CSS pixels (0.75 pt).
        assert abs(w - _mm_to_pt(page_w)) < 1 and abs(h - _mm_to_pt(page_h)) < 1
        assert w > _mm_to_pt(spec.trim_mm[0]) + 15  # type: ignore[index]


def test_chromium_escapes_script_in_copy(chromium: ChromiumRenderer) -> None:
    layout = build_layout(_inputs(headline=XSS[:60]), measure="chromium")
    result = chromium.render(layout, output="png")
    # The page background stays white: the script was drawn as text.
    assert _pixel(result.data, (2, 2)) == (255, 255, 255)


def _probe_page(body: str, *, csp: bool) -> str:
    meta = f'<meta http-equiv="Content-Security-Policy" content="{compose.DOCUMENT_CSP}">' if csp else ""
    return (f"<!DOCTYPE html><html><head>{meta}<style>html,body{{margin:0;background:#ffffff}}"
            f"</style></head><body>{body}</body></html>")


@pytest.mark.parametrize("csp", [False, True])
def test_chromium_blocks_http_images_and_css(
    chromium: ChromiumRenderer, http_server: tuple[str, list[str]], csp: bool
) -> None:
    host, hits = http_server
    port = host.split(":")[1]
    body = (
        f'<img src="http://{host}/img.png" width="10" height="10">'
        f'<img src="http://localhost:{port}/by-name.png">'
        f'<img src="http://169.254.169.254/latest/meta-data/">'
        f'<div style="width:50px;height:50px;background:url(http://{host}/css.png)"></div>'
        f'<link rel="stylesheet" href="http://{host}/sheet.css">'
        f'<style>@import url(http://{host}/import.css);'
        f'@font-face{{font-family:x;src:url(http://{host}/font.ttf)}}'
        f'p{{font-family:x}}</style><p>text</p>'
        f'<iframe src="http://{host}/frame"></iframe>'
    )
    layout = build_layout(_inputs(), measure="chromium")
    chromium.render_html(_probe_page(body, csp=csp), layout, output="png")
    assert hits == []
    if not csp:
        # Without the CSP the router is what stops them; prove it saw them.
        blocked = " ".join(chromium.last_blocked)
        assert f"{host}/img.png" in blocked
        assert "169.254.169.254" in blocked
    else:
        # With the CSP the browser refuses them before any request is made,
        # proving the in-document policy is enforced on its own.
        assert not any(host in url for url in chromium.last_blocked)


def test_chromium_runs_with_javascript_off(chromium: ChromiumRenderer) -> None:
    # No CSP here, so only the context setting can stop the script. <noscript>
    # content is drawn only when scripting is disabled.
    body = ("<script>document.body.style.background='#ff0000'</script>"
            "<noscript><div style='position:absolute;inset:0;background:#00ff00'></div></noscript>")
    layout = build_layout(_inputs(), measure="chromium")
    result = chromium.render_html(_probe_page(body, csp=False), layout, output="png")
    assert _pixel(result.data, (150, 125)) == (0, 255, 0)
