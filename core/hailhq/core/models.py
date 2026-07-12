import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    ARRAY,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    Text,
    UniqueConstraint,
    func,
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


class User(Base):
    """Better-auth's users table (owned by hail-website). Mapped read-mostly;
    the ONLY column this codebase writes is phone_number."""

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    email: Mapped[str] = mapped_column(Text, nullable=False)
    phone_number: Mapped[str | None] = mapped_column(Text, nullable=True)


class Contact(Base):
    """Manual org contact — phone and/or email (CHECK enforces at least one).

    organization_id carries no FK: organizations lives in the website DB (see
    the module docstring convention on OrganizationMember / migration 0001).
    """

    __tablename__ = "contacts"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    phone_e164: Mapped[str | None] = mapped_column(Text, nullable=True)
    email: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        TS, server_default=text("now()"), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        TS, server_default=text("now()"), onupdate=func.now(), nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            "phone_e164 IS NOT NULL OR email IS NOT NULL",
            name="contacts_phone_or_email",
        ),
        Index(
            "contacts_org_phone_key",
            "organization_id",
            "phone_e164",
            unique=True,
            postgresql_where=text("phone_e164 IS NOT NULL"),
        ),
        Index(
            "contacts_org_email_key",
            "organization_id",
            "email",
            unique=True,
            postgresql_where=text("email IS NOT NULL"),
        ),
        Index("contacts_org_idx", "organization_id"),
    )


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
    amount_cents: Mapped[Decimal] = mapped_column(Numeric(14, 1), nullable=False)
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


class Suppression(Base):
    """Do-not-contact entry — org-scoped, or global when ``organization_id``
    is NULL.

    Backs the pre-send compliance gate (``hailhq.core.compliance_gate``):
    a send is blocked when a row matches ``(recipient, channel)`` or
    ``(recipient, 'all')``, scoped to the sending org OR a NULL (platform-
    wide) row. ``recipient`` is normalized — E.164 for voice, lowercased
    for email — so lookups are a plain equality match.

    A voice row IS an internal DNC entry; there is no separate DNC table.
    Populated by the unsubscribe link (``GET /unsubscribe``,
    ``source='unsubscribe_link'``), manual ops action
    (``source='manual'``), or a future bounce/complaint handler
    (``source='bounce'``).
    """

    __tablename__ = "suppressions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    organization_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    recipient: Mapped[str] = mapped_column(Text, nullable=False)
    channel: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TS, server_default=text("now()"), nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            "channel IN ('voice','email','sms','all')",
            name="suppressions_channel_check",
        ),
        Index("suppressions_recipient_channel_idx", "recipient", "channel"),
    )


class OrgClosure(Base):
    """Local record that an org's account was closed/deleted on hail-website.

    hail's own DB does not own account/org lifecycle — organizations live in
    hail-website's separate Postgres (better-auth schema), cross-referenced
    only by a bare ``organization_id`` with no FK (same posture as
    ``OrganizationMember``/``ApiKey`` above). Without this table hail has no
    way to tell whether/when an org closed, so it can't enforce the
    retention policy (account duration + 12 months) on its own.

    Populated by ``POST /internal/org-closures``, which hail-website calls
    when it closes/deletes an account (see
    ``api/hailhq/api/routes/internal/org_closures.py``). Read by
    ``hailhq.core.retention.purge_expired_data``.
    """

    __tablename__ = "org_closures"

    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True
    )
    closed_at: Mapped[datetime] = mapped_column(TS, nullable=False)
    # Free-form provenance of the closure notification, e.g. "hail_website".
    source: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TS, server_default=text("now()"), nullable=False
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


class Sms(Base):
    __tablename__ = "sms"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
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
    body: Mapped[str] = mapped_column(Text, nullable=False)
    provider: Mapped[str] = mapped_column(Text, server_default="twilio", nullable=False)
    provider_message_sid: Mapped[str | None] = mapped_column(
        Text, unique=True, nullable=True
    )
    segment_count: Mapped[int] = mapped_column(
        Integer, server_default="1", nullable=False
    )
    error_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    requested_at: Mapped[datetime] = mapped_column(
        TS, server_default=text("now()"), nullable=False
    )
    sent_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    metadata_: Mapped[dict] = mapped_column(
        "metadata", JSONB, server_default=text("'{}'::jsonb"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        TS, server_default=text("now()"), nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            "direction IN ('outbound','inbound')",
            name="sms_direction_check",
        ),
        CheckConstraint(
            "status IN ('queued','sent','delivered','failed','undelivered','received')",
            name="sms_status_check",
        ),
    )


