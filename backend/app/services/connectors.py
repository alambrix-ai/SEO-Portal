"""Connector installation, credential handling, and instantiation.

Credentials arrive from the console, are validated against the connector's own
field declaration, split into secret and non-secret parts, and the secret part
is encrypted under the organisation's data key before it touches the database.
They are decrypted only here, when an agent run needs a live client.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.connectors.base import registry
from app.connectors.base.connector import BaseConnector, Capability, HealthReport
from app.connectors.base.credentials import (
    ACCESS_TOKEN_EXPIRY_KEY,
    Credentials,
    apply_defaults,
    from_platform_sign_in,
    validate,
)
from app.core.exceptions import ConnectorError, NotFoundError
from app.core.logging import get_logger
from app.db.base import utcnow
from app.models.connector import ConnectorHealth, ConnectorRecord
from app.models.workspace import Organization, User
from app.services import audit
from app.services.encryption import Ctx, OrgCipher

log = get_logger(__name__)

#: Shown when a connector failure has no authored message we can pass on.
CONNECTION_PROBLEM = "This connection reported a problem. Check it and try again."

# Text shaped like a credential: vendor key prefixes, bearer tokens, AWS key
# ids, ``key=value`` pairs and long unbroken runs of key-like characters.
_SECRET_SHAPED = re.compile(
    r"(?i)\b(?:sk|pk|rk|xox[abpr]|gh[pousr]|glpat)[-_][A-Za-z0-9_\-]{6,}"
    r"|\bAKIA[0-9A-Z]{12,}"
    r"|\bbearer\s+\S+"
    r"|\b(?:api[_-]?key|access[_-]?token|token|secret|password|passwd)\s*[=:]"
    r"|[A-Za-z0-9+/_\-]{32,}"
)


def public_connector_error(exc: BaseException, *, fallback: str = CONNECTION_PROBLEM) -> str:
    """Copy safe to store in ``last_error`` and show a marketer.

    Only a :class:`ConnectorError`'s authored ``message`` is ever passed on,
    softened, and only when it carries nothing shaped like a credential or a
    stack trace. Anything else becomes ``fallback``: raw exception text can
    hold vendor bodies, keys or internals.
    """
    from app.core.user_messages import soften_technical_message

    if not isinstance(exc, ConnectorError):
        return fallback
    raw = (exc.message or "").strip()
    if not raw or _SECRET_SHAPED.search(raw):
        return fallback
    message = soften_technical_message(raw)
    # Text too technical to rescue softens to the generic sentence; the
    # caller's fallback says more than that.
    if message == soften_technical_message("") or _SECRET_SHAPED.search(message):
        return fallback
    return message[:300]


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
    from app.models.portal import FeatureKind
    from app.services import portal_features

    if not portal_features.is_enabled(db, FeatureKind.CONNECTOR, slug):
        raise NotFoundError(f"Connector {slug!r} is not available in this workspace")

    record = db.execute(
        select(ConnectorRecord).where(
            ConnectorRecord.tenant_id == tenant_id, ConnectorRecord.slug == slug
        )
    ).scalar_one_or_none()
    if record is None or (
        not record.connected and slug in portal_features.hidden_connector_slugs(db)
    ):
        # Hidden as unverified (Portal Admin -> Verification): no longer
        # offered, but a connection already made stays reachable.
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
    """Every enabled installation, ordered for display: connected first.

    The ordering is harmless to the callers that do not care about it — the
    connector bundle builds a dict — and saves the two that do from sorting
    the same list two different ways.
    """
    from app.services import portal_features

    allowed = portal_features.enabled_connector_slugs(db)
    # Unverified types hidden in Portal Admin leave the catalogue, except
    # where this workspace already connected one.
    hidden = portal_features.hidden_connector_slugs(db)
    records = [
        r
        for r in db.execute(
            select(ConnectorRecord).where(ConnectorRecord.tenant_id == tenant_id)
        ).scalars()
        if r.slug in allowed and (r.connected or r.slug not in hidden)
    ]
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
    supplied = platform_supplied(spec, credentials.values)
    return [
        f.key
        for f in spec.fields
        if f.required
        and not f.is_oauth
        and f.key not in supplied
        and not credentials.values.get(f.key)
    ]


def platform_supplied(spec, values: dict[str, str]) -> frozenset[str]:  # noqa: ANN001
    """Fields the platform fills in for a connection made by signing in."""
    if spec is not None and spec.oauth is not None and from_platform_sign_in(values):
        return spec.oauth.platform_fields
    # Phase 2B: a key the platform includes (PageSpeed Insights, Places).
    option = getattr(spec, "platform_key", None)
    if option is not None and option.available:
        from app.connectors.base.connector import uses_platform_key

        if uses_platform_key(values):
            return frozenset({option.field})
    return frozenset()


def split_values(spec, values: dict[str, str]) -> tuple[dict[str, str], dict[str, str]]:  # noqa: ANN001
    """``(secrets, hints)``: what is encrypted, and what is kept readable.

    Declared secret fields are encrypted, and so is the stored access
    token's expiry — it is not declared as a field, and anything undeclared
    would otherwise land in the readable hints.
    """
    # Tokens a sign-in stores are secret even on a connector that does not
    # declare them as fields (a Facebook Page connection keeps the user
    # token it lists Pages with; X and Pinterest keep a refresh token).
    secret_keys = {f.key for f in spec.fields if f.is_secret} | {
        ACCESS_TOKEN_EXPIRY_KEY,
        "accessToken",
        "refreshToken",
    }
    secrets = {k: v for k, v in values.items() if k in secret_keys and v}
    hints = {k: v for k, v in values.items() if k not in secret_keys and v}
    return secrets, hints


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
                # An AI connection saved before the model was required.
                "Choose a model — open this connection and pick one from the list."
                if absent == ["model"]
                else f"Missing {', '.join(absent)} — reconnect this integration"
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
        raise ConnectorError("This connection is not available right now.")

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
        from app.core.user_messages import soften_technical_message

        detail = soften_technical_message(instance.diagnose(exc) or str(exc))
        raise ConnectorError(detail) from exc

    if report.probed and not report.ok:
        from app.core.user_messages import soften_technical_message

        detail = soften_technical_message(
            report.detail or "Those credentials could not be verified."
        )
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
        raise ConnectorError("This connection is not available right now.")

    cleaned = {k: (v or "").strip() for k, v in (values or {}).items()}
    # Documented defaults (model, max tokens, effort, …) fill blanks first.
    cleaned = apply_defaults(spec.fields, cleaned)

    # A connection made by signing in keeps its tokens when only its
    # settings are edited: the form never holds them, so a blank secret here
    # means "unchanged", not "remove". Pasting a refresh token is the manual
    # path, and replaces the sign-in entirely — as does pasting any of the
    # connector's own credentials (a Mailchimp API key, a Shopify admin
    # token), on vendors whose manual route has no refresh token.
    if spec.oauth is not None and not any(
        cleaned.get(key) for key in ({"refreshToken"} | spec.oauth.token_fields)
    ):
        existing = load_credentials(record, OrgCipher.for_org(org)).values
        if from_platform_sign_in(existing):
            submitted = {
                k: v
                for k, v in cleaned.items()
                if v or not any(f.key == k and f.is_secret for f in spec.fields)
            }
            cleaned = {**existing, **submitted}

    supplied = platform_supplied(spec, cleaned)
    validate(
        spec.fields,
        cleaned,
        oauth_done=oauth_completed or record.oauth_completed,
        platform_supplied=supplied,
    )

    # The shape is right; now find out whether the vendor agrees. This raises,
    # and the record has not been touched yet, so a bad credential simply
    # fails to connect rather than being stored and discovered later.
    report = verify_credentials(
        slug=slug,
        values=cleaned,
        oauth_completed=oauth_completed or record.oauth_completed,
        org_id=org.id,
    )
    store_verified(
        db,
        org=org,
        actor=actor,
        record=record,
        values=cleaned,
        report=report,
        oauth_completed=oauth_completed or record.oauth_completed,
    )

    audit.record_user_action(
        db,
        user=actor,
        action=f"connected {record.name}",
        module="connectors",
        ip_address=ip_address,
        context={"connector": slug},
    )
    return record


def store_verified(
    db: Session,
    *,
    org: Organization,
    actor: User,
    record: ConnectorRecord,
    values: dict[str, str],
    report: HealthReport,
    oauth_completed: bool = False,
) -> ConnectorRecord:
    """Write credentials the vendor has just accepted, and mark them live.

    Shared by the form (:func:`connect`) and the sign-in flows, so both
    encrypt the same way and leave the record in the same state.
    """
    spec = registry.get_spec(record.slug)
    if spec is None:
        raise ConnectorError("This connection is not available right now.")
    cipher = OrgCipher.for_org(org)
    secrets, hints = split_values(spec, values)

    record.credentials_encrypted = cipher.encrypt_json(
        secrets, context=Ctx.CONNECTOR_CREDENTIALS
    )
    record.credential_hints = hints
    record.credentials_version += 1
    record.oauth_completed = oauth_completed
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
            record.last_error = public_connector_error(exc)
            record.health = ConnectorHealth.DEGRADED.value
            log.warning("on_connect for %s reported %s", record.slug, type(exc).__name__)
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
                log.warning("on_disconnect for %s reported %s", slug, type(exc).__name__)

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
        record.last_error = (
            "" if report.ok
            else public_connector_error(ConnectorError(report.detail or CONNECTION_PROBLEM))
        )
    except ConnectorError as exc:
        log.warning("Health check for %s failed (%s)", slug, type(exc).__name__)
        record.health = ConnectorHealth.FAILING.value
        record.last_error = public_connector_error(exc)
    db.flush()
    return record


def counts(db: Session, *, tenant_id: str) -> tuple[int, int]:
    """``(connected, total)`` for the dashboard tile."""
    records = list_records(db, tenant_id=tenant_id)
    return sum(1 for r in records if r.connected), len(records)


# ── AI models: the vendor's own list ───────────────────────────────────────
@dataclass(slots=True)
class ModelListing:
    """What the connect dialog needs to offer a model picker."""

    #: False when the vendor publishes no list (the dialog asks for a name).
    listable: bool = True
    models: list = field(default_factory=list)
    #: The vendor's newest text model by its own timestamp, or "".
    suggested: str = ""
    #: "The list comes from your OpenAI account."
    source: str = ""
    #: Plain words when the list could not be read; the dialog then offers
    #: "Type a model name" instead.
    error: str = ""
    #: Set when the saved model is no longer on the vendor's list.
    stale: str = ""
    #: The vendor's own models page.
    models_url: str = ""


def _key_required(klass: type[BaseConnector]) -> bool:
    """True unless the connector's form declares ``apiKey`` optional.

    A form with no ``apiKey`` field at all counts as required, so a vendor
    added later is refused without a key until its form says otherwise.
    """
    for spec_field in klass.spec.fields:
        if spec_field.key == "apiKey":
            return spec_field.required
    return True


def model_listing(
    db: Session,
    *,
    org: Organization,
    slug: str,
    values: dict[str, str] | None = None,
) -> ModelListing:
    """The models this key can use, read live from the vendor.

    ``values`` are what the person has typed in the dialog (before saving);
    a blank key there means "the saved one". ``None`` uses the saved
    connection only. Keys are used for the call and never returned or logged.
    """
    from app.connectors.base.llm_models import (
        BASE_URL_FIELD,
        ModelChoice,
        SelfHostedModelConnector,
    )
    from app.core.exceptions import InvalidInputError
    from app.core.user_messages import soften_technical_message
    from app.llm import model_catalog

    klass = registry.get_class(slug)
    if klass is None or not issubclass(klass, ModelChoice):
        raise NotFoundError("This integration has no model to choose.")
    record = get_record(db, tenant_id=org.id, slug=slug)
    stored = load_credentials(record, OrgCipher.for_org(org)).values if record.connected else {}
    if values is None:
        if not record.connected:
            raise InvalidInputError("Connect this integration first.")
        merged = dict(stored)
    else:
        typed = {k: (v or "").strip() for k, v in values.items()}
        merged = {**stored, **{k: v for k, v in typed.items() if v}}

    instance = klass(Credentials(values=merged), org_id=org.id)
    vendor = instance.name
    out = ModelListing(
        source=f"The list comes from your {vendor} account.",
        models_url=getattr(klass, "models_url", "") or "",
    )
    # A self-hosted server (vLLM, Ollama) needs its address, and a key only
    # if it asks for one. Every other vendor needs the key its form requires.
    if issubclass(klass, SelfHostedModelConnector):
        if not (merged.get(BASE_URL_FIELD) or "").strip():
            out.error = (
                f"Enter the address of your {vendor} server first, and the list "
                "of models will appear."
            )
            return out
    elif _key_required(klass) and not merged.get("apiKey"):
        out.error = f"Paste your {vendor} API key first, and the list of models will appear."
        return out
    try:
        with instance:
            models = instance.available_models()
    except model_catalog.ModelListUnavailable:
        out.listable = False
        out.source = ""
        out.error = (
            f"{vendor} does not offer a list of models we can read. Type the model "
            f"name exactly as {vendor}'s models page shows it."
        )
        return out
    except ConnectorError as exc:
        log.info("Model list for %s could not be read: %s", slug, exc)
        out.error = (
            f"We could not load the model list from {vendor}. "
            f"{soften_technical_message(str(exc))} You can type a model name instead."
        )
        return out

    out.models = models
    out.suggested = model_catalog.suggested_text_model(models)
    saved = (stored.get("model") or "").strip()
    if saved and model_catalog.find(models, saved) is None:
        out.stale = model_catalog.stale_model_message(saved, vendor)
    return out
