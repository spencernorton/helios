"""Locate and inspect the Kimi Code CLI used for native ACP sessions."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from helios.backend.process.env_scrub import scrubbed_child_env


class KimiBinaryNotFound(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class KimiBinary:
    path: Path

    def __str__(self) -> str:
        return str(self.path)


def _is_runnable(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


def find_kimi_binary() -> KimiBinary:
    override = os.environ.get("HELIOS_KIMI_BINARY", "").strip()
    if override:
        path = Path(override).expanduser()
        if _is_runnable(path):
            return KimiBinary(path.resolve())
        raise KimiBinaryNotFound(f"HELIOS_KIMI_BINARY is not executable: {override}")
    candidates = []
    on_path = shutil.which("kimi")
    if on_path:
        candidates.append(Path(on_path))
    candidates.append(Path.home() / ".local" / "bin" / "kimi")
    for path in candidates:
        if _is_runnable(path):
            return KimiBinary(path.resolve())
    raise KimiBinaryNotFound(
        "Kimi Code CLI is not installed. Install `kimi`, then sign in with `kimi login`."
    )


_VERSION_RE = re.compile(r"(\d+\.\d+\.\d+)")


def kimi_version(binary: KimiBinary | None = None) -> str:
    resolved = binary or find_kimi_binary()
    try:
        proc = subprocess.run(
            [str(resolved.path), "--version"], capture_output=True, text=True,
            timeout=10, env=scrubbed_child_env(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    match = _VERSION_RE.search(proc.stdout or proc.stderr)
    return match.group(1) if proc.returncode == 0 and match else ""