class SmsEvent(Base):
    """Append-only SMS lifecycle event (mirrors CallEvent/EmailEvent).

    ``organization_id`` is denormalized from the parent sms row so the
    org-wide ``GET /events`` stream filters without a join. The
    (sms_id, kind, occurred_at) unique constraint is sized for the
    planned Twilio status-callback ingest (at-least-once redelivery,
    same absorption strategy as ``EmailEvent``).
    """

    __tablename__ = "sms_events"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    sms_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sms.id", ondelete="CASCADE"), nullable=False
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(
        TS, server_default=text("now()"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        TS, server_default=text("now()"), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("sms_id", "kind", "occurred_at", name="sms_events_dedup_uq"),
        Index("sms_events_sms_occurred_idx", "sms_id", "occurred_at"),
        Index(
            "sms_events_org_occurred_kind_idx",
            "organization_id",
            "occurred_at",
            "kind",
        ),
    )


class EmailEvent(Base):
    """Append-only email lifecycle event (mirrors CallEvent).

    ``organization_id`` is denormalized from the parent email so stats
    queries aggregate without a join. The (email_id, kind, occurred_at)
    unique constraint absorbs SNS at-least-once redelivery — inserts use
    ON CONFLICT DO NOTHING and skip fanout when nothing was inserted.
    """

    __tablename__ = "email_events"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    email_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("emails.id", ondelete="CASCADE"), nullable=False
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(TS, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TS, server_default=text("now()"), nullable=False
    )

    __table_args__ = (
        # Tradeoff: two distinct events sharing (email_id, kind, occurred_at)
        # — e.g. two clicks in the same millisecond — collapse to one row.
        # Accepted because absorbing SNS at-least-once redelivery matters more.
        UniqueConstraint(
            "email_id", "kind", "occurred_at", name="email_events_dedup_uq"
        ),
        Index("email_events_email_occurred_idx", "email_id", "occurred_at"),
        Index(
            "email_events_org_occurred_kind_idx",
            "organization_id",
            "occurred_at",
            "kind",
        ),
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


class EmailDomain(Base):
    """A sending-domain identity registered with the email provider.

    Two flavors of row:

    * ``kind = 'hail_mail'`` — a per-org sending address of the form
      ``<local_prefix_user>+<local_prefix_org>@<HAIL_MAIL_BASE_DOMAIN>``
      (e.g. ``alice+acme@mail.hail.so``). The two prefix columns are the
      source of truth; ``domain`` is the computed full address kept in
      sync at write time. The parent base domain is pre-verified by the
      operator out-of-band, so these rows land at ``verified`` immediately
      and ``dns_records`` stays empty.
    * ``kind = 'custom'`` — a tenant-controlled bare DNS name (e.g.
      ``acme.com``). The prefix columns are both NULL.
      ``verification_status`` starts at ``pending`` and ``dns_records``
      carries the three CNAMEs the tenant must publish before SES will
      flip the identity to ``verified``.

    Dual-purpose ``domain`` column (intentional for v1):

    * For ``custom`` rows it's the bare DNS identity SES verifies.
    * For ``hail_mail`` rows it's the full ``<user>+<org>@<base>``
      address used as the wire ``From:``.

    A future refactor could split into ``dns_domain`` (always the
    registrable parent) + an optional ``local_part`` so the columns
    don't mean different things per ``kind``. For v1 the denormalized
    materialization is the right trade — it keeps the unique constraint
    ``(organization_id, domain)`` working for both flavors with one
    index and lets the API serialize the visible address from a single
    column.
    """

    __tablename__ = "email_domains"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    domain: Mapped[str] = mapped_column(Text, nullable=False)
    # Both NULL for kind='custom'; both required for kind='hail_mail'. The
    # full sending address (`domain`) is computed from these at write time
    # and kept in sync — these two are the source of truth, `domain` is the
    # convenience materialization other callers can read without re-parsing.
    local_prefix_user: Mapped[str | None] = mapped_column(Text, nullable=True)
    local_prefix_org: Mapped[str | None] = mapped_column(Text, nullable=True)
    verification_status: Mapped[str] = mapped_column(
        Text, server_default="pending", nullable=False
    )
    # JSON array of {name, value, type, priority} entries (DKIM CNAMEs + the
    # custom MAIL FROM MX/SPF) — surfaced in the response so the tenant can
    # paste them straight into their DNS console.
    dns_records: Mapped[list[dict]] = mapped_column(
        "dns_records",
        JSONB,
        server_default=text("'[]'::jsonb"),
        nullable=False,
    )
    mail_from_domain: Mapped[str | None] = mapped_column(Text, nullable=True)
    # SES MAIL FROM verification status (pending/verified/failed); NULL until a
    # custom MAIL FROM is configured. Secondary to verification_status.
    mail_from_status: Mapped[str | None] = mapped_column(Text, nullable=True)
    provider: Mapped[str] = mapped_column(Text, server_default="ses", nullable=False)
    provider_resource_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    verified_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    inbound_enabled: Mapped[bool] = mapped_column(
        Boolean, server_default=text("false"), nullable=False
    )
    forward_to: Mapped[list[str] | None] = mapped_column(ARRAY(Text), nullable=True)
    forward_rate_per_hour: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TS, server_default=text("now()"), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        TS, server_default=text("now()"), nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            "kind IN ('hail_mail','custom')",
            name="email_domains_kind_check",
        ),
        CheckConstraint(
            "verification_status IN ('pending','verified','failed')",
            name="email_domains_verification_status_check",
        ),
        # Hail-mail rows need both prefixes; custom rows leave both NULL.
        # Mirrors the migration so Base.metadata.create_all (tests) produces
        # the same shape.
        CheckConstraint(
            "(kind = 'hail_mail' AND local_prefix_user IS NOT NULL "
            "AND local_prefix_org IS NOT NULL) "
            "OR (kind = 'custom' AND local_prefix_user IS NULL "
            "AND local_prefix_org IS NULL)",
            name="email_domains_prefix_kind_consistency",
        ),
        # An org can't register the same domain twice.  Custom domains are
        # globally unique (one org per domain) — see the partial index below.
        # hail_mail rows are org-scoped by this constraint only (their global
        # uniqueness is enforced on the prefix pair, not the domain column).
        UniqueConstraint(
            "organization_id", "domain", name="email_domains_org_domain_unique"
        ),
        # Custom sender domains must be globally unique across all orgs:
        # Hail uses a single shared SES account, so a second org claiming
        # the same domain could ride the real owner's SES verification and
        # (with inbound enabled) intercept their mail.
        Index(
            "email_domains_custom_domain_global_uq",
            "domain",
            unique=True,
            postgresql_where=text("kind = 'custom'"),
        ),
        # Hail-mail addresses route inbound mail by (user, org) prefix with
        # no org scoping at lookup time (email_ingest matches the prefix
        # pair, not the domain column) — the pair must be globally unique or
        # org B could register org A's prefixes and intercept mail, even if
        # HAIL_MAIL_BASE_DOMAIN changed between the two registrations.
        Index(
            "email_domains_hail_mail_prefix_uq",
            "local_prefix_user",
            "local_prefix_org",
            unique=True,
            postgresql_where=text("kind = 'hail_mail'"),
        ),
    )


