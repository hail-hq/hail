import uuid
from datetime import datetime

from sqlalchemy import (
    ARRAY,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import INET, JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from hailhq.core.call_end_reasons import CallEndReasonDB
from hailhq.core.schemas import TERMINAL_CALL_STATUSES


class Base(DeclarativeBase):
    pass


TS = DateTime(timezone=True)


# The website owns `organizations`, `members`, `api_keys`. hail/api only reads
# from them. Cross-history columns (organization_id, api_key_id) carry no FK
# so the two migration tools don't need to coordinate.


class OrganizationMember(Base):
    """Read-only mirror of the website's ``members`` table.

    Used by deps.py to map ``api_keys.reference_id`` → ``organization_id``.
    """

    __tablename__ = "members"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, index=True
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, index=True
    )
    role: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(TS, nullable=False)


class AccountCredit(Base):
    """Append-only ledger; balance = SUM(amount_cents) per org.

    Aggregate per-batch for high-volume channels: one row per email blast
    (qty=N), not per send, otherwise the table grows linearly with traffic.
    """

    __tablename__ = "account_credits"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    channel: Mapped[str] = mapped_column(Text, nullable=False)
    amount_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    qty: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TS, server_default=text("now()"), nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            "kind IN ('credit','debit')",
            name="account_credits_kind_check",
        ),
        CheckConstraint(
            "channel IN ('voice','sms','email','credit')",
            name="account_credits_channel_check",
        ),
        CheckConstraint(
            "(kind = 'credit' AND amount_cents > 0) OR "
            "(kind = 'debit' AND amount_cents < 0)",
            name="account_credits_amount_sign_check",
        ),
    )


class UsageEvent(Base):
    """Raw, channel-typed unit counts written by voicebot / SMS / email senders.

    No money math at this layer — units are bare consumption counts:
      * voice: ``units`` = ``duration_ms``
      * sms:   ``units`` = segment count
      * email: ``units`` = recipient count

    The website's private rater reads ``WHERE priced_at IS NULL``, applies
    its private cents-per-unit rates, writes the matching dollar debit row
    to ``account_credits``, then stamps ``priced_at`` here. Self-host
    operators never run the rater — this table accumulates as a raw
    analytics primitive they can query directly.
    """

    __tablename__ = "usage_events"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )
    channel: Mapped[str] = mapped_column(Text, nullable=False)
    units: Mapped[int] = mapped_column(Integer, nullable=False)
    ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(
        TS, server_default=text("now()"), nullable=False
    )
    priced_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)

    __table_args__ = (
        CheckConstraint(
            "channel IN ('voice','sms','email')",
            name="usage_events_channel_check",
        ),
        CheckConstraint(
            "units >= 0",
            name="usage_events_units_nonneg_check",
        ),
    )


