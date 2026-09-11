"""Operations CLI.

    python manage.py init-db          # create schema + row-level security
    python manage.py sync-catalog     # back-fill new agents/connectors
    python manage.py run-agent <org-slug> <agent-slug>
    python manage.py tick            # run every due agent once
    python manage.py rollup          # daily metric rollup
    python manage.py rotate-keys     # re-wrap org data keys under the new KEK
    python manage.py check           # configuration and connectivity report
"""
from __future__ import annotations

import argparse
import sys

from sqlalchemy import select

from app.core.config import settings
from app.core.logging import configure_logging, get_logger

log = get_logger(__name__)


# ── Commands ───────────────────────────────────────────────────────────────
def cmd_init_db(args: argparse.Namespace) -> int:
    """Create the schema and apply row-level security.

    Equivalent to running the first Alembic revision; useful for a fresh
    development database or a test container.
    """
    from app.db.base import Base
    from app.db.rls import apply_policies
    from app.db.session import engine
    import app.models  # noqa: F401

    with engine.begin() as conn:
        conn.exec_driver_sql("CREATE EXTENSION IF NOT EXISTS pgcrypto")
        Base.metadata.create_all(bind=conn)
        if settings.db_enforce_rls:
            apply_policies(conn)
    print(f"Schema ready on {_safe_dsn()}")
    return 0


def cmd_sync_catalog(args: argparse.Namespace) -> int:
    """Install agents and connectors added since each workspace was created."""
    from app.db.session import SessionLocal
    from app.services.provisioning import sync_all_organizations

    db = SessionLocal()
    try:
        # Commits per workspace internally; see `run_per_organization`.
        result = sync_all_organizations(db)
    finally:
        db.close()
    print(
        f"Added {result['agents_added']} agents, "
        f"{result['connectors_added']} connectors, "
        f"{result['budgets_added']} budget rows"
    )
    return 0



def cmd_run_agent(args: argparse.Namespace) -> int:
    from app.db.session import session_scope
    from app.models.workspace import Organization
    from app.orchestration.runner import run_agent_by_slug

    from app.db.session import SessionLocal

    lookup = SessionLocal()
    try:
        org = lookup.execute(
            select(Organization).where(Organization.slug == args.org)
        ).scalar_one_or_none()
        if org is None:
            print(f"No workspace with slug {args.org!r}", file=sys.stderr)
            return 1
        tenant_id = org.id
    finally:
        lookup.close()

    with session_scope(tenant_id) as db:
        outcome = run_agent_by_slug(
            db,
            tenant_id=tenant_id,
            slug=args.agent,
            trigger="manual",
            triggered_by="cli",
        )
        print(f"{args.agent}: {outcome.status} — {outcome.summary}")
    return 0 if outcome.status != "failed" else 1


def cmd_tick(args: argparse.Namespace) -> int:
    from app.orchestration.scheduler import scheduler

    count = scheduler.tick()
    print(f"Ran {count} due agent(s)")
    return 0


def cmd_rollup(args: argparse.Namespace) -> int:
    from app.orchestration.scheduler import rollup_daily_metrics

    rollup_daily_metrics()
    print("Daily metrics rolled up")
    return 0


def cmd_rotate_keys(args: argparse.Namespace) -> int:
    """Re-wrap every organisation's data key under the current master key.

    Run after setting a new ``MASTER_ENCRYPTION_KEY`` and moving the old one
    into ``MASTER_ENCRYPTION_KEY_RETIRED``. Data itself is not re-encrypted —
    only the wrapped keys — so this is fast and safe to repeat.
    """
    from app.core.crypto import keyring
    from app.db.session import SessionLocal
    from app.models.workspace import Organization
    from app.services.encryption import OrgCipher

    target_version = keyring().active_version
    db = SessionLocal()
    rotated = skipped = failed = 0
    try:
        for org in db.execute(select(Organization)).scalars():
            if org.dek_version == target_version:
                skipped += 1
                continue
            try:
                org.wrapped_dek = OrgCipher.rewrap(org.wrapped_dek, org.id)
                org.dek_version = target_version
                from app.db.base import utcnow

                org.dek_rotated_at = utcnow()
                rotated += 1
            except Exception as exc:  # noqa: BLE001
                failed += 1
                log.error("Could not re-wrap the key for %s: %s", org.slug, exc)
        db.commit()
    finally:
        db.close()

    print(f"Re-wrapped {rotated}, already current {skipped}, failed {failed}")
    return 1 if failed else 0


