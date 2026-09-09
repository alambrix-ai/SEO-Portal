"""Connector installation, credential handling, and instantiation.

Credentials arrive from the console, are validated against the connector's own
field declaration, split into secret and non-secret parts, and the secret part
is encrypted under the organisation's data key before it touches the database.
They are decrypted only here, when an agent run needs a live client.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.connectors.base import registry
from app.connectors.base.connector import BaseConnector, Capability, HealthReport
from app.connectors.base.credentials import Credentials, split_hints, validate
from app.core.exceptions import ConnectorError, NotFoundError
from app.core.logging import get_logger
from app.db.base import utcnow
from app.models.connector import ConnectorHealth, ConnectorRecord
from app.models.workspace import Organization, User
from app.services import audit
from app.services.encryption import Ctx, OrgCipher

log = get_logger(__name__)


@dataclass(slots=True)
class ConnectorBundle:
    """Live connector instances for one organisation, ready for an agent run."""

    instances: dict[str, BaseConnector] = field(default_factory=dict)
    names: dict[str, str] = field(default_factory=dict)
    connected: frozenset[str] = frozenset()

    def with_capability(self, capability: Capability) -> list[BaseConnector]:
        return [c for c in self.instances.values() if c.supports(capability)]

    def first_with_capability(self, capability: Capability) -> BaseConnector | None:
        for slug in registry.connector_slugs():
            connector = self.instances.get(slug)
            if connector is not None and connector.supports(capability):
                return connector
        return None

    def close(self) -> None:
        for connector in self.instances.values():
            connector.close()


# ── Reads ──────────────────────────────────────────────────────────────────
def get_record(db: Session, *, tenant_id: str, slug: str) -> ConnectorRecord:
    record = db.execute(
        select(ConnectorRecord).where(
            ConnectorRecord.tenant_id == tenant_id, ConnectorRecord.slug == slug
        )
    ).scalar_one_or_none()
    if record is None:
        raise NotFoundError(f"Connector {slug!r} is not available in this workspace")
    return record


def connector_rank(record: ConnectorRecord) -> int:
    """How near the top of the catalogue this installation belongs.

    Twenty-five cards of which one is wired up is a catalogue, not a status
    screen, and the wired-up one should not be somewhere in the middle of it.

    0. **Connected but unhealthy** — it is being relied on and it is not
       working, which is the one row somebody needs to see today.
    1. **Connected and fine.**
    2. **Not connected** — still an offer.
    """
    if not record.connected:
        return 2
    return 0 if record.health in ("failing", "degraded") else 1


def list_records(db: Session, *, tenant_id: str) -> list[ConnectorRecord]:
    """Every installation, ordered for display: connected first.

    The ordering is harmless to the callers that do not care about it — the
    connector bundle builds a dict — and saves the two that do from sorting
    the same list two different ways.
    """
    records = list(
        db.execute(
            select(ConnectorRecord).where(ConnectorRecord.tenant_id == tenant_id)
        ).scalars()
    )
    order = {slug: i for i, slug in enumerate(registry.connector_slugs())}
    records.sort(
        key=lambda r: (connector_rank(r), order.get(r.slug, len(order)), r.name)
    )
    return records


def load_credentials(record: ConnectorRecord, cipher: OrgCipher) -> Credentials:
    """Decrypt one installation's credentials."""
    values = cipher.decrypt_json(
        record.credentials_encrypted, context=Ctx.CONNECTOR_CREDENTIALS, default={}
    )
    # Non-secret hints live in the clear; merged so a connector sees one map.
    merged = {**(record.credential_hints or {}), **(values or {})}
    return Credentials(values=merged, oauth_completed=record.oauth_completed)


def instantiate(record: ConnectorRecord, cipher: OrgCipher) -> BaseConnector | None:
    """Build a live client for a connected installation."""
    klass = registry.get_class(record.slug)
    if klass is None:
        log.warning("No implementation for connector %s", record.slug)
        return None
    return klass(load_credentials(record, cipher), org_id=record.tenant_id)


def missing_credentials(record: ConnectorRecord, credentials: Credentials) -> list[str]:
    """Required fields this installation has no value for.

    A record can be flagged ``connected`` and still be unusable — credentials
    were cleared, a field was added to the spec after it was wired up, or a
    migration moved the data. Every call the connector makes then fails on the
    same missing key.
    """
    spec = registry.get_spec(record.slug)
    if spec is None:
        return []
    return [
        f.key
        for f in spec.fields
        if f.required and not f.is_oauth and not credentials.values.get(f.key)
    ]