class Email(Base):
    """A single email message, outbound or inbound.

    Outbound mirrors the ``Call`` shape: requested → sent / failed /
    bounced / complained, with provider_message_id surfaced for
    downstream correlation (SES delivery webhooks land here in v1.5).
    Inbound rows carry ``direction='inbound'`` and status ``received``.
    """

    __tablename__ = "emails"

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
    email_domain_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("email_domains.id", ondelete="RESTRICT"),
        nullable=True,
    )
    from_address: Mapped[str] = mapped_column(Text, nullable=False)
    to_addresses: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False)
    cc_addresses: Mapped[list[str] | None] = mapped_column(ARRAY(Text), nullable=True)
    bcc_addresses: Mapped[list[str] | None] = mapped_column(ARRAY(Text), nullable=True)
    reply_to: Mapped[str | None] = mapped_column(Text, nullable=True)
    subject: Mapped[str] = mapped_column(Text, nullable=False)
    body_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    body_html: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(Text, server_default="queued", nullable=False)
    # Populated on terminal rows. For send failures we store a short opaque
    # provider-error code (e.g. SES ``MessageRejected``); free-form because
    # the SES surface is wider than calls and we don't want a v2 migration
    # every time AWS adds a new error.
    end_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    provider: Mapped[str] = mapped_column(Text, server_default="ses", nullable=False)
    provider_message_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    requested_at: Mapped[datetime] = mapped_column(
        TS, server_default=text("now()"), nullable=False
    )
    sent_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    failed_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    metadata_: Mapped[dict] = mapped_column(
        "metadata", JSONB, server_default=text("'{}'::jsonb"), nullable=False
    )
    direction: Mapped[str] = mapped_column(
        Text, server_default="outbound", nullable=False
    )
    provider_received_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    message_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    in_reply_to: Mapped[str | None] = mapped_column(Text, nullable=True)
    references_ids: Mapped[list[str] | None] = mapped_column(ARRAY(Text), nullable=True)
    raw_s3_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    spam_verdict: Mapped[str | None] = mapped_column(Text, nullable=True)
    virus_verdict: Mapped[str | None] = mapped_column(Text, nullable=True)
    dkim_verdict: Mapped[str | None] = mapped_column(Text, nullable=True)
    spf_verdict: Mapped[str | None] = mapped_column(Text, nullable=True)
    dmarc_verdict: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Denormalised from email_domains.kind so the inbound dedup indexes can
    # branch on it without a join. NULL for outbound rows (kind is not
    # meaningful there) and for legacy inbound rows created before this column.
    email_domain_kind: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TS, server_default=text("now()"), nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('queued','sent','delivered','failed','bounced',"
            "'complained','received')",
            name="emails_status_check",
        ),
        CheckConstraint(
            "array_length(to_addresses, 1) >= 1",
            name="emails_to_addresses_nonempty",
        ),
        CheckConstraint(
            "body_text IS NOT NULL OR body_html IS NOT NULL",
            name="emails_body_required",
        ),
        CheckConstraint(
            "direction IN ('outbound','inbound')",
            name="emails_direction_check",
        ),
        CheckConstraint(
            "direction = 'inbound' OR email_domain_id IS NOT NULL",
            name="emails_outbound_has_domain",
        ),
        Index(
            "emails_provider_message_id_outbound_uq",
            "provider_message_id",
            unique=True,
            postgresql_where=text(
                "direction = 'outbound' AND provider_message_id IS NOT NULL"
            ),
        ),
        # Inbound dedup — split by email_domain_kind so hail_mail deduplicates
        # per-org (one row regardless of how many recipients share the same
        # org) while custom deduplicates per-domain (one row per receiving
        # domain, even within the same org).
        Index(
            "emails_hailmail_inbound_message_id_uq",
            "organization_id",
            "message_id",
            unique=True,
            postgresql_where=text(
                "direction = 'inbound' AND message_id IS NOT NULL"
                " AND email_domain_kind = 'hail_mail'"
            ),
        ),
        Index(
            "emails_custom_inbound_message_id_uq",
            "email_domain_id",
            "message_id",
            unique=True,
            postgresql_where=text(
                "direction = 'inbound' AND message_id IS NOT NULL"
                " AND email_domain_kind = 'custom'"
            ),
        ),
        # Provider-message-id fallback for mail without a Message-ID header.
        Index(
            "emails_hailmail_inbound_pmid_uq",
            "organization_id",
            "provider_message_id",
            unique=True,
            postgresql_where=text(
                "direction = 'inbound' AND provider_message_id IS NOT NULL"
                " AND email_domain_kind = 'hail_mail'"
            ),
        ),
        Index(
            "emails_custom_inbound_pmid_uq",
            "email_domain_id",
            "provider_message_id",
            unique=True,
            postgresql_where=text(
                "direction = 'inbound' AND provider_message_id IS NOT NULL"
                " AND email_domain_kind = 'custom'"
            ),
        ),
        Index(
            "emails_org_direction_created_idx",
            "organization_id",
            "direction",
            text("created_at DESC"),
        ),
        Index("emails_message_id_idx", "message_id"),
        # The forward worker polls this every second; keep it index-only.
        # Direct-send rows pass through 'queued' only momentarily, so the
        # partial index stays tiny.
        Index(
            "emails_forward_queue_idx",
            "created_at",
            postgresql_where=text("status = 'queued' AND direction = 'outbound'"),
        ),
    )


