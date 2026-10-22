"""Creative Studio model choice: allow/deny globs, friendly picker, strict resolution.

Pure unit tests. Connections are real connector classes with made-up keys;
no vendor is ever called. Model ids here are shaped like real ones only so the
recommended globs from .env.example can be exercised.
"""
from __future__ import annotations

import pytest

from app.connectors.base import registry
from app.connectors.base.credentials import Credentials
from app.core.config import settings
from app.services.creative import model_choice as mc
from app.services.creative.model_choice import ModelChoiceError

KEY = "sk-test-SECRET-key-0123456789abcdef"

# The recommended lists from research R4 / .env.example.
TEXT_ALLOW = ["claude-*", "gpt-*", "o[0-9]*", "gemini-*", "sonar*", "grok-*", "ollama:*", "vllm:*"]
TEXT_DENY = [
    "*image*", "*realtime*", "*audio*", "*tts*", "*transcribe*", "*embedding*",
    "*-live*", "gemini-nano-banana*", "*video*",
]
IMAGE_ALLOW = ["gpt-image-*", "chatgpt-image-*", "gemini-*-image*", "gemini-nano-banana*"]
IMAGE_DENY = ["dall-e-*", "gpt-image-1"]


@pytest.fixture
def recommended(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "creative_allowed_text_models", TEXT_ALLOW)
    monkeypatch.setattr(settings, "creative_denied_text_models", TEXT_DENY)
    monkeypatch.setattr(settings, "creative_allowed_image_models", IMAGE_ALLOW)
    monkeypatch.setattr(settings, "creative_denied_image_models", IMAGE_DENY)


@pytest.fixture
def deny_all(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "creative_allowed_text_models", "creative_denied_text_models",
        "creative_allowed_image_models", "creative_denied_image_models",
    ):
        monkeypatch.setattr(settings, name, [])


def _connected(slug: str, values: dict[str, str]):  # noqa: ANN202
    return registry.get_class(slug)(Credentials(values=values), org_id="o")


def _bundle() -> dict:
    return {
        "anthropic_claude": _connected("anthropic_claude", {"apiKey": KEY, "model": "claude-test-1"}),
        "openai": _connected(
            "openai", {"apiKey": KEY, "model": "gpt-test-5", "imageModel": "gpt-image-2"}
        ),
    }


# ── Matching ───────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("connector", "model", "allowed"),
    [
        ("anthropic_claude", "claude-test-1", True),
        ("anthropic_claude", "CLAUDE-Test-1", True),  # case-insensitive
        ("openai", "gpt-test-5", True),
        ("openai", "o4", True),
        ("openai", "o4-mini", True),
        ("perplexity", "sonar-pro", True),
        ("openai", "gpt-realtime-x", False),  # denied
        ("openai", "gpt-image-2", False),  # an image model is not a writer
        ("openai", "text-embedding-3", False),  # not allowed at all
        ("google_gemini", "gemini-nano-banana-2", False),  # denied for text
        ("anthropic_claude", "", False),
        ("anthropic_claude", "other-model", False),
    ],
)
def test_text_allow_and_deny_globs(recommended, connector: str, model: str, allowed: bool) -> None:  # noqa: ANN001
    assert mc.text_approved(connector, model) is allowed


def test_a_connector_prefix_limits_a_pattern_to_that_connector() -> None:
    assert mc.is_approved("ollama", "llama3:8b", ["ollama:*"], [])
    assert mc.is_approved("vllm", "mistral-7b", ["ollama:*", "vllm:*"], [])
    # The same model through any other connector is not approved.
    assert not mc.is_approved("openai", "llama3:8b", ["ollama:*"], [])
    assert not mc.is_approved("anthropic_claude", "anything", ["ollama:*"], [])
    # A connector-scoped deny wins over an unscoped allow.
    assert not mc.is_approved("openai", "gpt-x", ["gpt-*"], ["openai:gpt-x"])
    assert mc.is_approved("google_gemini", "gpt-x", ["gpt-*"], ["openai:gpt-x"])


def test_an_unknown_prefix_is_part_of_the_model_glob() -> None:
    # Ollama ids contain ':' themselves; "llama3:*" is a model glob, not a connector.
    assert mc.is_approved("ollama", "llama3:8b", ["llama3:*"], [])


