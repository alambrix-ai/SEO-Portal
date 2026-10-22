"""Creative Studio copy schema, format catalogue, font registry and QA.

Model output is untrusted, so most of these tests are refusals: an invented
claim, an invented number, a claim index the product does not have, a colour
that could carry CSS, copy too long for its format, a brand "don't". The
copy that passes is the exception the tests have to earn.
"""
from __future__ import annotations

import hashlib
import json

import pytest
from pydantic import ValidationError

from app.agents.dynamic_creative_optimizer import specs
from app.models.creative import FONT_IDS
from app.services.creative import fonts, formats, qa, schema
from app.services.creative.qa import Box, ProductFacts

APPROVED = ["Free delivery on every order", "Rated 4.8 by 1,200 customers", "Made in Pune"]
FACTS = ProductFacts(
    name="Acme Trail 2 running shoe",
    description="Lightweight trail shoe, 30 day returns.",
    price_text="From ₹4,999",
    approved_claims=tuple(APPROVED),
)


def _copy(**overrides: object) -> dict[str, object]:
    data: dict[str, object] = {
        "headline": "Run further on every trail this season",
        "body": "Lightweight grip with 30 day returns.",
        "cta": "Shop now",
        "claim_ids": [0],
    }
    data.update(overrides)
    return data


def _parse(format_id: str = "banner_300x250", **overrides: object) -> schema.CreativeCopy:
    return schema.parse_copy(_copy(**overrides), format_id=format_id, approved_claims=APPROVED)


# ── Format catalogue ───────────────────────────────────────────────────────
def test_catalogue_has_every_research_size() -> None:
    assert formats.DISPLAY_BANNER_SIZES == (
        "300x250", "728x90", "160x600", "336x280", "300x600", "970x250", "320x50",
    )
    expected = {
        "instagram_portrait": "1080x1350",
        "instagram_square": "1080x1080",
        "instagram_story": "1080x1920",
        "linkedin_post": "1200x627",
        "x_post": "1600x900",
        "pinterest_pin": "1000x1500",
        "facebook_post": "1200x630",
        "email_header": "600x200",
    }
    for format_id, size in expected.items():
        assert formats.get_format(format_id).size == size


def test_every_dco_display_size_is_in_the_catalogue() -> None:
    """The IAB sizes the ads agent already uses must all be producible."""
    for name in ("Google Ads", "Microsoft Ads", "DSP / SSP Exchange"):
        for size in specs.PLATFORMS[name].dimensions:
            assert size in formats.DISPLAY_BANNER_SIZES, (name, size)


def test_banners_are_capped_at_150_kb_and_eight_headline_words() -> None:
    for format_id, spec in formats.FORMATS.items():
        if spec.kind == "banner":
            assert spec.max_bytes == 150 * 1024, format_id
            assert spec.limits.headline_words == 8, format_id


@pytest.mark.parametrize("format_id", ["flyer_a4", "flyer_letter", "brochure_bifold", "brochure_trifold"])
def test_print_formats_have_3mm_bleed(format_id: str) -> None:
    spec = formats.get_format(format_id)
    assert spec.mime == "application/pdf" and spec.bleed_mm == 3.0
    assert spec.trim_mm is not None and spec.page_mm is not None
    assert spec.page_mm == (spec.trim_mm[0] + 6, spec.trim_mm[1] + 6)
    assert spec.width_px == round(spec.page_mm[0] / 25.4 * 300)


def test_a4_flyer_page_and_brochure_panels() -> None:
    assert formats.get_format("flyer_a4").page_mm == (216.0, 303.0)
    assert formats.get_format("brochure_bifold").panels == 2
    assert formats.get_format("brochure_trifold").panels == 3


@pytest.mark.parametrize("bad", ["banner_1x1", "", "../flyer_a4", None])
def test_unknown_format_is_refused(bad: object) -> None:
    with pytest.raises(ValueError):
        formats.get_format(bad)  # type: ignore[arg-type]


