"""Alembic environment.

The database URL comes from application settings rather than ``alembic.ini``,
so migrations and the running service can never point at different databases.
"""
from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from app.core.config import settings
from app.db.base import Base

# Importing the model package registers every mapper for autogenerate.
import app.models  # noqa: F401

config = context.config
config.set_main_option("sqlalchemy.url", settings.sqlalchemy_url)

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
        url=settings.sqlalchemy_url,
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
