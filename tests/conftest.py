from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

FAKE_HADD = """\
#!/usr/bin/env python3
import sys

positional = [a for a in sys.argv[1:] if not a.startswith("-")]
out, inputs = positional[0], sorted(positional[1:])
names = set()
for path in inputs:
    with open(path) as f:
        names.update(line for line in f.read().split("\\n") if line)
with open(out, "w") as f:
    f.write("\\n".join(sorted(names)))
"""


@pytest.fixture
def fake_hadd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Install a stub ``hadd`` executable that records its inputs in the output."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "hadd"
    script.write_text(FAKE_HADD)
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ.get("PATH", ""))
    return script


@pytest.fixture
def make_root_files(tmp_path_factory):
    """Factory fixture creating N empty .root placeholder files."""

    def _make(directory: Path, count: int) -> list[Path]:
        directory.mkdir(parents=True, exist_ok=True)
        files = [directory / f"file_{i:03d}.root" for i in range(count)]
        for f in files:
            f.write_text(f"{f.name}\n")
        return files

    return _make