# ── Copy schema ────────────────────────────────────────────────────────────
def test_valid_copy_parses_and_grounds() -> None:
    copy = _parse(claim_ids=[0, 2])
    assert schema.ground_claims(copy, APPROVED) == (APPROVED[0], APPROVED[2])


def test_copy_parses_from_json_text() -> None:
    copy = schema.parse_copy(json.dumps(_copy()), format_id="instagram_portrait", approved_claims=APPROVED)
    assert copy.cta == "Shop now"


@pytest.mark.parametrize("claim_ids", [[3], [99], [-1]])
def test_unknown_claim_index_is_refused(claim_ids: list[int]) -> None:
    with pytest.raises(ValidationError):
        _parse(claim_ids=claim_ids)


def test_claim_index_is_refused_when_product_has_no_claims() -> None:
    with pytest.raises(ValidationError):
        schema.parse_copy(_copy(claim_ids=[0]), format_id="banner_300x250", approved_claims=[])


def test_claims_without_context_are_refused() -> None:
    """Deny by default: no product claims supplied means no claim may be used."""
    with pytest.raises(ValidationError):
        schema.CreativeCopy.model_validate(_copy(claim_ids=[0]))


def test_duplicate_and_string_claim_ids_are_refused() -> None:
    with pytest.raises(ValidationError):
        _parse(claim_ids=[0, 0])
    with pytest.raises(ValidationError):
        _parse(claim_ids=["0"])  # strict: no coercion


def test_ground_claims_rechecks_changed_product() -> None:
    copy = _parse(claim_ids=[2])
    with pytest.raises(ValueError):
        schema.ground_claims(copy, APPROVED[:2])
    with pytest.raises(ValueError):
        schema.ground_claims(copy, [APPROVED[0], APPROVED[1], {"text": "not a string"}])


@pytest.mark.parametrize(
    "field", ["disclaimer", "claims", "html", "style", "background_color"]
)
def test_model_cannot_add_fields(field: str) -> None:
    with pytest.raises(ValidationError):
        _parse(**{field: "Terms apply"})


def test_headline_too_long_for_banner_is_refused() -> None:
    nine_words = "Run far on any trail in any wet day"
    with pytest.raises(ValidationError, match="words"):
        _parse(headline=nine_words)
    # The same headline is fine where only characters are capped.
    assert _parse("instagram_portrait", headline=nine_words).headline == nine_words


def test_copy_over_format_caps_is_refused() -> None:
    with pytest.raises(ValidationError, match="too long"):
        _parse(headline="x" * 41)
    with pytest.raises(ValidationError, match="body"):
        _parse(body="word " * 30)
    with pytest.raises(ValidationError, match="button"):
        _parse(cta="Shop the full range")
    with pytest.raises(ValidationError, match="no room"):
        _parse("banner_320x50", headline="Trail shoes for every runner", body="Anything", cta="Shop")


def test_copy_over_global_caps_is_refused_without_a_format() -> None:
    with pytest.raises(ValidationError):
        schema.CreativeCopy.model_validate(_copy(claim_ids=[], body="x" * (formats.MAX_BODY_CHARS + 1)))


@pytest.mark.parametrize(
    "headline",
    ["", "   ", "Two\nlines", "Hidden​space", "Flip ‮reversed", "Bell\x07"],
)
def test_empty_multiline_or_hidden_characters_are_refused(headline: str) -> None:
    with pytest.raises(ValidationError):
        _parse(headline=headline)


def test_resolve_disclaimer_is_template_sourced() -> None:
    assert schema.resolve_disclaimer(product_disclaimer=" T&Cs apply. ", kit_disclaimer="T&Cs apply.") == "T&Cs apply."
    composed = schema.compose_copy(
        _parse(claim_ids=[1]), approved_claims=APPROVED,
        product_disclaimer="Prices include GST.", kit_disclaimer="Acme Ltd.",
    )
    assert composed.disclaimer == "Prices include GST. Acme Ltd."
    assert composed.claims == (APPROVED[1],)


@pytest.mark.parametrize("colour", ["#1A2B3C", "#ffffff"])
def test_palette_accepts_hex(colour: str) -> None:
    palette = schema.Palette(background=colour, text="#000000", accent="#2563eb", on_accent="#ffffff")
    assert palette.background == colour.lower()