class EmailAttachment(Base):
    __tablename__ = "email_attachments"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    email_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("emails.id", ondelete="CASCADE"),
        nullable=False,
    )
    filename: Mapped[str] = mapped_column(Text, nullable=False)
    content_type: Mapped[str] = mapped_column(Text, nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    s3_key: Mapped[str] = mapped_column(Text, nullable=False)
    content_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TS, server_default=text("now()"), nullable=False
    )


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


class WebhookSubscription(Base):
    """Org-wide outbound webhook subscription.

    Mirrors Stripe's surface: tenants register a target_url + event_types
    + a generated secret (Fernet-encrypted at rest, see
    ``hailhq.core.secret_cipher``), and the delivery worker POSTs
    matching events with an HMAC signature header. ``consecutive_failures``
    auto-disables the subscription after 50; an admin re-enables via PATCH.
    """

    __tablename__ = "webhook_subscriptions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )
    target_url: Mapped[str] = mapped_column(Text, nullable=False)
    secret_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    event_types: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False)
    status: Mapped[str] = mapped_column(Text, server_default="active", nullable=False)
    consecutive_failures: Mapped[int] = mapped_column(
        Integer, server_default="0", nullable=False
    )
    last_success_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    last_failure_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TS, server_default=text("now()"), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        TS, server_default=text("now()"), nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('active','disabled')",
            name="webhook_subscriptions_status_check",
        ),
        CheckConstraint(
            "cardinality(event_types) >= 1",
            name="webhook_subscriptions_event_types_nonempty",
        ),
    )


