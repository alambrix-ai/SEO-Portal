"""Which connectors an agent uses, and the operator's choice of them.

Pure unit tests: no database. The agent context, the setup screen's
statements, the configuration validator and the plan (entitlements) hook are
exercised against connector doubles, with the real agent declarations and
the real catalogue order.
"""
from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.agents.base import choice as choice_rules
from app.agents.base import pages as page_rules
from app.agents.base.context import AgentContext
from app.agents.base.contracts import UseMode
from app.agents.base.plain import guide_for
from app.agents.base.registry import all_agents, get_agent
from app.connectors.base import registry as connector_registry
from app.connectors.base.connector import Capability
from app.connectors.base.interfaces import RemotePage
from app.core.exceptions import ConnectorError
from app.models.agent import AgentRecord
from app.services import agent_requirements as req
from app.services import entitlements, images, views
from app.services.entitlements import ConnectorPolicy


# ── Helpers ────────────────────────────────────────────────────────────────
def _connector(slug: str, pages: list[RemotePage] | Exception | None = None) -> SimpleNamespace:
    """A connector double that supports exactly what its catalogue spec says."""
    spec = connector_registry.get_spec(slug)
    assert spec is not None, slug

    def list_pages(limit: int = 200) -> list[RemotePage]:
        if isinstance(pages, Exception):
            raise pages
        return list(pages or [])

    return SimpleNamespace(
        slug=slug,
        supports=lambda capability, _caps=spec.capabilities: capability in _caps,
        list_pages=list_pages,
    )


def _ctx(
    agent_slug: str,
    slugs: list[str],
    *,
    choices: dict | None = None,
    pages: dict | None = None,
    settings: dict | None = None,
) -> AgentContext:
    stored = dict(settings or {})
    if choices is not None:
        stored[choice_rules.CHOICES_KEY] = choices
    record = AgentRecord(slug=agent_slug, name=agent_slug, settings=stored, scope="")
    connectors = {slug: _connector(slug, (pages or {}).get(slug)) for slug in slugs}
    return AgentContext(
        db=None,  # type: ignore[arg-type]
        org=SimpleNamespace(id="t", settings={}),  # type: ignore[arg-type]
        record=record,
        cipher=None,  # type: ignore[arg-type]
        llm=None,  # type: ignore[arg-type]
        connectors=connectors,
        connector_names={s: connector_registry.get_spec(s).name for s in slugs},
        connected_slugs=frozenset(slugs),
    )


def _slugs(connectors) -> list[str]:  # noqa: ANN001
    return [getattr(c, "slug", None) for c in connectors]


def _slug(connector) -> str | None:  # noqa: ANN001
    return getattr(connector, "slug", None)


def _page(remote_id: str, url: str) -> RemotePage:
    return RemotePage(remote_id, url, url, metadata={"writable": True})


@pytest.fixture
def no_meta(monkeypatch):  # noqa: ANN001, ANN201
    """A plan that does not include Meta Ads for any agent."""

    def policy(db, org, agent_slug, capability):  # noqa: ANN001, ANN202, ARG001
        return ConnectorPolicy(
            allowed=frozenset(s for s in connector_registry.connector_slugs() if s != "meta_ads"),
            reason="Available on Pro",
        )

    monkeypatch.setattr(entitlements, "connector_policy", policy)


# ── Storage ────────────────────────────────────────────────────────────────
def test_selections_are_lists_and_old_string_values_still_read():
    stored = {"_connectors": {"create_page": "shopify", "list_pages": ["wordpress", "github", "wordpress"]}}
    assert choice_rules.saved_choices(stored) == {
        "create_page": ["shopify"],
        "list_pages": ["wordpress", "github"],
    }
    # A choice saved as a plain string before lists existed is still honoured.
    ctx = _ctx("seo_article_writer", ["wordpress", "shopify"], choices={"create_page": "shopify"})
    assert _slug(ctx.with_capability(Capability.CREATE_PAGE)) == "shopify"


def test_saved_choices_ignore_malformed_values():
    assert choice_rules.saved_choices({"_connectors": {"a": "x", "b": "", "c": 3, "d": [1, "y"]}}) == {
        "a": ["x"],
        "d": ["y"],
    }
    assert choice_rules.saved_choices({"_connectors": "nonsense"}) == {}
    assert choice_rules.saved_choices(None) == {}


