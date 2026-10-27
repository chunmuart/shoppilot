"""进程启动时的应用源文件指纹，用于评测定位被测服务版本。"""
from __future__ import annotations

import hashlib
from pathlib import Path


def app_source_fingerprint() -> str:
    root = Path(__file__).resolve().parents[1]
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.suffix in {".py", ".yml", ".yaml"}:
            digest.update(path.relative_to(root).as_posix().encode())
            digest.update(b"\0")
            digest.update(path.read_bytes())
            digest.update(b"\0")
    return digest.hexdigest()
