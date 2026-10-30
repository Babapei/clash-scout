"""store 模块的单元测试。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from clash_ip_tracker import store


class StoreTest(unittest.TestCase):
    def test_new_record_defaults(self) -> None:
        rec = store.new_ip_record()
        self.assertEqual(rec["status"], store.UNKNOWN)
        self.assertEqual(rec["ok"], 0)
        self.assertEqual(rec["fail"], 0)
        self.assertEqual(rec["consecutive_fail"], 0)
        self.assertIsNone(rec["last_ok"])

    def test_probe_ok_marks_alive(self) -> None:
        rec = store.new_ip_record()
        store.apply_probe(rec, ok=True, latency_ms=123.4)
        self.assertEqual(rec["status"], store.ALIVE)
        self.assertEqual(rec["ok"], 1)
        self.assertAlmostEqual(rec["latency_ms"], 123.4, places=1)

    def test_single_fail_is_flaky_not_dead(self) -> None:
        rec = store.new_ip_record()
        store.apply_probe(rec, ok=False, latency_ms=None)
        self.assertEqual(rec["status"], store.FLAKY)
        self.assertEqual(rec["consecutive_fail"], 1)

    def test_two_consecutive_fails_is_dead(self) -> None:
        rec = store.new_ip_record()
        store.apply_probe(rec, ok=False, latency_ms=None)
        store.apply_probe(rec, ok=False, latency_ms=None)
        self.assertEqual(rec["status"], store.DEAD)
        self.assertEqual(rec["consecutive_fail"], 2)

    def test_fail_then_ok_becomes_flaky(self) -> None:
        rec = store.new_ip_record()
        store.apply_probe(rec, ok=False, latency_ms=None)
        store.apply_probe(rec, ok=True, latency_ms=100)
        self.assertEqual(rec["status"], store.FLAKY)
        self.assertEqual(rec["consecutive_fail"], 0)

    def test_success_rate(self) -> None:
        rec = store.new_ip_record()
        self.assertIsNone(store.success_rate(rec))
        store.apply_probe(rec, ok=True, latency_ms=1)
        store.apply_probe(rec, ok=True, latency_ms=1)
        store.apply_probe(rec, ok=False, latency_ms=None)
        self.assertAlmostEqual(store.success_rate(rec), 2 / 3, places=3)

    def test_needs_check(self) -> None:
        rec = store.new_ip_record()
        self.assertTrue(store.needs_check(rec, recheck_minutes=10))
        rec["last_check"] = store.now_iso()
        self.assertFalse(store.needs_check(rec, recheck_minutes=10))

    def test_locked_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "table.json"
            with store.locked(path) as table:
                node = store.get_domain(table, "example.com", 443)
                store.upsert_seen(node, "1.2.3.4")
            with store.locked(path) as table:
                self.assertIn("1.2.3.4", table["domains"]["example.com"]["ips"])
            raw = json.loads(path.read_text())
            self.assertEqual(raw["version"], store.SCHEMA_VERSION)

    def test_mark_stale(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "table.json"
            with store.locked(path) as table:
                node = store.get_domain(table, "example.com", 443)
                rec = store.upsert_seen(node, "1.2.3.4")
                rec["last_seen"] = "2020-01-01T00:00:00+08:00"
            with store.locked(path) as table:
                node = table["domains"]["example.com"]
                count = store.mark_stale(node, stale_days=7)
                self.assertEqual(count, 1)
                self.assertTrue(node["ips"]["1.2.3.4"]["stale"])


if __name__ == "__main__":
    unittest.main()
