"""Firecrawl queue-only recovery contract regressions."""
import hashlib
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import kopiur_firecrawl_native as fixture


class FirecrawlTests(unittest.TestCase):
    def test_schema_is_pinned(self):
        raw = (fixture.ROOT / 'scripts/fixtures/firecrawl-nuq.sql').read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(), fixture.SCHEMA_SHA256)

    def test_queue_schema_excludes_scheduler_and_global_tuning(self):
        ddl = fixture.queue_schema()
        self.assertNotIn('cron.schedule', ddl)
        self.assertNotIn('ALTER SYSTEM', ddl)
        for table in ('queue_scrape', 'queue_scrape_backlog', 'queue_crawl_finished', 'group_crawl'):
            self.assertIn('CREATE TABLE IF NOT EXISTS nuq.' + table, ddl)

    def test_checksum_failure_is_closed(self):
        with patch.object(Path, 'read_bytes', return_value=b'changed schema'):
            with self.assertRaises(ValueError):
                fixture.queue_schema()

    def test_restored_original_owner_required(self):
        native = fixture.contract()
        drill = native['DockerDrill']('firecrawl')
        drill.api_key = 'original-owner'
        value = json.dumps({'job': fixture.JOB, 'backlog': fixture.BACKLOG, 'owner': 'changed-owner'}).encode()
        with patch.dict(native, {'run': lambda *a, **k: type('Result', (), {'stdout': value})()}):
            with self.assertRaises(ValueError):
                drill.application_state('restored')

    def test_fixture_does_not_accept_scheduler_or_multistore(self):
        with patch('kopiur_native_fixture.exercise') as exercise:
            fixture.fixture()
        limitations = exercise.call_args.args[3]
        self.assertFalse(limitations['pg_cron_scheduler_qualified'])
        self.assertFalse(limitations['production_rabbitmq_redis_qualified'])
        self.assertFalse(limitations['production_scrape_api_playwright_qualified'])


if __name__ == '__main__':
    unittest.main()
