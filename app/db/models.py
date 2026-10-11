"""Every table BlessForge keeps, including the ones nothing writes yet.

The commercial plan (section 4 and 5A) names the tables; they are all
created now, in the first migration, so a later package adds behaviour
rather than schema churn. What each one is for is said once, on the class.

Types are chosen to run on Postgres -- the production database -- and on
SQLite, which a node-less dev run and the offline tests fall back to:

  * JSON columns are JSONB on Postgres and JSON text elsewhere.
  * Integer keys are BIGINT on Postgres; SQLite only auto-increments a
    column declared INTEGER PRIMARY KEY, so it gets that instead.
  * Times are timezone-aware. SQLite hands them back naive, so the code
    that reads them (app/db/__init__.py `epoch`) treats naive as UTC.
  * Money is integer minor units (cents, paise), never a float.

Nothing here holds a secret in the clear: passwords are scrypt records,
sessions are stored by the SHA-256 of their token, and a node's token is
stored encrypted.
"""
from __future__ import annotations

import datetime as dt

from sqlalchemy import (JSON, BigInteger, Boolean, DateTime, Float, ForeignKey,
                        Index, Integer, String, Text, UniqueConstraint, func)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

JSONType = JSON().with_variant(JSONB(), "postgresql")
BigID = BigInteger().with_variant(Integer(), "sqlite")
TS = DateTime(timezone=True)


def now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class Base(DeclarativeBase):
    pass


def _pk() -> Mapped[int]:
    return mapped_column(BigID, primary_key=True, autoincrement=True)


def _created() -> Mapped[dt.datetime]:
    return mapped_column(TS, nullable=False, default=now, server_default=func.now())


# --- identity ------------------------------------------------------------------


class User(Base):
    """One identity for the storefront and the panel.

    `username` is how today's accounts sign in; `email` arrives with public
    sign-up (WP-07) and is optional until then. `password_hash` is the whole
    scrypt record -- algorithm, n, r, p, salt, hash -- so the parameters can
    be raised without invalidating anyone.
    """
    __tablename__ = "users"
    id: Mapped[int] = _pk()
    username: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    email: Mapped[str | None] = mapped_column(String(254), unique=True)
    email_verified_at: Mapped[dt.datetime | None] = mapped_column(TS)
    display: Mapped[str] = mapped_column(String(60), nullable=False, default="")
    password_hash: Mapped[dict] = mapped_column(JSONType, nullable=False)
    totp_secret: Mapped[str | None] = mapped_column(String(128))
    recovery_codes: Mapped[list] = mapped_column(JSONType, nullable=False, default=list)
    role: Mapped[str] = mapped_column(String(24), nullable=False, default="member")
    permissions: Mapped[list] = mapped_column(JSONType, nullable=False, default=list)
    denied: Mapped[list] = mapped_column(JSONType, nullable=False, default=list)
    scope: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    locale: Mapped[str] = mapped_column(String(16), nullable=False, default="en")
    must_change: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    failures: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    locked_until: Mapped[dt.datetime | None] = mapped_column(TS)
    last_login: Mapped[dt.datetime | None] = mapped_column(TS)
    disabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[dt.datetime] = _created()


class Session(Base):
    """A signed-in browser. Keyed by the SHA-256 of its token, never the token."""
    __tablename__ = "sessions"
    token_sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(
        BigID, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    created_at: Mapped[dt.datetime] = _created()
    last_seen: Mapped[dt.datetime] = mapped_column(TS, nullable=False, default=now)
    expires_at: Mapped[dt.datetime] = mapped_column(TS, nullable=False, index=True)
    ip: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    user_agent: Mapped[str] = mapped_column(String(200), nullable=False, default="")


class Customer(Base):
    """Billing identity for a user. Country decides currency and tax."""
    __tablename__ = "customers"
    id: Mapped[int] = _pk()
    user_id: Mapped[int] = mapped_column(
        BigID, ForeignKey("users.id", ondelete="CASCADE"), unique=True, nullable=False)
    legal_name: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    address: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    country: Mapped[str] = mapped_column(String(2), nullable=False, default="NL")
    vat_id: Mapped[str | None] = mapped_column(String(32))
    vat_verified_at: Mapped[dt.datetime | None] = mapped_column(TS)
    stripe_customer_id: Mapped[str | None] = mapped_column(String(64), unique=True)
    created_at: Mapped[dt.datetime] = _created()


# --- capacity -------------------------------------------------------------------


class Region(Base):
    """Where servers can be sold. A new region is a row, not a deploy."""
    __tablename__ = "regions"
    code: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="EUR")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="open")
    created_at: Mapped[dt.datetime] = _created()


