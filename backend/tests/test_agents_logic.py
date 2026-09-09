"""Tests for the agents' decision logic.

These cover the parts that decide something real without a database: the
budget allocator that moves money, the fraud and spam detectors that block
traffic, the creative validator that gates what goes out under the brand, and
the audience clustering.
"""
from __future__ import annotations

import pytest

from app.agents.click_fraud_controller import detectors
from app.agents.dynamic_creative_optimizer import specs
from app.agents.first_party_audience_modeler import vectors
from app.agents.predictive_budget_engine import allocator
from app.agents.referral_spam_guard import rules
from app.connectors.base.interfaces import TrafficSample


# ── Budget allocator ───────────────────────────────────────────────────────
def _states(**channels: tuple[int, float, int]) -> list[allocator.ChannelState]:
    """channel -> (percent, cac, conversions)"""
    return [
        allocator.ChannelState(
            channel=name,
            percent=percent,
            cac=cac,
            spend=percent * 100.0,
            conversions=conversions,
        )
        for name, (percent, cac, conversions) in channels.items()
    ]


def test_allocation_always_totals_one_hundred():
    plan = allocator.allocate(
        _states(
            google=(32, 18.4, 200),
            meta=(24, 21.2, 150),
            linkedin=(16, 34.5, 60),
            tiktok=(10, 15.9, 90),
            dsp=(18, 27.1, 80),
        )
    )
    assert plan.total == 100


def test_allocation_favours_the_lowest_cac():
    plan = allocator.allocate(
        _states(cheap=(20, 10.0, 100), expensive=(20, 40.0, 100))
    )
    assert plan.shifts["cheap"] > 0
    assert plan.shifts["expensive"] < 0
    assert plan.percents["cheap"] > plan.percents["expensive"]


def test_movement_is_capped_per_pass():
    """One bad hour of data must not swing the whole account."""
    plan = allocator.allocate(
        _states(a=(90, 50.0, 100), b=(10, 5.0, 100))
    )
    for channel, shift in plan.shifts.items():
        assert abs(shift) <= allocator.MAX_SHIFT_POINTS, channel


def test_active_channel_keeps_a_floor():
    """A temporarily expensive channel is throttled, never switched off.

    A channel starved to zero stops producing the conversion data needed to
    ever recover, so the floor is load-bearing rather than cosmetic.
    """
    plan = allocator.allocate(
        _states(good=(50, 8.0, 500), awful=(50, 200.0, 10))
    )
    assert plan.percents["awful"] >= allocator.MIN_ACTIVE_SHARE


def test_locked_channels_are_untouched():
    states = _states(locked=(40, 90.0, 100), free_a=(30, 10.0, 100), free_b=(30, 20.0, 100))
    states[0].locked = True
    plan = allocator.allocate(states)
    assert plan.percents["locked"] == 40
    assert plan.shifts["locked"] == 0
    assert plan.total == 100


def test_channels_without_credible_data_hold_their_share():
    """Reallocating on two conversions is noise, not signal."""
    states = _states(
        established=(50, 20.0, 400),
        brand_new=(50, 3.0, 1),  # spectacular CAC from a single conversion
    )
    plan = allocator.allocate(states)
    assert plan.percents["brand_new"] == 50
    assert plan.shifts["brand_new"] == 0


def test_no_credible_data_means_no_reallocation():
    plan = allocator.allocate(_states(a=(50, 10.0, 1), b=(50, 20.0, 2)))
    assert all(shift == 0 for shift in plan.shifts.values())
    assert "Not enough conversion data" in plan.rationale


def test_projected_cac_improves_when_shifting_to_cheaper_channels():
    states = _states(cheap=(20, 10.0, 200), expensive=(80, 40.0, 200))
    plan = allocator.allocate(states)
    before = allocator.projected_cac(states, {s.channel: s.percent for s in states})
    after = allocator.projected_cac(states, plan.percents)
    assert after < before


def test_tiny_movements_are_not_worth_making():
    plan = allocator.allocate(_states(a=(50, 20.0, 100), b=(50, 20.1, 100)))
    assert not plan.is_meaningful


# ── Referral spam ──────────────────────────────────────────────────────────
def _referrer(domain: str, **kw) -> TrafficSample:  # noqa: ANN003
    return TrafficSample(
        source=domain,
        referrer=domain,
        sessions=kw.get("sessions", 10),
        bounce_rate=kw.get("bounce_rate", 0.5),
        avg_session_seconds=kw.get("avg_session_seconds", 60.0),
    )