# ── Runtime: one connector ─────────────────────────────────────────────────
def test_with_capability_uses_the_chosen_connector():
    ctx = _ctx("seo_article_writer", ["wordpress", "shopify"], choices={"create_page": ["shopify"]})
    assert _slug(ctx.with_capability(Capability.CREATE_PAGE)) == "shopify"
    assert _slugs(ctx.candidates(Capability.CREATE_PAGE)) == ["shopify"]


def test_without_a_choice_the_first_in_catalogue_order_is_used():
    ctx = _ctx("seo_article_writer", ["shopify", "wordpress"])
    assert _slug(ctx.with_capability(Capability.CREATE_PAGE)) == "wordpress"
    assert _slugs(ctx.candidates(Capability.CREATE_PAGE)) == ["wordpress", "shopify"]


def test_a_chosen_connector_that_was_disconnected_falls_back_to_automatic():
    ctx = _ctx("seo_article_writer", ["wordpress", "shopify"], choices={"create_page": ["webflow"]})
    assert ctx.chosen_connector(Capability.CREATE_PAGE) is None
    assert _slug(ctx.with_capability(Capability.CREATE_PAGE)) == "wordpress"


def test_a_choice_covers_the_capabilities_declared_with_it():
    ctx = _ctx(
        "review_responder", ["google_business_profile"], choices={"read_reviews": ["google_business_profile"]}
    )
    assert _slug(ctx.chosen_connector(Capability.REPLY_REVIEW)) == "google_business_profile"


def test_the_article_writer_saves_to_the_chosen_website():
    from app.agents.seo_article_writer.agent import choose_publisher

    ctx = _ctx("seo_article_writer", ["wordpress", "shopify"], choices={"create_page": ["shopify"]})
    publisher, _ = choose_publisher(ctx)
    assert _slug(publisher) == "shopify"


def test_the_email_marketer_uses_the_chosen_platform():
    from app.agents.email_marketer.agent import email_platform

    ctx = _ctx("email_marketer", ["mailchimp", "brevo"], choices={"create_email_campaign": ["brevo"]})
    assert _slug(email_platform(ctx)) == "brevo"
    assert _slug(email_platform(_ctx("email_marketer", ["mailchimp", "brevo"]))) == "mailchimp"


def test_images_are_made_with_the_chosen_ai_service():
    ctx = _ctx("social_media_manager", ["openai", "google_gemini"], choices={"generate_image": ["google_gemini"]})
    assert images.provider_for(ctx) == "google_gemini"
    assert images.provider_for(_ctx("social_media_manager", ["openai", "google_gemini"])) == "openai"


def test_image_generation_honours_the_provider(monkeypatch):  # noqa: ANN001
    from app.services import connectors as connector_service

    def keyed(key: str, image_model: str) -> SimpleNamespace:
        values = {"apiKey": key, "imageModel": image_model}
        return SimpleNamespace(
            name=key,
            credentials=SimpleNamespace(require=lambda k: values[k], get=lambda k, d="": values.get(k) or d),
        )

    bundle = SimpleNamespace(instances={"openai": keyed("o", "img-o"), "google_gemini": keyed("g", "img-g")})
    monkeypatch.setattr(connector_service, "build_connector_bundle", lambda db, org: bundle)
    used: list[tuple[str, str]] = []
    monkeypatch.setattr(
        images, "_openai", lambda key, model, prompt, size: used.append((key, model)) or (b"x", "image/png", 1, 1)
    )
    monkeypatch.setattr(
        images, "_gemini", lambda key, model, prompt: used.append((key, model)) or (b"x", "image/png", 1, 1)
    )
    db = SimpleNamespace(add=lambda row: None, flush=lambda: None)
    org = SimpleNamespace(id="t")
    images.generate(db, org, "p", provider="google_gemini")
    images.generate(db, org, "p")
    images.generate(db, org, "p", provider="disconnected")
    # Each call uses that connection's own chosen image model.
    assert used == [("g", "img-g"), ("o", "img-o"), ("o", "img-o")]


