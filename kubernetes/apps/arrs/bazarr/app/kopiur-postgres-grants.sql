-- Dedicated Bazarr recovery identity. Run only against the owned application DB.
-- Credentials come from stdin/environment, never shell arguments or public files.
\set ON_ERROR_STOP on
\getenv backup_role BACKUP_USER
\getenv backup_password BACKUP_PASSWORD
\getenv application_owner APPLICATION_OWNER
BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '30s';
SELECT set_config('kopiur.backup_role', :'backup_role', true) AS backup_role_setting \gset
SELECT set_config('kopiur.backup_password', :'backup_password', true) AS backup_password_setting \gset
SELECT set_config('kopiur.application_owner', :'application_owner', true) AS application_owner_setting \gset
DO $provision$
DECLARE
  backup_role text := current_setting('kopiur.backup_role');
  backup_password text := current_setting('kopiur.backup_password');
  application_owner text := current_setting('kopiur.application_owner');
  role_oid oid;
  ownership_marker constant text := 'K8S-92 readonly backup for arrs/bazarr';
BEGIN
  IF current_database() <> 'bazarr' OR backup_role <> 'kopiur_bazarr' OR
     application_owner !~ '^[a-z][a-z0-9_]{0,62}$' OR
     backup_password !~ '^[a-f0-9]{64}$' OR application_owner = backup_role THEN
    RAISE EXCEPTION 'unexpected provisioning contract';
  END IF;
  IF pg_get_userbyid((SELECT datdba FROM pg_database WHERE datname = current_database())) <> application_owner THEN
    RAISE EXCEPTION 'application database ownership changed';
  END IF;
  IF EXISTS (SELECT 1 FROM pg_namespace WHERE nspname NOT LIKE 'pg_%'
             AND nspname NOT IN ('public','information_schema')) OR
     EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
             WHERE n.nspname='public' AND c.relkind IN ('r','p','S','v','m','f')
             AND pg_get_userbyid(c.relowner) <> application_owner) OR
     EXISTS (SELECT 1 FROM pg_class WHERE relrowsecurity) OR
     EXISTS (SELECT 1 FROM pg_largeobject_metadata) THEN
    RAISE EXCEPTION 'unqualified application schema';
  END IF;
  SELECT oid INTO role_oid FROM pg_roles WHERE rolname=backup_role;
  IF role_oid IS NOT NULL AND
     shobj_description(role_oid, 'pg_authid') IS DISTINCT FROM ownership_marker THEN
    RAISE EXCEPTION 'existing identity is not owned by this provisioner';
  END IF;
  IF role_oid IS NOT NULL AND
     EXISTS (SELECT 1 FROM pg_auth_members WHERE member=role_oid OR roleid=role_oid) THEN
    RAISE EXCEPTION 'backup identity has unexpected memberships';
  END IF;
  IF role_oid IS NULL THEN
    EXECUTE format('CREATE ROLE %I LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS NOINHERIT', backup_role);
    EXECUTE format('COMMENT ON ROLE %I IS %L', backup_role, ownership_marker);
  END IF;
  -- Never broaden or silently repair the authority of an existing role.
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname=backup_role AND
             (rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls OR rolinherit)) THEN
    RAISE EXCEPTION 'elevated backup identity';
  END IF;
  EXECUTE format('ALTER ROLE %I LOGIN PASSWORD %L', backup_role, backup_password);
  EXECUTE format('ALTER ROLE %I SET default_transaction_read_only = on', backup_role);
  EXECUTE format('GRANT CONNECT ON DATABASE %I TO %I', current_database(), backup_role);
  EXECUTE format('GRANT USAGE ON SCHEMA public TO %I', backup_role);
  EXECUTE format('GRANT SELECT ON ALL TABLES IN SCHEMA public TO %I', backup_role);
  EXECUTE format('GRANT SELECT ON ALL SEQUENCES IN SCHEMA public TO %I', backup_role);
  EXECUTE format('ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA public GRANT SELECT ON TABLES TO %I', application_owner, backup_role);
  EXECUTE format('ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA public GRANT SELECT ON SEQUENCES TO %I', application_owner, backup_role);
  IF has_database_privilege(backup_role, current_database(), 'CREATE') OR
     has_schema_privilege(backup_role, 'public', 'CREATE') OR
     EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
             WHERE n.nspname='public' AND c.relkind IN ('r','p','v','m','f')
             AND has_table_privilege(backup_role,c.oid,'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')) OR
     EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
             WHERE n.nspname='public' AND c.relkind='S'
             AND has_sequence_privilege(backup_role,c.oid,'UPDATE')) THEN
    RAISE EXCEPTION 'backup identity has unexpected write authority';
  END IF;
END
$provision$;
COMMIT;