@pytest.mark.parametrize(
    "domain",
    ["best-cams-live.xyz", "hot-webcam-deals.click", "xxx-tube-hits.online"],
)
def test_adult_spam_is_caught_by_name(domain: str):
    verdict, confidence = rules.classify(_referrer(domain))
    assert verdict is rules.Verdict.ADULT
    assert confidence > 0.9


def test_known_ghost_referrer_is_caught():
    verdict, confidence = rules.classify(_referrer("semalt.com"))
    assert verdict is rules.Verdict.GHOST
    assert confidence > 0.95


def test_ghost_behaviour_is_caught_without_a_bad_name():
    """Traffic that never rendered the page: total bounce, no time on site."""
    verdict, _ = rules.classify(
        _referrer("innocuous-name.top", sessions=8, bounce_rate=1.0, avg_session_seconds=0.3)
    )
    assert verdict is rules.Verdict.GHOST


def test_legitimate_referrers_are_left_alone():
    for domain in ("news.ycombinator.com", "reddit.com", "google.com", "autoblog-central.com"):
        verdict, _ = rules.classify(
            _referrer(domain, sessions=200, bounce_rate=0.45, avg_session_seconds=120.0)
        )
        assert verdict is rules.Verdict.CLEAN, domain


def test_ambiguous_referrer_is_escalated_not_blocked():
    """Suspicious but not conclusive must reach the model, not the blocklist."""
    verdict, _ = rules.classify(
        _referrer("some-startup.xyz", sessions=12, bounce_rate=0.9, avg_session_seconds=8.0)
    )
    assert verdict is rules.Verdict.UNKNOWN


def test_self_referral_is_not_spam():
    assert rules.is_own_domain("northgateauto.com", "northgateauto.com")
    assert rules.is_own_domain("shop.northgateauto.com", "northgateauto.com")
    assert not rules.is_own_domain("northgateauto.com.evil.xyz", "northgateauto.com")


def test_operator_blocklist_is_honoured():
    verdict, confidence = rules.classify(
        _referrer("annoying-but-clean.com"),
        extra_blocklist=frozenset({"annoying-but-clean.com"}),
    )
    assert verdict is not rules.Verdict.CLEAN
    assert confidence == 1.0


# ── Click fraud ────────────────────────────────────────────────────────────
def _traffic(**kw) -> TrafficSample:  # noqa: ANN003
    return TrafficSample(
        source=kw.pop("source", "placement-1"),
        sessions=kw.pop("sessions", 10),
        bounce_rate=kw.pop("bounce_rate", 0.5),
        avg_session_seconds=kw.pop("avg_session_seconds", 45.0),
        channel="dsp",
        signals=kw.pop("signals", {}),
    )


def test_datacenter_origin_is_fraud():
    verdict, confidence, reason = detectors.assess(
        _traffic(signals={"is_datacenter": True})
    )
    assert verdict is detectors.Verdict.FRAUD
    assert reason == detectors.Reason.DATACENTER
    assert confidence > 0.95


def test_headless_browser_is_fraud():
    verdict, _, reason = detectors.assess(_traffic(signals={"ua_family": "headless"}))
    assert verdict is detectors.Verdict.FRAUD
    assert reason == detectors.Reason.BOT_SIGNATURE


def test_impossible_geo_velocity_is_fraud():
    verdict, _, reason = detectors.assess(_traffic(signals={"geo_velocity_kmh": 1800}))
    assert verdict is detectors.Verdict.FRAUD
    assert reason == detectors.Reason.GEO_VELOCITY


def test_duplicate_fingerprints_are_fraud():
    verdict, _, reason = detectors.assess(
        _traffic(signals={"fingerprint_repeat_count": 40})
    )
    assert verdict is detectors.Verdict.FRAUD
    assert reason == detectors.Reason.DUPLICATE_FINGERPRINT


def test_ordinary_traffic_is_clean():
    """Low engagement alone is not fraud — plenty of real people bounce."""
    verdict, _, _ = detectors.assess(
        _traffic(
            sessions=60,
            bounce_rate=0.78,
            avg_session_seconds=22.0,
            signals={"asn_type": "residential", "has_mouse_events": True},
        )
    )
    assert verdict is detectors.Verdict.CLEAN


def test_borderline_traffic_is_escalated():
    verdict, _, _ = detectors.assess(
        _traffic(
            sessions=8,
            bounce_rate=0.93,
            avg_session_seconds=2.0,
            signals={"fingerprint_repeat_count": 6, "clicks_per_minute": 14},
        )
    )
    assert verdict is detectors.Verdict.UNCERTAIN