@pytest.mark.parametrize(
    "colour",
    ["red", "#fff", "rgb(0,0,0)", "#12345g", "#123456;background:url(x)", "#123456\n", 0x123456],
)
def test_palette_refuses_non_hex(colour: object) -> None:
    with pytest.raises(ValidationError):
        schema.Palette(background=colour, text="#000000", accent="#2563eb", on_accent="#ffffff")


# ── Copy content QA ────────────────────────────────────────────────────────
def _content(copy: schema.CreativeCopy, banned: list[object] | None = None) -> qa.QAReport:
    return qa.check_copy_content(
        copy, spec=formats.get_format("instagram_portrait"), facts=FACTS, banned_phrases=banned or []
    )


def test_grounded_copy_passes_content_qa() -> None:
    copy = _parse("instagram_portrait", headline="Acme Trail 2 from ₹4,999", body="Rated 4.8 by 1200 runners.")
    report = _content(copy)
    assert report.passed, report.to_dict()


def test_invented_claim_guaranteed_is_rejected() -> None:
    report = _content(_parse("instagram_portrait", headline="Guaranteed faster trail times"))
    assert not report.passed
    assert "unsupportable_claim" in [i.code for i in report.issues]


@pytest.mark.parametrize("headline", ["Save 50% on trail shoes", "Join 10,000 happy runners", "Now ₹3,999 only"])
def test_invented_number_is_rejected(headline: str) -> None:
    report = _content(_parse("instagram_portrait", headline=headline))
    assert [i.code for i in report.issues] == ["ungrounded_number"]


def test_issue_messages_never_quote_the_copy() -> None:
    report = _content(_parse("instagram_portrait", headline="Guaranteed 77% off"))
    blob = json.dumps(report.to_dict())
    assert "77" not in blob and "Guaranteed" not in blob


@pytest.mark.parametrize("phrase", ["cheap", "CHEAP", "  best   in class "])
def test_brand_dont_phrase_is_rejected(phrase: str) -> None:
    copy = _parse("instagram_portrait", body="A cheap shoe that is best in class.")
    report = _content(copy, banned=[phrase])
    assert "banned_phrase" in [i.code for i in report.issues]


def test_banned_phrase_matches_whole_words_only() -> None:
    copy = _parse("instagram_portrait", body="Our cheapest pair still grips.")
    assert qa.check_banned_phrases(copy, ["cheap"]) is None
    # Non-string or regex-looking entries are inert, not errors.
    assert qa.check_banned_phrases(copy, [None, 42, ".*", ""]) is None


def test_all_caps_and_exclamations_follow_the_dco_rules() -> None:
    assert qa.check_platform_rules(_parse(headline="RUN THE TRAIL TODAY")).code == "all_caps_headline"  # type: ignore[union-attr]
    copy = _parse(body="Grippy! Light!")
    assert qa.check_platform_rules(copy).code == "too_many_exclamations"  # type: ignore[union-attr]


def test_short_banner_headline_is_only_a_warning() -> None:
    report = qa.check_copy_content(
        _parse(headline="Trail shoes", body=""), spec=formats.get_format("banner_300x250"), facts=FACTS
    )
    assert report.passed and [i.code for i in report.issues] == ["short_headline"]


def test_product_facts_from_orm_like_object() -> None:
    class Row:
        name = "Shoe"
        description = None
        price_text = "₹99"
        approved_claims = ["Ships in 2 days", {"bad": 1}]
        disclaimer = ""

    facts = ProductFacts.from_product(Row())
    assert facts.approved_claims == ("Ships in 2 days",)
    assert qa.check_numbers_grounded(_parse("instagram_portrait", headline="Ships in 2 days for 99", body="", claim_ids=[]), facts) is None


# ── Contrast ───────────────────────────────────────────────────────────────
def test_contrast_ratio_known_values() -> None:
    assert qa.contrast_ratio("#000000", "#ffffff") == pytest.approx(21.0)
    assert qa.contrast_ratio("#ffffff", "#ffffff") == pytest.approx(1.0)
    assert qa.contrast_ratio("#767676", "#ffffff") == pytest.approx(4.54, abs=0.01)


