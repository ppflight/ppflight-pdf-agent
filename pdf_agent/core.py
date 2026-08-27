"""Security-focused implementation of the PPFlight PDF Agent protocol.

The agent deliberately has a very small trust boundary: ADMIN can select a
previously configured renderer, but it never gets to supply a command, URL,
path, or HTML to execute.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import select
import signal
import shutil
import sqlite3
import socketserver
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import socket
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, BinaryIO, Dict, Iterable, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

VERSION = "1.0.3"
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_DOWNLOAD_NAME_RE = re.compile(r"^PPFlight-[A-Za-z0-9][A-Za-z0-9._-]{0,127}\.pdf$")
_TOKEN_RE = re.compile(r"^[A-Za-z0-9._~-]{32,4096}$")
_FONT_SHA256 = "c16259db764b4f0a9c72e64fd1bc6ad1059c67c8c67b129b0f9d0cc62e0ef497"
_ALLOWED_TASK_KEYS = {
    "render": {"id", "type", "artifact_id", "revision", "snapshot", "snapshot_sha256", "download_filename"},
    "invalidate": {"id", "type", "artifact_id", "revision"},
}
_RENDERER_DIAGNOSTIC_CODES = frozenset({
    "renderer_artifact_failed",
    "renderer_cache_failed",
    "renderer_dependencies_failed",
    "renderer_exit_failed",
    "renderer_input_failed",
    "renderer_input_rejected",
    "renderer_internal_failed",
    "renderer_invalid_pdf",
    "renderer_invalid_report",
    "renderer_output_overflow",
    "renderer_render_failed",
    "renderer_start_failed",
    "renderer_timed_out",
})
# This is deliberately the complete set of task-failure values that may reach
# stderr (and therefore systemd's journal).  Do not derive log text from an
# exception, task, renderer output, or other runtime data.
TASK_DIAGNOSTIC_CODES = _RENDERER_DIAGNOSTIC_CODES | frozenset({"task_processing_failed"})
_RENDERER_EXIT_MARKERS = {
    b"PPFLIGHT_RENDERER_ERROR=artifact": "renderer_artifact_failed",
    b"PPFLIGHT_RENDERER_ERROR=cache": "renderer_cache_failed",
    b"PPFLIGHT_RENDERER_ERROR=dependencies": "renderer_dependencies_failed",
    b"PPFLIGHT_RENDERER_ERROR=input": "renderer_input_rejected",
    b"PPFLIGHT_RENDERER_ERROR=internal": "renderer_internal_failed",
    b"PPFLIGHT_RENDERER_ERROR=render": "renderer_render_failed",
}


class AgentError(Exception):
    """An expected, safe-to-report agent failure."""


class RendererError(AgentError):
    """A renderer failure with a locally safe, fixed diagnostic code."""

    def __init__(self, code: str):
        self.code = code if code in _RENDERER_DIAGNOSTIC_CODES else "renderer_exit_failed"
        super().__init__(self.code)


def _task_diagnostic_code(error: AgentError) -> str:
    """Map every task error to the strict local diagnostic allow-list."""
    if isinstance(error, RendererError):
        return error.code
    return "task_processing_failed"


def _write_task_diagnostic(code: str) -> None:
    """Write a fixed event for journald without exposing task or error data."""
    if code not in TASK_DIAGNOSTIC_CODES:
        code = "task_processing_failed"
    print("ppflight-pdf-agent: task_failed=" + code, file=sys.stderr, flush=True)


class AdminConflict(AgentError):
    """ADMIN reports that another effective Agent already exists (HTTP 409)."""


class AdminAuthenticationError(AgentError):
    """ADMIN explicitly rejected the bound Agent credential (HTTP 401)."""


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> bytes:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise ValueError("invalid base64url")
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def canonical_json(value: Any) -> bytes:
    """Return the unique JSON representation used for snapshots and grants."""
    _ensure_json_depth(value)
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                          allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc:
        raise AgentError("value is not canonical JSON") from exc


def canonical_json_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def _ensure_json_depth(value: Any, maximum: int = 64) -> None:
    """Reject adversarial nesting without recursively walking the value."""
    stack = [(value, 1)]
    while stack:
        current, depth = stack.pop()
        if depth > maximum:
            raise AgentError("JSON nesting exceeds limit")
        if isinstance(current, dict):
            stack.extend((item, depth + 1) for item in current.values())
        elif isinstance(current, list):
            stack.extend((item, depth + 1) for item in current)


def _decode_json(value: Any, error: str) -> Any:
    try:
        decoded = json.loads(value)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, TypeError) as exc:
        raise AgentError(error) from exc
    _ensure_json_depth(decoded)
    return decoded


def _absolute(path: str, name: str) -> Path:
    item = Path(path)
    if not item.is_absolute():
        raise AgentError("%s must be an absolute path" % name)
    return item.resolve()


@dataclass(frozen=True)
class AgentConfig:
    admin_url: str
    artifact_dir: Path
    state_path: Path
    db_path: Path
    cache_dir: Path
    download_audience: str
    download_port: int = 9760
    load1_max: float = 2.0
    mem_available_min: int = 2 * 1024 * 1024 * 1024
    disk_free_min: int = 1024 * 1024 * 1024
    heartbeat_seconds: int = 30
    poll_interval_seconds: int = 5
    request_timeout_seconds: int = 20
    renderer_timeout_seconds: int = 90
    pdf_max_bytes: int = 10 * 1024 * 1024

    @classmethod
    def load(cls, file_name: str) -> "AgentConfig":
        try:
            with open(file_name, "rb") as source:
                content = source.read(65537)
        except OSError as exc:
            raise AgentError("cannot read JSON configuration") from exc
        if len(content) > 65536:
            raise AgentError("JSON configuration exceeds limit")
        raw = _decode_json(content, "cannot read JSON configuration")
        if not isinstance(raw, dict):
            raise AgentError("configuration must be a JSON object")
        required = {"admin_url", "artifact_dir", "state_path", "db_path", "cache_dir",
                    "download_audience"}
        permitted = required | {"download_port", "load1_max", "mem_available_min",
                                "disk_free_min",
                                "heartbeat_seconds", "poll_interval_seconds", "request_timeout_seconds",
                                "renderer_timeout_seconds", "pdf_max_bytes"}
        if set(raw) - permitted or required - set(raw):
            raise AgentError("configuration has missing or unknown keys")
        if not isinstance(raw["admin_url"], str):
            raise AgentError("admin_url must be a credential-free HTTPS URL")
        try:
            parsed = urlparse(raw["admin_url"])
            parsed_port = parsed.port
        except ValueError as exc:
            raise AgentError("admin_url must be a credential-free HTTPS URL") from exc
        if (parsed.scheme != "https" or not parsed.netloc or not parsed.hostname or parsed.username or parsed.password
                or parsed.query or parsed.fragment or parsed_port is not None and not 1 <= parsed_port <= 65535):
            raise AgentError("admin_url must be a credential-free HTTPS URL")
        audience = raw["download_audience"]
        if not isinstance(audience, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", audience):
            raise AgentError("download_audience is required")
        try:
            config = cls(
                admin_url=raw["admin_url"].rstrip("/"), artifact_dir=_absolute(raw["artifact_dir"], "artifact_dir"),
                state_path=_absolute(raw["state_path"], "state_path"), db_path=_absolute(raw["db_path"], "db_path"),
                cache_dir=_absolute(raw["cache_dir"], "cache_dir"), download_audience=audience,
                **{k: raw[k] for k in permitted - required if k in raw},
            )
        except (TypeError, ValueError) as exc:
            raise AgentError("configuration contains an invalid value") from exc
        integers = (config.download_port, config.mem_available_min, config.disk_free_min, config.heartbeat_seconds,
                    config.poll_interval_seconds, config.request_timeout_seconds,
                    config.renderer_timeout_seconds, config.pdf_max_bytes)
        if any(isinstance(item, bool) or not isinstance(item, int) for item in integers):
            raise AgentError("configuration limits must be numeric")
        if isinstance(config.load1_max, bool) or not isinstance(config.load1_max, (int, float)):
            raise AgentError("configuration limits must be numeric")
        if (not 1 <= config.download_port <= 65535 or config.load1_max < 0
                or config.mem_available_min < 0 or config.disk_free_min < 0):
            raise AgentError("configuration contains an out-of-range value")
        if min(config.heartbeat_seconds, config.poll_interval_seconds, config.request_timeout_seconds,
               config.renderer_timeout_seconds, config.pdf_max_bytes) <= 0:
            raise AgentError("configuration durations and limits must be positive")
        if not 2 <= config.poll_interval_seconds <= 30:
            raise AgentError("poll_interval_seconds must be between 2 and 30")
        return config


class StateStore:
    def __init__(self, config: AgentConfig):
        self.config = config
        config.artifact_dir.mkdir(parents=True, exist_ok=True)
        config.cache_dir.mkdir(parents=True, exist_ok=True)
        config.state_path.parent.mkdir(parents=True, exist_ok=True)
        config.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(config.db_path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        with self.lock, self.db:
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("""CREATE TABLE IF NOT EXISTS artifacts (
                artifact_id TEXT PRIMARY KEY, revision INTEGER NOT NULL, status TEXT NOT NULL,
                filename TEXT, download_filename TEXT, sha256 TEXT, size INTEGER NOT NULL DEFAULT 0, updated_at INTEGER NOT NULL)""")
            self.db.execute("""CREATE TABLE IF NOT EXISTS completions (
                task_id TEXT PRIMARY KEY, task_fingerprint TEXT NOT NULL, success INTEGER NOT NULL,
                result_json TEXT NOT NULL, delivered INTEGER NOT NULL DEFAULT 0, completed_at INTEGER NOT NULL)""")
            artifact_columns = {row["name"] for row in self.db.execute("PRAGMA table_info(artifacts)")}
            if "download_filename" not in artifact_columns:
                self.db.execute("ALTER TABLE artifacts ADD COLUMN download_filename TEXT")
            completion_columns = {row["name"] for row in self.db.execute("PRAGMA table_info(completions)")}
            if "delivered" not in completion_columns:
                self.db.execute("ALTER TABLE completions ADD COLUMN delivered INTEGER NOT NULL DEFAULT 0")
            if "task_fingerprint" not in completion_columns:
                self.db.execute("ALTER TABLE completions ADD COLUMN task_fingerprint TEXT NOT NULL DEFAULT ''")

    def close(self) -> None:
        with self.lock:
            self.db.close()

    def read_binding(self) -> Optional[Dict[str, Any]]:
        try:
            with open(self.config.state_path, "rb") as source:
                content = source.read(65537)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise AgentError("binding state is unreadable") from exc
        if len(content) > 65536:
            raise AgentError("binding state is unreadable")
        data = _decode_json(content, "binding state is unreadable")
        expected = {"agent_id", "agent_uuid", "agent_token", "download_hmac_key", "bound_at"}
        if (not isinstance(data, dict) or set(data) != expected or not isinstance(data["agent_id"], str)
                or not isinstance(data["agent_uuid"], str) or not isinstance(data["agent_token"], str)
                or not isinstance(data["download_hmac_key"], str) or not _TOKEN_RE.fullmatch(data["agent_token"])):
            raise AgentError("binding state is invalid")
        try:
            key = _b64decode(data["download_hmac_key"])
            uuid.UUID(data["agent_uuid"])
        except (ValueError, AttributeError) as exc:
            raise AgentError("binding state is invalid") from exc
        if len(key) < 32:
            raise AgentError("binding state has an unsafe download key")
        return data

    def write_binding(self, binding: Dict[str, Any]) -> None:
        agent_id, agent_uuid = binding.get("agent_id"), binding.get("agent_uuid")
        token, key = binding.get("agent_token"), binding.get("download_hmac_key")
        if not isinstance(agent_id, str) or not _ID_RE.fullmatch(agent_id):
            raise AgentError("ADMIN returned an invalid agent_id")
        try:
            uuid.UUID(agent_uuid)
            key_bytes = _b64decode(key)
        except (ValueError, AttributeError, TypeError) as exc:
            raise AgentError("ADMIN returned invalid binding credentials") from exc
        if not isinstance(token, str) or not _TOKEN_RE.fullmatch(token) or len(key_bytes) < 32:
            raise AgentError("ADMIN returned unsafe binding credentials")
        payload = canonical_json({"agent_id": agent_id, "agent_uuid": agent_uuid, "agent_token": token,
                                  "download_hmac_key": key, "bound_at": int(time.time())})
        fd, temporary = tempfile.mkstemp(prefix=".state-", dir=str(self.config.state_path.parent))
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as target:
                target.write(payload)
                target.flush()
                os.fsync(target.fileno())
            os.replace(temporary, self.config.state_path)
            os.chmod(self.config.state_path, 0o600)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def clear_binding(self) -> None:
        """Remove invalid bearer/HMAC material after an explicit ADMIN revocation."""
        try:
            self.config.state_path.unlink()
        except FileNotFoundError:
            return
        except OSError as exc:
            raise AgentError("invalid binding state could not be removed") from exc
        # Persist the unlink where the filesystem supports directory fsync, so a
        # restart cannot resurrect a credential after a clean revoke response.
        directory_fd: Optional[int] = None
        try:
            directory_fd = os.open(self.config.state_path.parent,
                                   os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0))
            os.fsync(directory_fd)
        except OSError:
            pass
        finally:
            if directory_fd is not None:
                try:
                    os.close(directory_fd)
                except OSError:
                    pass

    def completion(self, task_id: str, fingerprint: str) -> Optional[Dict[str, Any]]:
        with self.lock:
            row = self.db.execute("SELECT task_fingerprint,result_json FROM completions WHERE task_id=?", (task_id,)).fetchone()
            if row and not hmac.compare_digest(row["task_fingerprint"], fingerprint):
                raise AgentError("task_id was reused with a different payload")
            return _decode_json(row["result_json"], "completion state is unreadable") if row else None

    def save_completion(self, task_id: str, fingerprint: str, success: bool, result: Dict[str, Any]) -> None:
        with self.lock, self.db:
            old = self.db.execute("SELECT task_fingerprint FROM completions WHERE task_id=?", (task_id,)).fetchone()
            if old:
                if not hmac.compare_digest(old["task_fingerprint"], fingerprint):
                    raise AgentError("task_id was reused with a different payload")
                return
            self.db.execute("INSERT INTO completions(task_id,task_fingerprint,success,result_json,completed_at) VALUES(?,?,?,?,?)",
                            (task_id, fingerprint, int(success), canonical_json(result).decode("utf-8"), int(time.time())))

    def pending_completions(self) -> Iterable[sqlite3.Row]:
        with self.lock:
            return self.db.execute("SELECT task_id,result_json FROM completions WHERE delivered=0 ORDER BY completed_at,task_id").fetchall()

    def mark_delivered(self, task_id: str) -> None:
        with self.lock, self.db:
            self.db.execute("UPDATE completions SET delivered=1 WHERE task_id=?", (task_id,))

    def prune_completions(self, now: Optional[int] = None) -> None:
        cutoff = (int(time.time()) if now is None else now) - 30 * 86400
        with self.lock, self.db:
            self.db.execute("DELETE FROM completions WHERE delivered=1 AND completed_at < ?", (cutoff,))
            self.db.execute("""DELETE FROM completions WHERE task_id IN (
                SELECT task_id FROM completions WHERE delivered=1 ORDER BY completed_at DESC LIMIT -1 OFFSET 10000)""")

    def ready_artifact(self, artifact_id: str, revision: int) -> Optional[sqlite3.Row]:
        with self.lock:
            return self.db.execute("SELECT * FROM artifacts WHERE artifact_id=? AND revision=? AND status='ready'",
                                   (artifact_id, revision)).fetchone()

    def artifact_file(self, artifact_id: str, revision: int) -> Optional[Tuple[BinaryIO, int, str]]:
        """Return a verified artifact on the same FD that will be served.

        Resolving and hashing a pathname before opening it for the HTTP response
        leaves a replacement race.  Keep the descriptor open from fstat/hash
        verification through response streaming instead.
        """
        row = self.ready_artifact(artifact_id, revision)
        if not row or not row["filename"]:
            return None
        candidate = (self.config.artifact_dir / row["filename"]).resolve()
        if candidate.parent != self.config.artifact_dir or not candidate.is_file():
            return None
        fd: Optional[int] = None
        try:
            flags = os.O_RDONLY
            flags |= getattr(os, "O_CLOEXEC", 0)
            # O_NOFOLLOW is unavailable on a few supported Python/platform
            # combinations. The resolved-parent check above remains the
            # fallback there; on Linux deployments, refuse a final symlink.
            flags |= getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(candidate, flags)
            details = os.fstat(fd)
            if not stat.S_ISREG(details.st_mode) or details.st_size != int(row["size"]):
                return None
            digest = hashlib.sha256()
            while True:
                block = os.read(fd, 65536)
                if not block:
                    break
                digest.update(block)
            if not isinstance(row["sha256"], str) or not hmac.compare_digest(digest.hexdigest(), row["sha256"]):
                return None
            name = row["download_filename"]
            if not isinstance(name, str) or not _DOWNLOAD_NAME_RE.fullmatch(name):
                return None
            os.lseek(fd, 0, os.SEEK_SET)
            source = os.fdopen(fd, "rb")
            fd = None  # source now owns the descriptor.
            return source, int(row["size"]), name
        except OSError:
            return None
        finally:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass

    def mark_ready(self, artifact_id: str, revision: int, filename: str, download_filename: str, sha256: str, size: int) -> None:
        if not _DOWNLOAD_NAME_RE.fullmatch(download_filename):
            raise AgentError("unsafe download filename")
        with self.lock, self.db:
            old = self.db.execute("SELECT revision,filename FROM artifacts WHERE artifact_id=?", (artifact_id,)).fetchone()
            if old and old["revision"] > revision:
                raise AgentError("stale artifact revision")
            self.db.execute("""INSERT INTO artifacts(artifact_id,revision,status,filename,download_filename,sha256,size,updated_at)
                VALUES(?,?, 'ready',?,?,?,?,?) ON CONFLICT(artifact_id) DO UPDATE SET
                revision=excluded.revision,status='ready',filename=excluded.filename,download_filename=excluded.download_filename,sha256=excluded.sha256,
                size=excluded.size,updated_at=excluded.updated_at""",
                (artifact_id, revision, filename, download_filename, sha256, size, int(time.time())))
        if old and old["filename"] and old["filename"] != filename:
            old_path = (self.config.artifact_dir / old["filename"]).resolve()
            if old_path.parent == self.config.artifact_dir:
                try:
                    old_path.unlink()
                except FileNotFoundError:
                    pass

    def revoke(self, artifact_id: str, revision: int) -> None:
        with self.lock, self.db:
            old = self.db.execute("SELECT revision,filename FROM artifacts WHERE artifact_id=?", (artifact_id,)).fetchone()
            if old and old["revision"] > revision:
                raise AgentError("stale artifact revision")
            self.db.execute("""INSERT INTO artifacts(artifact_id,revision,status,filename,download_filename,sha256,size,updated_at)
                VALUES(?,?, 'revoked',NULL,NULL,NULL,0,?) ON CONFLICT(artifact_id) DO UPDATE SET
                revision=excluded.revision,status='revoked',filename=NULL,download_filename=NULL,sha256=NULL,size=0,updated_at=excluded.updated_at""",
                (artifact_id, revision, int(time.time())))
        if old and old["filename"]:
            path = (self.config.artifact_dir / old["filename"]).resolve()
            if path.parent == self.config.artifact_dir:
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass

    def summary(self) -> Dict[str, int]:
        """Operational counts only: safe to include in a heartbeat, never identifiers or PII."""
        with self.lock:
            artifacts = self.db.execute("SELECT status,COUNT(*) AS count FROM artifacts GROUP BY status").fetchall()
            completions = self.db.execute("SELECT success,COUNT(*) AS count FROM completions GROUP BY success").fetchall()
        answer = {"artifacts_ready": 0, "artifacts_revoked": 0, "tasks_succeeded": 0, "tasks_failed": 0}
        for row in artifacts:
            if row["status"] == "ready":
                answer["artifacts_ready"] = int(row["count"])
            elif row["status"] == "revoked":
                answer["artifacts_revoked"] = int(row["count"])
        for row in completions:
            answer["tasks_succeeded" if row["success"] else "tasks_failed"] = int(row["count"])
        return answer


class AdminClient:
    """JSON-only HTTPS client with redirects disabled."""
    def __init__(self, config: AgentConfig):
        self.config = config
        class NoRedirect(HTTPRedirectHandler):
            def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
                return None
        self.opener = build_opener(NoRedirect())

    def _url(self, path: str) -> str:
        if not path.startswith("/") or "?" in path or "#" in path:
            raise AgentError("unsafe ADMIN protocol path")
        return self.config.admin_url + path

    def post(self, path: str, data: Dict[str, Any], token: Optional[str] = None,
             expected_status: Tuple[int, ...] = (200,)) -> Dict[str, Any]:
        headers = {"Content-Type": "application/json", "Accept": "application/json",
                   "Cache-Control": "no-store", "User-Agent": "ppflight-pdf-agent/%s" % VERSION}
        if token:
            headers["Authorization"] = "Bearer " + token
        request = Request(self._url(path), data=canonical_json(data), method="POST",
                          headers=headers)
        try:
            with self.opener.open(request, timeout=self.config.request_timeout_seconds) as response:
                if response.geturl() != request.full_url:
                    raise AgentError("ADMIN redirect refused")
                if response.status not in expected_status:
                    raise AgentError("unexpected ADMIN response")
                if response.headers.get_content_type().lower() != "application/json":
                    raise AgentError("ADMIN response must use application/json")
                body = response.read(1024 * 1024 + 1)
        except HTTPError as exc:
            # Laravel's authenticated control API uses 401 for a revoked or
            # invalid Agent token. A public 403 can originate at Cloudflare/WAF
            # and must remain retryable rather than deleting valid credentials.
            if exc.code == 401:
                raise AdminAuthenticationError("ADMIN rejected the bound Agent credential (HTTP %s)" % exc.code) from exc
            if exc.code == 409:
                try:
                    detail = _decode_json(exc.read(65536), "ADMIN returned invalid conflict JSON")
                except (AgentError, OSError):
                    detail = {}
                if isinstance(detail, dict) and detail.get("code") == "pdf_agent_already_bound":
                    raise AdminConflict("ADMIN rejected binding: pdf_agent_already_bound; only one effective PDF Agent is permitted (HTTP 409)") from exc
                raise AdminConflict("ADMIN rejected binding: only one effective PDF Agent is permitted (HTTP 409)") from exc
            raise AgentError("ADMIN request failed (HTTP %s)" % exc.code) from exc
        except (URLError, OSError) as exc:
            raise AgentError("ADMIN HTTPS request failed") from exc
        if len(body) > 1024 * 1024:
            raise AgentError("ADMIN response exceeds limit")
        value = _decode_json(body, "ADMIN returned invalid JSON")
        if not isinstance(value, dict):
            raise AgentError("ADMIN response must be an object")
        if "data" in value:
            if not isinstance(value["data"], dict):
                raise AgentError("ADMIN response data must be an object")
            return value["data"]
        return value


def require_admin_ok(answer: Dict[str, Any], operation: str) -> None:
    """A 2xx response alone is never proof that an ADMIN state change succeeded."""
    if not isinstance(answer, dict) or answer.get("ok") is not True:
        raise AgentError("ADMIN %s did not explicitly succeed" % operation)


def validate_task(value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict) or not isinstance(value.get("type"), str):
        raise AgentError("task must be a JSON object")
    task_type = value["type"]
    if task_type not in _ALLOWED_TASK_KEYS or set(value) != _ALLOWED_TASK_KEYS[task_type]:
        raise AgentError("task schema is invalid")
    if not all(isinstance(value.get(k), str) and _ID_RE.fullmatch(value[k]) for k in ("id", "artifact_id")):
        raise AgentError("task has invalid identifiers")
    if isinstance(value["revision"], bool) or not isinstance(value["revision"], int) or value["revision"] < 0:
        raise AgentError("task has invalid revision")
    if task_type == "render":
        if (not isinstance(value["snapshot"], dict) or not isinstance(value["snapshot_sha256"], str)
                or not _SHA_RE.fullmatch(value["snapshot_sha256"])
                or not isinstance(value["download_filename"], str)
                or not _DOWNLOAD_NAME_RE.fullmatch(value["download_filename"])):
            raise AgentError("render task schema is invalid")
        snapshot_bytes = canonical_json(value["snapshot"])
        if len(snapshot_bytes) > 512 * 1024:
            raise AgentError("snapshot exceeds input limit")
        if not hmac.compare_digest(hashlib.sha256(snapshot_bytes).hexdigest(), value["snapshot_sha256"]):
            raise AgentError("snapshot SHA256 mismatch")
    return value


def low_load(config: AgentConfig) -> bool:
    try:
        load_one = os.getloadavg()[0]
        available = 0
        with open("/proc/meminfo", encoding="ascii") as source:
            for line in source:
                if line.startswith("MemAvailable:"):
                    available = int(line.split()[1]) * 1024
                    break
        free_disk = shutil.disk_usage(config.artifact_dir).free
        return (load_one <= config.load1_max and available >= config.mem_available_min
                and free_disk >= config.disk_free_min)
    except (OSError, ValueError, IndexError):
        return False


class Renderer:
    def __init__(self, config: AgentConfig):
        self.config = config
        self.root = Path(__file__).resolve().parent.parent
        self.script = (self.root / "renderer" / "bin" / "render.php").resolve()
        php_candidate = shutil.which("php", path="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin")
        self.php = os.path.realpath(php_candidate) if php_candidate else None
        font = (self.root / "renderer" / "assets" / "PPFlightSansSC-Regular.ttf").resolve()
        if (not self.php or not os.path.isabs(self.php) or not os.access(self.php, os.X_OK)
                or not self.script.is_file() or not font.is_file()):
            raise AgentError("fixed PHP renderer installation is unavailable")
        with open(font, "rb") as source:
            self.font_hash = hashlib.sha256(source.read()).hexdigest()
        if not hmac.compare_digest(self.font_hash, _FONT_SHA256):
            raise AgentError("fixed PPFlight font checksum mismatch")
        self.environment = {"PPFLIGHT_REQUIRE_CJK_FONT": "1", "PPFLIGHT_CJK_FONT_SHA256": _FONT_SHA256}

    @staticmethod
    def _exit_diagnostic(output: bytes) -> str:
        """Classify the renderer's bounded output without logging it."""
        return _RENDERER_EXIT_MARKERS.get(output.strip(), "renderer_exit_failed")

    def render(self, snapshot: Dict[str, Any], artifact_id: str, revision: int) -> Tuple[str, str, int]:
        # Snapshot is data, never a command argument or a file path controlled by ADMIN.
        try:
            snapshot_bytes = canonical_json(snapshot)
        except AgentError as exc:
            raise RendererError("renderer_input_rejected") from exc
        if len(snapshot_bytes) > 512 * 1024:
            raise RendererError("renderer_input_rejected")
        try:
            descriptor, output_name = tempfile.mkstemp(prefix=".render-", suffix=".pdf", dir=str(self.config.artifact_dir))
            os.close(descriptor)
            os.unlink(output_name)
        except OSError as exc:
            raise RendererError("renderer_artifact_failed") from exc
        final_name = "PPFlight-%s-%s.pdf" % (artifact_id, revision)
        final_path = (self.config.artifact_dir / final_name).resolve()
        if final_path.parent != self.config.artifact_dir:
            raise RendererError("renderer_artifact_failed")
        writer: Optional[threading.Thread] = None
        process: Optional[subprocess.Popen] = None
        try:
            # This argv is intentionally not configurable.  The renderer accepts canonical JSON only on stdin.
            argv = [self.php, "-d", "memory_limit=256M", str(self.script), "--output", output_name,
                    "--cache-dir", str(self.config.cache_dir)]
            try:
                process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                           shell=False, close_fds=True, start_new_session=True, cwd=str(self.root),
                                           env=self.environment)
            except OSError as exc:
                raise RendererError("renderer_start_failed") from exc
            writer_error = []
            def write_stdin() -> None:
                try:
                    if process.stdin:
                        process.stdin.write(snapshot_bytes)
                        process.stdin.close()
                except (BrokenPipeError, OSError, ValueError) as exc:
                    writer_error.append(exc)
            writer = threading.Thread(target=write_stdin, daemon=True)
            writer.start()
            output = bytearray()
            deadline = time.monotonic() + self.config.renderer_timeout_seconds
            try:
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise subprocess.TimeoutExpired(argv, self.config.renderer_timeout_seconds)
                    readable, _, _ = select.select([process.stdout], [], [], min(remaining, 0.2))
                    if not readable:
                        if process.poll() is not None:
                            break
                        continue
                    chunk = os.read(process.stdout.fileno(), 8193 - len(output)) if process.stdout else b""
                    output.extend(chunk)
                    if len(output) > 8192:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait()
                        raise RendererError("renderer_output_overflow")
                    if not chunk:
                        break
                if process.wait(timeout=max(0.1, deadline - time.monotonic())) != 0:
                    raise RendererError(self._exit_diagnostic(bytes(output)))
            except subprocess.TimeoutExpired as exc:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
                raise RendererError("renderer_timed_out") from exc
            writer.join(timeout=1)
            if writer.is_alive() or writer_error:
                raise RendererError("renderer_input_failed")
            try:
                try:
                    report = _decode_json(bytes(output), "renderer did not return a JSON report")
                except AgentError as exc:
                    raise RendererError("renderer_invalid_report") from exc
                if (not isinstance(report, dict) or set(report) != {"ok", "sha256", "size_bytes"}
                        or report["ok"] is not True or not isinstance(report["sha256"], str)
                        or not _SHA_RE.fullmatch(report["sha256"]) or isinstance(report["size_bytes"], bool)
                        or not isinstance(report["size_bytes"], int)):
                    raise RendererError("renderer_invalid_report")
                size = os.path.getsize(output_name)
                if size < 5 or size > self.config.pdf_max_bytes:
                    raise RendererError("renderer_invalid_pdf")
                with open(output_name, "rb") as produced:
                    if produced.read(5) != b"%PDF-":
                        raise RendererError("renderer_invalid_pdf")
                    produced.seek(0)
                    digest = hashlib.sha256()
                    while True:
                        block = produced.read(65536)
                        if not block:
                            break
                        digest.update(block)
                digest_value = digest.hexdigest()
                if report["size_bytes"] != size or not hmac.compare_digest(report["sha256"], digest_value):
                    raise RendererError("renderer_invalid_report")
                os.replace(output_name, final_path)
                return final_name, digest_value, size
            except OSError as exc:
                raise RendererError("renderer_artifact_failed") from exc
        finally:
            if process is not None and process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    pass
            if writer is not None:
                writer.join(timeout=1)
            if process is not None:
                for stream in (process.stdin, process.stdout):
                    if stream is not None and not stream.closed:
                        stream.close()
            for item in (output_name,):
                try:
                    os.unlink(item)
                except FileNotFoundError:
                    pass