# ── Runtime: several websites ──────────────────────────────────────────────
def test_harvest_each_collects_from_every_chosen_website():
    pages = {
        "wordpress": [_page("1", "/a")],
        "shopify": [_page("2", "/b")],
        "github": [_page("3", "/c")],
    }
    ctx = _ctx(
        "on_page_seo_sync",
        ["wordpress", "shopify", "github"],
        choices={"list_pages": ["wordpress", "github"]},
        pages=pages,
    )
    found = page_rules.harvest_each(ctx, need_usable=True, prefer_writable=True)
    assert sorted(h.source_slug for h in found.found) == ["github", "wordpress"]
    assert found.problems == []


def test_one_failing_website_is_reported_and_does_not_stop_the_others():
    pages = {"wordpress": ConnectorError("token expired"), "github": [_page("3", "/c")]}
    ctx = _ctx("on_page_seo_sync", ["wordpress", "github"], choices={"list_pages": ["wordpress", "github"]}, pages=pages)
    found = page_rules.harvest_each(ctx, need_usable=True, prefer_writable=True)
    assert [h.source_slug for h in found.found] == ["github"]
    assert len(found.problems) == 1 and "WordPress" in found.problems[0] and "token expired" in found.problems[0]


def test_harvest_each_without_a_choice_keeps_first_that_works():
    pages = {"wordpress": [_page("1", "/a")], "shopify": [_page("2", "/b")]}
    ctx = _ctx("on_page_seo_sync", ["wordpress", "shopify"], pages=pages)
    found = page_rules.harvest_each(ctx, need_usable=True, prefer_writable=True)
    assert [h.source_slug for h in found.found] == ["wordpress"]


def test_the_crawler_copy_is_synced_first_so_a_writable_copy_wins():
    crawled = RemotePage("u", "/a", "A", metadata={"source": "rendered"})
    pages = {"wordpress": [_page("1", "/a")], "site_crawler": [crawled]}
    ctx = _ctx(
        "on_page_seo_sync", ["wordpress", "site_crawler"], choices={"list_pages": ["wordpress", "site_crawler"]},
        pages=pages,
    )
    found = page_rules.harvest_each(ctx, need_usable=True, prefer_writable=True)
    assert [h.source_slug for h in found.found] == ["site_crawler", "wordpress"]


def test_page_agents_work_only_on_pages_from_the_chosen_websites():
    chosen = _ctx("technical_seo_auditor", ["wordpress", "github"], choices={"list_pages": ["github"]})
    assert chosen.page_sources(Capability.LIST_PAGES) == ["github"]
    assert _ctx("technical_seo_auditor", ["wordpress", "github"]).page_sources(Capability.LIST_PAGES) is None

    aeo = _ctx("aeo_qa_injector", ["wordpress", "github"], choices={"write_page": ["wordpress"]})
    assert aeo.page_sources(Capability.WRITE_PAGE) == ["wordpress"]
    # A chosen website that is disconnected right now still bounds the pages:
    # nothing from an unchosen website is touched.
    gone = _ctx("knowledge_graph_schema", ["github"], choices={"write_page": ["wordpress"]})
    assert gone.page_sources(Capability.WRITE_PAGE) == ["wordpress"]


def test_on_page_rewrites_follow_the_websites_it_collects_from():
    ctx = _ctx("on_page_seo_sync", ["wordpress", "github"], choices={"list_pages": ["github"]})
    assert ctx.page_sources(Capability.WRITE_PAGE) == ["github"]
    assert _ctx("on_page_seo_sync", ["wordpress"]).page_sources(Capability.WRITE_PAGE) is None


def test_source_filter_builds_a_sql_condition():
    from app.models.seo import SeoPage

    ctx = _ctx("aeo_qa_injector", ["wordpress", "github"], choices={"write_page": ["wordpress"]})
    condition = page_rules.source_filter(ctx, SeoPage.cms_slug, Capability.WRITE_PAGE)
    assert "cms_slug" in str(condition) and "IN" in str(condition).upper()
    assert page_rules.source_filter(_ctx("aeo_qa_injector", ["wordpress"]), SeoPage.cms_slug, Capability.WRITE_PAGE) is None


# ── Runtime: uses all ──────────────────────────────────────────────────────
def test_uses_all_defaults_to_every_connected_one():
    ctx = _ctx("predictive_budget_engine", ["google_ads", "meta_ads", "linkedin_ads"])
    assert _slugs(ctx.all_with_capability(Capability.READ_AD_PERFORMANCE)) == [
        "google_ads",
        "meta_ads",
        "linkedin_ads",
    ]


