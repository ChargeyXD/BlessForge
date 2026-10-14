"""Server stats history: players, CPU and memory over time.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-14 08:00:00
"""
from alembic import op
import sqlalchemy as sa

revision = '0002'
down_revision = '0001'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('server_stats',
    sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), autoincrement=True, nullable=False),
    sa.Column('server_id', sa.String(length=36), nullable=False),
    sa.Column('at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('res', sa.Integer(), nullable=False),
    sa.Column('samples', sa.Integer(), nullable=False),
    sa.Column('up', sa.Integer(), nullable=False),
    sa.Column('online', sa.Integer(), nullable=False),
    sa.Column('max_players', sa.Integer(), nullable=True),
    sa.Column('cpu', sa.Float(), nullable=True),
    sa.Column('mem', sa.BigInteger(), nullable=True),
    sa.Column('mem_limit', sa.BigInteger(), nullable=True),
    sa.ForeignKeyConstraint(['server_id'], ['servers.uuid'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('server_id', 'at', 'res', name='uq_server_stats_server_at_res')
    )
    op.create_index('ix_server_stats_server_at', 'server_stats', ['server_id', 'at'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_server_stats_server_at', table_name='server_stats')
    op.drop_table('server_stats')
