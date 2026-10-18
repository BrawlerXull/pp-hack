"""device entries, refund claim index, in-flight refunds, dispute status

Device blocks move from the guard_state.blocked_fingerprints array into device_entries, where they expire
(24 h) and can be released one by one. Blocks in force are carried over with a fresh 24 h expiry and the
incident that last set the guard state. A partial unique index allows one refund claim per capture.

Revision ID: 340bb5957569
Revises: d2bb4af2fcfe
Create Date: 2026-10-18 09:57:05.177594

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "340bb5957569"
down_revision: str | Sequence[str] | None = "d2bb4af2fcfe"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CLAIM_WHERE = "type = 'REFUND' AND status IN ('executed', 'awaiting_approval', 'rejected', 'in_flight')"


def upgrade() -> None:
    op.create_table(
        "device_entries",
        sa.Column("id", sa.String(length=16), nullable=False),
        sa.Column("shop_id", sa.String(length=64), nullable=False),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=10), nullable=False),
        sa.Column("incident_id", sa.String(length=16), nullable=True),
        sa.Column("reason", sa.String(length=200), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["shop_id"], ["shops.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_device_entries_shop_kind", "device_entries", ["shop_id", "kind", "expires_at"], unique=False
    )
    op.create_index(
        "uq_device_entries_open",
        "device_entries",
        ["shop_id", "fingerprint", "kind"],
        unique=True,
        postgresql_where=sa.text("released_at IS NULL"),
    )
    op.execute(
        """
        INSERT INTO device_entries (id, shop_id, fingerprint, kind, incident_id, reason, created_at, expires_at)
        SELECT DISTINCT ON (g.shop_id, fp)
               substr(md5(random()::text || g.shop_id || fp), 1, 16), g.shop_id, fp, 'block',
               g.set_by_incident, 'carried over from the old block list', now(), now() + interval '24 hours'
        FROM guard_state g, unnest(g.blocked_fingerprints) AS fp
        """
    )
    op.drop_column("guard_state", "blocked_fingerprints")

    op.add_column("actions", sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True))
    # Before the index: older code could leave two claims on one capture (per-incident check). Keep the
    # one that settled it (executed, in flight, then the owner's Keep (rejected) over a pending approval, so
    # a Keep is never lost; oldest first) and mark the rest failed, which is outside the index.
    op.execute(
        f"""
        UPDATE actions SET status = 'failed', error = 'duplicate refund claim closed by migration 340bb5957569'
        WHERE id IN (
            SELECT id FROM (
                SELECT id, row_number() OVER (
                    PARTITION BY params ->> 'attempt_id'
                    ORDER BY CASE status WHEN 'executed' THEN 0 WHEN 'in_flight' THEN 1
                                         WHEN 'rejected' THEN 2 ELSE 3 END, created_at
                ) AS n
                FROM actions WHERE {CLAIM_WHERE}
            ) ranked WHERE n > 1
        )
        """
    )
    op.create_index(
        "uq_actions_refund_attempt",
        "actions",
        [sa.literal_column("(params ->> 'attempt_id')")],
        unique=True,
        postgresql_where=sa.text(CLAIM_WHERE),
    )
    op.add_column("checkout_attempts", sa.Column("dispute_status", sa.String(length=40), nullable=True))


def downgrade() -> None:
    op.add_column(
        "guard_state",
        sa.Column(
            "blocked_fingerprints",
            postgresql.ARRAY(sa.VARCHAR(length=64)),
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
    )
    op.execute(
        """
        UPDATE guard_state g SET blocked_fingerprints = coalesce((
            SELECT array_agg(d.fingerprint ORDER BY d.fingerprint) FROM device_entries d
            WHERE d.shop_id = g.shop_id AND d.kind = 'block' AND d.released_at IS NULL AND d.expires_at > now()
        ), '{}')
        """
    )
    op.drop_column("checkout_attempts", "dispute_status")
    # In-flight refunds have no meaning to the old code: leave them visible as failed for the owner to check.
    op.execute(
        "UPDATE actions SET status = 'failed', error = 'in flight at downgrade' WHERE status = 'in_flight'"
    )
    op.drop_index("uq_actions_refund_attempt", table_name="actions", postgresql_where=sa.text(CLAIM_WHERE))
    op.drop_column("actions", "claimed_at")
    op.drop_index(
        "uq_device_entries_open", table_name="device_entries", postgresql_where=sa.text("released_at IS NULL")
    )
    op.drop_index("ix_device_entries_shop_kind", table_name="device_entries")
    op.drop_table("device_entries")
