-- Unmodified Pocket ID v2.18.0 PostgreSQL migrations, 40cb14f89309286dcafaa62dac82a88a4b5ac583.
-- 20241211111554_init.up.sql
CREATE TABLE app_config_variables
(
    key           VARCHAR(100) NOT NULL PRIMARY KEY,
    value         TEXT NOT NULL,
    type          VARCHAR(20) NOT NULL,
    is_public     BOOLEAN DEFAULT FALSE NOT NULL,
    is_internal   BOOLEAN DEFAULT FALSE NOT NULL,
    default_value TEXT
);

CREATE TABLE user_groups
(
    id            UUID NOT NULL PRIMARY KEY,
    created_at    TIMESTAMPTZ,
    friendly_name VARCHAR(255) NOT NULL,
    name          VARCHAR(255) NOT NULL UNIQUE
);

CREATE TABLE users
(
    id         UUID NOT NULL PRIMARY KEY,
    created_at TIMESTAMPTZ,
    username   VARCHAR(255) NOT NULL UNIQUE,
    email      VARCHAR(255) NOT NULL UNIQUE,
    first_name VARCHAR(100),
    last_name  VARCHAR(100),
    is_admin   BOOLEAN DEFAULT FALSE NOT NULL
);

CREATE TABLE audit_logs
(
    id         UUID NOT NULL PRIMARY KEY,
    created_at TIMESTAMPTZ,
    event      VARCHAR(100) NOT NULL,
    ip_address INET NOT NULL,
    data       JSONB NOT NULL,
    user_id    UUID REFERENCES users ON DELETE SET NULL,
    user_agent TEXT,
    country    VARCHAR(100),
    city       VARCHAR(100)
);

CREATE TABLE custom_claims
(
    id            UUID NOT NULL PRIMARY KEY,
    created_at    TIMESTAMPTZ,
    key           VARCHAR(255) NOT NULL,
    value         TEXT NOT NULL,
    user_id       UUID REFERENCES users ON DELETE CASCADE,
    user_group_id UUID REFERENCES user_groups ON DELETE CASCADE,
    CONSTRAINT custom_claims_unique UNIQUE (key, user_id, user_group_id),
    CHECK (user_id IS NOT NULL OR user_group_id IS NOT NULL)
);

CREATE TABLE oidc_authorization_codes
(
    id                           UUID NOT NULL PRIMARY KEY,
    created_at                   TIMESTAMPTZ,
    code                         VARCHAR(255) NOT NULL UNIQUE,
    scope                        TEXT NOT NULL,
    nonce                        VARCHAR(255),
    expires_at                   TIMESTAMPTZ NOT NULL,
    user_id                      UUID NOT NULL REFERENCES users ON DELETE CASCADE,
    client_id                    UUID NOT NULL,
    code_challenge               VARCHAR(255),
    code_challenge_method_sha256 BOOLEAN
);

CREATE TABLE oidc_clients
(
    id            UUID NOT NULL PRIMARY KEY,
    created_at    TIMESTAMPTZ,
    name          VARCHAR(255),
    secret        TEXT,
    callback_urls JSONB,
    image_type    VARCHAR(10),
    created_by_id UUID REFERENCES users ON DELETE SET NULL,
    is_public     BOOLEAN DEFAULT FALSE
);

CREATE TABLE one_time_access_tokens
(
    id         UUID NOT NULL PRIMARY KEY,
    created_at TIMESTAMPTZ,
    token      VARCHAR(255) NOT NULL UNIQUE,
    expires_at TIMESTAMPTZ NOT NULL,
    user_id    UUID NOT NULL REFERENCES users ON DELETE CASCADE
);

CREATE TABLE user_authorized_oidc_clients
(
    scope     VARCHAR(255),
    user_id   UUID NOT NULL REFERENCES users ON DELETE CASCADE,
    client_id UUID NOT NULL REFERENCES oidc_clients ON DELETE CASCADE,
    PRIMARY KEY (user_id, client_id)
);

CREATE TABLE user_groups_users
(
    user_id       UUID NOT NULL REFERENCES users ON DELETE CASCADE,
    user_group_id UUID NOT NULL REFERENCES user_groups ON DELETE CASCADE,
    PRIMARY KEY (user_id, user_group_id)
);

