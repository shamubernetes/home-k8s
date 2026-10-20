-- Pocket ID v2.18.0 OidcClient.ImageType/DarkImageType and S3 Root=uploads.
-- Run ONLY against an isolated restore of database.dump, never a later live query.
-- Schema drift must fail rather than accepting an empty reference list.
SELECT coalesce(json_agg(object_key ORDER BY object_key), '[]'::json)
FROM (
  SELECT 'uploads/oidc-client-images/' ||
    CASE WHEN id ~ '^[A-Za-z0-9._-]+$' THEN id
      ELSE 'cimd-' || encode(sha256(convert_to(id, 'UTF8')), 'hex') END ||
    suffix || '.' || extension AS object_key
  FROM oidc_clients
  CROSS JOIN LATERAL (VALUES ('', image_type), ('-dark', dark_image_type)) images(suffix, extension)
  WHERE extension IS NOT NULL AND extension <> ''
) required;