class Node(Base):
    """A machine running the BlessForge agent. Capacity is computed from this."""
    __tablename__ = "nodes"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    region: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("regions.code", ondelete="SET NULL"))
    name: Mapped[str] = mapped_column(String(80), nullable=False, default="")
    fqdn: Mapped[str] = mapped_column(String(253), nullable=False)
    agent_port: Mapped[int] = mapped_column(Integer, nullable=False, default=8443)
    sftp_port: Mapped[int] = mapped_column(Integer, nullable=False, default=2022)
    scheme: Mapped[str] = mapped_column(String(8), nullable=False, default="https")
    token_id: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    token_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    agent_version: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    total_ram_mb: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    reserved_ram_mb: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    headroom_ram_mb: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    overhead_pct: Mapped[float] = mapped_column(Float, nullable=False, default=0.10)
    house_reserve_ram_mb: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cpu_threads: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    disk_gb: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    last_seen_at: Mapped[dt.datetime | None] = mapped_column(TS)
    created_at: Mapped[dt.datetime] = _created()


class Allocation(Base):
    """A port on a node; the edge maps `public_port` to it."""
    __tablename__ = "allocations"
    __table_args__ = (UniqueConstraint("node_id", "ip", "port"),)
    id: Mapped[int] = _pk()
    node_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("nodes.id", ondelete="CASCADE"), nullable=False)
    ip: Mapped[str] = mapped_column(String(64), nullable=False, default="0.0.0.0")
    port: Mapped[int] = mapped_column(Integer, nullable=False)
    server_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("servers.uuid", ondelete="SET NULL"), index=True)
    public_port: Mapped[int | None] = mapped_column(Integer)


# --- catalogue and billing --------------------------------------------------------


class Plan(Base):
    __tablename__ = "plans"
    id: Mapped[int] = _pk()
    code: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    ram_mb: Mapped[int] = mapped_column(Integer, nullable=False)
    cpu_pct: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    disk_gb: Mapped[int] = mapped_column(Integer, nullable=False)
    backup_slots: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    max_databases: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_ram_on_node: Mapped[int | None] = mapped_column(Integer)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class PlanPrice(Base):
    """A price is data, never code. A change is a new row (and a new Stripe Price)."""
    __tablename__ = "plan_prices"
    id: Mapped[int] = _pk()
    plan_id: Mapped[int] = mapped_column(
        BigID, ForeignKey("plans.id", ondelete="CASCADE"), nullable=False, index=True)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    term: Mapped[str] = mapped_column(String(16), nullable=False)
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    stripe_price_id: Mapped[str | None] = mapped_column(String(64), unique=True)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[dt.datetime] = _created()


class Subscription(Base):
    """Mirrors Stripe through its webhooks."""
    __tablename__ = "subscriptions"
    id: Mapped[int] = _pk()
    customer_id: Mapped[int] = mapped_column(
        BigID, ForeignKey("customers.id", ondelete="CASCADE"), nullable=False, index=True)
    plan_id: Mapped[int] = mapped_column(BigID, ForeignKey("plans.id"), nullable=False)
    region: Mapped[str | None] = mapped_column(String(32), ForeignKey("regions.code"))
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="pending")
    stripe_subscription_id: Mapped[str | None] = mapped_column(String(64), unique=True)
    current_period_end: Mapped[dt.datetime | None] = mapped_column(TS)
    cancel_at_period_end: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[dt.datetime] = _created()


# --- servers --------------------------------------------------------------------


class Server(Base):
    """The desired state of one server. The agent's container spec is made from this.

    `kind` is customer, personal or staff_test; a personal server has no
    subscription and never touches Stripe. `limits` holds ram, cpu, disk,
    swap, io, backup_slots, databases and extra_ports.
    """
    __tablename__ = "servers"
    uuid: Mapped[str] = mapped_column(String(36), primary_key=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False, default="personal")
    subscription_id: Mapped[int | None] = mapped_column(
        BigID, ForeignKey("subscriptions.id", ondelete="SET NULL"))
    owner_id: Mapped[int | None] = mapped_column(
        BigID, ForeignKey("users.id", ondelete="SET NULL"), index=True)
    node_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("nodes.id", ondelete="SET NULL"), index=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    port: Mapped[int] = mapped_column(Integer, nullable=False)
    template: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    mc_version: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    java_major: Mapped[int | None] = mapped_column(Integer)
    executable: Mapped[str] = mapped_column(Text, nullable=False, default="")
    execution_command: Mapped[str] = mapped_column(Text, nullable=False, default="")
    limits_source: Mapped[str] = mapped_column(String(8), nullable=False, default="custom")
    limits: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    capacity_class: Mapped[str] = mapped_column(String(8), nullable=False, default="reserved")
    expires_at: Mapped[dt.datetime | None] = mapped_column(TS)
    notes: Mapped[str] = mapped_column(Text, nullable=False, default="")
    subdomain: Mapped[str | None] = mapped_column(String(63), unique=True)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="installing")
    auto_start: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[dt.datetime] = _created()
    updated_at: Mapped[dt.datetime] = mapped_column(TS, nullable=False, default=now,
                                                    onupdate=now)


