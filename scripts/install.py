#!/usr/bin/env python3
"""Install or remove the archive-chat-traces user-level Codex Stop hook."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from pathlib import Path
import shlex
import shutil
import sys
import tempfile
from typing import Any, Dict, List

import archive_trace


SCRIPT_PATH = (Path(__file__).resolve().parent / "archive_trace.py").resolve()


def default_codex_home() -> Path:
    configured = os.environ.get("CODEX_HOME")
    return Path(configured).expanduser() if configured else Path.home() / ".codex"


def load_hooks(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {
            "description": "User-level Codex lifecycle hooks.",
            "hooks": {},
        }
    try:
        with path.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"refusing to modify invalid JSON at {path}: {error}") from error
    if not isinstance(value, dict):
        raise RuntimeError(f"refusing to modify non-object JSON at {path}")
    hooks = value.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise RuntimeError(f"'hooks' must be an object in {path}")
    return value


def backup(path: Path) -> Path | None:
    if not path.exists():
        return None
    timestamp = dt.datetime.now().strftime("%Y%m%dT%H%M%S")
    target = path.with_name(f"{path.name}.backup-{timestamp}")
    shutil.copy2(path, target)
    os.chmod(target, 0o600)
    return target


def atomic_write(path: Path, value: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, path)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


def is_our_handler(handler: Any) -> bool:
    if not isinstance(handler, dict) or handler.get("type") != "command":
        return False
    command = handler.get("command")
    if not isinstance(command, str):
        return False
    normalized = command.replace("\\", "/")
    return "archive-chat-traces/scripts/archive_trace.py" in normalized


def handler_for(archive_dir: Path, codex_home: Path) -> Dict[str, Any]:
    python = Path("/usr/bin/python3")
    if not python.exists():
        python = Path(sys.executable).resolve()
    command_parts = [
        str(python),
        str(SCRIPT_PATH),
        "hook",
        "--archive-dir",
        str(archive_dir.expanduser().resolve()),
        "--codex-home",
        str(codex_home.expanduser().resolve()),
    ]
    return {
        "type": "command",
        "command": " ".join(shlex.quote(part) for part in command_parts),
        "timeout": 30,
        "statusMessage": "Archiving chat transcript",
    }


def remove_handlers(config: Dict[str, Any]) -> int:
    hooks = config["hooks"]
    groups = hooks.get("Stop", [])
    if not isinstance(groups, list):
        raise RuntimeError("hooks.Stop must be an array")

    removed = 0
    retained_groups: List[Any] = []
    for group in groups:
        if not isinstance(group, dict):
            retained_groups.append(group)
            continue
        handlers = group.get("hooks")
        if not isinstance(handlers, list):
            retained_groups.append(group)
            continue
        retained = [handler for handler in handlers if not is_our_handler(handler)]
        removed += len(handlers) - len(retained)
        if retained:
            updated = dict(group)
            updated["hooks"] = retained
            retained_groups.append(updated)

    if retained_groups:
        hooks["Stop"] = retained_groups
    else:
        hooks.pop("Stop", None)
    return removed


def install(args: argparse.Namespace) -> int:
    codex_home = Path(args.codex_home).expanduser().resolve()
    archive_dir = Path(args.archive_dir).expanduser().resolve()
    hooks_path = codex_home / "hooks.json"
    config = load_hooks(hooks_path)
    removed = remove_handlers(config)
    config["hooks"].setdefault("Stop", []).append(
        {"hooks": [handler_for(archive_dir, codex_home)]}
    )

    previous = hooks_path.read_text(encoding="utf-8") if hooks_path.exists() else None
    rendered = json.dumps(config, ensure_ascii=True, indent=2) + "\n"
    backup_path = None
    if previous != rendered:
        backup_path = backup(hooks_path)
        atomic_write(hooks_path, config)

    archive_trace.ensure_private_dir(archive_dir)
    result: Dict[str, Any] = {
        "archive_dir": str(archive_dir),
        "backup": str(backup_path) if backup_path else None,
        "hook_config": str(hooks_path),
        "installed": True,
        "replaced_handlers": removed,
        "trust_required": True,
    }
    exit_code = 0
    if args.backfill:
        fill = archive_trace.backfill(
            codex_home, archive_dir, args.backfill_model_prefix
        )
        result["backfill"] = fill
        if fill["failures"]:
            exit_code = 1
    print(json.dumps(result, ensure_ascii=True, indent=2, sort_keys=True))
    return exit_code


def uninstall(args: argparse.Namespace) -> int:
    codex_home = Path(args.codex_home).expanduser().resolve()
    hooks_path = codex_home / "hooks.json"
    config = load_hooks(hooks_path)
    removed = remove_handlers(config)
    backup_path = None
    if removed:
        backup_path = backup(hooks_path)
        atomic_write(hooks_path, config)
    print(
        json.dumps(
            {
                "archive_deleted": False,
                "backup": str(backup_path) if backup_path else None,
                "hook_config": str(hooks_path),
                "removed_handlers": removed,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def status(args: argparse.Namespace) -> int:
    codex_home = Path(args.codex_home).expanduser().resolve()
    hooks_path = codex_home / "hooks.json"
    config = load_hooks(hooks_path)
    groups = config["hooks"].get("Stop", [])
    installed = any(
        is_our_handler(handler)
        for group in groups
        if isinstance(group, dict)
        for handler in group.get("hooks", [])
        if isinstance(group.get("hooks"), list)
    )
    result = {
        "hook_config": str(hooks_path),
        "installed": installed,
        **archive_trace.archive_status(Path(args.archive_dir)),
    }
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if installed else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    install_parser = subparsers.add_parser("install")
    install_parser.add_argument("--codex-home", default=str(default_codex_home()))
    install_parser.add_argument(
        "--archive-dir", default=str(archive_trace.default_archive_root())
    )
    install_parser.add_argument("--backfill", action="store_true")
    install_parser.add_argument("--backfill-model-prefix")

    uninstall_parser = subparsers.add_parser("uninstall")
    uninstall_parser.add_argument("--codex-home", default=str(default_codex_home()))

    status_parser = subparsers.add_parser("status")
    status_parser.add_argument("--codex-home", default=str(default_codex_home()))
    status_parser.add_argument(
        "--archive-dir", default=str(archive_trace.default_archive_root())
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.command == "install":
        return install(args)
    if args.command == "uninstall":
        return uninstall(args)
    if args.command == "status":
        return status(args)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