def test_body_text_needs_4_5_and_large_text_needs_3() -> None:
    grey = "#949494"  # about 3.03:1 on white
    assert 3.0 <= qa.contrast_ratio(grey, "#ffffff") < 4.5
    assert qa.check_contrast(grey, "#ffffff", size_px=16) is not None
    assert qa.check_contrast(grey, "#ffffff", size_px=24) is None
    assert qa.check_contrast(grey, "#ffffff", size_px=19, bold=True) is None
    assert qa.check_contrast(grey, "#ffffff", size_px=19, bold=False) is not None
    assert qa.check_contrast("#767676", "#ffffff", size_px=16) is None
    assert qa.check_contrast("#aaaaaa", "#ffffff", size_px=40) is not None


def test_contrast_refuses_non_hex() -> None:
    with pytest.raises(ValueError):
        qa.contrast_ratio("white", "#000000")


# ── Text fit ───────────────────────────────────────────────────────────────
def _fixed_width(size: int) -> qa.Measure:
    return lambda text: len(text) * size * 0.5


def test_fit_text_finds_the_largest_size() -> None:
    result = qa.fit_text(
        "Run further", Box(0, 0, 110, 40), font_id="inter", max_size_px=60, min_size_px=8,
        measure_factory=_fixed_width,
    )
    assert result.fits and result.size_px == 20 and result.lines == ("Run further",)


def test_text_that_cannot_fit_is_flagged() -> None:
    issue = qa.check_text_fit(
        "Supercalifragilistic trail shoes", Box(0, 0, 50, 20), font_id="inter",
        max_size_px=40, min_size_px=12, measure_factory=_fixed_width,
    )
    assert issue is not None and issue.code == "text_does_not_fit"


def test_text_fit_uses_pillow_metrics() -> None:
    measure = qa.pillow_measure("lato", 40)
    assert measure("Wide words") > measure("Wide")
    result = qa.fit_text("Run further on every trail", Box(0, 0, 300, 250), font_id="lato",
                         max_size_px=80, min_size_px=10, max_lines=2)
    assert result.fits and 10 <= result.size_px <= 80 and len(result.lines) <= 2


def test_text_fit_refuses_unknown_font() -> None:
    with pytest.raises(ValueError):
        qa.pillow_measure("../../etc/passwd", 20)


# ── Logo, safe zones, file size ────────────────────────────────────────────
def test_logo_size_and_clear_space() -> None:
    spec = formats.get_format("instagram_portrait")
    assert qa.check_logo(Box(100, 100, 200, 80), spec) == []
    assert [i.code for i in qa.check_logo(Box(100, 100, 60, 16), spec)] == ["logo_too_small"]
    # Clear space is 25% of the shorter side (20px): text 10px away crowds it.
    crowded = qa.check_logo(Box(100, 100, 200, 80), spec, others=[Box(310, 100, 100, 40)])
    assert [i.code for i in crowded] == ["logo_crowded"]
    assert [i.code for i in qa.check_logo(Box(0, 0, 200, 80), spec)] == ["logo_crowded"]


def test_pinterest_and_print_safe_areas() -> None:
    pin = formats.get_format("pinterest_pin")
    assert qa.check_safe_zone(Box(100, 100, 200, 50), pin) is not None
    assert qa.check_safe_zone(Box(100, 300, 200, 50), pin) is None
    flyer = formats.get_format("flyer_a4")
    assert qa.check_safe_zone(Box(10, 10, 100, 100), flyer) is not None  # inside the bleed
    assert qa.check_safe_zone(Box(100, 100, 100, 100), flyer) is None