CREATE TABLE webauthn_credentials
(
    id               UUID NOT NULL PRIMARY KEY,
    created_at       TIMESTAMPTZ,
    name             VARCHAR(255) NOT NULL,
    credential_id    BYTEA NOT NULL UNIQUE,
    public_key       BYTEA NOT NULL,
    attestation_type VARCHAR(20) NOT NULL,
    transport        JSONB NOT NULL,
    user_id          UUID REFERENCES users ON DELETE CASCADE,
    backup_eligible  BOOLEAN DEFAULT FALSE NOT NULL,
    backup_state     BOOLEAN DEFAULT FALSE NOT NULL
);

CREATE TABLE webauthn_sessions
(
    id                UUID NOT NULL PRIMARY KEY,
    created_at        TIMESTAMPTZ,
    challenge         VARCHAR(255) NOT NULL UNIQUE,
    expires_at        TIMESTAMPTZ NOT NULL,
    user_verification VARCHAR(255) NOT NULL
);
-- 20250103145821_pkce_column.up.sql
ALTER TABLE oidc_clients ADD COLUMN pkce_enabled BOOLEAN DEFAULT FALSE;
-- 20250117164229_ldap.up.sql
ALTER TABLE users ADD COLUMN ldap_id TEXT;
ALTER TABLE user_groups ADD COLUMN ldap_id TEXT;

CREATE UNIQUE INDEX users_ldap_id ON users (ldap_id);
CREATE UNIQUE INDEX user_groups_ldap_id ON user_groups (ldap_id);

-- 20250118180712_rename_email_config_variable.up.sql
UPDATE app_config_variables SET key = 'emailLoginNotificationEnabled' WHERE key = 'emailEnabled';
-- 20250120102531_fix_empty_ldap_id_string.up.sql
UPDATE users SET ldap_id = null WHERE ldap_id = '';
UPDATE user_groups SET ldap_id = null WHERE ldap_id = '';
-- 20250131154719_oidc_user_group_restriction.up.sql
CREATE TABLE oidc_clients_allowed_user_groups
(
    user_group_id  UUID NOT NULL REFERENCES user_groups ON DELETE CASCADE,
    oidc_client_id UUID NOT NULL REFERENCES oidc_clients ON DELETE CASCADE,
    PRIMARY KEY (oidc_client_id, user_group_id)
);



-- 20250203082527_fix_empty_user_group_ldap_id_string.up.sql
UPDATE user_groups SET ldap_id = null WHERE ldap_id = '';
-- 20250210152631_post_logout_url.up.sql
ALTER TABLE oidc_clients ADD COLUMN logout_callback_urls JSONB;
-- 20250225182112_manual_smtp_tls_selection.up.sql
UPDATE app_config_variables AS target
SET value = CASE
    WHEN target.value = 'true' AND (SELECT value FROM app_config_variables WHERE key = 'smtpPort' LIMIT 1) = '587' THEN 'starttls'
    WHEN target.value = 'true' THEN 'tls'
    ELSE 'none'
END
    WHERE target.key = 'smtpTls';
-- 20250302220732_api_key_auth.up.sql
CREATE TABLE api_keys (
    id UUID PRIMARY KEY,
    name VARCHAR(255) NOT NULL,
    key VARCHAR(255) NOT NULL UNIQUE,
    description TEXT,
    expires_at TIMESTAMPTZ NOT NULL,
    last_used_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ,
    user_id UUID REFERENCES users ON DELETE CASCADE
);

CREATE INDEX idx_api_keys_key ON api_keys(key);
-- 20250320171311_user_locale.up.sql
ALTER TABLE users ADD COLUMN locale TEXT;
-- 20250323184520_oidc_refresh_tokens.up.sql
CREATE TABLE oidc_refresh_tokens (
    id UUID NOT NULL PRIMARY KEY,
    created_at TIMESTAMPTZ,
    token VARCHAR(255) NOT NULL UNIQUE,
    expires_at TIMESTAMPTZ NOT NULL,
    scope TEXT NOT NULL,
    user_id UUID NOT NULL REFERENCES users ON DELETE CASCADE,
    client_id UUID NOT NULL REFERENCES oidc_clients ON DELETE CASCADE
);

CREATE INDEX idx_oidc_refresh_tokens_token ON oidc_refresh_tokens(token);
-- 20250403082322_audit_logs_indexes.up.sql
CREATE INDEX idx_audit_logs_event ON audit_logs(event);
CREATE INDEX idx_audit_logs_user_id ON audit_logs(user_id);
CREATE INDEX idx_audit_logs_client_name ON audit_logs(("data"->>'clientName'));