def build_connector_bundle(db: Session, org: Organization) -> ConnectorBundle:
    """Every *usable* connector for one organisation, instantiated.

    Usable, not merely flagged connected. An installation whose required
    credentials are missing is left out of the bundle and marked failing, so
    the agents treat it as absent and route to another connector with the same
    capability — or report honestly that they are waiting on one.

    The alternative is what this replaces: a connector that looks available,
    is handed to every agent that wants it, and raises "Missing credential"
    on every call, on every tick, forever. That reads like a fault in the
    agents and it is really an unfinished setup, so it is diagnosed here once
    and recorded on the record the console reads.
    """
    cipher = OrgCipher.for_org(org)
    bundle = ConnectorBundle()
    bundle.names = {spec.slug: spec.name for spec in registry.all_specs()}

    connected: set[str] = set()
    for record in list_records(db, tenant_id=org.id):
        bundle.names[record.slug] = record.name
        if not record.connected:
            continue

        credentials = load_credentials(record, cipher)
        absent = missing_credentials(record, credentials)
        if absent:
            reason = (
                f"Missing {', '.join(absent)} — reconnect this integration"
                if len(absent) > 1
                else f"Missing {absent[0]} — reconnect this integration"
            )
            # Only log when the diagnosis changes. This runs on every agent
            # run of every tick, and a setup gap that is already recorded does
            # not need saying again.
            if record.health != ConnectorHealth.FAILING.value or record.last_error != reason:
                log.warning("%s is connected but unusable: %s", record.slug, reason)
            record.health = ConnectorHealth.FAILING.value
            record.last_error = reason
            continue

        klass = registry.get_class(record.slug)
        if klass is None:
            log.warning("No implementation for connector %s", record.slug)
            continue
        bundle.instances[record.slug] = klass(credentials, org_id=record.tenant_id)
        connected.add(record.slug)

    bundle.connected = frozenset(connected)
    return bundle


# ── Writes ─────────────────────────────────────────────────────────────────
def verify_credentials(
    *, slug: str, values: dict[str, str], oauth_completed: bool, org_id: str
) -> HealthReport:
    """Ask the vendor whether these credentials work, before storing them.

    Called with the values the operator just typed rather than with anything
    read back from the database, and *before* the record is touched — so a
    rejected submission leaves no trace, and there is no half-connected state
    to reason about or clean up.

    This replaces a separate Test button, which had the shape of the problem
    backwards: it let a wrong token be saved and marked connected, and only
    told anybody if they happened to press it. In between, every agent that
    wanted that connector would take it and fail.

    A :class:`ConnectorError` here is the vendor's own refusal, and it is
    passed through to the dialog verbatim — "the token can read this
    repository but not write to it" is worth far more to somebody than
    "invalid credentials".
    """
    klass = registry.get_class(slug)
    if klass is None:
        raise ConnectorError(f"Connector {slug!r} has no implementation on this build")

    instance = klass(
        Credentials(values=dict(values), oauth_completed=oauth_completed), org_id=org_id
    )
    try:
        with instance:
            report = instance.check_health()
    except ConnectorError as exc:
        # Give the connector a chance to explain its own platform's limits
        # before the raw vendor error is handed to somebody who can only
        # retype the token.
        raise ConnectorError(instance.diagnose(exc) or str(exc)) from exc

    if report.probed and not report.ok:
        detail = report.detail or "the credentials were rejected"
        raise ConnectorError(instance.diagnose(ConnectorError(detail)) or detail)
    return report


