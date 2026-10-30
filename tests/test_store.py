"""store 模块的单元测试。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from clash_ip_tracker import store


class RecordTest(unittest.TestCase):
    def test_new_record_defaults(self) -> None:
        rec = store.new_ip_record()
        self.assertEqual(rec["status"], store.UNKNOWN)
        self.assertEqual(rec["ok"], 0)
        self.assertEqual(rec["fail"], 0)
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


class TableTest(unittest.TestCase):
    def test_locked_roundtrip_with_environment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "table.json"
            with store.locked(path) as table:
                env = store.get_environment(table, "gw:192.168.1.1", "home")
                node = store.get_domain(env, "example.com", 443)
                store.upsert_seen(node, "1.2.3.4")
            with store.locked(path) as table:
                node = store.find_domain(table, "gw:192.168.1.1", "example.com")
                self.assertIsNotNone(node)
                self.assertIn("1.2.3.4", node["ips"])
            raw = json.loads(path.read_text())
            self.assertEqual(raw["version"], store.SCHEMA_VERSION)

    def test_environments_are_isolated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "table.json"
            with store.locked(path) as table:
                home = store.get_domain(store.get_environment(table, "home", "home"), "x.com", 1)
                store.apply_probe(store.upsert_seen(home, "1.1.1.1"), ok=True, latency_ms=10)
                campus = store.get_domain(store.get_environment(table, "campus", "campus"), "x.com", 1)
                store.apply_probe(store.upsert_seen(campus, "1.1.1.1"), ok=False, latency_ms=None)
            with store.locked(path) as table:
                h = store.find_domain(table, "home", "x.com")["ips"]["1.1.1.1"]
                c = store.find_domain(table, "campus", "x.com")["ips"]["1.1.1.1"]
                self.assertEqual(h["status"], store.ALIVE)
                self.assertEqual(c["status"], store.FLAKY)

    def test_migrate_v1(self) -> None:
        v1 = {
            "version": 1,
            "domains": {"a.com": {"ports": [1], "ips": {"1.1.1.1": store.new_ip_record()}}},
        }
        migrated = store.migrate(v1)
        self.assertEqual(migrated["version"], store.SCHEMA_VERSION)
        self.assertIn("unknown", migrated["environments"])
        self.assertIn("a.com", migrated["environments"]["unknown"]["domains"])

    def test_mark_stale(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "table.json"
            with store.locked(path) as table:
                env = store.get_environment(table, "home", "home")
                node = store.get_domain(env, "example.com", 443)
                rec = store.upsert_seen(node, "1.2.3.4")
                rec["last_seen"] = "2020-01-01T00:00:00+08:00"
            with store.locked(path) as table:
                node = store.find_domain(table, "home", "example.com")
                self.assertEqual(store.mark_stale(node, stale_days=7), 1)
                self.assertTrue(node["ips"]["1.2.3.4"]["stale"])

    def test_all_domains_dedup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "table.json"
            with store.locked(path) as table:
                for env_id in ("home", "campus"):
                    env = store.get_environment(table, env_id, env_id)
                    store.get_domain(env, "same.com", 1)
            with store.locked(path) as table:
                self.assertEqual(store.all_domains(table), ["same.com"])


if __name__ == "__main__":
    unittest.main()
