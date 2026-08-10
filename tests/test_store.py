"""Tests for store.py — SQLite persistence used by --store and the watchlist.

Covers the second-sighting crash: findings are built by callers as
`item.engagement.score if item.engagement else 0`, and Engagement.score is
Optional[int]. When an item has an engagement object whose metric is absent,
engagement_score is present-but-None. The update branch then ran
max(None, ...) and raised TypeError, so the *second* time a URL was seen the
whole store call died.
"""

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import store


class StoreTempDBTestCase(unittest.TestCase):
    """Point store at a throwaway DB file for each test."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "research.db"
        self._orig_get_db_path = store._get_db_path
        store._get_db_path = lambda: self.db_path
        store.init_db()

    def tearDown(self):
        store._get_db_path = self._orig_get_db_path
        self._tmp.cleanup()


class TestConnectPragmas(StoreTempDBTestCase):
    def test_busy_timeout_is_set(self):
        # Default is 0, which makes a concurrent writer fail instantly rather
        # than waiting out a short write.
        conn = store._connect()
        try:
            (value,) = conn.execute("PRAGMA busy_timeout").fetchone()
        finally:
            conn.close()
        self.assertGreaterEqual(value, 5000)


class TestStoreFindingsNoneEngagement(StoreTempDBTestCase):
    def _finding(self, engagement_score):
        return {
            "source": "reddit",
            "url": "https://reddit.com/r/test/comments/abc",
            "title": "A post",
            "author": "r/test",
            "content": "body",
            "summary": "",
            "engagement_score": engagement_score,
            "relevance_score": 0.5,
        }

    def test_second_sighting_with_none_engagement_does_not_crash(self):
        topic = store.add_topic("test topic")
        run_one = store.record_run(topic["id"], source_mode="both", status="completed")
        store.store_findings(run_one, topic["id"], [self._finding(None)])

        # Second run sees the same URL — this is the path that used to raise
        # TypeError on max(None, ...).
        run_two = store.record_run(topic["id"], source_mode="both", status="completed")
        result = store.store_findings(run_two, topic["id"], [self._finding(None)])
        self.assertIsNotNone(result)

    def test_second_sighting_keeps_the_higher_engagement(self):
        topic = store.add_topic("test topic 2")
        run_one = store.record_run(topic["id"], source_mode="both", status="completed")
        store.store_findings(run_one, topic["id"], [self._finding(42)])

        run_two = store.record_run(topic["id"], source_mode="both", status="completed")
        store.store_findings(run_two, topic["id"], [self._finding(None)])

        conn = store._connect()
        try:
            row = conn.execute(
                "SELECT engagement_score, sighting_count FROM findings WHERE source_url = ?",
                ("https://reddit.com/r/test/comments/abc",),
            ).fetchone()
        finally:
            conn.close()
        # A missing metric must not wipe a previously recorded score.
        self.assertEqual(row["engagement_score"], 42)
        self.assertEqual(row["sighting_count"], 2)


if __name__ == "__main__":
    unittest.main()