def test_image_allow_and_deny_globs(recommended) -> None:  # noqa: ANN001
    assert mc.image_approved("openai", "gpt-image-2")
    assert mc.image_approved("google_gemini", "gemini-3-pro-image-preview")
    assert mc.image_approved("google_gemini", "gemini-nano-banana-2")
    # Exact deny for the retiring model, but not its siblings.
    assert not mc.image_approved("openai", "gpt-image-1")
    assert mc.image_approved("openai", "gpt-image-1.5")
    assert not mc.image_approved("openai", "dall-e-3")
    assert not mc.image_approved("openai", "gpt-test-5")


def test_an_empty_allow_list_approves_nothing() -> None:
    assert not mc.is_approved("openai", "gpt-test-5", [], [])
    assert not mc.is_approved("openai", "gpt-test-5", [], ["nothing"])


def test_fnmatch_is_case_sensitive_only_after_lower_casing() -> None:
    # Patterns and ids are both lower-cased, so a shouty config still works.
    assert mc.is_approved("openai", "gpt-test", ["GPT-*"], [])


# ── Picker ─────────────────────────────────────────────────────────────────
def test_picker_uses_friendly_names_and_keeps_raw_ids_in_advanced(recommended) -> None:  # noqa: ANN001
    picker = mc.build_picker(_bundle())
    assert picker.text_ready is True
    assert picker.reason == ""
    labels = [o.label for o in picker.text]
    assert labels == ["Claude, best for writing", "ChatGPT"]
    claude = picker.text[0]
    assert claude.available and claude.advanced.model == "claude-test-1"
    assert claude.advanced.connector == "anthropic_claude"
    # No raw model id in anything but `advanced`.
    for option in picker.text + picker.image:
        for raw in ("claude-test-1", "gpt-test-5", "gpt-image-2", "anthropic_claude"):
            assert raw not in option.label and raw not in option.reason
    image_labels = [o.label for o in picker.image]
    assert image_labels == ["OpenAI image", "No AI image: use my photos or brand colours"]
    assert all(o.available for o in picker.image)


def test_empty_allow_lists_give_a_plain_reason_not_an_error(deny_all) -> None:  # noqa: ANN001
    picker = mc.build_picker(_bundle())
    assert picker.text_ready is False
    assert picker.reason == "An administrator needs to approve AI models for Creative Studio."
    assert picker.reason_code == "models_not_approved"
    assert all(not o.available and o.reason == mc.NEEDS_APPROVAL for o in picker.text)
    # "No AI image" stays available even when nothing else is approved.
    none = picker.image[-1]
    assert none.key == "none" and none.available
    assert [o.available for o in picker.image[:-1]] == [False]


