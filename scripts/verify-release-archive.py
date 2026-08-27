#!/usr/bin/env python3
"""Validate a release tarball before root extracts it.

Only regular files and directories under one conservative top-level directory
are permitted. Links, devices, traversal, duplicates and decompression bombs
are rejected; GNU tar may safely extract the validated archive afterwards.
"""
from __future__ import annotations

import re
import sys
import tarfile
from pathlib import PurePosixPath

MAX_MEMBERS = 10_000
MAX_FILE_BYTES = 50 * 1024 * 1024
MAX_TOTAL_BYTES = 250 * 1024 * 1024
ROOT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$")


def fail(message: str) -> "NoReturn":
    print(f"unsafe release archive: {message}", file=sys.stderr)
    raise SystemExit(2)


def main() -> int:
    if len(sys.argv) != 2:
        fail("usage: verify-release-archive.py ARCHIVE")
    roots: set[str] = set()
    names: set[str] = set()
    total = 0
    try:
        with tarfile.open(sys.argv[1], "r:gz") as archive:
            members = archive.getmembers()
            if not members or len(members) > MAX_MEMBERS:
                fail("member count is empty or excessive")
            for member in members:
                path = PurePosixPath(member.name)
                parts = path.parts
                if (not parts or path.is_absolute() or any(part in ("", ".", "..") for part in parts)
                        or "\\" in member.name or "\x00" in member.name):
                    fail("member path is not normalized")
                if not ROOT_RE.fullmatch(parts[0]):
                    fail("top-level directory name is unsafe")
                roots.add(parts[0])
                normalized = str(path)
                if normalized in names:
                    fail("duplicate member path")
                names.add(normalized)
                if not (member.isdir() or member.isfile()):
                    fail("links and special files are forbidden")
                if member.size < 0 or member.size > MAX_FILE_BYTES:
                    fail("member size exceeds limit")
                total += member.size
                if total > MAX_TOTAL_BYTES:
                    fail("expanded archive exceeds limit")
    except (OSError, tarfile.TarError) as exc:
        fail(f"cannot read gzip tarball ({exc.__class__.__name__})")
    if len(roots) != 1:
        fail("archive must contain exactly one top-level directory")
    root = next(iter(roots))
    if root not in names:
        fail("top-level directory entry is required")
    print(root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
