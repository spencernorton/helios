from __future__ import annotations

import os
from pathlib import Path

import pytest

from helios.backend.kimi_binary import KimiBinaryNotFound, find_kimi_binary


def test_explicit_binary_must_be_executable(tmp_path, monkeypatch):
    missing = tmp_path / "missing-kimi"
    monkeypatch.setenv("HELIOS_KIMI_BINARY", str(missing))
    with pytest.raises(KimiBinaryNotFound, match="HELIOS_KIMI_BINARY"):
        find_kimi_binary()


def test_explicit_binary_wins(tmp_path, monkeypatch):
    binary = tmp_path / "kimi"
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o700)
    monkeypatch.setenv("HELIOS_KIMI_BINARY", str(binary))
    assert find_kimi_binary().path == binary.resolve()
    assert os.access(Path(find_kimi_binary().path), os.X_OK)