class WebhookDelivery(Base):
    """Per-attempt audit + retry queue for outbound webhook events.

    A delivery is owned by an org-wide ``WebhookSubscription``;
    ``email_domain_id`` records the informational source domain of the
    event (surfaced as the ``X-Hail-Email-Domain`` header), not a routing
    target. The background worker polls ``status='pending' AND
    next_attempt_at <= now()`` with SKIP LOCKED, POSTs, and updates the row.
    """

    __tablename__ = "webhook_deliveries"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    subscription_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("webhook_subscriptions.id", ondelete="CASCADE"),
        nullable=False,
    )
    email_domain_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("email_domains.id", ondelete="SET NULL"),
        nullable=True,
    )
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    event_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, server_default="0", nullable=False)
    status: Mapped[str] = mapped_column(Text, server_default="pending", nullable=False)
    response_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    response_body: Mapped[str | None] = mapped_column(Text, nullable=True)
    next_attempt_at: Mapped[datetime] = mapped_column(
        TS, server_default=text("now()"), nullable=False
    )
    succeeded_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TS, server_default=text("now()"), nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('pending','succeeded','failed','dead')",
            name="webhook_deliveries_status_check",
        ),
    )


class OrgProviderConfig(Base):
    """Per-org BYO provider config for a voice-pipeline layer.

    Cloud-console feature (managed via ``routes/internal/provider_config``,
    never the public API). An org may hold multiple saved configs per layer
    (one per provider — unique on org+layer+provider), with at most one
    marked ``is_active`` per (org, layer); the voicebot uses the active row.
    ``encrypted_api_key`` is Fernet ciphertext under
    ``HAIL_PROVIDER_SECRET_KEY`` (see ``hailhq.core.secret_cipher``) — same
    at-rest posture as ``WebhookSubscription.secret_encrypted``. A row with
    ``encrypted_api_key IS NULL`` is a params-only override (e.g. a custom
    voice_id spoken through Hail's own TTS key). ``params`` shape is
    validated per layer by ``hailhq.core.provider_config``.
    """

    __tablename__ = "org_provider_config"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )
    layer: Mapped[str] = mapped_column(Text, nullable=False)
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    encrypted_api_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    key_last4: Mapped[str | None] = mapped_column(Text, nullable=True)
    key_set_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    params: Mapped[dict] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb"), nullable=False
    )
    fallback_enabled: Mapped[bool] = mapped_column(
        Boolean, server_default=text("false"), nullable=False
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, server_default=text("false"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        TS, server_default=text("now()"), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        TS, server_default=text("now()"), onupdate=func.now(), nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            "layer IN ('llm','tts','stt')", name="org_provider_config_layer_check"
        ),
        UniqueConstraint(
            "organization_id",
            "layer",
            "provider",
            name="org_provider_config_org_layer_provider_key",
        ),
        Index(
            "org_provider_config_one_active_idx",
            "organization_id",
            "layer",
            unique=True,
            postgresql_where=text("is_active"),
        ),
        Index("org_provider_config_org_idx", "organization_id"),
    )