class Agent:
    def __init__(self, config: AgentConfig):
        self.config, self.store, self.admin, self.renderer = config, StateStore(config), AdminClient(config), Renderer(config)
        self._next_heartbeat = 0.0

    def close(self) -> None:
        self.store.close()

    def bind(self, binding_code: str, replace: bool = False) -> str:
        old = self.store.read_binding()
        if old and not replace:
            raise AgentError("a local binding already exists; use --replace only after intentionally replacing it")
        if not isinstance(binding_code, str) or not binding_code or len(binding_code) > 200:
            raise AgentError("a non-empty one-time binding code is required")
        # Local state is not altered until ADMIN accepts this binding.
        hostname = re.sub(r"[^A-Za-z0-9.-]", "-", socket.gethostname().encode("ascii", "ignore").decode("ascii"))[:63].strip(".-") or "pdf-agent"
        answer = self.admin.post("/agents/bind", {
            "binding_code": binding_code,
            "identity": {"hostname": hostname},
            "capabilities": {"binding_mode": "singleton", "concurrency": 1,
                             "task_types": ["render", "invalidate"]},
            "protocol_version": 1,
        }, expected_status=(200, 201))
        required = {"agent_id", "agent_uuid", "agent_token", "download_hmac_key", "binding_slot", "singleton"}
        if (set(answer) != required
                or answer.get("binding_slot") != "primary" or answer.get("singleton") is not True):
            raise AgentError("ADMIN bind response schema is invalid")
        self.store.write_binding(answer)
        return answer["agent_id"]

    def binding(self) -> Dict[str, Any]:
        state = self.store.read_binding()
        if not state:
            raise AgentError("agent is not bound; run bind first")
        return state

    def _complete(self, agent_id: str, task_id: str, result: Dict[str, Any]) -> None:
        state = self.binding()
        answer = self.admin.post("/agents/complete", {"agent_id": agent_id, "task_id": task_id, "result": result},
                                 token=state["agent_token"])
        require_admin_ok(answer, "completion")
        self.store.mark_delivered(task_id)

    def _deliver_pending(self, agent_id: str) -> None:
        """Deliver durable results before accepting any more work."""
        for item in self.store.pending_completions():
            result = _decode_json(item["result_json"], "completion state is unreadable")
            if not isinstance(result, dict):
                raise AgentError("completion state is unreadable")
            self._complete(agent_id, item["task_id"], result)

    def process_task(self, raw_task: Any) -> Dict[str, Any]:
        task = validate_task(raw_task)
        fingerprint = canonical_json_sha256(task)
        already = self.store.completion(task["id"], fingerprint)
        if already is not None:
            return already
        try:
            if task["type"] == "invalidate":
                self.store.revoke(task["artifact_id"], task["revision"])
                result = {"status": "revoked", "artifact_id": task["artifact_id"], "revision": task["revision"]}
            else:
                current = self.store.ready_artifact(task["artifact_id"], task["revision"])
                if current:
                    result = {"status": "ready", "artifact_id": task["artifact_id"], "revision": task["revision"],
                              "sha256": current["sha256"], "size": current["size"]}
                else:
                    filename, sha256, size = self.renderer.render(task["snapshot"], task["artifact_id"], task["revision"])
                    self.store.mark_ready(task["artifact_id"], task["revision"], filename, task["download_filename"], sha256, size)
                    result = {"status": "ready", "artifact_id": task["artifact_id"], "revision": task["revision"],
                              "sha256": sha256, "size": size}
            self.store.save_completion(task["id"], fingerprint, True, result)
            return result
        except AgentError as exc:
            _write_task_diagnostic(_task_diagnostic_code(exc))
            result = {"status": "failed", "code": "processing_failed"}
            self.store.save_completion(task["id"], fingerprint, False, result)
            return result

    def check(self) -> Dict[str, Any]:
        state = self.binding()
        answer = self.admin.post("/agents/heartbeat", {"agent_id": state["agent_id"], "version": VERSION,
                                 "binding_mode": "singleton", "capacity": 1, "running_jobs": 0,
                                 "summary": self.store.summary()}, token=state["agent_token"])
        require_admin_ok(answer, "heartbeat")
        return {"bound": True, "agent_id": state["agent_id"], "admin_ok": True, "low_load": low_load(self.config)}

    def cycle(self) -> Optional[Dict[str, Any]]:
        state = self.binding()
        agent_id = state["agent_id"]
        if time.monotonic() >= self._next_heartbeat:
            answer = self.admin.post("/agents/heartbeat", {"agent_id": agent_id, "version": VERSION,
                                     "binding_mode": "singleton", "capacity": 1, "running_jobs": 0,
                                     "summary": self.store.summary()}, token=state["agent_token"])
            require_admin_ok(answer, "heartbeat")
            self._next_heartbeat = time.monotonic() + self.config.heartbeat_seconds
        self.store.prune_completions()
        self._deliver_pending(agent_id)
        if not low_load(self.config):
            return None
        claimed = self.admin.post("/agents/claim", {"agent_id": agent_id, "max_jobs": 1}, token=state["agent_token"])
        if set(claimed) != {"task"}:
            raise AgentError("ADMIN claim response schema is invalid")
        if claimed["task"] is None:
            return None
        task = validate_task(claimed["task"])
        result = self.process_task(task)
        self._complete(agent_id, task["id"], result)
        return result

    def run(self, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                self.cycle()
            except AdminAuthenticationError:
                try:
                    self.store.clear_binding()
                except AgentError:
                    # A failed local cleanup must not leave a revoked worker
                    # polling or serving from its still-running listener.
                    pass
                print("ppflight-pdf-agent: admin_authentication_invalid", file=sys.stderr, flush=True)
                stop.set()
                break
            except AdminConflict:
                print("ppflight-pdf-agent: admin_conflict", file=sys.stderr, flush=True)
                stop.set()
                break
            except AgentError as exc:
                message = str(exc)
                if message.startswith("ADMIN HTTPS"):
                    code = "admin_unreachable"
                elif message.startswith("ADMIN request"):
                    code = "admin_rejected"
                elif message.startswith("renderer") or message.startswith("cannot start fixed PHP"):
                    code = "renderer_failed"
                else:
                    code = "agent_error"
                print("ppflight-pdf-agent: " + code, file=sys.stderr, flush=True)
            stop.wait(self.config.poll_interval_seconds)

    def mint_download_grant(self, artifact_id: str, revision: int, expires_at: Optional[int] = None) -> str:
        if not _ID_RE.fullmatch(artifact_id) or revision < 0:
            raise AgentError("invalid artifact grant")
        now = int(time.time())
        exp = now + 300 if expires_at is None else expires_at
        if not isinstance(exp, int) or exp <= now or exp > now + 300:
            raise AgentError("grant expiry must be no more than five minutes")
        state = self.binding()
        payload = {"aud": self.config.download_audience, "artifact": artifact_id, "revision": revision,
                   "exp": exp, "agent_uuid": state["agent_uuid"]}
        encoded = _b64encode(canonical_json(payload))
        signature = _b64encode(hmac.new(_b64decode(state["download_hmac_key"]), encoded.encode("ascii"), hashlib.sha256).digest())
        return encoded + "." + signature

    def verify_download_grant(self, token: str, artifact_id: str, revision: int) -> bool:
        try:
            encoded, signature = token.split(".")
            state = self.binding()
            expected = _b64encode(hmac.new(_b64decode(state["download_hmac_key"]), encoded.encode("ascii"), hashlib.sha256).digest())
            if not hmac.compare_digest(expected, signature):
                return False
            claims = _decode_json(_b64decode(encoded), "invalid download grant")
            now = int(time.time())
            return (isinstance(claims, dict) and set(claims) == {"aud", "artifact", "revision", "exp", "agent_uuid"}
                    and isinstance(claims["aud"], str) and isinstance(claims["artifact"], str)
                    and isinstance(claims["agent_uuid"], str) and isinstance(claims["revision"], int)
                    and not isinstance(claims["revision"], bool) and isinstance(claims["exp"], int)
                    and not isinstance(claims["exp"], bool) and claims["aud"] == self.config.download_audience
                    and claims["artifact"] == artifact_id and claims["revision"] == revision
                    and claims["agent_uuid"] == state["agent_uuid"]
                    and now < claims["exp"] <= now + 300)
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError, TypeError, AgentError, RecursionError):
            return False


