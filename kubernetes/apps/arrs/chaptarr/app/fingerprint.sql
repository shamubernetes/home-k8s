\set ON_ERROR_STOP on
SELECT format(
  'SELECT %L || chr(9) || count(*)::text || chr(9) || coalesce(md5(string_agg(md5(row_to_json(t)::text), '''' ORDER BY md5(row_to_json(t)::text))), md5('''')) FROM %I.%I t;',
  schemaname || '.' || tablename, schemaname, tablename
)
FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename
\gexec
