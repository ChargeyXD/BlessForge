"""Initial schema: every table in COMMERCIAL-PLAN section 4 and 5A.

Created together so later packages add behaviour, not schema churn. The
fleet's own state (racks, activity, mod decisions) and the accounts that
used to live in /data/state JSON files move here too; app/db/importer.py
carries existing files across on first start.

Revision ID: 0001
Revises: 
Create Date: 2026-10-11 21:31:03.344390
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '0001'
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('announcements',
    sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), autoincrement=True, nullable=False),
    sa.Column('scope', sa.String(length=8), nullable=False),
    sa.Column('text', sa.Text(), nullable=False),
    sa.Column('starts_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('ends_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('audit_log',
    sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), autoincrement=True, nullable=False),
    sa.Column('at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('actor', sa.String(length=64), nullable=False),
    sa.Column('action', sa.String(length=64), nullable=False),
    sa.Column('target', sa.String(length=64), nullable=False),
    sa.Column('ticket_id', sa.String(length=32), nullable=True),
    sa.Column('detail', sa.Text(), nullable=False),
    sa.Column('ip', sa.String(length=64), nullable=False),
    sa.Column('ok', sa.Boolean(), nullable=False),
    sa.Column('before', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=True),
    sa.Column('after', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_audit_log_action'), 'audit_log', ['action'], unique=False)
    op.create_index(op.f('ix_audit_log_actor'), 'audit_log', ['actor'], unique=False)
    op.create_index(op.f('ix_audit_log_at'), 'audit_log', ['at'], unique=False)
    op.create_index('ix_audit_log_at_id', 'audit_log', ['at', 'id'], unique=False)
    op.create_index(op.f('ix_audit_log_target'), 'audit_log', ['target'], unique=False)
    op.create_table('bundles',
    sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), autoincrement=True, nullable=False),
    sa.Column('code', sa.String(length=32), nullable=False),
    sa.Column('name', sa.String(length=80), nullable=False),
    sa.Column('items', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('discount', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('stripe_price_id', sa.String(length=64), nullable=True),
    sa.Column('active', sa.Boolean(), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('code'),
    sa.UniqueConstraint('stripe_price_id')
    )
    op.create_table('content_items',
    sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), autoincrement=True, nullable=False),
    sa.Column('kind', sa.String(length=32), nullable=False),
    sa.Column('slug', sa.String(length=120), nullable=False),
    sa.Column('locale', sa.String(length=16), nullable=False),
    sa.Column('draft', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('published', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=True),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('published_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('kind', 'slug', 'locale')
    )
    op.create_table('coupons',
    sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), autoincrement=True, nullable=False),
    sa.Column('code', sa.String(length=32), nullable=False),
    sa.Column('kind', sa.String(length=16), nullable=False),
    sa.Column('value', sa.Integer(), nullable=False),
    sa.Column('currency', sa.String(length=3), nullable=True),
    sa.Column('max_redemptions', sa.Integer(), nullable=True),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('stripe_promotion_code_id', sa.String(length=64), nullable=True),
    sa.Column('active', sa.Boolean(), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('code'),
    sa.UniqueConstraint('stripe_promotion_code_id')
    )
    op.create_table('jobs',
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('kind', sa.String(length=32), nullable=False),
    sa.Column('title', sa.String(length=200), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('progress', sa.Float(), nullable=False),
    sa.Column('step', sa.String(length=200), nullable=False),
    sa.Column('result', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=True),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('server_id', sa.String(length=64), nullable=True),
    sa.Column('created_by', sa.String(length=64), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_jobs_server_id'), 'jobs', ['server_id'], unique=False)
    op.create_table('meta',
    sa.Column('key', sa.String(length=64), nullable=False),
    sa.Column('value', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.PrimaryKeyConstraint('key')
    )
    op.create_table('mod_decisions',
    sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), autoincrement=True, nullable=False),
    sa.Column('key', sa.String(length=200), nullable=False),
    sa.Column('verdict', sa.String(length=8), nullable=False),
    sa.Column('scope', sa.String(length=64), nullable=False),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('example_file', sa.String(length=255), nullable=False),
    sa.Column('mod_id', sa.String(length=128), nullable=True),
    sa.Column('project_id', sa.String(length=64), nullable=True),
    sa.Column('source', sa.String(length=16), nullable=True),
    sa.Column('reason', sa.Text(), nullable=False),
    sa.Column('evidence', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('added_at', sa.Float(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_mod_decisions_key'), 'mod_decisions', ['key'], unique=False)
    op.create_table('notices',
    sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), autoincrement=True, nullable=False),
    sa.Column('reporter', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('target', sa.String(length=200), nullable=False),
    sa.Column('category', sa.String(length=32), nullable=False),
    sa.Column('body', sa.Text(), nullable=False),
    sa.Column('decision', sa.String(length=32), nullable=True),
    sa.Column('statement_of_reasons', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('decided_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('plans',
    sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), autoincrement=True, nullable=False),
    sa.Column('code', sa.String(length=32), nullable=False),
    sa.Column('name', sa.String(length=80), nullable=False),
    sa.Column('ram_mb', sa.Integer(), nullable=False),
    sa.Column('cpu_pct', sa.Integer(), nullable=False),
    sa.Column('disk_gb', sa.Integer(), nullable=False),
    sa.Column('backup_slots', sa.Integer(), nullable=False),
    sa.Column('max_databases', sa.Integer(), nullable=False),
    sa.Column('max_ram_on_node', sa.Integer(), nullable=True),
    sa.Column('active', sa.Boolean(), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('code')
    )
    op.create_table('racks',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('name', sa.String(length=48), nullable=False),
    sa.Column('motif', sa.String(length=16), nullable=False),
    sa.Column('position', sa.Integer(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('regions',
    sa.Column('code', sa.String(length=32), nullable=False),
    sa.Column('name', sa.String(length=80), nullable=False),
    sa.Column('currency', sa.String(length=3), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('code')
    )
    op.create_table('server_activity',
    sa.Column('server_id', sa.String(length=64), nullable=False),
    sa.Column('record', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.PrimaryKeyConstraint('server_id')
    )
    op.create_table('users',
    sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), autoincrement=True, nullable=False),
    sa.Column('username', sa.String(length=32), nullable=False),
    sa.Column('email', sa.String(length=254), nullable=True),
    sa.Column('email_verified_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('display', sa.String(length=60), nullable=False),
    sa.Column('password_hash', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('totp_secret', sa.String(length=128), nullable=True),
    sa.Column('recovery_codes', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('role', sa.String(length=24), nullable=False),
    sa.Column('permissions', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('denied', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('scope', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('locale', sa.String(length=16), nullable=False),
    sa.Column('must_change', sa.Boolean(), nullable=False),
    sa.Column('failures', sa.Integer(), nullable=False),
    sa.Column('locked_until', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_login', sa.DateTime(timezone=True), nullable=True),
    sa.Column('disabled', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('email'),
    sa.UniqueConstraint('username')
    )
    op.create_table('webhook_events',
    sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), autoincrement=True, nullable=False),
    sa.Column('provider', sa.String(length=16), nullable=False),
    sa.Column('event_id', sa.String(length=128), nullable=False),
    sa.Column('type', sa.String(length=64), nullable=False),
    sa.Column('processed_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('provider', 'event_id')
    )
    op.create_table('consents',
    sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), autoincrement=True, nullable=False),
    sa.Column('user_id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=False),
    sa.Column('order_id', sa.String(length=64), nullable=True),
    sa.Column('document', sa.String(length=16), nullable=False),
    sa.Column('version', sa.String(length=32), nullable=False),
    sa.Column('accepted_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('ip', sa.String(length=64), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_consents_user_id'), 'consents', ['user_id'], unique=False)
    op.create_table('customers',
    sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), autoincrement=True, nullable=False),
    sa.Column('user_id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=False),
    sa.Column('legal_name', sa.String(length=200), nullable=False),
    sa.Column('address', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('country', sa.String(length=2), nullable=False),
    sa.Column('vat_id', sa.String(length=32), nullable=True),
    sa.Column('vat_verified_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('stripe_customer_id', sa.String(length=64), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('stripe_customer_id'),
    sa.UniqueConstraint('user_id')
    )
    op.create_table('feature_flags',
    sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), autoincrement=True, nullable=False),
    sa.Column('plan_id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=False),
    sa.Column('flag', sa.String(length=48), nullable=False),
    sa.Column('value', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.ForeignKeyConstraint(['plan_id'], ['plans.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('plan_id', 'flag')
    )
    op.create_table('nodes',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('region', sa.String(length=32), nullable=True),
    sa.Column('name', sa.String(length=80), nullable=False),
    sa.Column('fqdn', sa.String(length=253), nullable=False),
    sa.Column('agent_port', sa.Integer(), nullable=False),
    sa.Column('sftp_port', sa.Integer(), nullable=False),
    sa.Column('scheme', sa.String(length=8), nullable=False),
    sa.Column('token_id', sa.String(length=32), nullable=False),
    sa.Column('token_encrypted', sa.Text(), nullable=False),
    sa.Column('agent_version', sa.String(length=32), nullable=False),
    sa.Column('total_ram_mb', sa.Integer(), nullable=False),
    sa.Column('reserved_ram_mb', sa.Integer(), nullable=False),
    sa.Column('headroom_ram_mb', sa.Integer(), nullable=False),
    sa.Column('overhead_pct', sa.Float(), nullable=False),
    sa.Column('house_reserve_ram_mb', sa.Integer(), nullable=False),
    sa.Column('cpu_threads', sa.Integer(), nullable=False),
    sa.Column('disk_gb', sa.Integer(), nullable=False),
    sa.Column('state', sa.String(length=16), nullable=False),
    sa.Column('last_seen_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['region'], ['regions.code'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('token_id')
    )
    op.create_table('plan_prices',
    sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), autoincrement=True, nullable=False),
    sa.Column('plan_id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=False),
    sa.Column('currency', sa.String(length=3), nullable=False),
    sa.Column('term', sa.String(length=16), nullable=False),
    sa.Column('amount_minor', sa.BigInteger(), nullable=False),
    sa.Column('stripe_price_id', sa.String(length=64), nullable=True),
    sa.Column('active', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['plan_id'], ['plans.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('stripe_price_id')
    )
    op.create_index(op.f('ix_plan_prices_plan_id'), 'plan_prices', ['plan_id'], unique=False)
    op.create_table('rack_members',
    sa.Column('server_id', sa.String(length=64), nullable=False),
    sa.Column('rack_id', sa.String(length=64), nullable=False),
    sa.ForeignKeyConstraint(['rack_id'], ['racks.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('server_id')
    )
    op.create_index(op.f('ix_rack_members_rack_id'), 'rack_members', ['rack_id'], unique=False)
    op.create_table('sessions',
    sa.Column('token_sha256', sa.String(length=64), nullable=False),
    sa.Column('user_id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('last_seen', sa.DateTime(timezone=True), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('ip', sa.String(length=64), nullable=False),
    sa.Column('user_agent', sa.String(length=200), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('token_sha256')
    )
    op.create_index(op.f('ix_sessions_expires_at'), 'sessions', ['expires_at'], unique=False)
    op.create_index(op.f('ix_sessions_user_id'), 'sessions', ['user_id'], unique=False)
    op.create_table('subscriptions',
    sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), autoincrement=True, nullable=False),
    sa.Column('customer_id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=False),
    sa.Column('plan_id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=False),
    sa.Column('region', sa.String(length=32), nullable=True),
    sa.Column('status', sa.String(length=24), nullable=False),
    sa.Column('stripe_subscription_id', sa.String(length=64), nullable=True),
    sa.Column('current_period_end', sa.DateTime(timezone=True), nullable=True),
    sa.Column('cancel_at_period_end', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['customer_id'], ['customers.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['plan_id'], ['plans.id'], ),
    sa.ForeignKeyConstraint(['region'], ['regions.code'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('stripe_subscription_id')
    )
    op.create_index(op.f('ix_subscriptions_customer_id'), 'subscriptions', ['customer_id'], unique=False)
    op.create_table('servers',
    sa.Column('uuid', sa.String(length=36), nullable=False),
    sa.Column('kind', sa.String(length=16), nullable=False),
    sa.Column('subscription_id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=True),
    sa.Column('owner_id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=True),
    sa.Column('node_id', sa.String(length=64), nullable=True),
    sa.Column('name', sa.String(length=120), nullable=False),
    sa.Column('port', sa.Integer(), nullable=False),
    sa.Column('template', sa.String(length=32), nullable=False),
    sa.Column('mc_version', sa.String(length=32), nullable=False),
    sa.Column('java_major', sa.Integer(), nullable=True),
    sa.Column('executable', sa.Text(), nullable=False),
    sa.Column('execution_command', sa.Text(), nullable=False),
    sa.Column('limits_source', sa.String(length=8), nullable=False),
    sa.Column('limits', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('capacity_class', sa.String(length=8), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('notes', sa.Text(), nullable=False),
    sa.Column('subdomain', sa.String(length=63), nullable=True),
    sa.Column('status', sa.String(length=24), nullable=False),
    sa.Column('auto_start', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['node_id'], ['nodes.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['owner_id'], ['users.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['subscription_id'], ['subscriptions.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('uuid'),
    sa.UniqueConstraint('subdomain')
    )
    op.create_index(op.f('ix_servers_node_id'), 'servers', ['node_id'], unique=False)
    op.create_index(op.f('ix_servers_owner_id'), 'servers', ['owner_id'], unique=False)
    op.create_table('allocations',
    sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), autoincrement=True, nullable=False),
    sa.Column('node_id', sa.String(length=64), nullable=False),
    sa.Column('ip', sa.String(length=64), nullable=False),
    sa.Column('port', sa.Integer(), nullable=False),
    sa.Column('server_id', sa.String(length=36), nullable=True),
    sa.Column('public_port', sa.Integer(), nullable=True),
    sa.ForeignKeyConstraint(['node_id'], ['nodes.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['server_id'], ['servers.uuid'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('node_id', 'ip', 'port')
    )
    op.create_index(op.f('ix_allocations_server_id'), 'allocations', ['server_id'], unique=False)
    op.create_table('backups',
    sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), autoincrement=True, nullable=False),
    sa.Column('server_id', sa.String(length=36), nullable=False),
    sa.Column('snapshot_id', sa.String(length=80), nullable=False),
    sa.Column('size', sa.BigInteger(), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['server_id'], ['servers.uuid'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_backups_server_id'), 'backups', ['server_id'], unique=False)
    op.create_table('schedules',
    sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), autoincrement=True, nullable=False),
    sa.Column('server_id', sa.String(length=36), nullable=False),
    sa.Column('cron', sa.String(length=64), nullable=False),
    sa.Column('action', sa.String(length=16), nullable=False),
    sa.Column('payload', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('enabled', sa.Boolean(), nullable=False),
    sa.Column('next_run', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['server_id'], ['servers.uuid'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_schedules_next_run'), 'schedules', ['next_run'], unique=False)
    op.create_index(op.f('ix_schedules_server_id'), 'schedules', ['server_id'], unique=False)
    op.create_table('server_users',
    sa.Column('server_id', sa.String(length=36), nullable=False),
    sa.Column('user_id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=False),
    sa.Column('permissions', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.ForeignKeyConstraint(['server_id'], ['servers.uuid'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('server_id', 'user_id')
    )
    op.create_table('tickets',
    sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), autoincrement=True, nullable=False),
    sa.Column('user_id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=False),
    sa.Column('server_id', sa.String(length=36), nullable=True),
    sa.Column('category', sa.String(length=24), nullable=False),
    sa.Column('subject', sa.String(length=200), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('priority', sa.String(length=8), nullable=False),
    sa.Column('sla_paused', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('closed_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['server_id'], ['servers.uuid'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_tickets_user_id'), 'tickets', ['user_id'], unique=False)
    op.create_table('access_grants',
    sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), autoincrement=True, nullable=False),
    sa.Column('ticket_id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=False),
    sa.Column('server_id', sa.String(length=36), nullable=False),
    sa.Column('scopes', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('granted_by', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=True),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['granted_by'], ['users.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['server_id'], ['servers.uuid'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['ticket_id'], ['tickets.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_access_grants_ticket_id'), 'access_grants', ['ticket_id'], unique=False)
    op.create_table('ticket_messages',
    sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), autoincrement=True, nullable=False),
    sa.Column('ticket_id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=False),
    sa.Column('author_id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=True),
    sa.Column('body', sa.Text(), nullable=False),
    sa.Column('internal', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['author_id'], ['users.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['ticket_id'], ['tickets.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_ticket_messages_ticket_id'), 'ticket_messages', ['ticket_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_ticket_messages_ticket_id'), table_name='ticket_messages')
    op.drop_table('ticket_messages')
    op.drop_index(op.f('ix_access_grants_ticket_id'), table_name='access_grants')
    op.drop_table('access_grants')
    op.drop_index(op.f('ix_tickets_user_id'), table_name='tickets')
    op.drop_table('tickets')
    op.drop_table('server_users')
    op.drop_index(op.f('ix_schedules_server_id'), table_name='schedules')
    op.drop_index(op.f('ix_schedules_next_run'), table_name='schedules')
    op.drop_table('schedules')
    op.drop_index(op.f('ix_backups_server_id'), table_name='backups')
    op.drop_table('backups')
    op.drop_index(op.f('ix_allocations_server_id'), table_name='allocations')
    op.drop_table('allocations')
    op.drop_index(op.f('ix_servers_owner_id'), table_name='servers')
    op.drop_index(op.f('ix_servers_node_id'), table_name='servers')
    op.drop_table('servers')
    op.drop_index(op.f('ix_subscriptions_customer_id'), table_name='subscriptions')
    op.drop_table('subscriptions')
    op.drop_index(op.f('ix_sessions_user_id'), table_name='sessions')
    op.drop_index(op.f('ix_sessions_expires_at'), table_name='sessions')
    op.drop_table('sessions')
    op.drop_index(op.f('ix_rack_members_rack_id'), table_name='rack_members')
    op.drop_table('rack_members')
    op.drop_index(op.f('ix_plan_prices_plan_id'), table_name='plan_prices')
    op.drop_table('plan_prices')
    op.drop_table('nodes')
    op.drop_table('feature_flags')
    op.drop_table('customers')
    op.drop_index(op.f('ix_consents_user_id'), table_name='consents')
    op.drop_table('consents')
    op.drop_table('webhook_events')
    op.drop_table('users')
    op.drop_table('server_activity')
    op.drop_table('regions')
    op.drop_table('racks')
    op.drop_table('plans')
    op.drop_table('notices')
    op.drop_index(op.f('ix_mod_decisions_key'), table_name='mod_decisions')
    op.drop_table('mod_decisions')
    op.drop_table('meta')
    op.drop_index(op.f('ix_jobs_server_id'), table_name='jobs')
    op.drop_table('jobs')
    op.drop_table('coupons')
    op.drop_table('content_items')
    op.drop_table('bundles')
    op.drop_index(op.f('ix_audit_log_target'), table_name='audit_log')
    op.drop_index('ix_audit_log_at_id', table_name='audit_log')
    op.drop_index(op.f('ix_audit_log_at'), table_name='audit_log')
    op.drop_index(op.f('ix_audit_log_actor'), table_name='audit_log')
    op.drop_index(op.f('ix_audit_log_action'), table_name='audit_log')
    op.drop_table('audit_log')
    op.drop_table('announcements')