class ServerUser(Base):
    """A subuser the customer invited onto one server."""
    __tablename__ = "server_users"
    server_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("servers.uuid", ondelete="CASCADE"), primary_key=True)
    user_id: Mapped[int] = mapped_column(
        BigID, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    permissions: Mapped[list] = mapped_column(JSONType, nullable=False, default=list)


class Backup(Base):
    __tablename__ = "backups"
    id: Mapped[int] = _pk()
    server_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("servers.uuid", ondelete="CASCADE"), nullable=False, index=True)
    snapshot_id: Mapped[str] = mapped_column(String(80), nullable=False, default="")
    size: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    created_at: Mapped[dt.datetime] = _created()


class Schedule(Base):
    __tablename__ = "schedules"
    id: Mapped[int] = _pk()
    server_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("servers.uuid", ondelete="CASCADE"), nullable=False, index=True)
    cron: Mapped[str] = mapped_column(String(64), nullable=False)
    action: Mapped[str] = mapped_column(String(16), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    next_run: Mapped[dt.datetime | None] = mapped_column(TS, index=True)


# --- records ----------------------------------------------------------------------


class WebhookEvent(Base):
    """Every provider event applied exactly once."""
    __tablename__ = "webhook_events"
    __table_args__ = (UniqueConstraint("provider", "event_id"),)
    id: Mapped[int] = _pk()
    provider: Mapped[str] = mapped_column(String(16), nullable=False)
    event_id: Mapped[str] = mapped_column(String(128), nullable=False)
    type: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    processed_at: Mapped[dt.datetime] = _created()


class Consent(Base):
    """Proof that a person accepted a document, at a version, for an order."""
    __tablename__ = "consents"
    id: Mapped[int] = _pk()
    user_id: Mapped[int] = mapped_column(
        BigID, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    order_id: Mapped[str | None] = mapped_column(String(64))
    document: Mapped[str] = mapped_column(String(16), nullable=False)
    version: Mapped[str] = mapped_column(String(32), nullable=False)
    accepted_at: Mapped[dt.datetime] = _created()
    ip: Mapped[str] = mapped_column(String(64), nullable=False, default="")


class JobRecord(Base):
    """History of the job queue (WP-04); the live state is in Redis."""
    __tablename__ = "jobs"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    progress: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    step: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    result: Mapped[dict | None] = mapped_column(JSONType)
    error: Mapped[str | None] = mapped_column(Text)
    server_id: Mapped[str | None] = mapped_column(String(64), index=True)
    created_by: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[dt.datetime] = _created()
    finished_at: Mapped[dt.datetime | None] = mapped_column(TS)


class AuditEntry(Base):
    """Everything anyone did. Admin writes carry before/after values."""
    __tablename__ = "audit_log"
    __table_args__ = (Index("ix_audit_log_at_id", "at", "id"),)
    id: Mapped[int] = _pk()
    at: Mapped[dt.datetime] = mapped_column(TS, nullable=False, default=now, index=True)
    actor: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    action: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    target: Mapped[str] = mapped_column(String(64), nullable=False, default="", index=True)
    ticket_id: Mapped[str | None] = mapped_column(String(32))
    detail: Mapped[str] = mapped_column(Text, nullable=False, default="")
    ip: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    ok: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    before: Mapped[dict | None] = mapped_column(JSONType)
    after: Mapped[dict | None] = mapped_column(JSONType)


# --- the fleet, as the panel keeps it today --------------------------------------


class Rack(Base):
    """A user-made group of servers on the Racks screen."""
    __tablename__ = "racks"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(48), nullable=False)
    motif: Mapped[str] = mapped_column(String(16), nullable=False, default="sakura")
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class RackMember(Base):
    """Which rack a server hangs on. A server is on at most one."""
    __tablename__ = "rack_members"
    server_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    rack_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("racks.id", ondelete="CASCADE"), nullable=False, index=True)


class ServerActivity(Base):
    """When each server was last seen up, as the fleet poll witnessed it."""
    __tablename__ = "server_activity"
    server_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    record: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)


class ModDecision(Base):
    """An operator's allow/block decision about a jar in the client-only review."""
    __tablename__ = "mod_decisions"
    id: Mapped[int] = _pk()
    key: Mapped[str] = mapped_column(String(200), nullable=False, default="", index=True)
    verdict: Mapped[str] = mapped_column(String(8), nullable=False)
    scope: Mapped[str] = mapped_column(String(64), nullable=False, default="global")
    name: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    example_file: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    mod_id: Mapped[str | None] = mapped_column(String(128))
    project_id: Mapped[str | None] = mapped_column(String(64))
    source: Mapped[str | None] = mapped_column(String(16))
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    evidence: Mapped[list] = mapped_column(JSONType, nullable=False, default=list)
    added_at: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)


