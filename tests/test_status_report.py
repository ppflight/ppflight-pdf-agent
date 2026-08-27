import importlib.util
import json
import sqlite3
import tempfile
import unittest
import uuid
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "status-report.py"
SPEC = importlib.util.spec_from_file_location("status_report", SCRIPT)
STATUS_REPORT = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(STATUS_REPORT)


class StatusReportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.artifacts = self.root / "artifacts"
        self.state = self.root / "state.json"
        self.database = self.root / "agent.sqlite3"
        self.cache = self.root / "cache"
        self.artifacts.mkdir()
        self.cache.mkdir()
        self.config = self.root / "config.json"
        self.config.write_text(json.dumps({
            "admin_url": "https://www.ppflight.com/api/pdf-agent/v1",
            "artifact_dir": str(self.artifacts),
            "state_path": str(self.state),
            "db_path": str(self.database),
            "cache_dir": str(self.cache),
            "download_audience": "ppflight-pdf-download",
            "download_port": 9760,
            "poll_interval_seconds": 10,
            "mem_available_min": 0,
            "disk_free_min": 0,
            "load1_max": 100000,
        }))

    def tearDown(self):
        self.temp.cleanup()

    def create_database(self):
        connection = sqlite3.connect(self.database)
        connection.executescript("""
            CREATE TABLE artifacts (
                artifact_id TEXT PRIMARY KEY, revision INTEGER, status TEXT,
                filename TEXT, download_filename TEXT, sha256 TEXT, size INTEGER, updated_at INTEGER
            );
            CREATE TABLE completions (
                task_id TEXT PRIMARY KEY, task_fingerprint TEXT, success INTEGER,
                result_json TEXT, delivered INTEGER, completed_at INTEGER
            );
            INSERT INTO artifacts VALUES ('one', 1, 'ready', 'PPFlight-one-1.pdf', 'PPFlight-one.pdf', '', 9, 1);
            INSERT INTO artifacts VALUES ('two', 1, 'revoked', NULL, NULL, NULL, 0, 1);
            INSERT INTO completions VALUES ('ok', '', 1, '{}', 1, 1);
            INSERT INTO completions VALUES ('failed', '', 0, '{}', 0, 1);
        """)
        connection.close()

    def test_reports_binding_pdf_and_task_counts_without_credentials(self):
        agent_uuid = str(uuid.uuid4())
        self.state.write_text(json.dumps({
            "agent_id": "agent-1",
            "agent_uuid": agent_uuid,
            "agent_token": "this-must-never-be-reported",
            "download_hmac_key": "also-must-never-be-reported",
            "bound_at": 1,
        }))
        self.create_database()
        (self.artifacts / "PPFlight-one-1.pdf").write_bytes(b"%PDF-test")
        (self.artifacts / "ignore.txt").write_text("ignored")
        report = STATUS_REPORT.collect(str(self.config))
        self.assertEqual(report["binding"], "bound")
        self.assertEqual(report["agent_uuid"], agent_uuid)
        self.assertEqual(report["artifacts_ready"], 1)
        self.assertEqual(report["artifacts_revoked"], 1)
        self.assertEqual(report["tasks_succeeded"], 1)
        self.assertEqual(report["tasks_failed"], 1)
        self.assertEqual(report["tasks_awaiting_delivery"], 1)
        self.assertEqual(report["pdf_files"], 1)
        self.assertEqual(report["pdf_bytes"], 9)
        self.assertNotIn("agent_token", report)
        self.assertNotIn("download_hmac_key", report)

    def test_unbound_empty_install_reports_zeroes(self):
        report = STATUS_REPORT.collect(str(self.config))
        self.assertEqual(report["binding"], "unbound")
        self.assertEqual(report["database"], "absent")
        self.assertEqual(report["pdf_files"], 0)
        self.assertEqual(report["tasks_succeeded"], 0)


if __name__ == "__main__":
    unittest.main()
