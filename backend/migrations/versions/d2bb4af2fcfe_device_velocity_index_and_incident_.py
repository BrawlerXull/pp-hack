"""device velocity index and incident rules_hint

ix_attempts_shop_fp_ts serves the guard's per-device count on every checkout (app/guard/check.py).
rules_hint is nullable: existing incidents simply have no hint.

Revision ID: d2bb4af2fcfe
Revises: cc5ad03d6705
Create Date: 2026-10-18 09:19:34.871241

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "d2bb4af2fcfe"
down_revision: Union[str, Sequence[str], None] = "cc5ad03d6705"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_index(
        "ix_attempts_shop_fp_ts", "checkout_attempts", ["shop_id", "fingerprint_hash", "ts"], unique=False
    )
    op.add_column("incidents", sa.Column("rules_hint", sa.String(length=40), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("incidents", "rules_hint")
    op.drop_index("ix_attempts_shop_fp_ts", table_name="checkout_attempts")