def test_a_saved_model_off_the_allow_list_is_shown_unavailable(recommended, monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setattr(settings, "creative_allowed_text_models", ["claude-*"])
    picker = mc.build_picker(_bundle())
    by_key = {o.key: o for o in picker.text}
    assert by_key["anthropic_claude"].available
    assert not by_key["openai"].available
    assert by_key["openai"].reason_code == "model_not_allowed"


def test_picker_with_no_connections_says_so(recommended) -> None:  # noqa: ANN001
    picker = mc.build_picker({})
    assert picker.text == [] and picker.text_ready is False
    assert picker.reason_code == "not_connected"
    assert [o.key for o in picker.image] == ["none"]


def test_self_hosted_connectors_have_no_image_option() -> None:
    assert "ollama" in mc.TEXT_LABELS and "vllm" in mc.TEXT_LABELS
    assert "ollama" not in mc.IMAGE_LABELS and "vllm" not in mc.IMAGE_LABELS
    assert mc.TEXT_LABELS["ollama"] == "Your own model (Ollama)"


# ── Strict text resolution ────────────────────────────────────────────────
def test_resolve_text_uses_exactly_the_chosen_connector_and_model(recommended) -> None:  # noqa: ANN001
    choice = mc.resolve_text(_bundle(), "openai", "gpt-test-6")
    assert (choice.connector, choice.model) == ("openai", "gpt-test-6")
    assert choice.provider.model == "gpt-test-6"


def test_resolve_text_defaults_to_the_saved_model_and_still_checks_it(recommended) -> None:  # noqa: ANN001
    choice = mc.resolve_text(_bundle(), "anthropic_claude")
    assert choice.model == "claude-test-1"


def test_a_disconnected_model_is_refused_without_falling_back(recommended) -> None:  # noqa: ANN001
    with pytest.raises(ModelChoiceError) as err:
        mc.resolve_text(_bundle(), "google_gemini", "gemini-test-2")
    assert err.value.reason == "not_connected"
    assert err.value.status_code == 422
    assert "Gemini isn't connected" in err.value.message


def test_a_model_not_on_the_allow_list_is_refused(recommended) -> None:  # noqa: ANN001
    with pytest.raises(ModelChoiceError) as err:
        mc.resolve_text(_bundle(), "openai", "text-embedding-3")
    assert err.value.reason == "model_not_allowed"
    assert "text-embedding" not in err.value.message


def test_a_denied_model_is_refused_even_though_allowed(recommended) -> None:  # noqa: ANN001
    with pytest.raises(ModelChoiceError) as err:
        mc.resolve_text(_bundle(), "openai", "gpt-realtime-x")
    assert err.value.reason == "model_not_allowed"


def test_an_empty_allow_list_refuses_with_the_approval_message(deny_all) -> None:  # noqa: ANN001
    with pytest.raises(ModelChoiceError) as err:
        mc.resolve_text(_bundle(), "anthropic_claude", "claude-test-1")
    assert err.value.reason == "models_not_approved"
    assert err.value.message == mc.NEEDS_APPROVAL


@pytest.mark.parametrize("connector", ["wordpress", "made_up", "", "OpenAI; DROP", "../openai", None, 7])
def test_an_unknown_connector_is_refused(recommended, connector) -> None:  # noqa: ANN001
    with pytest.raises(ModelChoiceError) as err:
        mc.resolve_text(_bundle(), connector, "gpt-test-5")
    assert err.value.reason == "unknown_connector"


@pytest.mark.parametrize("bad", ["../../v1/files", "m?key=1", "a/b", "m x", "x" * 129, "-m"])
def test_an_unsafe_model_id_is_refused(recommended, bad: str) -> None:  # noqa: ANN001
    with pytest.raises(ModelChoiceError):
        mc.resolve_text(_bundle(), "openai", bad)


def test_a_connection_without_a_key_is_not_ready_and_leaks_nothing(recommended) -> None:  # noqa: ANN001
    broken = {"openai": _connected("openai", {"model": "gpt-test-5"})}
    with pytest.raises(ModelChoiceError) as err:
        mc.resolve_text(broken, "openai", "gpt-test-5")
    assert err.value.reason == "model_unavailable"
    assert "apiKey" not in err.value.message and "Connectors" not in err.value.message


# ── Strict image resolution ───────────────────────────────────────────────
def test_no_ai_image_is_always_allowed(deny_all) -> None:  # noqa: ANN001
    choice = mc.resolve_image({}, "none")
    assert choice.uses_ai is False and choice.model == ""


def test_resolve_image_checks_connection_and_lists(recommended) -> None:  # noqa: ANN001
    assert mc.resolve_image(_bundle(), "openai").model == "gpt-image-2"
    with pytest.raises(ModelChoiceError) as err:
        mc.resolve_image(_bundle(), "openai", "gpt-image-1")
    assert err.value.reason == "model_not_allowed"
    with pytest.raises(ModelChoiceError) as err:
        mc.resolve_image(_bundle(), "google_gemini", "gemini-3-pro-image")
    assert err.value.reason == "not_connected"
    with pytest.raises(ModelChoiceError) as err:
        mc.resolve_image(_bundle(), "ollama", "anything")
    assert err.value.reason == "unknown_connector"


def test_resolve_image_with_an_empty_allow_list(deny_all) -> None:  # noqa: ANN001
    with pytest.raises(ModelChoiceError) as err:
        mc.resolve_image(_bundle(), "openai", "gpt-image-2")
    assert err.value.reason == "models_not_approved"


def test_the_api_error_shape_carries_a_reason_key(recommended) -> None:  # noqa: ANN001
    from app.core.user_messages import public_error_message

    with pytest.raises(ModelChoiceError) as err:
        mc.resolve_text(_bundle(), "google_gemini", "gemini-test-2")
    # The API handler shows the authored message as written.
    assert public_error_message(err.value) == err.value.message
    assert err.value.details == {"reason": "not_connected"}
