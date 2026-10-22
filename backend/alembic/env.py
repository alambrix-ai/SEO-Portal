"""Alembic environment.

The database URL comes from application settings rather than ``alembic.ini``,
so migrations and the running service can never point at different databases.
``MIGRATION_DATABASE_URL`` (the schema-owner role) wins when it is set; the
service itself connects as the app role through ``DATABASE_URL``.
"""
from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from app.core.config import settings
from app.db.base import Base

# Importing the model package registers every mapper for autogenerate.
import app.models  # noqa: F401


def migration_url() -> str:
    """The owner DSN when MIGRATION_DATABASE_URL is set, else the app's DSN."""
    return settings.migration_database_url or settings.sqlalchemy_url


config = context.config
# The ini parser treats "%" as interpolation, and a URL-encoded password is
# full of them, so they are escaped here. Never logged: the URL has a password.
config.set_main_option("sqlalchemy.url", migration_url().replace("%", "%%"))

if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def include_object(obj, name, type_, reflected, compare_to) -> bool:  # noqa: ANN001
    """Keep autogenerate focused on the application's own schema."""
    if type_ == "table" and name in ("spatial_ref_sys",):
        return False
    return True


def run_migrations_offline() -> None:
    context.configure(
        url=migration_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
        include_object=include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
            include_object=include_object,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
