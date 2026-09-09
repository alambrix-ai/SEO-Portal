"""One pending proposal per agent per target.

Adds ``approval_items.payload_digest`` and the index the de-duplication query
uses.

Why the column exists: the approvals queue holds decisions a person still owes
an answer to, and an agent on a schedule re-derives the same recommendation
every run. Without a way to recognise "I have already proposed this", each
pass added another card — one decision turning into dozens of copies, a
meaningless badge count, and copies left behind to be applied again after the
first was approved.

The digest is a SHA-256 prefix of the canonical payload. It cannot be the
ciphertext: AES-GCM draws a fresh nonce per write, so the same payload never
encrypts the same way twice. It is non-reversible and holds no content.

Existing pending rows are backfilled with an empty digest, which is
deliberate. An empty digest matches nothing, so the first run after this
migration treats each existing item as a proposal whose content is unknown and
updates it in place rather than adding another — which is exactly the intent.

Revision ID: 0003_one_pending_per_target
Revises: 0002_passwordless_sign_in
Create Date: 2026-09-08
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# Kept under 32 characters: that is the width of alembic_version.version_num,
# and a longer id fails the version bump *after* the DDL has run.
revision: str = "0003_one_pending_per_target"
down_revision: str | None = "0002_passwordless_sign_in"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "approval_items",
        sa.Column("payload_digest", sa.String(length=32), nullable=False, server_default=""),
    )
    op.create_index(
        "ix_approval_items_payload_digest", "approval_items", ["payload_digest"]
    )
    op.create_index(
        "ix_approval_open_target",
        "approval_items",
        ["tenant_id", "status", "agent_slug", "target_kind", "target_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_approval_open_target", table_name="approval_items")
    op.drop_index("ix_approval_items_payload_digest", table_name="approval_items")
    op.drop_column("approval_items", "payload_digest")
