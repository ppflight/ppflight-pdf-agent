import hashlib
import http.client
import io
import json
import os
import tempfile
import threading
import time
import unittest
from email.message import Message
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError

import agent as agent_cli
from pdf_agent.core import (AdminAuthenticationError, AdminClient, AdminConflict, Agent, AgentConfig, AgentError,
                            DownloadServer, Renderer, RendererError, TASK_DIAGNOSTIC_CODES,
                            canonical_json_sha256, validate_task)


class AgentCoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.config = AgentConfig(
            admin_url="https://admin.example.test",
            artifact_dir=root / "artifacts", cache_dir=root / "cache", state_path=root / "state.json",
            db_path=root / "state.sqlite", download_audience="ppflight-test",
            download_port=0, load1_max=1_000_000, mem_available_min=0, disk_free_min=0,
        )
        self.agent = Agent(self.config)

    def tearDown(self):
        self.agent.close()
        self.temp.cleanup()

    def bind_local(self):
        self.agent.admin.post = Mock(return_value={"agent_id": "agent-1", "agent_uuid": "00000000-0000-4000-8000-000000000000",
                                                   "agent_token": "t" * 32, "download_hmac_key": "eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHg",
                                                   "binding_slot": "primary", "singleton": True})
        self.agent.bind("one-time-code")

    def render_task(self, task_id="job-1"):
        snapshot = {"booking": {"id": 12}, "locale": "en"}
        return {"id": task_id, "type": "render", "artifact_id": "artifact-1", "revision": 4, "download_filename": "PPFlight-confirmation.pdf",
                "snapshot": snapshot, "snapshot_sha256": canonical_json_sha256(snapshot)}

    def test_snapshot_schema_and_hash_are_strict(self):
        task = self.render_task()
        self.assertEqual(validate_task(task), task)
        task["unexpected"] = True
        with self.assertRaises(AgentError):
            validate_task(task)
        self.assertEqual(
            self.agent.renderer.font_hash,
            "c16259db764b4f0a9c72e64fd1bc6ad1059c67c8c67b129b0f9d0cc62e0ef497",
        )
        task = self.render_task()
        task["snapshot_sha256"] = "0" * 64
        with self.assertRaises(AgentError):
            validate_task(task)

    def test_binding_refuses_replacement_until_admin_success(self):
        self.bind_local()
        original = self.agent.binding()
        with self.assertRaises(AgentError):
            self.agent.bind("another-code")
        self.agent.admin.post = Mock(return_value={"agent_id": "agent-2", "agent_uuid": "00000000-0000-4000-8000-000000000002",
                                                   "agent_token": "t" * 32, "download_hmac_key": "eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHg",
                                                   "binding_slot": "secondary", "singleton": False})
        with self.assertRaises(AgentError):
            self.agent.bind("another-code", replace=True)
        self.assertEqual(self.agent.binding(), original)
        self.assertEqual(self.config.state_path.stat().st_mode & 0o777, 0o600)
        saved = self.config.state_path.read_text(encoding="utf-8")
        self.assertNotIn("another-code", saved)

    def test_bind_sends_identity_and_singleton_capability(self):
        self.bind_local()
        _, body = self.agent.admin.post.call_args.args
        self.assertEqual(body["binding_code"], "one-time-code")
        self.assertEqual(body["capabilities"]["binding_mode"], "singleton")
        self.assertEqual(body["capabilities"]["concurrency"], 1)

    def test_render_completion_and_artifact_are_idempotent(self):
        produced = self.config.artifact_dir / "artifact-1-4.pdf"
        produced.write_bytes(b"%PDF-1.7\nunit-test")
        digest = hashlib.sha256(produced.read_bytes()).hexdigest()
        self.agent.renderer.render = Mock(return_value=(produced.name, digest, produced.stat().st_size))
        one = self.agent.process_task(self.render_task())
        two = self.agent.process_task(self.render_task())
        self.assertEqual(one, two)
        self.agent.renderer.render.assert_called_once()
        item = self.agent.store.artifact_file("artifact-1", 4)
        self.assertIsNotNone(item)
        source, _, _ = item
        try:
            self.assertEqual(source.read(), produced.read_bytes())
        finally:
            source.close()

    def test_renderer_exit_output_maps_only_to_fixed_diagnostics(self):
        cases = {
            b"PPFLIGHT_RENDERER_ERROR=artifact\n": "renderer_artifact_failed",
            b"PPFLIGHT_RENDERER_ERROR=cache\n": "renderer_cache_failed",
            b"PPFLIGHT_RENDERER_ERROR=dependencies\n": "renderer_dependencies_failed",
            b"PPFLIGHT_RENDERER_ERROR=input\n": "renderer_input_rejected",
            b"PPFLIGHT_RENDERER_ERROR=internal\n": "renderer_internal_failed",
            b"PPFLIGHT_RENDERER_ERROR=render\n": "renderer_render_failed",
            b"warning\nPPFLIGHT_RENDERER_ERROR=render\n": "renderer_exit_failed",
            b"customer=Alice token=secret https://example.test/download?grant=signature\n": "renderer_exit_failed",
        }
        for output, expected in cases.items():
            with self.subTest(output=output):
                self.assertEqual(Renderer._exit_diagnostic(output), expected)
                self.assertIn(expected, TASK_DIAGNOSTIC_CODES)

    def test_renderer_failure_diagnostic_is_safe_and_failure_completion_is_idempotent(self):
        self.agent.renderer.render = Mock(side_effect=RendererError("renderer_timed_out"))
        stderr = io.StringIO()
        with patch("sys.stderr", stderr):
            first = self.agent.process_task(self.render_task())
            second = self.agent.process_task(self.render_task())
        self.assertEqual(first, {"status": "failed", "code": "processing_failed"})
        self.assertEqual(second, first)
        self.agent.renderer.render.assert_called_once()
        self.assertEqual(stderr.getvalue(), "ppflight-pdf-agent: task_failed=renderer_timed_out\n")

    def test_task_failure_never_logs_exception_text_or_sensitive_values(self):
        secret = "Alice Example token=super-secret /private/state.json https://example.test/?grant=signed-value"
        self.agent.renderer.render = Mock(side_effect=AgentError(secret))
        stderr = io.StringIO()
        with patch("sys.stderr", stderr):
            result = self.agent.process_task(self.render_task())
        self.assertEqual(result, {"status": "failed", "code": "processing_failed"})
        diagnostic = stderr.getvalue()
        self.assertEqual(diagnostic, "ppflight-pdf-agent: task_failed=task_processing_failed\n")
        self.assertTrue(diagnostic.rstrip().split("=", 1)[1] in TASK_DIAGNOSTIC_CODES)
        for unsafe in ("Alice", "super-secret", "state.json", "https://", "signed-value"):
            self.assertNotIn(unsafe, diagnostic)

    def test_fixed_renderer_writes_a_real_ppflight_prefixed_pdf(self):
        vendor = Path(__file__).resolve().parents[1] / "renderer" / "vendor" / "autoload.php"
        if not vendor.is_file():
            self.skipTest("renderer/vendor is not installed")
        snapshot = json.loads((Path(__file__).parent / "renderer_fixture.json").read_text(encoding="utf-8"))
        filename, digest, size = self.agent.renderer.render(snapshot, "invoice-100", 1)
        produced = self.config.artifact_dir / filename
        self.assertTrue(filename.startswith("PPFlight-"))
        self.assertNotIn("PPFlight Cloud", filename)
        self.assertEqual(produced.read_bytes()[:5], b"%PDF-")
        self.assertEqual(hashlib.sha256(produced.read_bytes()).hexdigest(), digest)
        self.assertEqual(produced.stat().st_size, size)

    def test_fixed_renderer_rejects_invalid_snapshot_with_input_diagnostic(self):
        vendor = Path(__file__).resolve().parents[1] / "renderer" / "vendor" / "autoload.php"
        if not vendor.is_file():
            self.skipTest("renderer/vendor is not installed")
        with self.assertRaises(RendererError) as failure:
            self.agent.renderer.render({}, "invoice-invalid", 1)
        self.assertEqual(failure.exception.code, "renderer_input_rejected")

    def test_same_task_id_with_different_canonical_payload_is_rejected(self):
        produced = self.config.artifact_dir / "artifact-1-4.pdf"
        produced.write_bytes(b"%PDF-1.7\nunit-test")
        digest = hashlib.sha256(produced.read_bytes()).hexdigest()
        self.agent.renderer.render = Mock(return_value=(produced.name, digest, produced.stat().st_size))
        task = self.render_task()
        self.agent.process_task(task)
        changed = self.render_task()
        changed["snapshot"] = {"booking": {"id": 13}, "locale": "en"}
        changed["snapshot_sha256"] = canonical_json_sha256(changed["snapshot"])
        with self.assertRaises(AgentError):
            self.agent.process_task(changed)

    def test_download_requires_agent_bound_short_lived_capability(self):
        self.bind_local()
        pdf = self.config.artifact_dir / "artifact-1-4.pdf"
        pdf.write_bytes(b"%PDF-1.7\nunit-test")
        self.agent.store.mark_ready("artifact-1", 4, pdf.name, "PPFlight-confirmation.pdf", hashlib.sha256(pdf.read_bytes()).hexdigest(), pdf.stat().st_size)
        server = DownloadServer(self.agent)
        server.start()
        try:
            token = self.agent.mint_download_grant("artifact-1", 4)
            port = server.httpd.server_port
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
            connection.request("GET", "/v1/download/artifact-1?grant=" + token)
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertEqual(response.getheader("Cache-Control"), "no-store")
            self.assertEqual(response.getheader("Content-Disposition"), 'attachment; filename="PPFlight-confirmation.pdf"')
            self.assertEqual(response.read(), pdf.read_bytes())
            connection.close()
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
            connection.request("HEAD", "/v1/download/artifact-1?grant=" + token)
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertEqual(response.read(), b"")
            bad = token[:-1] + ("A" if token[-1] != "A" else "B")
            connection.close()
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
            connection.request("GET", "/v1/download/artifact-1?grant=" + bad)
            self.assertEqual(connection.getresponse().status, 404)
            connection.close()
        finally:
            server.close()

    def test_expired_or_cross_agent_grants_are_rejected(self):
        self.bind_local()
        with self.assertRaises(AgentError):
            self.agent.mint_download_grant("artifact-1", 1, int(time.time()) + 301)
        token = self.agent.mint_download_grant("artifact-1", 1)
        state = self.agent.binding()
        state["agent_uuid"] = "00000000-0000-4000-8000-000000000001"
        self.agent.store.write_binding(state)
        self.assertFalse(self.agent.verify_download_grant(token, "artifact-1", 1))

    def test_config_requires_https_and_fixed_renderer_protocol(self):
        root = Path(self.temp.name)
        values = {"admin_url": "http://invalid.test", "artifact_dir": str(root / "a"),
                  "cache_dir": str(root / "c"), "state_path": str(root / "s"), "db_path": str(root / "d"),
                  "download_audience": "a"}
        name = root / "agent.json"
        name.write_text(json.dumps(values), encoding="utf-8")
        with self.assertRaises(AgentError):
            AgentConfig.load(str(name))
        values["admin_url"] = "https://valid.test"
        values["download_hmac_key"] = "x" * 32
        name.write_text(json.dumps(values), encoding="utf-8")
        with self.assertRaises(AgentError):
            AgentConfig.load(str(name))

    def test_pending_completion_is_redelivered_with_bearer_before_claim(self):
        self.bind_local()
        self.agent.store.save_completion("job-1", "a" * 64, True, {"status": "ready"})
        calls = []
        def post(path, data, token=None, **kwargs):
            calls.append((path, data, token))
            return {"task": None} if path == "/agents/claim" else {"ok": True}
        self.agent.admin.post = Mock(side_effect=post)
        self.agent.cycle()
        self.assertEqual([call[0] for call in calls], ["/agents/heartbeat", "/agents/complete", "/agents/claim"])
        self.assertTrue(all(call[2] == "t" * 32 for call in calls))
        self.assertEqual(list(self.agent.store.pending_completions()), [])

    def test_pending_failed_completion_is_redelivered_before_claim(self):
        self.bind_local()
        failed = {"status": "failed", "code": "processing_failed"}
        self.agent.store.save_completion("job-failed", "b" * 64, False, failed)
        calls = []

        def post(path, data, token=None, **kwargs):
            calls.append((path, data, token))
            return {"task": None} if path == "/agents/claim" else {"ok": True}

        self.agent.admin.post = Mock(side_effect=post)
        self.agent.cycle()
        self.assertEqual([call[0] for call in calls], ["/agents/heartbeat", "/agents/complete", "/agents/claim"])
        self.assertEqual(calls[1][1]["result"], failed)
        self.assertEqual(list(self.agent.store.pending_completions()), [])

    def test_check_rejects_heartbeat_without_explicit_success(self):
        self.bind_local()
        for response in ({"ok": False}, {}):
            with self.subTest(response=response):
                self.agent.admin.post = Mock(return_value=response)
                with self.assertRaises(AgentError):
                    self.agent.check()

    def test_admin_response_requires_application_json_content_type(self):
        class Response:
            status = 200
            headers = Message()
            def __enter__(self): return self
            def __exit__(self, *unused): return False
            def geturl(self): return "https://admin.example.test/agents/heartbeat"
            def read(self, _limit): return b'{"ok":true}'
        Response.headers["Content-Type"] = "text/plain"
        class Opener:
            def open(self, _request, timeout): return Response()
        client = AdminClient(self.config)
        client.opener = Opener()
        with self.assertRaises(AgentError):
            client.post("/agents/heartbeat", {})

    def test_only_admin_401_is_distinct_from_retryable_http_failures(self):
        class Opener:
            def __init__(self, status): self.status = status
            def open(self, request, timeout):
                raise HTTPError(request.full_url, self.status, "rejected", Message(), io.BytesIO(b"{}"))

        client = AdminClient(self.config)
        client.opener = Opener(401)
        with self.assertRaises(AdminAuthenticationError):
            client.post("/agents/heartbeat", {})
        for status in (403, 503):
            with self.subTest(status=status):
                client.opener = Opener(status)
                with self.assertRaises(AgentError) as failure:
                    client.post("/agents/heartbeat", {})
                self.assertNotIsInstance(failure.exception, AdminAuthenticationError)

    def test_admin_response_rejects_excessive_json_nesting(self):
        class Response:
            status = 200
            headers = Message()
            def __enter__(self): return self
            def __exit__(self, *unused): return False
            def geturl(self): return "https://admin.example.test/agents/heartbeat"
            def read(self, _limit): return (b'{"x":' * 10000) + b'0' + (b'}' * 10000)
        Response.headers["Content-Type"] = "application/json"
        class Opener:
            def open(self, _request, timeout): return Response()
        client = AdminClient(self.config)
        client.opener = Opener()
        with self.assertRaises(AgentError):
            client.post("/agents/heartbeat", {})

    def test_admin_conflict_stops_worker_loop(self):
        stop = threading.Event()
        self.agent.cycle = Mock(side_effect=AdminConflict("conflict"))
        with patch("sys.stderr", io.StringIO()):
            self.agent.run(stop)
        self.assertTrue(stop.is_set())
        self.agent.cycle.assert_called_once()

    def test_authentication_revocation_clears_binding_stops_worker_and_rejects_old_grant(self):
        self.bind_local()
        pdf = self.config.artifact_dir / "artifact-1-4.pdf"
        original = b"%PDF-original"
        pdf.write_bytes(original)
        self.agent.store.mark_ready("artifact-1", 4, pdf.name, "PPFlight-test.pdf",
                                    hashlib.sha256(original).hexdigest(), len(original))
        grant = self.agent.mint_download_grant("artifact-1", 4)
        server = DownloadServer(self.agent)
        server.start()
        stop = threading.Event()
        self.agent.admin.post = Mock(side_effect=AdminAuthenticationError("ADMIN rejected the bound Agent credential (HTTP 401)"))
        try:
            with patch("sys.stderr", io.StringIO()):
                self.agent.run(stop)
            self.assertTrue(stop.is_set())
            self.assertFalse(self.config.state_path.exists())
            # The CLI closes this listener in its run-finally path. Even before
            # that teardown completes, its old grant cannot read an artifact.
            connection = http.client.HTTPConnection("127.0.0.1", server.httpd.server_port, timeout=3)
            connection.request("GET", "/v1/download/artifact-1?grant=" + grant)
            self.assertEqual(connection.getresponse().status, 404)
            connection.close()
        finally:
            server.close()

    def test_403_5xx_and_network_failures_keep_binding_for_retry(self):
        self.bind_local()
        for failure in (
            AgentError("ADMIN request failed (HTTP 403)"),
            AgentError("ADMIN request failed (HTTP 503)"),
            AgentError("ADMIN HTTPS request failed"),
        ):
            with self.subTest(failure=str(failure)):
                self.agent.admin.post = Mock(side_effect=failure)
                with self.assertRaises(AgentError):
                    self.agent.cycle()
                self.assertTrue(self.config.state_path.exists())

    def test_failed_completion_response_stays_pending_and_prevents_claim(self):
        self.bind_local()
        self.agent.store.save_completion("job-1", "a" * 64, True, {"status": "ready"})
        calls = []
        def post(path, data, token=None, **kwargs):
            calls.append(path)
            return {"ok": True} if path == "/agents/heartbeat" else {"ok": False}
        self.agent.admin.post = Mock(side_effect=post)
        with self.assertRaises(AgentError):
            self.agent.cycle()
        self.assertEqual(calls, ["/agents/heartbeat", "/agents/complete"])
        self.assertEqual(len(list(self.agent.store.pending_completions())), 1)

    def test_new_revision_removes_superseded_pdf_and_old_revision_is_unavailable(self):
        old = self.config.artifact_dir / "artifact-1-1.pdf"
        new = self.config.artifact_dir / "artifact-1-2.pdf"
        old.write_bytes(b"%PDF-old")
        new.write_bytes(b"%PDF-new")
        self.agent.store.mark_ready("artifact-1", 1, old.name, "PPFlight-old.pdf", hashlib.sha256(old.read_bytes()).hexdigest(), old.stat().st_size)
        self.agent.store.mark_ready("artifact-1", 2, new.name, "PPFlight-new.pdf", hashlib.sha256(new.read_bytes()).hexdigest(), new.stat().st_size)
        self.assertFalse(old.exists())
        self.assertIsNone(self.agent.store.artifact_file("artifact-1", 1))
        item = self.agent.store.artifact_file("artifact-1", 2)
        self.assertIsNotNone(item)
        source, _, _ = item
        try:
            self.assertEqual(source.read(), new.read_bytes())
        finally:
            source.close()

    def test_download_rejects_tampered_artifact_and_public_health_host(self):
        self.bind_local()
        pdf = self.config.artifact_dir / "artifact-1-4.pdf"
        pdf.write_bytes(b"%PDF-original")
        self.agent.store.mark_ready("artifact-1", 4, pdf.name, "PPFlight-test.pdf",
                                    hashlib.sha256(pdf.read_bytes()).hexdigest(), pdf.stat().st_size)
        pdf.write_bytes(b"%PDF-tampered-and-longer")
        self.assertIsNone(self.agent.store.artifact_file("artifact-1", 4))
        pdf.write_bytes(b"%PDF-original")
        pdf.write_bytes(b"%PDF-corrupt!")  # same byte length, different digest
        self.assertIsNone(self.agent.store.artifact_file("artifact-1", 4))
        server = DownloadServer(self.agent)
        server.start()
        try:
            conn = http.client.HTTPConnection("127.0.0.1", server.httpd.server_port, timeout=2)
            conn.request("GET", "/healthz", headers={"Host": "pdf-worker.ppflight.com"})
            self.assertEqual(conn.getresponse().status, 404)
            conn.close()
        finally:
            server.close()

    def test_download_streams_the_verified_fd_after_same_size_path_replacement(self):
        """An atomic path replacement after verification cannot change the response."""
        self.bind_local()
        pdf = self.config.artifact_dir / "artifact-1-4.pdf"
        original = b"%PDF-original"
        replacement_contents = b"%PDF-replaced"
        self.assertEqual(len(original), len(replacement_contents))
        pdf.write_bytes(original)
        self.agent.store.mark_ready("artifact-1", 4, pdf.name, "PPFlight-test.pdf",
                                    hashlib.sha256(original).hexdigest(), len(original))
        verified_artifact_file = self.agent.store.artifact_file

        def replace_after_verification(artifact_id, revision):
            item = verified_artifact_file(artifact_id, revision)
            replacement = self.config.artifact_dir / "replacement.pdf"
            replacement.write_bytes(replacement_contents)
            os.replace(replacement, pdf)
            return item

        server = DownloadServer(self.agent)
        server.start()
        try:
            token = self.agent.mint_download_grant("artifact-1", 4)
            with patch.object(self.agent.store, "artifact_file", side_effect=replace_after_verification):
                connection = http.client.HTTPConnection("127.0.0.1", server.httpd.server_port, timeout=3)
                connection.request("GET", "/v1/download/artifact-1?grant=" + token)
                response = connection.getresponse()
                self.assertEqual(response.status, 200)
                self.assertEqual(response.read(), original)
                connection.close()
            self.assertEqual(pdf.read_bytes(), replacement_contents)
        finally:
            server.close()

    def test_concurrent_download_lookup_and_revision_updates_are_serialized(self):
        self.bind_local()
        first = self.config.artifact_dir / "artifact-1-1.pdf"
        first.write_bytes(b"%PDF-first")
        self.agent.store.mark_ready("artifact-1", 1, first.name, "PPFlight-first.pdf", hashlib.sha256(first.read_bytes()).hexdigest(), first.stat().st_size)
        server = DownloadServer(self.agent)
        server.start()
        failures = []
        token = self.agent.mint_download_grant("artifact-1", 1)
        def download() -> None:
            try:
                for _ in range(12):
                    conn = http.client.HTTPConnection("127.0.0.1", server.httpd.server_port, timeout=2)
                    conn.request("GET", "/v1/download/artifact-1?grant=" + token)
                    self.assertIn(conn.getresponse().status, (200, 404))
                    conn.close()
            except Exception as exc:  # Thread assertion failures must reach the test runner.
                failures.append(exc)
        workers = [threading.Thread(target=download) for _ in range(3)]
        try:
            for worker in workers: worker.start()
            for revision in range(2, 11, 2):
                next_file = self.config.artifact_dir / ("artifact-1-%s.pdf" % revision)
                next_file.write_bytes(b"%PDF-new")
                self.agent.store.mark_ready("artifact-1", revision, next_file.name, "PPFlight-next.pdf", hashlib.sha256(next_file.read_bytes()).hexdigest(), next_file.stat().st_size)
                self.agent.store.revoke("artifact-1", revision + 1)
            for worker in workers: worker.join()
            self.assertEqual(failures, [])
        finally:
            server.close()

    def test_cli_reads_binding_code_from_stdin(self):
        fake_agent = Mock()
        fake_agent.bind.return_value = "agent-1"
        with patch.object(agent_cli, "AgentConfig") as config, patch.object(agent_cli, "Agent", return_value=fake_agent), \
             patch("sys.stdin", io.StringIO("stdin-secret\n")), patch("sys.stdout", io.StringIO()):
            config.load.return_value = object()
            self.assertEqual(agent_cli.main(["--config", "/unused", "bind", "--code-stdin"]), 0)
        fake_agent.bind.assert_called_once_with("stdin-secret", False)


if __name__ == "__main__":
    unittest.main()