-- 20250408120918_app_config_cols.up.sql
ALTER TABLE app_config_variables DROP type;
ALTER TABLE app_config_variables DROP is_public;
ALTER TABLE app_config_variables DROP is_internal;
ALTER TABLE app_config_variables DROP default_value;
-- 20250412000000_add_user_disabled_field.up.sql
ALTER TABLE users
ADD COLUMN IF NOT EXISTS disabled BOOLEAN DEFAULT FALSE NOT NULL;
-- 20250417120000_add_expiration_email_sent_to_api_keys.up.sql
ALTER TABLE api_keys
ADD COLUMN expiration_email_sent BOOLEAN NOT NULL DEFAULT FALSE;
-- 20250421221059_add_device_codes.up.sql
CREATE TABLE oidc_device_codes
(
    id             UUID        NOT NULL PRIMARY KEY,
    created_at     TIMESTAMPTZ,
    device_code    TEXT        NOT NULL UNIQUE,
    user_code      TEXT        NOT NULL UNIQUE,
    scope          TEXT        NOT NULL,
    expires_at     TIMESTAMPTZ NOT NULL,
    is_authorized  BOOLEAN     NOT NULL DEFAULT FALSE,
    user_id        UUID REFERENCES users ON DELETE CASCADE,
    client_id      UUID        NOT NULL REFERENCES oidc_clients ON DELETE CASCADE
);

-- 20250601204251_clear_default_app_config_variables.up.sql
DELETE FROM app_config_variables WHERE value = '';
-- 20250609202611_client_credentials.up.sql
ALTER TABLE oidc_clients ADD COLUMN credentials JSONB NULL;