class ApiKey(Base):
    """Read-only mirror of the auth backend's ``api_keys`` table.

    The table is owned and migrated by the website's auth backend; hail/api
    only reads from it during request authentication. Most fields are nullable
    in the DB because the backend fills defaults at write-time rather than via
    DB defaults — so a NULL ``enabled`` means "use the backend default (true)",
    not "disabled".
    """

    __tablename__ = "api_keys"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    name: Mapped[str | None] = mapped_column(Text, nullable=True)
    start: Mapped[str | None] = mapped_column(Text, nullable=True)
    reference_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    prefix: Mapped[str | None] = mapped_column(Text, nullable=True)
    key: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    enabled: Mapped[bool | None] = mapped_column(nullable=True)
    request_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_request: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    created_at: Mapped[datetime] = mapped_column(TS, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(TS, nullable=False)
    permissions: Mapped[str | None] = mapped_column(Text, nullable=True)


class PhoneNumber(Base):
    __tablename__ = "phone_numbers"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    # Nullable: pool numbers (is_pool=TRUE) have no owner.
    organization_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    e164: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    country_code: Mapped[str] = mapped_column(Text, nullable=False)
    number_type: Mapped[str] = mapped_column(Text, nullable=False)
    capabilities: Mapped[list[str]] = mapped_column(
        ARRAY(Text), server_default=text("ARRAY['voice','sms']"), nullable=False
    )
    provider: Mapped[str] = mapped_column(Text, server_default="twilio", nullable=False)
    provider_resource_id: Mapped[str] = mapped_column(Text, nullable=False)
    provisioning_state: Mapped[str] = mapped_column(
        Text, server_default="pending", nullable=False
    )
    provisioning_metadata: Mapped[dict] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb"), nullable=False
    )
    acquired_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    released_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TS, server_default=text("now()"), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        TS, server_default=text("now()"), nullable=False
    )
    # Pool fields — TRUE iff this number is shared across orgs; reserved_call_id
    # is the single source of truth for "currently in use".
    is_pool: Mapped[bool] = mapped_column(
        Boolean, server_default=text("FALSE"), nullable=False
    )
    # use_alter=True so the circular FK (calls.from_number_id ↔
    # phone_numbers.reserved_call_id) can be created/dropped via ALTER TABLE
    # rather than inline — required for Base.metadata.create_all/drop_all in
    # tests, and harmless in production where the migration creates the FK
    # explicitly.
    reserved_call_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "calls.id",
            ondelete="SET NULL",
            use_alter=True,
            name="phone_numbers_reserved_call_id_fkey",
        ),
        nullable=True,
    )

    __table_args__ = (
        CheckConstraint(
            "number_type IN ('local','mobile','toll_free')",
            name="phone_numbers_number_type_check",
        ),
        CheckConstraint(
            "provisioning_state IN ('pending','active','failed','released')",
            name="phone_numbers_state_check",
        ),
        CheckConstraint(
            "(is_pool = TRUE AND organization_id IS NULL)"
            " OR (is_pool = FALSE AND organization_id IS NOT NULL)",
            name="phone_numbers_pool_owner_xor",
        ),
    )


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )
    external_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    metadata_: Mapped[dict] = mapped_column(
        "metadata", JSONB, server_default=text("'{}'::jsonb"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        TS, server_default=text("now()"), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        TS, server_default=text("now()"), nullable=False
    )


class Call(Base):
    __tablename__ = "calls"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("conversations.id", ondelete="SET NULL"),
        nullable=True,
    )
    from_number_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("phone_numbers.id"), nullable=False
    )
    from_e164: Mapped[str] = mapped_column(Text, nullable=False)
    to_e164: Mapped[str] = mapped_column(Text, nullable=False)
    direction: Mapped[str] = mapped_column(
        Text, server_default="outbound", nullable=False
    )
    status: Mapped[str] = mapped_column(Text, server_default="queued", nullable=False)
    # Backed by the `call_end_reason` Postgres ENUM. See
    # hailhq.core.call_end_reasons for the canonical value list. Stays
    # nullable because non-terminal rows have no reason yet; the DB CHECK
    # added in migration 0003 enforces non-null for terminal statuses.
    end_reason: Mapped[str | None] = mapped_column(CallEndReasonDB, nullable=True)
    provider: Mapped[str] = mapped_column(Text, server_default="twilio", nullable=False)
    provider_call_sid: Mapped[str | None] = mapped_column(
        Text, unique=True, nullable=True
    )
    livekit_room: Mapped[str | None] = mapped_column(Text, nullable=True)
    voice_config: Mapped[dict] = mapped_column(JSONB, nullable=False)
    # Snapshot of the effective max-call-duration at insert time. Pool-number
    # sweeper backstop uses this (now() > requested_at + max_duration + grace);
    # snapshotting prevents a runtime config tweak from retroactively shortening
    # a live call's reservation window. Nullable for back-compat with pre-0002 rows.
    max_duration_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    initial_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    transcript: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    recording_s3_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    recording_duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cost_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)
    requested_at: Mapped[datetime] = mapped_column(
        TS, server_default=text("now()"), nullable=False
    )
    started_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    answered_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    metadata_: Mapped[dict] = mapped_column(
        "metadata", JSONB, server_default=text("'{}'::jsonb"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        TS, server_default=text("now()"), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        TS, server_default=text("now()"), nullable=False
    )

    __table_args__ = (
        # Mirrors migration 0003 — every terminal row must carry an
        # end_reason. Defined here too so Base.metadata.create_all (tests)
        # produces the same shape as the alembic-managed schema.
        CheckConstraint(
            "status NOT IN ("
            + ",".join(f"'{s}'" for s in sorted(TERMINAL_CALL_STATUSES))
            + ") OR end_reason IS NOT NULL",
            name="calls_end_reason_when_terminal",
        ),
    )


class CallEvent(Base):
    __tablename__ = "call_events"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    call_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("calls.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(
        TS, server_default=text("now()"), nullable=False
    )


class IdempotencyKey(Base):
    __tablename__ = "idempotency_keys"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )
    request_hash: Mapped[str] = mapped_column(Text, nullable=False)
    response_status: Mapped[int] = mapped_column(Integer, nullable=False)
    response_body: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TS, server_default=text("now()"), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(TS, nullable=False)


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )
    # ``api_keys.id`` of the caller, or NULL for self-host ``HAIL_API_KEY``
    # requests (no row in ``api_keys``). No FK; ``api_keys`` is owned by the
    # website's auth backend.
    api_key_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    action: Mapped[str] = mapped_column(Text, nullable=False)
    resource_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    resource_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    ip_address: Mapped[str | None] = mapped_column(INET, nullable=True)
    user_agent: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(
        TS, server_default=text("now()"), nullable=False
    )