class DownloadServer:
    """A local-only, capability-token protected PDF server."""
    def __init__(self, agent: Agent):
        self.agent = agent
        outer = self
        class BoundedHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
            daemon_threads = True
            request_queue_size = 16
            _slots = threading.BoundedSemaphore(8)
            def process_request(self, request: Any, client_address: Any) -> None:
                if not self._slots.acquire(blocking=False):
                    request.close()
                    return
                super().process_request(request, client_address)
            def process_request_thread(self, request: Any, client_address: Any) -> None:
                try:
                    super().process_request_thread(request, client_address)
                finally:
                    self._slots.release()
            def handle_error(self, request: Any, client_address: Any) -> None:
                # Never print a traceback that could include a grant-bearing request.
                return
        class Handler(BaseHTTPRequestHandler):
            server_version = "PPFlightPDF"
            sys_version = ""
            def log_message(self, format: str, *args: Any) -> None:
                return  # query grants must never reach access logs
            def setup(self) -> None:
                super().setup()
                self.connection.settimeout(15)
            def do_GET(self) -> None: self._serve(True)
            def do_HEAD(self) -> None: self._serve(False)
            def _headers(self, status: int, length: int = 0) -> None:
                self.send_response(status)
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("X-Frame-Options", "DENY")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("Content-Security-Policy", "sandbox; default-src 'none'")
                self.send_header("Cross-Origin-Resource-Policy", "same-site")
                self.send_header("Permissions-Policy", "geolocation=(), microphone=(), camera=()")
                self.send_header("Connection", "close")
                self.send_header("Content-Length", str(length))
                self.end_headers()
            def _serve(self, body: bool) -> None:
                parsed = urlparse(self.path)
                health_host = (self.headers.get("Host") or "").lower()
                if (parsed.path == "/healthz" and not parsed.query
                        and health_host in ("127.0.0.1", "127.0.0.1:9760", "localhost", "localhost:9760")):
                    self._headers(200, 2)
                    if body: self.wfile.write(b"ok")
                    return
                match = re.fullmatch(r"/v1/download/([A-Za-z0-9][A-Za-z0-9._-]{0,127})", parsed.path)
                from urllib.parse import parse_qs
                query = parse_qs(parsed.query, keep_blank_values=True, strict_parsing=False)
                if not match or set(query) != {"grant"} or len(query["grant"]) != 1:
                    self._headers(404); return
                token = query["grant"][0]
                # Revision is authenticated inside the grant; inspect it only after signature verification.
                try:
                    claims = _decode_json(_b64decode(token.split(".")[0]), "invalid download grant")
                    revision = claims.get("revision")
                except (ValueError, json.JSONDecodeError, AttributeError, IndexError, AgentError, RecursionError):
                    self._headers(404); return
                artifact_id = match.group(1)
                if isinstance(revision, bool) or not isinstance(revision, int) or not outer.agent.verify_download_grant(token, artifact_id, revision):
                    self._headers(404); return
                item = outer.agent.store.artifact_file(artifact_id, revision)
                if not item:
                    self._headers(404); return
                source, size, download_name = item
                self.send_response(200)
                self.send_header("Content-Type", "application/pdf")
                self.send_header("Content-Disposition", 'attachment; filename="%s"' % download_name)
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("X-Frame-Options", "DENY")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("Content-Security-Policy", "sandbox; default-src 'none'")
                self.send_header("Cross-Origin-Resource-Policy", "same-site")
                self.send_header("Permissions-Policy", "geolocation=(), microphone=(), camera=()")
                self.send_header("Connection", "close")
                self.send_header("Content-Length", str(size))
                self.end_headers()
                try:
                    if body:
                        remaining = size
                        while remaining:
                            block = source.read(min(65536, remaining))
                            if not block: break
                            self.wfile.write(block)
                            remaining -= len(block)
                finally:
                    source.close()
        self.httpd = BoundedHTTPServer(("127.0.0.1", agent.config.download_port), Handler)
        self.thread: Optional[threading.Thread] = None

    def start(self) -> None:
        self.thread = threading.Thread(target=self.httpd.serve_forever, name="pdf-agent-download", daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        if self.thread: self.thread.join(timeout=5)