def test_the_budget_engine_manages_only_the_chosen_platforms():
    from app.agents.predictive_budget_engine.agent import excluded_channels
    from app.models.ads import CHANNEL_CONNECTOR

    ctx = _ctx(
        "predictive_budget_engine",
        ["google_ads", "meta_ads", "linkedin_ads"],
        choices={"read_ad_performance": ["google_ads", "linkedin_ads"]},
    )
    assert _slugs(ctx.all_with_capability(Capability.READ_AD_PERFORMANCE)) == ["google_ads", "linkedin_ads"]
    # The budget write follows the same selection.
    assert _slugs(ctx.all_with_capability(Capability.WRITE_AD_BUDGET)) == ["google_ads", "linkedin_ads"]
    assert not ctx.uses_connector("meta_ads", Capability.WRITE_AD_BUDGET)
    meta_channels = {ch for ch, slug in CHANNEL_CONNECTOR.items() if slug == "meta_ads"}
    assert meta_channels and excluded_channels(ctx) == meta_channels


def test_the_creative_optimizer_writes_only_for_the_chosen_platforms():
    ctx = _ctx(
        "dynamic_creative_optimizer", ["google_ads", "meta_ads"], choices={"upload_creative": ["meta_ads"]}
    )
    assert _slugs(ctx.all_with_capability(Capability.UPLOAD_CREATIVE)) == ["meta_ads"]


def test_click_fraud_blocks_only_on_chosen_platforms_that_can_block():
    ctx = _ctx(
        "click_fraud_controller",
        ["google_ads", "meta_ads", "dsp_exchange"],
        choices={"read_traffic_quality": ["google_ads", "meta_ads"]},
    )
    assert _slugs(ctx.all_with_capability(Capability.READ_TRAFFIC_QUALITY)) == ["google_ads", "meta_ads"]
    assert _slugs(ctx.all_with_capability(Capability.BLOCK_PLACEMENT)) == ["google_ads"]


def test_a_uses_all_selection_is_never_widened_when_its_platforms_disconnect():
    ctx = _ctx("predictive_budget_engine", ["google_ads"], choices={"read_ad_performance": ["meta_ads"]})
    assert ctx.all_with_capability(Capability.READ_AD_PERFORMANCE) == []


def test_a_one_connector_choice_never_narrows_a_capability_the_agent_uses_all_of():
    # The spam guard reads from one analytics account but pushes exclusions
    # to every one; its choice of reader must not narrow the push.
    ctx = _ctx(
        "referral_spam_guard",
        ["google_analytics_4", "adobe_analytics"],
        choices={"read_referrers": ["adobe_analytics"]},
    )
    assert _slug(ctx.with_capability(Capability.READ_REFERRERS)) == "adobe_analytics"
    assert _slugs(ctx.all_with_capability(Capability.READ_REFERRERS)) == [
        "google_analytics_4",
        "adobe_analytics",
    ]


def test_social_selection_replaces_the_old_networks_setting():
    from app.agents.social_media_manager.agent import chosen_networks

    slugs = ["facebook_pages", "x_twitter", "linkedin_pages"]
    selected = _ctx(
        "social_media_manager", slugs,
        choices={"publish_social": ["x_twitter"]},
        settings={"networks": "facebook, pinterest"},
    )
    assert chosen_networks(selected) == (["x"], [])

    # Saved before the selection existed: still honoured, downloads included.
    legacy = _ctx("social_media_manager", slugs, settings={"networks": "facebook, pinterest"})
    assert chosen_networks(legacy)[0] == ["facebook", "pinterest"]

    every = _ctx("social_media_manager", slugs)
    assert chosen_networks(every)[0] == ["facebook", "linkedin", "x"]


def test_saving_a_social_selection_retires_the_old_setting_on_screen():
    agent = get_agent("social_media_manager")
    [use] = req.uses_for(
        agent, (Capability.PUBLISH_SOCIAL,), usable={"facebook_pages"}, settings={"networks": "pinterest"}
    )
    assert "saved earlier" in use.statement and "pinterest" in use.statement
    [now] = req.uses_for(
        agent,
        (Capability.PUBLISH_SOCIAL,),
        usable={"facebook_pages"},
        choices={"publish_social": ["facebook_pages"]},
        settings={"networks": "pinterest"},
    )
    assert "saved earlier" not in now.statement


