"""Optional bounded LOC measurement using cloc, never language-byte conversion."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
from typing import Any, Dict


class LocMeasurementError(RuntimeError):
    """Raised when the explicit optional cloc feature cannot run safely."""


def _count_files(root: Path, maximum_files: int) -> int:
    count = 0
    for path in root.rglob("*"):
        if ".git" in path.parts:
            continue
        if path.is_file():
            count += 1
            if count > maximum_files:
                raise LocMeasurementError(
                    f"repository exceeds bounded LOC input limit of {maximum_files} files"
                )
    return count


def measure_loc(repository_directory: Path, *, maximum_files: int = 20_000) -> Dict[str, Any]:
    """Run cloc against a local checkout only when the executable is installed."""
    if not repository_directory.is_dir():
        raise LocMeasurementError(f"not a local repository directory: {repository_directory}")
    cloc = shutil.which("cloc")
    if cloc is None:
        raise LocMeasurementError("cloc is not installed; optional LOC analysis was not run")
    file_count = _count_files(repository_directory, maximum_files)
    completed = subprocess.run(
        [cloc, "--json", "--quiet", "--by-file", str(repository_directory)],
        check=False,
        capture_output=True,
        text=True,
        timeout=300,
    )
    if completed.returncode not in (0, 1):
        raise LocMeasurementError(f"cloc failed with exit code {completed.returncode}: {completed.stderr.strip()}")
    try:
        parsed = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise LocMeasurementError("cloc produced invalid JSON") from error
    return {
        "metric": "cloc-code-lines",
        "repository_directory": str(repository_directory),
        "input_file_count": file_count,
        "maximum_files": maximum_files,
        "cloc": parsed,
    }
