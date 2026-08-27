#!/usr/bin/env python3
"""Print local PPFlight PDF Agent operational statistics without secrets."""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import stat
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pdf_agent.core import AgentConfig, AgentError, low_load  # noqa: E402


def human_bytes(value: int) -> str:
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    amount = float(value)
    for unit in units:
        if amount < 1024 or unit == units[-1]:
            return f"{int(amount)} {unit}" if unit == "B" else f"{amount:.1f} {unit}"
        amount /= 1024
    return f"{value} B"


def binding_report(path: Path) -> dict:
    try:
        with path.open("rb") as source:
            payload = source.read(65537)
    except FileNotFoundError:
        return {"binding": "unbound", "agent_uuid": None, "bound_at": None}
    if len(payload) > 65536:
        raise AgentError("binding state exceeds the safe size limit")
    try:
        data = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AgentError("binding state is unreadable") from exc
    required = {"agent_id", "agent_uuid", "agent_token", "download_hmac_key", "bound_at"}
    if (not isinstance(data, dict) or set(data) != required
            or not all(isinstance(data[key], str) for key in
                       ("agent_id", "agent_uuid", "agent_token", "download_hmac_key"))
            or isinstance(data["bound_at"], bool) or not isinstance(data["bound_at"], int)):
        raise AgentError("binding state is invalid")
    try:
        uuid.UUID(data["agent_uuid"])
        bound_at = datetime.fromtimestamp(data["bound_at"], timezone.utc).isoformat()
    except (ValueError, OSError, OverflowError) as exc:
        raise AgentError("binding state is invalid") from exc
    return {"binding": "bound", "agent_uuid": data["agent_uuid"], "bound_at": bound_at}


def database_report(path: Path) -> dict:
    result = {
        "database": "absent",
        "artifacts_ready": 0,
        "artifacts_revoked": 0,
        "tasks_succeeded": 0,
        "tasks_failed": 0,
        "tasks_awaiting_delivery": 0,
    }
    if not path.is_file() or path.is_symlink():
        return result
    uri = "file:%s?mode=ro" % quote(str(path), safe="/")
    try:
        connection = sqlite3.connect(uri, uri=True, timeout=2)
        try:
            artifact_rows = connection.execute(
                "SELECT status, COUNT(*) FROM artifacts GROUP BY status"
            ).fetchall()
            task_rows = connection.execute(
                "SELECT success, COUNT(*) FROM completions GROUP BY success"
            ).fetchall()
            pending = connection.execute(
                "SELECT COUNT(*) FROM completions WHERE delivered=0"
            ).fetchone()[0]
        finally:
            connection.close()
    except sqlite3.Error as exc:
        raise AgentError("local statistics database is unreadable") from exc
    result["database"] = "ok"
    for status_value, count in artifact_rows:
        if status_value == "ready":
            result["artifacts_ready"] = int(count)
        elif status_value == "revoked":
            result["artifacts_revoked"] = int(count)
    for success, count in task_rows:
        result["tasks_succeeded" if success else "tasks_failed"] = int(count)
    result["tasks_awaiting_delivery"] = int(pending)
    return result


def pdf_report(path: Path) -> dict:
    count = 0
    size = 0
    try:
        entries = path.iterdir()
        for entry in entries:
            if not entry.name.startswith("PPFlight-") or entry.suffix.lower() != ".pdf":
                continue
            details = os.stat(entry, follow_symlinks=False)
            if stat.S_ISREG(details.st_mode):
                count += 1
                size += details.st_size
        free = os.statvfs(path).f_bavail * os.statvfs(path).f_frsize
    except OSError as exc:
        raise AgentError("artifact directory is unreadable") from exc
    return {"pdf_files": count, "pdf_bytes": size, "artifact_disk_free": free}


def collect(config_path: str) -> dict:
    config = AgentConfig.load(config_path)
    result = binding_report(config.state_path)
    result.update(database_report(config.db_path))
    result.update(pdf_report(config.artifact_dir))
    result["generation_gate"] = "ready" if low_load(config) else "waiting_for_low_load"
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="status-report.py")
    parser.add_argument("--config", required=True)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        report = collect(args.config)
    except AgentError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(report, sort_keys=True))
        return 0
    binding_text = "已绑定" if report["binding"] == "bound" else "未绑定"
    gate_text = "可生成" if report["generation_gate"] == "ready" else "等待低负载"
    print(f"{'绑定状态':<16} {binding_text}")
    if report["agent_uuid"]:
        print(f"{'Agent UUID':<18} {report['agent_uuid']}")
        print(f"{'绑定时间（UTC）':<14} {report['bound_at']}")
    print(f"{'生成条件':<16} {gate_text}")
    print(f"{'账单记录':<16} 可用={report['artifacts_ready']} 已撤销={report['artifacts_revoked']}")
    print(f"{'PDF 文件':<17} {report['pdf_files']} 个（{human_bytes(report['pdf_bytes'])}）")
    print(f"{'任务统计':<16} 成功={report['tasks_succeeded']} 失败={report['tasks_failed']} "
          f"待回传={report['tasks_awaiting_delivery']}")
    print(f"{'PDF 磁盘可用':<15} {human_bytes(report['artifact_disk_free'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