# ── Statements ─────────────────────────────────────────────────────────────
def _use(agent: str, capability: Capability):  # noqa: ANN202
    use = get_agent(agent).spec.use_for(capability)
    assert use is not None
    return use


def test_one_connected_is_preselected_and_stated():
    use = req.describe_use(_use("seo_article_writer", Capability.GENERATE_IMAGE), usable={"openai"})
    assert use.control == "single" and use.selected == ["openai"]
    assert use.statement == "Uses OpenAI."


def test_two_connected_preselect_what_is_used():
    use = req.describe_use(
        _use("seo_article_writer", Capability.GENERATE_IMAGE), usable={"openai", "google_gemini"}
    )
    assert use.control == "single" and not use.automatic_option
    assert [o.slug for o in use.options] == ["openai", "google_gemini"]
    assert use.selected == ["openai"] and use.statement == "Uses OpenAI."


def test_an_agent_that_tries_in_turn_offers_automatic():
    use = req.describe_use(_use("seo_article_writer", Capability.CREATE_PAGE), usable={"wordpress", "shopify"})
    assert use.automatic_option and use.selected == []
    assert use.statement == (
        "Automatic tries WordPress, then Shopify, and uses the first one that is set up to create pages."
    )


def test_a_disconnected_single_choice_names_only_what_is_used():
    use = req.describe_use(
        _use("seo_article_writer", Capability.GENERATE_IMAGE), usable={"google_gemini"}, chosen=["openai"]
    )
    assert use.stale and use.selected == ["google_gemini"]
    # Every connector is equal: the old choice is not mentioned.
    assert use.statement == "Uses Google Gemini."


def test_several_websites_are_stated():
    use = req.describe_use(
        _use("on_page_seo_sync", Capability.LIST_PAGES),
        usable={"wordpress", "github", "shopify"},
        chosen=["github", "wordpress"],
    )
    assert use.control == "multi" and use.selected == ["wordpress", "github"]
    assert use.statement == "Collects pages from WordPress and GitHub."
    assert use.template == "Collects pages from {names}."


def test_page_source_defaults_to_every_website_and_states_a_selection():
    every = req.describe_use(_use("technical_seo_auditor", Capability.LIST_PAGES), usable={"wordpress", "github"})
    assert every.control == "multi" and every.selected == ["wordpress", "github"]
    assert every.statement == "Audits every page On-Page SEO Sync has collected."
    some = req.describe_use(
        _use("technical_seo_auditor", Capability.LIST_PAGES), usable={"wordpress", "github"}, chosen=["github"]
    )
    assert some.statement == "Audits the pages collected from GitHub."


def test_uses_all_says_all_or_how_many_of_how_many():
    use = _use("predictive_budget_engine", Capability.READ_AD_PERFORMANCE)
    usable = {"meta_ads", "google_ads", "linkedin_ads"}
    every = req.describe_use(use, usable=usable)
    assert every.control == "multi" and every.selected == ["google_ads", "meta_ads", "linkedin_ads"]
    assert every.statement == "Uses all connected ad platforms: Google Ads, Meta Ads and LinkedIn Ads."
    some = req.describe_use(use, usable=usable, chosen=["google_ads", "meta_ads"])
    assert some.statement == "Uses Google Ads and Meta Ads (of 3 connected)."
    assert "keeps its current budget" in some.note


def test_needs_carry_the_uses_and_a_shared_choice_is_asked_once():
    needs = req.needs_for(get_agent("review_responder"), usable=frozenset({"google_business_profile"}))
    uses = [u for n in needs for u in n.uses]
    assert [u.primary for u in uses] == [True, False]
    assert [u.control for u in uses] == ["single", ""]


def test_a_followed_selection_is_reported_without_a_second_control():
    needs = req.needs_for(
        get_agent("on_page_seo_sync"),
        usable=frozenset({"wordpress", "site_crawler"}),
        choices={"list_pages": ["wordpress", "site_crawler"]},
    )
    write = next(u for n in needs for u in n.uses if n.key == "write_page")
    assert write.control == "" and not write.primary
    # The crawler cannot be written to, so only WordPress is named.
    assert write.statement == "Rewrites only pages collected from WordPress."


