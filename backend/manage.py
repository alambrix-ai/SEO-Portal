"""Operations CLI.

    python manage.py init-db          # create schema + row-level security
    python manage.py sync-catalog     # back-fill new agents/connectors
    python manage.py run-agent <org-slug> <agent-slug>
    python manage.py tick            # run every due agent once
    python manage.py rollup          # daily metric rollup
    python manage.py rotate-keys     # re-wrap org data keys under the new KEK
    python manage.py check           # configuration and connectivity report
    python manage.py verify <slug> --workspace <id|slug> [--allow-writes] [--record]
                                     [--environment live|test_account|sandbox]
                                     [--set verifyKEY=VALUE ...]
    python manage.py verify --all --workspace <id|slug>   # every connected connector
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


def _find_workspace(db, ref: str):  # noqa: ANN001, ANN202
    """A workspace by id or slug, or None."""
    import uuid

    from app.models.workspace import Organization

    ref = (ref or "").strip()
    try:
        uuid.UUID(ref)
    except ValueError:
        pass
    else:
        org = db.get(Organization, ref)
        if org is not None:
            return org
    return db.execute(select(Organization).where(Organization.slug == ref)).scalar_one_or_none()


def _print_report(report, *, name: str, workspace: str) -> None:  # noqa: ANN001
    from app.services.verification import ENVIRONMENT_LABELS

    env = ENVIRONMENT_LABELS.get(report.environment, report.environment)
    print(f"\n{name} ({report.slug}) - {env} - workspace {workspace}")
    if report.steps:
        width = max(len(s.key) for s in report.steps)
        for step in report.steps:
            mark = "SKIP" if step.skipped else ("PASS" if step.ok else "FAIL")
            took = "" if step.skipped else f"{step.seconds:6.2f}s"
            print(f"  {mark}  {step.kind:<5}  {step.key:<{width}}  {took:>7}  {step.detail}")
    if report.note:
        print(f"  Note: {report.note}")
    print(f"  Result: {'PASSED' if report.ok else 'FAILED'}")


def cmd_verify(args: argparse.Namespace) -> int:
    """Run connectors' live checks with one workspace's connection.

    Exit code 0 when every connector checked passed; 1 when any failed, could
    not be run, or has no live check yet; 2 for a usage problem (unknown
    workspace or connector, nothing to check).
    """
    from app.connectors.base import registry
    from app.core.exceptions import AppError
    from app.db.session import SessionLocal, session_scope
    from app.services import verification

    if bool(args.slug) == bool(args.all):
        print("Give one connector slug, or --all (not both).", file=sys.stderr)
        return 2
    if args.slug and registry.get_spec(args.slug) is None:
        print(f"No connector called {args.slug!r}.", file=sys.stderr)
        return 2

    options: dict[str, str] = {}
    for pair in args.set or []:
        key, sep, value = pair.partition("=")
        if not sep:
            print(f"--set needs key=value, got {pair!r}.", file=sys.stderr)
            return 2
        options[key.strip()] = value
    try:
        options = verification.clean_options(options)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    lookup = SessionLocal()
    try:
        org = _find_workspace(lookup, args.workspace)
        if org is None:
            print(f"No workspace with id or slug {args.workspace!r}.", file=sys.stderr)
            return 2
        tenant_id, org_name = org.id, org.slug
    finally:
        lookup.close()

    run_by = f"cli:{args.run_by}" if args.run_by else "cli"
    results: list[tuple[str, str]] = []
    with session_scope(tenant_id) as db:
        from app.models.workspace import Organization
        from app.services.connectors import build_connector_bundle

        org = db.get(Organization, tenant_id)
        if args.all:
            bundle = build_connector_bundle(db, org)
            slugs = [s for s in registry.connector_slugs() if s in bundle.connected]
            bundle.close()
            slugs = [s for s in slugs if verification.has_steps(s)]
            if not slugs:
                print(f"Nothing to check: no connector connected in {org_name} has a live check yet.")
                return 2
        else:
            slugs = [args.slug]

        for slug in slugs:
            name = registry.get_spec(slug).name
            outcome = verification.RunOutcome()
            try:
                report = verification.run(
                    db,
                    org,
                    slug,
                    allow_writes=args.allow_writes,
                    record=args.record,
                    environment=args.environment or None,
                    options=options,
                    run_by=run_by,
                    outcome=outcome,
                )
            except AppError as exc:
                print(f"\n{name} ({slug}): {exc.message}", file=sys.stderr)
                results.append((slug, "ERROR"))
                continue
            db.commit()
            _print_report(report, name=name, workspace=org_name)
            if not report.steps:
                results.append((slug, "NO CHECK"))
                continue
            for path in outcome.cassettes:
                print(f"  Recorded {path}")
            results.append((slug, "PASSED" if report.ok else "FAILED"))
    verification.invalidate_cache()

    if len(results) > 1:
        print("\nSummary")
        for slug, result in results:
            print(f"  {result:<8}  {slug}")
    return 0 if all(result == "PASSED" for _, result in results) else 1


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
        prog="manage.py", description="Synaprank.ai operations CLI"
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

    verify = sub.add_parser(
        "verify", help="run a connector's live checks against a real (test) account"
    )
    verify.add_argument("slug", nargs="?", default="", help="connector slug, e.g. site_crawler")
    verify.add_argument(
        "--all", action="store_true", help="every connected connector that has a live check"
    )
    verify.add_argument(
        "--workspace", required=True, help="the workspace (id or slug) whose connection to use"
    )
    verify.add_argument(
        "--allow-writes",
        action="store_true",
        help="also run write steps (they undo themselves); test or sandbox accounts only",
    )
    verify.add_argument(
        "--environment",
        choices=("live", "test_account", "sandbox"),
        default="",
        help="say what the connection points at, overriding its own setting for this run",
    )
    verify.add_argument(
        "--record",
        action="store_true",
        help="save the scrubbed HTTP exchanges under tests/cassettes/<slug>/",
    )
    verify.add_argument(
        "--set",
        action="append",
        default=[],
        metavar="verifyKEY=VALUE",
        help="a run-only option for the checks, e.g. verifyImageUrl=https://...; repeatable; "
        "names must start with 'verify'; never saved",
    )
    verify.add_argument("--run-by", default="", help="who ran it, stored with the result")
    verify.set_defaults(func=cmd_verify)
    return parser


def main() -> int:
    configure_logging()
    args = build_parser().parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