def test_file_size_and_dimensions() -> None:
    banner = formats.get_format("banner_300x250")
    assert qa.check_file_size(150 * 1024, banner) is None
    assert qa.check_file_size(150 * 1024 + 1, banner).code == "file_too_large"  # type: ignore[union-attr]
    assert qa.check_file_size(10 * 1024 * 1024, formats.get_format("instagram_portrait")) is None
    assert qa.check_file_size(5 * 1024 * 1024 + 1, formats.get_format("x_post")) is not None
    assert qa.check_dimensions(300, 250, banner) is None
    assert qa.check_dimensions(300, 251, banner) is not None


# ── Fonts ──────────────────────────────────────────────────────────────────
def test_font_registry_matches_the_allow_list() -> None:
    assert tuple(fonts.FONTS) == FONT_IDS
    assert set(fonts.PILLOW_FONT_IDS) == {"lato", "poppins", "bebas_neue"}
    for spec in fonts.FONTS.values():
        assert spec.pillow_id in fonts.PILLOW_FONT_IDS
        assert spec.is_static != spec.is_variable
    assert fonts.get_font("inter").is_variable and fonts.get_font("lato").is_static


def test_pillow_paths_stay_inside_the_font_dir() -> None:
    for font_id in FONT_IDS:
        path = fonts.pillow_font_path(font_id, 700)
        assert path.resolve().is_relative_to(fonts.FONT_DIR.resolve())
        assert path.suffix == ".ttf"
    assert fonts.pillow_font_path("oswald").name == "BebasNeue-Regular.ttf"
    assert fonts.pillow_font_path("poppins", 650).name == "Poppins-SemiBold.ttf"
    with pytest.raises(ValueError):
        fonts.pillow_font_path("comic_sans")


def test_upstream_urls_are_pinned_and_encoded() -> None:
    url = fonts.manifest()["inter/Inter[opsz,wght].ttf"]
    assert url == (
        "https://raw.githubusercontent.com/google/fonts/"
        f"{fonts.FONTS_COMMIT}/ofl/inter/Inter%5Bopsz%2Cwght%5D.ttf"
    )
    assert "lato/OFL.txt" in fonts.manifest()


def test_font_lock_parsing_and_verification() -> None:
    data = b"\x00\x01\x00\x00" + b"font-bytes"
    path = "lato/Lato-Regular.ttf"
    url = fonts.manifest()[path]
    pins = fonts.parse_lock(f"# comment\n{hashlib.sha256(data).hexdigest()} {path} {url}\n")
    fonts.verify_font_bytes(path, data, pins)
    with pytest.raises(fonts.FontIntegrityError):
        fonts.verify_font_bytes(path, data + b"x", pins)
    with pytest.raises(fonts.FontIntegrityError):
        fonts.verify_font_bytes(path, b"version https://git-lfs.github.com/spec/v1", pins)
    with pytest.raises(fonts.FontIntegrityError):
        fonts.verify_font_bytes("poppins/Poppins-Bold.ttf", data, pins)


@pytest.mark.parametrize(
    "line",
    [
        "{sha} ../../etc/passwd https://evil.example/x",
        "{sha} lato/Lato-Regular.ttf https://evil.example/Lato-Regular.ttf",
        "nothex lato/Lato-Regular.ttf {url}",
        "{sha} lato/Lato-Regular.ttf {url}\n{sha} lato/Lato-Regular.ttf {url}",
    ],
)
def test_font_lock_refuses_entries_outside_the_registry(line: str) -> None:
    url = fonts.manifest()["lato/Lato-Regular.ttf"]
    with pytest.raises(fonts.FontIntegrityError):
        fonts.parse_lock(line.format(sha="a" * 64, url=url))


# ── DCO behaviour unchanged ────────────────────────────────────────────────
def test_check_copy_delegates_to_check_text_unchanged() -> None:
    assert specs.check_text(headline="Guaranteed results", body_copy="x") == (
        "contains an unsupportable claim: 'guaranteed'"
    )
    assert specs.check_copy(headline="Guaranteed results", body_copy="x", platform="Meta Ads") == (
        specs.check_text(headline="Guaranteed results", body_copy="x")
    )
    assert specs.check_copy(headline="x" * 31, body_copy="", platform="Google Ads").startswith("headline")
    assert specs.check_text(headline="Fine", body_copy="Calm copy.") is None