def test_agent_view_returns_the_controls():
    record = AgentRecord(
        slug="on_page_seo_sync", name="On-Page SEO Sync", category="SEO & AEO", description="",
        status="paused", autonomy=False, metric_label="", schedule="Daily", scope="",
        notify_channel="None", llm_connector="", max_actions_per_day=20, configured=False,
        last_error="", actions_today=0,
        settings={choice_rules.CHOICES_KEY: {"list_pages": "github"}},
    )
    env = views.GuideEnv(
        usable=frozenset({"wordpress", "github"}),
        enabled_connectors={"wordpress", "github", "site_crawler"},
        guardrail="hybrid",
        has_pages=False,
    )
    out = views.agent_out(record, can_write=True, env=env)
    [use] = next(n for n in out.needs if n.key == "list_pages").uses
    assert use.control == "multi" and use.chosen == ["github"] and use.selected == ["github"]
    assert use.statement == "Collects pages from GitHub."


# ── Validation ─────────────────────────────────────────────────────────────
def test_valid_choices_are_stored_as_lists():
    stored, problems = req.clean_choices(
        get_agent("on_page_seo_sync"), {"list_pages": ["wordpress", "github"]}, usable={"wordpress", "github"}
    )
    assert problems == [] and stored == {"list_pages": ["wordpress", "github"]}
    single, problems = req.clean_choices(
        get_agent("seo_article_writer"), {"create_page": "shopify", "generate_image": []}, usable={"shopify"}
    )
    assert problems == [] and single == {"create_page": ["shopify"]}


@pytest.mark.parametrize(
    ("agent", "requested", "fragment"),
    [
        ("seo_article_writer", {"create_page": ["wordpress", "shopify"]}, "choose only one"),
        ("seo_article_writer", {"generate_image": ["openai", "google_gemini"]}, "choose only one"),
        ("on_page_seo_sync", {"list_pages": ["wordpress", "wordpress"]}, "listed twice"),
        ("on_page_seo_sync", {"write_page": ["wordpress"]}, "no choice of connection"),
        ("seo_article_writer", {"create_page": ["site_crawler"]}, "cannot publish new articles"),
        ("seo_article_writer", {"create_page": ["webflow"]}, "is not connected"),
        ("seo_article_writer", {"list_pages": ["wordpress"]}, "no choice of connection"),
        ("seo_article_writer", {"not_a_capability": ["wordpress"]}, "no connection choice"),
        ("seo_article_writer", {"create_page": ["no_such_connector"]}, "not an integration available"),
        ("predictive_budget_engine", {"write_ad_budget": ["google_ads"]}, "no choice of connection"),
        ("predictive_budget_engine", {"read_ad_performance": [3]}, "must be a list"),
    ],
)
def test_bad_choices_are_refused_with_a_reason(agent, requested, fragment):  # noqa: ANN001
    stored, problems = req.clean_choices(
        get_agent(agent),
        requested,
        usable={"wordpress", "shopify", "site_crawler", "openai", "google_gemini", "google_ads"},
    )
    assert stored == {} and len(problems) == 1 and fragment in problems[0], problems


def test_resaving_a_choice_whose_connector_was_disconnected_keeps_it():
    stored, problems = req.clean_choices(
        get_agent("on_page_seo_sync"),
        {"list_pages": ["webflow", "wordpress"]},
        usable={"wordpress"},
        saved={"list_pages": "webflow"},
    )
    assert problems == [] and stored == {"list_pages": ["webflow", "wordpress"]}


def test_a_connector_disabled_for_the_portal_cannot_be_chosen():
    _, problems = req.clean_choices(
        get_agent("seo_article_writer"), {"create_page": ["shopify"]}, usable={"shopify"}, enabled={"wordpress"}
    )
    assert problems and "not an integration available" in problems[0]


# ── Entitlements ───────────────────────────────────────────────────────────
def test_today_nothing_is_restricted():
    policy = entitlements.connector_policy(None, None, "predictive_budget_engine", Capability.READ_AD_PERFORMANCE)
    assert policy.allowed is None and policy.max_selected is None


def test_a_plan_restriction_is_applied_at_runtime(no_meta):  # noqa: ANN001
    ctx = _ctx("predictive_budget_engine", ["google_ads", "meta_ads"])
    assert _slugs(ctx.all_with_capability(Capability.READ_AD_PERFORMANCE)) == ["google_ads"]
    one = _ctx("dynamic_creative_optimizer", ["meta_ads"])
    assert one.with_capability(Capability.UPLOAD_CREATIVE) is None