def cmd_check(args: argparse.Namespace) -> int:
    """Report configuration and connectivity — the pre-flight before deploying."""
    from app.agents.base.registry import all_agents
    from app.connectors.base.registry import all_specs

    print(f"Environment      : {settings.app_env}")
    print(f"Database         : {_safe_dsn()}")
    print(f"TLS mode         : {settings.effective_sslmode}")
    print(f"RLS enforced     : {settings.db_enforce_rls}")
    print(
        "Model            : chosen per agent from connected AI connectors "
        f"(effort {settings.llm_effort})"
    )
    print(f"Public sign-up   : {settings.allow_public_signup}")
    print(
        f"Sign-in          : {settings.login_code_length}-digit emailed code, "
        f"{settings.login_code_ttl_minutes}m validity, "
        f"{settings.login_code_max_attempts} attempts"
    )

    ok = True

    try:
        from app.core.crypto import keyring

        ring = keyring()
        explicit = bool(settings.master_encryption_key)
        print(
            f"Encryption       : master key v{ring.active_version} "
            f"({'explicit' if explicit else 'DERIVED FROM SECRET_KEY — set one'})"
        )
        if not explicit and settings.is_production:
            ok = False
    except Exception as exc:  # noqa: BLE001
        print(f"Encryption       : FAILED — {exc}")
        ok = False

    try:
        from app.db.session import check_connection

        check_connection()
        print("Database         : reachable")
    except Exception as exc:  # noqa: BLE001
        print(f"Database         : UNREACHABLE — {exc}")
        ok = False

    print(f"Agents           : {len(all_agents())}")
    print(f"Connectors       : {len(all_specs())}")

    print(
        "Model access     : connect OpenAI / Claude / Gemini / Perplexity, "
        "then pick one when configuring each agent"
    )
    if not (settings.llm_price_input_per_mtok or settings.llm_price_output_per_mtok):
        print("Model pricing    : unset — run costs will read zero")

    if settings.app_env == "development":
        if not settings.smtp_host:
            print(
                "Email            : NOT CONFIGURED — APP_ENV=development uses "
                "SMTP; codes will be written to the log instead of sent"
            )
            ok = False
        else:
            print(f"Email            : SMTP {settings.smtp_host}:{settings.smtp_port} (APP_ENV=development)")
    else:
        # test / staging / production → Resend
        if not settings.resend_api_key:
            print(
                f"Email            : NOT CONFIGURED — APP_ENV={settings.app_env} "
                "requires RESEND_API_KEY"
            )
            ok = False
        else:
            print(f"Email            : Resend (HTTPS) (APP_ENV={settings.app_env})")

    print("\nStatus: " + ("ready" if ok else "NOT ready — see above"))
    return 0 if ok else 1


def _safe_dsn() -> str:
    """The DSN with any password removed, for printing."""
    from urllib.parse import urlsplit, urlunsplit

    parts = urlsplit(settings.sqlalchemy_url)
    netloc = parts.netloc
    if "@" in netloc:
        credentials, host = netloc.rsplit("@", 1)
        user = credentials.split(":", 1)[0]
        netloc = f"{user}:***@{host}"
    return urlunsplit((parts.scheme, netloc, parts.path, "", ""))


# ── Entry point ────────────────────────────────────────────────────────────
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="manage.py", description="AutoMarket AI operations CLI"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init-db", help="create schema and row-level security").set_defaults(
        func=cmd_init_db
    )
    sub.add_parser("sync-catalog", help="back-fill new agents and connectors").set_defaults(
        func=cmd_sync_catalog
    )


    run = sub.add_parser("run-agent", help="run one agent for one workspace")
    run.add_argument("org", help="organisation slug")
    run.add_argument("agent", help="agent slug")
    run.set_defaults(func=cmd_run_agent)

    sub.add_parser("tick", help="run every due agent once").set_defaults(func=cmd_tick)
    sub.add_parser("rollup", help="daily metric rollup").set_defaults(func=cmd_rollup)
    sub.add_parser("rotate-keys", help="re-wrap data keys under the new KEK").set_defaults(
        func=cmd_rotate_keys
    )
    sub.add_parser("check", help="configuration and connectivity report").set_defaults(
        func=cmd_check
    )
    return parser


def main() -> int:
    configure_logging()
    args = build_parser().parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
