#!/usr/bin/env python3
"""Fail closed if a local configuration weakens packaging invariants."""
import json
import sys
from pathlib import Path


def fail(message: str) -> int:
    print(f"ppflight-pdf-agent configuration rejected: {message}", file=sys.stderr)
    return 2


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        return fail("usage: verify-runtime-config.py CONFIG ARTIFACT_DIR")
    config_path, artifact_dir = map(Path, argv[1:])
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return fail("configuration must be readable JSON")
    if not isinstance(config, dict):
        return fail("configuration must be an object")
    if config.get("download_port") != 9760:
        return fail("download_port must remain 9760")
    try:
        configured_artifacts = Path(config["artifact_dir"]).resolve(strict=False)
    except (KeyError, TypeError):
        return fail("artifact_dir is missing")
    if configured_artifacts != artifact_dir.resolve(strict=False):
        return fail("artifact_dir differs from the unit's private artifact path")
    for key in ("state_path", "db_path", "cache_dir"):
        try:
            item = Path(config[key]).resolve(strict=False)
            item.relative_to(Path("/var/lib/ppflight-pdf-agent"))
        except (KeyError, TypeError, ValueError):
            return fail(f"{key} must remain under /var/lib/ppflight-pdf-agent")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