def test_only_safe_signal_keys_are_declared_for_storage():
    """Nothing identifying may be persisted on a fraud event."""
    for forbidden in ("ip", "ip_address", "device_id", "user_agent", "email"):
        assert forbidden not in detectors.SAFE_SIGNAL_KEYS


# ── Creative validation ────────────────────────────────────────────────────
def test_platform_copy_limits_are_enforced():
    problem = specs.check_copy(
        headline="A headline far longer than thirty characters for Google",
        body_copy="Fine.",
        platform="Google Ads",
    )
    assert problem is not None and "headline" in problem


def test_unsupportable_claims_are_rejected():
    problem = specs.check_copy(
        headline="Guaranteed lowest price",
        body_copy="Nobody beats us.",
        platform="Meta Ads",
    )
    assert problem is not None and "unsupportable" in problem


def test_all_caps_headline_is_rejected():
    problem = specs.check_copy(
        headline="BUY NOW TODAY", body_copy="Details inside.", platform="Meta Ads"
    )
    assert problem is not None and "caps" in problem


def test_valid_copy_passes():
    assert (
        specs.check_copy(
            headline="2026 SUV lineup",
            body_copy="Full details, current pricing, local stock.",
            platform="Google Ads",
        )
        is None
    )


def test_platform_aliases_resolve():
    assert specs.canonical_platform("google") == "Google Ads"
    assert specs.canonical_platform("Meta Ads") == "Meta Ads"
    assert specs.canonical_platform("facebook") == "Meta Ads"
    assert specs.canonical_platform("nonsense") is None


def test_variant_dimensions_snap_to_the_platform():
    variant = specs.normalise_variant(
        {
            "platform": "google",
            "dimensions": "999x999",
            "headline": "Short one",
            "body_copy": "Body copy here.",
        },
        allowed={"Google Ads"},
    )
    assert variant is not None
    assert variant.dimensions in specs.PLATFORMS["Google Ads"].dimensions


def test_variant_for_an_unconnected_platform_is_dropped():
    assert (
        specs.normalise_variant(
            {"platform": "tiktok", "headline": "x", "body_copy": "y"},
            allowed={"Google Ads"},
        )
        is None
    )


def test_product_extraction_reads_the_page():
    product = specs.extract_product(
        title="2026 SUV Lineup",
        url="/inventory/2026-suv-lineup",
        body="<p>Starting from $38,500.</p><ul><li>Warranty included</li></ul>",
    )
    assert product.price == "$38,500"
    assert "Warranty included" in product.features


# ── Audience clustering ────────────────────────────────────────────────────
def test_embedding_is_a_unit_vector():
    vector = vectors.embed({"inventory_view": 5, "finance_calculator": 2})
    magnitude = sum(v * v for v in vector) ** 0.5
    assert vector.__len__() == vectors.DIMS
    assert magnitude == pytest.approx(1.0, abs=1e-9)


def test_empty_signals_do_not_crash():
    assert vectors.embed({}) == [0.0] * vectors.DIMS


def test_unknown_markers_are_bucketed_deterministically():
    a = vectors.embed({"some_new_event": 3})
    b = vectors.embed({"some_new_event": 3})
    assert a == b
    assert any(v > 0 for v in a)


def test_clustering_separates_distinct_behaviours():
    service = [vectors.embed({"service_booking": 5, "call_click": 2}) for _ in range(40)]
    buying = [
        vectors.embed({"inventory_depth": 6, "finance_calculator": 4}) for _ in range(40)
    ]
    groups = vectors.kmeans(service + buying, k=2)
    sizes = sorted(len(g) for g in groups if g)
    assert sizes == [40, 40]


def test_clustering_is_stable_across_runs():
    """Segments must not reshuffle on every agent run."""
    data = [vectors.embed({"inventory_view": i % 7 + 1}) for i in range(60)]
    assert vectors.kmeans(data, k=3) == vectors.kmeans(data, k=3)


def test_cohesive_cluster_scores_high():
    members = [vectors.embed({"purchase": 4}) for _ in range(20)]
    cohesion, signals = vectors.describe(members)
    assert cohesion > 0.95
    assert signals[0] == "purchase"


def test_segment_labels_are_human_readable():
    assert vectors.label_for(["purchase"]) == "Repeat buyers"
    assert vectors.label_for(["service_booking"]) == "Service retention"
    assert vectors.label_for([]) == "General audience"