def connect(
    db: Session,
    *,
    org: Organization,
    actor: User,
    slug: str,
    values: dict[str, str],
    oauth_completed: bool = False,
    ip_address: str | None = None,
) -> ConnectorRecord:
    """Verify credentials with the vendor, then store them.

    In that order. Nothing is written until the vendor has accepted them —
    see :func:`verify_credentials`.
    """
    record = get_record(db, tenant_id=org.id, slug=slug)
    spec = registry.get_spec(slug)
    if spec is None:
        raise ConnectorError(f"Connector {slug!r} has no implementation on this build")

    cleaned = {k: (v or "").strip() for k, v in (values or {}).items()}
    validate(spec.fields, cleaned, oauth_done=oauth_completed or record.oauth_completed)

    # The shape is right; now find out whether the vendor agrees. This raises,
    # and the record has not been touched yet, so a bad credential simply
    # fails to connect rather than being stored and discovered later.
    report = verify_credentials(
        slug=slug,
        values=cleaned,
        oauth_completed=oauth_completed or record.oauth_completed,
        org_id=org.id,
    )

    cipher = OrgCipher.for_org(org)
    secret_keys = {f.key for f in spec.fields if f.is_secret}
    secrets = {k: v for k, v in cleaned.items() if k in secret_keys and v}

    record.credentials_encrypted = cipher.encrypt_json(
        secrets, context=Ctx.CONNECTOR_CREDENTIALS
    )
    record.credential_hints = split_hints(spec.fields, cleaned)
    record.credentials_version += 1
    record.oauth_completed = oauth_completed or record.oauth_completed
    record.connected = True
    record.connected_at = utcnow()
    record.connected_by = actor.name
    # Verified a moment ago, so the card can say so straight away rather than
    # showing "unknown" until something happens to probe it.
    record.health = (
        ConnectorHealth.OK.value if report.probed else ConnectorHealth.UNKNOWN.value
    )
    record.last_health_check_at = utcnow() if report.probed else None
    record.last_error = "" if report.probed else "No health probe for this integration"
    db.flush()

    # Let the integration do its own setup (webhook registration, etc.).
    instance = instantiate(record, cipher)
    if instance is not None:
        try:
            with instance:
                instance.on_connect()
        except ConnectorError as exc:
            # Connecting still succeeded; the setup step is recorded and can
            # be retried by the health check.
            record.last_error = str(exc)
            record.health = ConnectorHealth.DEGRADED.value
            log.warning("on_connect for %s reported: %s", slug, exc)

    audit.record_user_action(
        db,
        user=actor,
        action=f"connected {record.name}",
        module="connectors",
        ip_address=ip_address,
        context={"connector": slug},
    )
    return record


def disconnect(
    db: Session,
    *,
    org: Organization,
    actor: User,
    slug: str,
    ip_address: str | None = None,
) -> ConnectorRecord:
    """Remove stored credentials. The ciphertext is cleared, not orphaned."""
    record = get_record(db, tenant_id=org.id, slug=slug)

    if record.connected:
        cipher = OrgCipher.for_org(org)
        instance = instantiate(record, cipher)
        if instance is not None:
            try:
                with instance:
                    instance.on_disconnect()
            except ConnectorError as exc:
                log.warning("on_disconnect for %s reported: %s", slug, exc)

    record.connected = False
    record.oauth_completed = False
    record.credentials_encrypted = ""
    record.credential_hints = {}
    record.credentials_version += 1
    record.token_expires_at = None
    record.scopes = []
    record.health = ConnectorHealth.UNKNOWN.value
    record.last_error = ""
    db.flush()

    audit.record_user_action(
        db,
        user=actor,
        action=f"disconnected {record.name}",
        module="connectors",
        ip_address=ip_address,
        context={"connector": slug},
    )
    return record


def check_health(db: Session, *, org: Organization, slug: str) -> ConnectorRecord:
    """Probe one connector and store the verdict."""
    record = get_record(db, tenant_id=org.id, slug=slug)
    if not record.connected:
        record.health = ConnectorHealth.UNKNOWN.value
        return record

    cipher = OrgCipher.for_org(org)
    instance = instantiate(record, cipher)
    record.last_health_check_at = utcnow()

    if instance is None:
        record.health = ConnectorHealth.FAILING.value
        record.last_error = "No implementation available"
        return record

    try:
        with instance:
            report = instance.check_health()
        record.health = (
            ConnectorHealth.OK.value if report.ok else ConnectorHealth.FAILING.value
        )
        record.last_error = "" if report.ok else report.detail
    except ConnectorError as exc:
        record.health = ConnectorHealth.FAILING.value
        record.last_error = str(exc)
    db.flush()
    return record


def counts(db: Session, *, tenant_id: str) -> tuple[int, int]:
    """``(connected, total)`` for the dashboard tile."""
    records = list_records(db, tenant_id=tenant_id)
    return sum(1 for r in records if r.connected), len(records)