-- 20250619115053_add_country_index.up.sql
CREATE INDEX idx_audit_logs_country ON audit_logs(country);
-- 20250622151258_add_signup_tokens.up.sql
CREATE TABLE signup_tokens (
    id UUID NOT NULL PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL,
    token VARCHAR(255) NOT NULL UNIQUE,
    expires_at TIMESTAMPTZ NOT NULL,
    usage_limit INTEGER NOT NULL DEFAULT 1,
    usage_count INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX idx_signup_tokens_token ON signup_tokens(token);
CREATE INDEX idx_signup_tokens_expires_at ON signup_tokens(expires_at);
-- 20250628000000_audit_logs_ip_null.up.sql
ALTER TABLE audit_logs ALTER COLUMN ip_address DROP NOT NULL;

-- Add missing indexes
CREATE INDEX idx_audit_logs_created_at ON audit_logs(created_at);
CREATE INDEX idx_audit_logs_user_agent ON audit_logs(user_agent);

-- 20250630000000_kv_table.up.sql
-- The "kv" tables contains miscellaneous key-value pairs
CREATE TABLE kv
(
    "key"  TEXT NOT NULL PRIMARY KEY,
    "value" TEXT
);

-- 20250705000000_normalize.up.sql
-- Normalize (form NFC) all existing values in the database
DO $$
BEGIN
    -- This function is available only if the server's encoding is UTF8
    IF current_setting('server_encoding') = 'UTF8' THEN
        UPDATE api_keys SET
            name = normalize(name, NFC),
            description = normalize(description, NFC);

        UPDATE app_config_variables SET
            "value" = normalize("value", NFC)
        WHERE "key" = 'appName';

        UPDATE custom_claims SET
            "key" = normalize("key", NFC),
            "value" = normalize("value", NFC);

        UPDATE oidc_clients SET
            name = normalize(name, NFC);

        UPDATE users SET
            username = normalize(username, NFC),
            email = normalize(email, NFC),
            first_name = normalize(first_name, NFC),
            last_name = normalize(last_name, NFC);

        UPDATE user_groups SET
            friendly_name = normalize(friendly_name, NFC),
            "name" = normalize("name", NFC);
    ELSE
		RAISE NOTICE 'Skipping normalization: server_encoding is %', current_setting('server_encoding');
	END IF;
END;
$$ LANGUAGE plpgsql;
-- 20250810144214_apps_dashboard.up.sql
ALTER TABLE oidc_clients ADD COLUMN launch_url TEXT;

ALTER TABLE user_authorized_oidc_clients ADD COLUMN last_used_at TIMESTAMPTZ NOT NULL DEFAULT current_timestamp;
-- 20250814121300_requires_reauthentication.up.sql
ALTER TABLE oidc_clients ADD COLUMN requires_reauthentication BOOLEAN NOT NULL DEFAULT FALSE;

CREATE TABLE reauthentication_tokens (
    id TEXT PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    token TEXT NOT NULL UNIQUE,
    expires_at TIMESTAMPTZ NOT NULL,
    user_id uuid NOT NULL REFERENCES users ON DELETE CASCADE
);

CREATE INDEX idx_reauthentication_tokens_token ON reauthentication_tokens(token);
-- 20250822000000_foreign_keys_improvements.up.sql
ALTER TABLE audit_logs
    DROP CONSTRAINT IF EXISTS audit_logs_user_id_fkey,
    ADD CONSTRAINT audit_logs_user_id_fkey
        FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE;

ALTER TABLE oidc_authorization_codes
    ADD CONSTRAINT oidc_authorization_codes_client_fk
        FOREIGN KEY (client_id) REFERENCES oidc_clients (id) ON DELETE CASCADE;
-- 20250824164821_client_id_type.up.sql
-- Drop foreign keys that reference oidc_clients(id)
ALTER TABLE oidc_authorization_codes
    DROP CONSTRAINT IF EXISTS oidc_authorization_codes_client_fk;
ALTER TABLE user_authorized_oidc_clients
    DROP CONSTRAINT IF EXISTS user_authorized_oidc_clients_client_id_fkey;
ALTER TABLE oidc_refresh_tokens
    DROP CONSTRAINT IF EXISTS oidc_refresh_tokens_client_id_fkey;
ALTER TABLE oidc_device_codes
    DROP CONSTRAINT IF EXISTS oidc_device_codes_client_id_fkey;
ALTER TABLE oidc_clients_allowed_user_groups
    DROP CONSTRAINT IF EXISTS oidc_clients_allowed_user_groups_oidc_client_id_fkey;

-- Alter child columns to TEXT
ALTER TABLE oidc_authorization_codes
    ALTER COLUMN client_id TYPE TEXT USING client_id::text;

ALTER TABLE user_authorized_oidc_clients
    ALTER
        COLUMN client_id TYPE TEXT USING client_id::text;

ALTER TABLE oidc_refresh_tokens
    ALTER
        COLUMN client_id TYPE TEXT USING client_id::text;

ALTER TABLE oidc_device_codes
    ALTER
        COLUMN client_id TYPE TEXT USING client_id::text;

ALTER TABLE oidc_clients_allowed_user_groups
    ALTER
        COLUMN oidc_client_id TYPE TEXT USING oidc_client_id::text;

-- Alter parent primary key column to TEXT
ALTER TABLE oidc_clients
    ALTER
        COLUMN id TYPE TEXT USING id::text;

-- Recreate foreign keys with the new type
ALTER TABLE oidc_authorization_codes
    ADD CONSTRAINT oidc_authorization_codes_client_fk
        FOREIGN KEY (client_id) REFERENCES oidc_clients (id) ON DELETE CASCADE;

ALTER TABLE user_authorized_oidc_clients
    ADD CONSTRAINT user_authorized_oidc_clients_client_id_fkey
        FOREIGN KEY (client_id) REFERENCES oidc_clients (id) ON DELETE CASCADE;

ALTER TABLE oidc_refresh_tokens
    ADD CONSTRAINT oidc_refresh_tokens_client_id_fkey
        FOREIGN KEY (client_id) REFERENCES oidc_clients (id) ON DELETE CASCADE;

ALTER TABLE oidc_device_codes
    ADD CONSTRAINT oidc_device_codes_client_id_fkey
        FOREIGN KEY (client_id) REFERENCES oidc_clients (id) ON DELETE CASCADE;

ALTER TABLE oidc_clients_allowed_user_groups
    ADD CONSTRAINT oidc_clients_allowed_user_groups_oidc_client_id_fkey
        FOREIGN KEY (oidc_client_id) REFERENCES oidc_clients (id) ON DELETE CASCADE;
-- 20250917170000_user_display_name_and_username.up.sql
ALTER TABLE users ADD COLUMN display_name TEXT;
UPDATE users SET display_name = trim(coalesce(first_name,'') || ' ' || coalesce(last_name,''));
ALTER TABLE users ALTER COLUMN display_name SET NOT NULL;

CREATE EXTENSION IF NOT EXISTS citext;
ALTER TABLE users ALTER COLUMN username TYPE CITEXT COLLATE "C";
-- 20251001115300_optional_email.up.sql
ALTER TABLE users ALTER COLUMN email DROP NOT NULL;

-- 20251018000000_add_dark_logo.up.sql
ALTER TABLE oidc_clients ADD COLUMN dark_image_type TEXT;

-- 20251024000000_webauthn_credential_params.up.sql
ALTER TABLE webauthn_sessions ADD COLUMN credential_params JSONB NOT NULL DEFAULT '[]';
-- 20251106144600_redundant_indexes.up.sql
DROP INDEX IF EXISTS idx_api_keys_key;
DROP INDEX IF EXISTS idx_oidc_refresh_tokens_token;
DROP INDEX IF EXISTS idx_signup_tokens_token;
DROP INDEX IF EXISTS idx_reauthentication_tokens_token;
-- 20251110000000_storage_table.up.sql
-- The "storage" table contains file data stored in the database
CREATE TABLE storage
(
    path       TEXT NOT NULL PRIMARY KEY,
    data       BYTEA NOT NULL,
    size       BIGINT NOT NULL,
    mod_time   TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL
);

-- 20251115000000_api_keys_expires_at_index.up.sql
CREATE INDEX idx_api_keys_expires_at ON api_keys(expires_at);

-- 20251210000000_one_time_access_device_token.up.sql
ALTER TABLE one_time_access_tokens ADD COLUMN device_token VARCHAR(16);
-- 20251217000000_signup_token_groups.up.sql
CREATE TABLE signup_tokens_user_groups
(
    signup_token_id UUID NOT NULL,
    user_group_id   UUID NOT NULL,
    PRIMARY KEY (signup_token_id, user_group_id),
    FOREIGN KEY (signup_token_id) REFERENCES signup_tokens (id) ON DELETE CASCADE,
    FOREIGN KEY (user_group_id) REFERENCES user_groups (id) ON DELETE CASCADE
);
-- 20251217141000_v2_export_normalization.up.sql
-- This migration is part of v2

-- No-op in Postgres

-- 20251219000000_oidc_client_group_restriction.up.sql
ALTER TABLE oidc_clients
    ADD COLUMN is_group_restricted boolean NOT NULL DEFAULT false;

UPDATE oidc_clients oc
SET is_group_restricted =
    EXISTS (
    SELECT 1
    FROM oidc_clients_allowed_user_groups a
    WHERE a.oidc_client_id = oc.id
    );
-- 20251229173100_scim_service_providers.up.sql
CREATE TABLE scim_service_providers
(
    id             UUID PRIMARY KEY,
    created_at     TIMESTAMPTZ NOT NULL,
    endpoint       TEXT        NOT NULL,
    token          TEXT        NOT NULL,
    last_synced_at TIMESTAMPTZ,
    oidc_client_id TEXT        NOT NULL REFERENCES oidc_clients (id) ON DELETE CASCADE
);

ALTER TABLE users
    ADD COLUMN updated_at TIMESTAMPTZ;

ALTER TABLE user_groups
    ADD COLUMN updated_at TIMESTAMPTZ;

-- 20260102231700_ldap_admin_group_rename.up.sql
UPDATE app_config_variables
SET key = 'ldapAdminGroupName'
WHERE key = 'ldapAttributeAdminGroup'
  AND NOT EXISTS (
    SELECT 1
    FROM app_config_variables
    WHERE key = 'ldapAdminGroupName'
);
-- 20260103142200_user_nullable_fields.up.sql
-- No-op on Postgres
-- 20260104190900_add_nonce_to_device_codes.up.sql
ALTER TABLE oidc_device_codes ADD COLUMN nonce VARCHAR(255);

-- 20260106140900_sqlite_export_normalization.up.sql
-- No-op on Postgres
-- 20260109090200_email_verification.up.sql
CREATE TABLE email_verification_tokens
(
    id         UUID PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL,
    token      TEXT        NOT NULL UNIQUE,
    expires_at TIMESTAMPTZ NOT NULL,
    user_id    UUID        NOT NULL REFERENCES users ON DELETE CASCADE
);

ALTER TABLE users
    ADD COLUMN email_verified BOOLEAN NOT NULL DEFAULT FALSE;

UPDATE users
SET email_verified = EXISTS (SELECT 1
                             FROM app_config_variables
                             WHERE key = 'emailsVerified'
                               AND value = 'true');
-- 20260304090200_indexes.up.sql
CREATE INDEX IF NOT EXISTS idx_webauthn_sessions_expires_at ON webauthn_sessions (expires_at);
CREATE INDEX IF NOT EXISTS idx_one_time_access_tokens_expires_at ON one_time_access_tokens (expires_at);
CREATE INDEX IF NOT EXISTS idx_oidc_authorization_codes_expires_at ON oidc_authorization_codes (expires_at);
CREATE INDEX IF NOT EXISTS idx_oidc_refresh_tokens_expires_at ON oidc_refresh_tokens (expires_at);
CREATE INDEX IF NOT EXISTS idx_reauthentication_tokens_expires_at ON reauthentication_tokens (expires_at);
CREATE INDEX IF NOT EXISTS idx_email_verification_tokens_expires_at ON email_verification_tokens (expires_at);
-- 20260418120000_oidc_amr_claim.up.sql
ALTER TABLE oidc_authorization_codes
    ADD COLUMN authentication_method TEXT NOT NULL DEFAULT '';
ALTER TABLE oidc_refresh_tokens
    ADD COLUMN authentication_method TEXT NOT NULL DEFAULT '';
ALTER TABLE oidc_device_codes
    ADD COLUMN authentication_method TEXT NOT NULL DEFAULT '';

-- 20260518222000_oidc_refresh_token_id_token_jti.up.sql
ALTER TABLE oidc_refresh_tokens
    ADD COLUMN id_token_jti UUID;

CREATE INDEX idx_oidc_refresh_tokens_id_token_jti
    ON oidc_refresh_tokens(user_id, client_id, id_token_jti);

-- 20260601154900_par.up.sql
CREATE TABLE oidc_pushed_authorization_requests (
    id UUID NOT NULL PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL,
    request_uri TEXT NOT NULL UNIQUE,
    client_id TEXT NOT NULL REFERENCES oidc_clients(id) ON DELETE CASCADE,
    parameters JSONB NOT NULL DEFAULT '{}',
    expires_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX idx_oidc_par_expires_at ON oidc_pushed_authorization_requests (expires_at);

ALTER TABLE oidc_clients ADD COLUMN requires_pushed_authorization_requests BOOLEAN NOT NULL DEFAULT FALSE;

-- 20260607120000_oauth2_storage.up.sql
CREATE TABLE oauth2_sessions (
    id UUID NOT NULL PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL,
    kind TEXT NOT NULL,
    key TEXT NOT NULL,
    request_id TEXT NOT NULL,
    access_token_signature TEXT NOT NULL DEFAULT '',
    active BOOLEAN NOT NULL DEFAULT TRUE,
    request_data JSONB NOT NULL,
    expires_at TIMESTAMPTZ
);

CREATE UNIQUE INDEX idx_oauth2_sessions_kind_key ON oauth2_sessions (kind, key);
CREATE INDEX idx_oauth2_sessions_kind_request ON oauth2_sessions (kind, request_id);
CREATE INDEX idx_oauth2_sessions_expires_at ON oauth2_sessions (expires_at);

CREATE TABLE oauth2_jtis (
    id UUID NOT NULL PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL,
    jti TEXT NOT NULL UNIQUE,
    expires_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX idx_oauth2_jtis_expires_at ON oauth2_jtis (expires_at);

CREATE TABLE interaction_sessions
(
    id                         UUID        NOT NULL PRIMARY KEY,
    created_at                 TIMESTAMPTZ NOT NULL,

    consent_required           BOOLEAN     NOT NULL DEFAULT FALSE,
    reauthentication_required  BOOLEAN     NOT NULL DEFAULT FALSE,
    authentication_required    BOOLEAN     NOT NULL DEFAULT FALSE,
    account_selection_required BOOLEAN     NOT NULL DEFAULT FALSE,

    scopes                     JSONB       NOT NULL DEFAULT '[]',
    client_id                  TEXT        NOT NULL REFERENCES oidc_clients (id) ON DELETE CASCADE,
    user_id                    UUID        REFERENCES users (id) ON DELETE CASCADE,
    requested_at               TIMESTAMPTZ NOT NULL,
    reauthenticated_at         TIMESTAMPTZ,
    parameters                 JSONB       NOT NULL DEFAULT '{}'
);

CREATE INDEX idx_interaction_sessions_client_id
    ON interaction_sessions (client_id);
CREATE INDEX idx_interaction_sessions_user_id
    ON interaction_sessions (user_id);

-- Convert scope from string to json
ALTER TABLE user_authorized_oidc_clients
    ALTER COLUMN scope TYPE jsonb USING (
        CASE
            WHEN scope IS NULL OR btrim(scope) = '' THEN '[]'::jsonb
            ELSE to_jsonb(array_remove(regexp_split_to_array(scope, '[[:space:]]+'), ''))
        END
    ),
    ALTER COLUMN scope SET DEFAULT '[]'::jsonb,
    ALTER COLUMN scope SET NOT NULL;

DROP TABLE IF EXISTS oidc_pushed_authorization_requests;
DROP TABLE IF EXISTS oidc_device_codes;
DROP TABLE IF EXISTS oidc_refresh_tokens;
DROP TABLE IF EXISTS oidc_authorization_codes;

-- 20260626120000_oidc_client_skip_consent.up.sql
ALTER TABLE oidc_clients ADD COLUMN skip_consent BOOLEAN NOT NULL DEFAULT FALSE;

-- 20260626153900_add_pkce_supported_to_oidc_clients.up.sql
ALTER TABLE oidc_clients
ADD COLUMN pkce_supported BOOLEAN NOT NULL DEFAULT FALSE;
-- 20260707170000_oauth_apis.up.sql
CREATE TABLE apis (
    id UUID NOT NULL PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ,
    name TEXT NOT NULL,
    audience TEXT NOT NULL UNIQUE
);

CREATE TABLE api_permissions (
    id UUID NOT NULL PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL,
    api_id UUID NOT NULL REFERENCES apis(id) ON DELETE CASCADE,
    key TEXT NOT NULL,
    name TEXT NOT NULL,
    description TEXT,
    UNIQUE (api_id, key)
);

CREATE INDEX idx_api_permissions_api_id ON api_permissions(api_id);

CREATE TABLE oidc_clients_allowed_api_permissions (
    oidc_client_id TEXT NOT NULL REFERENCES oidc_clients(id) ON DELETE CASCADE,
    api_permission_id UUID NOT NULL REFERENCES api_permissions(id) ON DELETE CASCADE,
    subject_type TEXT NOT NULL CHECK (subject_type IN ('user', 'client')),
    PRIMARY KEY (oidc_client_id, api_permission_id, subject_type)
);

-- 20260708120000_add_oidc_client_description.up.sql
ALTER TABLE oidc_clients ADD COLUMN description TEXT NOT NULL DEFAULT '';

-- 20260708130000_move_instance_id_to_kv.up.sql
-- Move the instance ID out of the standalone app config table and into the "kv" table
INSERT INTO kv ("key", "value")
SELECT 'instance_id', "value"
    FROM app_config_variables
    WHERE "key" = 'instanceId'
    ON CONFLICT ("key") DO NOTHING;

DELETE FROM app_config_variables WHERE "key" = 'instanceId';

-- 20260718000000_freeze_config.up.sql
-- Freeze the app configuration
-- Encode every row of the standalone config table as a single JSON object (mapping key -> value) and store it in the "kv" table under the "config_migrated" key
--
-- json_object_agg aggregates all rows into a JSON object
-- The "HAVING count(*) > 0" clause ensures that nothing is written to the "kv" table when the config table is empty
INSERT INTO kv ("key", "value")
SELECT 'config_migrated', json_object_agg("key", "value")::text
FROM app_config_variables
HAVING count(*) > 0;

-- Drop the now-frozen standalone config table
DROP TABLE app_config_variables;

-- 20260722120000_oauth_storage_export_normalization.up.sql
-- No-op on PostgreSQL because its OAuth storage types already match the export format

-- 20260723000000_actor_tokens.up.sql
-- One-time access tokens are now stored in the actor state store, so the table is no longer needed.
DROP TABLE IF EXISTS one_time_access_tokens;

-- Freeze the signup tokens.
-- Encode every signup token (with its user group IDs) as a single JSON array and store it in the "kv" table under the "signup_tokens_migrated" key, so the singleton signup token actor can seed its state from it on first startup.
-- The "HAVING count(*) > 0" clause ensures nothing is written to the "kv" table when there are no signup tokens.
-- Timestamps are stored as Unix seconds so the frozen format is identical across databases.
INSERT INTO kv ("key", "value")
SELECT 'signup_tokens_migrated', json_agg(
    json_build_object(
        'id', st.id,
        'token', st.token,
        'expiresAt', extract(epoch FROM st.expires_at)::bigint,
        'usageLimit', st.usage_limit,
        'usageCount', st.usage_count,
        'createdAt', extract(epoch FROM st.created_at)::bigint,
        'userGroupIds', COALESCE(
            (SELECT json_agg(stug.user_group_id) FROM signup_tokens_user_groups stug WHERE stug.signup_token_id = st.id),
            '[]'::json
        )
    )
)::text
FROM signup_tokens st
HAVING count(*) > 0;

-- Drop the now-frozen signup token tables.
DROP TABLE signup_tokens_user_groups;
DROP TABLE signup_tokens;

-- 20260727120000_email_verification_actors.up.sql
DROP TABLE email_verification_tokens;

-- 20260728120000_oidc_client_id_metadata_document.up.sql
ALTER TABLE oidc_clients
    ADD COLUMN client_type TEXT NOT NULL DEFAULT 'standard';
ALTER TABLE oidc_clients
    ADD COLUMN metadata_expires_at TIMESTAMPTZ;
ALTER TABLE oidc_clients
    ADD COLUMN metadata_grant_types JSONB;

-- 20260731120000_oauth2_session_ownership.up.sql
ALTER TABLE oauth2_sessions
    ADD COLUMN client_id TEXT;

UPDATE oauth2_sessions
SET client_id = request_data ->> 'client_id';

DELETE FROM oauth2_sessions
WHERE client_id IS NULL
   OR NOT EXISTS (
       SELECT 1
       FROM oidc_clients
       WHERE oidc_clients.id = oauth2_sessions.client_id
   );

ALTER TABLE oauth2_sessions
    ALTER COLUMN client_id SET NOT NULL,
    ADD CONSTRAINT fk_oauth2_sessions_client_id
        FOREIGN KEY (client_id) REFERENCES oidc_clients(id) ON DELETE CASCADE,
    ADD CONSTRAINT chk_oauth2_sessions_client_id
        CHECK (client_id = request_data ->> 'client_id');

CREATE INDEX idx_oauth2_sessions_client_subject
    ON oauth2_sessions (client_id, (request_data #>> '{session,subject}'), kind, active);

-- 20260802120000_add_oidc_client_token_lifetimes.up.sql
ALTER TABLE oidc_clients ADD COLUMN access_token_duration_minutes BIGINT NOT NULL DEFAULT 60;
ALTER TABLE oidc_clients ADD COLUMN refresh_token_duration_minutes BIGINT NOT NULL DEFAULT 43200;

-- 20260807120000_oidc_client_multiple_secrets.up.sql
-- Move the single client secret into the credentials document, as the only entry of the new "secrets" array
-- Migrated secrets keep their bcrypt hash because the secret's value is not recoverable, and they never expire so that existing integrations keep working
UPDATE oidc_clients
SET credentials = COALESCE(credentials, '{}'::jsonb) || jsonb_build_object(
        'secrets', jsonb_build_array(jsonb_build_object(
                'id', gen_random_uuid()::text,
                'alg', 'bcrypt',
                'hash', secret,
                'createdAt', to_char(COALESCE(created_at, now()) AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"')
        ))
)
WHERE secret IS NOT NULL AND secret <> '';

ALTER TABLE oidc_clients DROP COLUMN secret;

-- 20260814120000_api_client_access.up.sql
-- Keep existing API audiences aligned with the canonical resource identifiers used by new APIs and OAuth resource resolution
UPDATE apis SET audience = RTRIM(audience, '/') WHERE audience <> RTRIM(audience, '/');

ALTER TABLE apis ADD COLUMN allow_cimd_clients BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE api_permissions ADD COLUMN allowed_for_cimd_clients BOOLEAN NOT NULL DEFAULT FALSE;

-- Access to an API is tracked separately from the permissions granted for it, because a client may be allowed to request tokens for an API without any scope
CREATE TABLE oidc_clients_allowed_apis (
    oidc_client_id TEXT NOT NULL REFERENCES oidc_clients(id) ON DELETE CASCADE,
    api_id UUID NOT NULL REFERENCES apis(id) ON DELETE CASCADE,
    subject_type TEXT NOT NULL CHECK (subject_type IN ('user', 'client')),
    PRIMARY KEY (oidc_client_id, api_id, subject_type)
);

-- The primary key leads with oidc_client_id, so the API-side lookups and the cascade from apis need their own index
CREATE INDEX idx_oidc_clients_allowed_apis_api_id ON oidc_clients_allowed_apis(api_id);

-- Every existing permission grant implied access to its API, so those clients keep exactly the access they had
INSERT INTO oidc_clients_allowed_apis (oidc_client_id, api_id, subject_type)
SELECT DISTINCT g.oidc_client_id, p.api_id, g.subject_type
FROM oidc_clients_allowed_api_permissions g
JOIN api_permissions p ON p.id = g.api_permission_id;

-- 20260907140000_webauthn_session_extensions.up.sql
ALTER TABLE webauthn_sessions ADD COLUMN extensions JSONB NOT NULL DEFAULT '{}';

-- 20260916120000_webauthn_credential_icon.up.sql
ALTER TABLE webauthn_credentials ADD COLUMN aaguid TEXT NOT NULL DEFAULT '00000000-0000-0000-0000-000000000000';

-- 20260923183637_add_oidc_client_backchannel_logout_url.up.sql
ALTER TABLE oidc_clients ADD COLUMN backchannel_logout_url TEXT NOT NULL DEFAULT '';

-- 20260929120000_refresh_token_rotation_grace.up.sql
ALTER TABLE oauth2_sessions ADD COLUMN rotated_at TIMESTAMPTZ;