class Meta(Base):
    """Small facts about the database itself, e.g. when the JSON import ran."""
    __tablename__ = "meta"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)


# --- storefront and support (section 5A) -------------------------------------------


class ContentItem(Base):
    """Storefront content: draft -> preview -> publish, versioned."""
    __tablename__ = "content_items"
    __table_args__ = (UniqueConstraint("kind", "slug", "locale"),)
    id: Mapped[int] = _pk()
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    slug: Mapped[str] = mapped_column(String(120), nullable=False)
    locale: Mapped[str] = mapped_column(String(16), nullable=False, default="en")
    draft: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    published: Mapped[dict | None] = mapped_column(JSONType)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    published_at: Mapped[dt.datetime | None] = mapped_column(TS)


class Coupon(Base):
    __tablename__ = "coupons"
    id: Mapped[int] = _pk()
    code: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False, default="percent")
    value: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    currency: Mapped[str | None] = mapped_column(String(3))
    max_redemptions: Mapped[int | None] = mapped_column(Integer)
    expires_at: Mapped[dt.datetime | None] = mapped_column(TS)
    stripe_promotion_code_id: Mapped[str | None] = mapped_column(String(64), unique=True)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class Bundle(Base):
    __tablename__ = "bundles"
    id: Mapped[int] = _pk()
    code: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    items: Mapped[list] = mapped_column(JSONType, nullable=False, default=list)
    discount: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    stripe_price_id: Mapped[str | None] = mapped_column(String(64), unique=True)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class Ticket(Base):
    __tablename__ = "tickets"
    id: Mapped[int] = _pk()
    user_id: Mapped[int] = mapped_column(
        BigID, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    server_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("servers.uuid", ondelete="SET NULL"))
    category: Mapped[str] = mapped_column(String(24), nullable=False, default="setup")
    subject: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="open")
    priority: Mapped[str] = mapped_column(String(8), nullable=False, default="normal")
    sla_paused: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[dt.datetime] = _created()
    closed_at: Mapped[dt.datetime | None] = mapped_column(TS)


class TicketMessage(Base):
    __tablename__ = "ticket_messages"
    id: Mapped[int] = _pk()
    ticket_id: Mapped[int] = mapped_column(
        BigID, ForeignKey("tickets.id", ondelete="CASCADE"), nullable=False, index=True)
    author_id: Mapped[int | None] = mapped_column(
        BigID, ForeignKey("users.id", ondelete="SET NULL"))
    body: Mapped[str] = mapped_column(Text, nullable=False)
    internal: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[dt.datetime] = _created()


class AccessGrant(Base):
    """A customer letting staff into one server, for one ticket, until it closes."""
    __tablename__ = "access_grants"
    id: Mapped[int] = _pk()
    ticket_id: Mapped[int] = mapped_column(
        BigID, ForeignKey("tickets.id", ondelete="CASCADE"), nullable=False, index=True)
    server_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("servers.uuid", ondelete="CASCADE"), nullable=False)
    scopes: Mapped[list] = mapped_column(JSONType, nullable=False, default=list)
    granted_by: Mapped[int | None] = mapped_column(
        BigID, ForeignKey("users.id", ondelete="SET NULL"))
    expires_at: Mapped[dt.datetime | None] = mapped_column(TS)
    revoked_at: Mapped[dt.datetime | None] = mapped_column(TS)


class Notice(Base):
    """A DSA notice-and-action report, and what was decided."""
    __tablename__ = "notices"
    id: Mapped[int] = _pk()
    reporter: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    target: Mapped[str] = mapped_column(String(200), nullable=False)
    category: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    body: Mapped[str] = mapped_column(Text, nullable=False, default="")
    decision: Mapped[str | None] = mapped_column(String(32))
    statement_of_reasons: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = _created()
    decided_at: Mapped[dt.datetime | None] = mapped_column(TS)


class FeatureFlag(Base):
    __tablename__ = "feature_flags"
    __table_args__ = (UniqueConstraint("plan_id", "flag"),)
    id: Mapped[int] = _pk()
    plan_id: Mapped[int] = mapped_column(
        BigID, ForeignKey("plans.id", ondelete="CASCADE"), nullable=False)
    flag: Mapped[str] = mapped_column(String(48), nullable=False)
    value: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)


class Announcement(Base):
    __tablename__ = "announcements"
    id: Mapped[int] = _pk()
    scope: Mapped[str] = mapped_column(String(8), nullable=False, default="panel")
    text: Mapped[str] = mapped_column(Text, nullable=False)
    starts_at: Mapped[dt.datetime | None] = mapped_column(TS)
    ends_at: Mapped[dt.datetime | None] = mapped_column(TS)
