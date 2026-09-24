#!/usr/bin/env python3
"""Install a versioned Engineering skill without replacing user modifications."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile


SOURCE = Path(__file__).resolve().parents[1]
MANIFEST = ".installation.json"
START = "<!-- engineering-agents:start -->"
END = "<!-- engineering-agents:end -->"
BLOCK = f"""{START}
## Engineering Agents

For software development in this project, read and use
`.agents/skills/engineering/SKILL.md` and its relevant references.
Use this project-local version if a personal copy is also installed.
Apply its delegation criteria for architecture, implementation, independent review
and optional QA. Respect project-specific instructions and the user's task scope.
{END}
"""


def reject_symlinks(path):
    """Do not follow a destination link into files outside the selected directory."""
    path = Path(os.path.abspath(path))
    for part in (path,) + tuple(path.parents):
        if part.is_symlink():
            raise ValueError(f"Refusing symlink destination: {part}")


def payload(directory):
    files = {}
    for path in directory.rglob("*"):
        relative = path.relative_to(directory)
        if "__pycache__" in relative.parts or path.suffix in (".pyc", ".pyo"):
            continue
        if path.is_symlink():
            raise ValueError(f"Refusing symlink in skill: {path}")
        if path.is_file() and relative.as_posix() != MANIFEST:
            files[relative.as_posix()] = path.read_bytes()
    return files


def hashes(files):
    return {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}


def install_skill(source, destination, update=False):
    reject_symlinks(destination)
    if source.resolve() in destination.resolve().parents:
        raise ValueError("Installation destination must not be inside the source skill directory.")
    files = payload(source)
    version = files["VERSION"].decode("utf-8").strip()
    expected = hashes(files)
    if source.resolve() == destination.resolve():
        return version

    if destination.exists():
        if not destination.is_dir():
            raise ValueError(f"Destination is not a directory: {destination}")
        manifest = destination / MANIFEST
        if manifest.is_symlink() or not manifest.is_file():
            raise ValueError(f"Existing unmanaged installation: {destination}")
        state = json.loads(manifest.read_text(encoding="utf-8"))
        actual = hashes(payload(destination))
        if state.get("package") != "engineering-agents" or actual != state.get("files"):
            raise ValueError(f"Local changes detected; preserve and reconcile them first: {destination}")
        if actual == expected:
            return version
        if not update:
            raise ValueError("A different version is installed. Use --update for an intentional update.")

    destination.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".engineering-stage-", dir=destination.parent))
    backup = None
    try:
        for name, data in files.items():
            target = stage / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        (stage / MANIFEST).write_text(json.dumps({
            "package": "engineering-agents", "version": version, "files": expected,
        }, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        if destination.exists():
            backup = Path(tempfile.mkdtemp(prefix=".engineering-backup-", dir=destination.parent))
            backup.rmdir()
            destination.rename(backup)
        try:
            stage.rename(destination)
        except OSError:
            if backup is not None:
                backup.rename(destination)
            raise
        if backup is not None:
            shutil.rmtree(backup)
    finally:
        if stage.exists():
            shutil.rmtree(stage)
    return version


def project_instructions(path):
    reject_symlinks(path)
    original = path.read_bytes() if path.exists() else b""
    content = original.decode("utf-8")
    if START in content or END in content:
        if content.count(START) != 1 or content.count(END) != 1:
            raise ValueError("Ambiguous Engineering Agents markers in AGENTS.md; reconcile manually.")
        start = content.index(START)
        end = content.index(END) + len(END)
        if end < start or content[start:end] != BLOCK.rstrip("\n"):
            raise ValueError("Modified Engineering Agents block in AGENTS.md; reconcile manually.")
        return original
    separator = b"" if not original else (b"\n" if original.endswith(b"\n") else b"\n\n")
    return original + separator + BLOCK.encode("utf-8")


def install_project(source, root, update=False):
    root = root.resolve()
    if not root.is_dir():
        raise ValueError(f"Project directory does not exist: {root}")
    if root == source.resolve() or root in source.resolve().parents:
        # An installed project-local skill is allowed; a source checkout is not.
        local = root / ".agents" / "skills" / "engineering"
        if source.resolve() != local.resolve():
            raise ValueError("Choose an application project, not the Engineering Agents source directory.")
    instructions = root / "AGENTS.md"
    new_content = project_instructions(instructions)  # Preflight before any skill mutation.
    destination = root / ".agents" / "skills" / "engineering"
    version = install_skill(source, destination, update)
    if not instructions.exists() or instructions.read_bytes() != new_content:
        # Existing bytes are kept exactly; only a new block is appended.
        instructions.write_bytes(new_content)
    return destination, version


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    scope = parser.add_mutually_exclusive_group(required=True)
    scope.add_argument("--user", action="store_true", help="Install in ~/.agents/skills/engineering")
    scope.add_argument("--project", type=Path, help="Install a pinned copy and append AGENTS.md instructions")
    parser.add_argument("--update", action="store_true", help="Replace an unchanged managed installation")
    args = parser.parse_args(argv)
    try:
        if args.user:
            # Resolve home first (macOS /var and /tmp can themselves be system links).
            destination = Path.home().resolve() / ".agents" / "skills" / "engineering"
            version = install_skill(SOURCE, destination, args.update)
        else:
            destination, version = install_project(SOURCE, args.project, args.update)
        print(f"Engineering Agents {version} installed: {destination}")
        return 0
    except (OSError, ValueError, KeyError) as error:
        print(f"Installation stopped: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
