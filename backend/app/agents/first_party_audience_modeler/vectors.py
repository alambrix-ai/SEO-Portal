"""First-party signal embedding and clustering.

All of this runs inside the tenant's own boundary. Interaction markers are
turned into vectors and clustered locally with k-means; what leaves the
platform is a segment's *size and signal labels*, never a person.

The implementation is deliberately dependency-free — no numpy, no external
vector service — because the vectors are small, the cluster counts are tiny,
and adding a network hop for this would mean sending behavioural data
somewhere it does not need to go.
"""
from __future__ import annotations

import hashlib
import math
import random
from dataclasses import dataclass, field

# Signal vocabulary. A marker outside this set is hashed into a bucket, so an
# unrecognised event still contributes rather than being dropped.
SIGNAL_VOCAB: tuple[str, ...] = (
    "inventory_view",
    "inventory_depth",
    "finance_calculator",
    "service_booking",
    "trade_in_valuation",
    "location_view",
    "brochure_download",
    "comparison_tool",
    "return_visit",
    "contact_form",
    "call_click",
    "chat_started",
    "email_open",
    "cart_started",
    "checkout_started",
    "purchase",
)
DIMS = len(SIGNAL_VOCAB)
# Minimum members before a cluster is worth activating: below this it is
# noise, and most ad platforms will not accept it as an audience anyway.
MIN_CLUSTER_SIZE = 100
# Clusters this diffuse do not describe a coherent audience.
MIN_COHESION = 0.35


@dataclass(slots=True)
class Cluster:
    label: str
    size: int
    cohesion: float
    top_signals: list[str] = field(default_factory=list)
    centroid: list[float] = field(default_factory=list)

    @property
    def is_activatable(self) -> bool:
        return self.size >= MIN_CLUSTER_SIZE and self.cohesion >= MIN_COHESION


def embed(signals: dict[str, float]) -> list[float]:
    """Turn one profile's interaction markers into a unit vector.

    Counts are log-scaled: the difference between one page view and three
    matters, between forty and forty-three does not.
    """
    vector = [0.0] * DIMS
    for name, raw in (signals or {}).items():
        try:
            weight = math.log1p(max(float(raw), 0.0))
        except (TypeError, ValueError):
            continue
        key = str(name).strip().lower()
        if key in SIGNAL_VOCAB:
            vector[SIGNAL_VOCAB.index(key)] += weight
        else:
            # Stable bucket for an unknown marker.
            bucket = int(hashlib.sha256(key.encode()).hexdigest(), 16) % DIMS
            vector[bucket] += weight * 0.5
    return _normalise(vector)


def _normalise(vector: list[float]) -> list[float]:
    magnitude = math.sqrt(sum(v * v for v in vector))
    if magnitude == 0:
        return vector
    return [v / magnitude for v in vector]


def cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=False))


def kmeans(
    vectors: list[list[float]], *, k: int, iterations: int = 25, seed: int = 7
) -> list[list[int]]:
    """Cluster vectors, returning member indices per cluster.

    Seeded so the same signal set produces the same segments — an operator
    should not see their audiences reshuffle on every run.
    """
    if not vectors:
        return []
    k = max(1, min(k, len(vectors)))
    rng = random.Random(seed)

    # k-means++ style seeding: spread the initial centroids out, so a run does
    # not collapse two centroids onto the same dense region.
    centroids = [list(vectors[rng.randrange(len(vectors))])]
    while len(centroids) < k:
        distances = [
            min(1.0 - cosine(v, c) for c in centroids) for v in vectors
        ]
        total = sum(distances)
        if total <= 0:
            centroids.append(list(vectors[rng.randrange(len(vectors))]))
            continue
        threshold = rng.random() * total
        cumulative = 0.0
        for vector, distance in zip(vectors, distances, strict=False):
            cumulative += distance
            if cumulative >= threshold:
                centroids.append(list(vector))
                break

    assignments = [0] * len(vectors)
    for _ in range(iterations):
        moved = False
        for i, vector in enumerate(vectors):
            best = max(range(len(centroids)), key=lambda c: cosine(vector, centroids[c]))
            if best != assignments[i]:
                assignments[i] = best
                moved = True

        for c in range(len(centroids)):
            members = [vectors[i] for i, a in enumerate(assignments) if a == c]
            if not members:
                continue
            centroids[c] = _normalise(
                [sum(m[d] for m in members) / len(members) for d in range(DIMS)]
            )
        if not moved:
            break

    return [
        [i for i, a in enumerate(assignments) if a == c] for c in range(len(centroids))
    ]


def describe(
    members: list[list[float]], *, centroid: list[float] | None = None
) -> tuple[float, list[str]]:
    """Return ``(cohesion, top_signals)`` for one cluster."""
    if not members:
        return 0.0, []
    center = centroid or _normalise(
        [sum(m[d] for m in members) / len(members) for d in range(DIMS)]
    )
    cohesion = sum(cosine(m, center) for m in members) / len(members)
    ranked = sorted(range(DIMS), key=lambda d: center[d], reverse=True)
    top = [SIGNAL_VOCAB[d] for d in ranked[:3] if center[d] > 0.05]
    return round(cohesion, 3), top


def suggest_k(population: int) -> int:
    """How many segments a population of this size can meaningfully support."""
    if population < MIN_CLUSTER_SIZE * 2:
        return 1
    return max(2, min(6, int(math.sqrt(population / MIN_CLUSTER_SIZE))))


def label_for(top_signals: list[str]) -> str:
    """A readable segment name derived from its dominant signals."""
    if not top_signals:
        return "General audience"
    named = {
        "purchase": "Repeat buyers",
        "checkout_started": "Checkout abandoners",
        "cart_started": "Cart abandoners",
        "finance_calculator": "Finance-focused shoppers",
        "trade_in_valuation": "Trade-in ready",
        "service_booking": "Service retention",
        "brochure_download": "Considered researchers",
        "comparison_tool": "Comparison shoppers",
        "inventory_depth": "High-intent researchers",
        "inventory_view": "In-market browsers",
        "location_view": "Local visitors",
        "return_visit": "Returning prospects",
        "call_click": "Ready to talk",
        "contact_form": "Enquiry starters",
    }
    return named.get(top_signals[0], top_signals[0].replace("_", " ").title())