def test_a_plan_restriction_is_refused_on_save(no_meta):  # noqa: ANN001
    agent = get_agent("predictive_budget_engine")
    _, problems = req.clean_choices(
        agent,
        {"read_ad_performance": ["meta_ads"]},
        usable={"google_ads", "meta_ads"},
        policy=entitlements.lookup(None, None),
    )
    assert problems and "Available on Pro" in problems[0]


def test_a_plan_restriction_is_shown_as_a_locked_option(no_meta):  # noqa: ANN001
    agent = get_agent("predictive_budget_engine")
    needs = req.needs_for(agent, usable=frozenset({"google_ads", "meta_ads"}), policy=entitlements.lookup(None, None))
    [use] = next(n for n in needs if n.key == "read_ad_performance").uses
    meta = next(o for o in use.options if o.slug == "meta_ads")
    assert meta.locked and meta.locked_reason == "Available on Pro"
    assert use.selected == ["google_ads"]
    assert use.statement == "Uses all connected ad platforms: Google Ads."


def test_a_plan_limit_on_how_many(monkeypatch):  # noqa: ANN001
    monkeypatch.setattr(
        entitlements, "connector_policy", lambda db, org, a, c: ConnectorPolicy(max_selected=1, reason="Upgrade")
    )
    _, problems = req.clean_choices(
        get_agent("on_page_seo_sync"),
        {"list_pages": ["wordpress", "github"]},
        usable={"wordpress", "github"},
        policy=entitlements.lookup(None, None),
    )
    assert problems and "at most 1" in problems[0]
    ctx = _ctx("predictive_budget_engine", ["google_ads", "meta_ads"])
    assert _slugs(ctx.all_with_capability(Capability.READ_AD_PERFORMANCE)) == ["google_ads"]


# ── The classification matches the code ────────────────────────────────────
_LOOKUP = re.compile(r"\b(with_capability|all_with_capability|candidates)\(\s*Capability\.([A-Z_]+)")
_AGENTS = Path(__file__).resolve().parents[1] / "app" / "agents"


def test_every_connector_lookup_in_an_agent_is_declared_with_the_right_mode():
    """``ctx.with_capability`` / ``candidates`` pick one; ``all_with_capability`` uses all.

    An agent that reads from one connector and also pushes something to all
    of them (the spam guard) declares ONE; so ``all_with_capability`` is
    allowed under either mode, and the single-connector lookups only under ONE.
    """
    for agent in all_agents():
        source = (_AGENTS / agent.slug / "agent.py").read_text(encoding="utf-8")
        for call, name in _LOOKUP.findall(source):
            capability = Capability[name]
            if capability is Capability.COMPLETE and agent.spec.requires_llm:
                continue  # the AI model has its own picker
            use = agent.spec.use_for(capability)
            assert use is not None, f"{agent.slug} looks up {name} without declaring it"
            if call in ("with_capability", "candidates"):
                assert use.mode is UseMode.ONE, f"{agent.slug}: {call}({name}) but declared {use.mode}"


def test_every_capability_an_agent_lists_as_a_need_is_classified():
    for agent in all_agents():
        guide = guide_for(agent.slug)
        listed = {
            *agent.spec.required_capabilities,
            *agent.spec.any_of_capabilities,
            *(extra.capability for extra in (guide.optional if guide else ())),
        }
        if agent.spec.requires_llm:
            listed.discard(Capability.COMPLETE)
        for capability in listed:
            assert agent.spec.use_for(capability) is not None, (agent.slug, capability)
        for use in agent.spec.connector_use:
            if use.follows is not None:
                assert agent.spec.use_for(use.follows) is not None, (agent.slug, use.follows)
                continue
            # A choice nobody can see is a choice nobody can make.
            assert ({use.capability, *use.also} & listed), (
                f"{agent.slug} offers a choice for {use.capability} but no need shows it"
            )
            assert use.question.endswith("?"), (agent.slug, use.capability)
            if use.mode is UseMode.ALL:
                assert use.group, (agent.slug, use.capability)
            if use.mode is UseMode.PAGE_SOURCE:
                assert use.multi and use.everything and use.selected, (agent.slug, use.capability)
