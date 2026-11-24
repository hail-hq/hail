"""Add ``sip_connections`` and the ``pbx`` number, leg and extension columns.

A customer connects their phone system (PBX) to Hail. Hail keeps the
connection (credentials, where to dial it), links each connected number to
it, records the legs as ``pbx`` legs, and lets a contact be an extension.

Tables checked first:
- ``org_provider_config``: BYO keys for ``llm``/``tts``/``stt`` only, one
  row per org, layer and provider. The voicebot loads every row as
  pipeline settings, so a PBX credential there would be read as one.
- ``phone_numbers``: numbers, not credentials. It only gets the link
  (``sip_connection_id``) to the new table.
Neither fits a PBX connection, so a new table.

Other changes:
- ``phone_numbers``: state ``unverified`` (a connected number before its
  ownership check; never ``pending``, which the order reconciler reads),
  ``sip_connection_id`` (FK, ``ON DELETE RESTRICT``), and a CHECK that
  ``provider = 'pbx'`` holds exactly when it is set.
- ``call_legs``: provider CHECK allows ``pbx``.
- ``contacts``: ``extension`` (digits, ``*``, ``#``, 1 to 15), and the
  "at least one way to reach" CHECK now counts it.

Downgrade reverses each change. It fails while ``pbx`` numbers, ``pbx``
legs, ``unverified`` numbers or extension-only contacts exist: the
restored CHECKs reject them. Remove those rows first.

Revision ID: 0092
Revises: 0091
"""

from __future__ import annotations

from alembic import op

revision: str = "0092"
down_revision: str | None = "0091"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE sip_connections (
          id                         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          organization_id            UUID NOT NULL,
          label                      TEXT NOT NULL,
          inbound_username           TEXT NOT NULL,
          encrypted_inbound_password TEXT,
          pbx_host                   TEXT,
          pbx_port                   INTEGER NOT NULL DEFAULT 5060,
          pbx_transport              TEXT NOT NULL DEFAULT 'auto',
          pbx_username               TEXT,
          encrypted_pbx_password     TEXT,
          default_region             TEXT NOT NULL,
          livekit_inbound_trunk_id   TEXT,
          livekit_outbound_trunk_id  TEXT,
          created_at                 TIMESTAMPTZ NOT NULL DEFAULT now(),
          updated_at                 TIMESTAMPTZ NOT NULL DEFAULT now(),
          deleted_at                 TIMESTAMPTZ,
          CONSTRAINT sip_connections_label_len
            CHECK (char_length(label) BETWEEN 1 AND 80),
          CONSTRAINT sip_connections_transport_check
            CHECK (pbx_transport IN ('auto','udp','tcp','tls')),
          CONSTRAINT sip_connections_port_check
            CHECK (pbx_port BETWEEN 1 AND 65535),
          CONSTRAINT sip_connections_inbound_username_key UNIQUE (inbound_username)
        )
        """)
    op.execute(
        "CREATE INDEX sip_connections_org_idx ON sip_connections (organization_id)"
        " WHERE deleted_at IS NULL"
    )

    op.execute(
        "ALTER TABLE phone_numbers ADD COLUMN sip_connection_id UUID"
        " REFERENCES sip_connections(id) ON DELETE RESTRICT"
    )
    # 0001 declared the state CHECK inline, so Postgres named it
    # phone_numbers_provisioning_state_check; models name it
    # phone_numbers_state_check. Drop whichever this database has.
    op.execute(
        "ALTER TABLE phone_numbers"
        " DROP CONSTRAINT IF EXISTS phone_numbers_provisioning_state_check"
    )
    op.execute(
        "ALTER TABLE phone_numbers DROP CONSTRAINT IF EXISTS phone_numbers_state_check"
    )
    op.execute(
        "ALTER TABLE phone_numbers ADD CONSTRAINT phone_numbers_state_check CHECK ("
        "provisioning_state IN ('pending','active','failed','released','unverified'))"
    )
    op.execute(
        "ALTER TABLE phone_numbers ADD CONSTRAINT phone_numbers_pbx_connection_check"
        " CHECK ((provider = 'pbx') = (sip_connection_id IS NOT NULL))"
    )
    op.execute(
        "CREATE INDEX phone_numbers_sip_connection_idx ON phone_numbers"
        " (sip_connection_id) WHERE sip_connection_id IS NOT NULL"
    )

    op.execute("ALTER TABLE call_legs DROP CONSTRAINT call_legs_provider_check")
    op.execute(
        "ALTER TABLE call_legs ADD CONSTRAINT call_legs_provider_check"
        " CHECK (provider IN ('twilio','telnyx','didww','pbx'))"
    )

    op.execute("ALTER TABLE contacts ADD COLUMN extension TEXT")
    op.execute("ALTER TABLE contacts DROP CONSTRAINT contacts_phone_or_email")
    op.execute(
        "ALTER TABLE contacts ADD CONSTRAINT contacts_phone_email_or_extension"
        " CHECK (phone_e164 IS NOT NULL OR email IS NOT NULL"
        " OR extension IS NOT NULL)"
    )
    op.execute(
        "ALTER TABLE contacts ADD CONSTRAINT contacts_extension_format"
        " CHECK (extension ~ '^[0-9*#]{1,15}$')"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE contacts DROP CONSTRAINT contacts_extension_format")
    op.execute("ALTER TABLE contacts DROP CONSTRAINT contacts_phone_email_or_extension")
    op.execute(
        "ALTER TABLE contacts ADD CONSTRAINT contacts_phone_or_email"
        " CHECK (phone_e164 IS NOT NULL OR email IS NOT NULL)"
    )
    op.execute("ALTER TABLE contacts DROP COLUMN extension")

    op.execute("ALTER TABLE call_legs DROP CONSTRAINT call_legs_provider_check")
    op.execute(
        "ALTER TABLE call_legs ADD CONSTRAINT call_legs_provider_check"
        " CHECK (provider IN ('twilio','telnyx','didww'))"
    )

    op.execute("DROP INDEX phone_numbers_sip_connection_idx")
    op.execute(
        "ALTER TABLE phone_numbers DROP CONSTRAINT phone_numbers_pbx_connection_check"
    )
    op.execute("ALTER TABLE phone_numbers DROP CONSTRAINT phone_numbers_state_check")
    op.execute(
        "ALTER TABLE phone_numbers ADD CONSTRAINT phone_numbers_state_check CHECK ("
        "provisioning_state IN ('pending','active','failed','released'))"
    )
    op.execute("ALTER TABLE phone_numbers DROP COLUMN sip_connection_id")

    op.execute("DROP TABLE sip_connections")
