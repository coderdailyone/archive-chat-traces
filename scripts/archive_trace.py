#!/usr/bin/env python3
"""Archive Codex session transcripts from a Stop hook or historical backfill."""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import tempfile
import sys
from typing import Any, Dict, Iterable, List, Optional, Tuple


BUFFER_SIZE = 1024 * 1024


class ArchiveError(RuntimeError):
    pass


def default_codex_home() -> Path:
    configured = os.environ.get("CODEX_HOME")
    return Path(configured).expanduser() if configured else Path.home() / ".codex"


def default_archive_root() -> Path:
    configured = os.environ.get("CODEX_CHAT_ARCHIVE_DIR")
    return Path(configured).expanduser() if configured else Path.home() / "CodexChatArchive"


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def ensure_private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path, 0o700)


def atomic_write_json(path: Path, value: Dict[str, Any]) -> None:
    ensure_private_dir(path.parent)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=True, indent=2, sort_keys=True)
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


def sha256_file(path: Path) -> Tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(BUFFER_SIZE)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def copy_with_hash(source: Path, destination: Path) -> Tuple[str, int]:
    ensure_private_dir(destination.parent)

    for attempt in range(3):
        before = source.stat()
        digest = hashlib.sha256()
        copied = 0
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{destination.name}.", dir=str(destination.parent)
        )
        try:
            with source.open("rb") as reader, os.fdopen(fd, "wb") as writer:
                while True:
                    chunk = reader.read(BUFFER_SIZE)
                    if not chunk:
                        break
                    writer.write(chunk)
                    digest.update(chunk)
                    copied += len(chunk)
                writer.flush()
                os.fsync(writer.fileno())
            after = source.stat()
            stable = before.st_size == after.st_size and before.st_mtime_ns == after.st_mtime_ns
            if stable and copied == after.st_size:
                os.chmod(tmp_name, 0o600)
                os.replace(tmp_name, destination)
                return digest.hexdigest(), copied
        finally:
            try:
                os.unlink(tmp_name)
            except FileNotFoundError:
                pass

        if attempt == 2:
            raise ArchiveError(f"transcript changed while being copied: {source}")

    raise ArchiveError(f"unable to copy transcript: {source}")


def classify_source(source: Path, codex_home: Path) -> Tuple[str, Path]:
    resolved = source.expanduser().resolve(strict=True)
    if not resolved.is_file() or resolved.suffix != ".jsonl":
        raise ArchiveError(f"not a JSONL transcript: {resolved}")

    for bucket in ("sessions", "archived_sessions"):
        base = (codex_home / bucket).expanduser().resolve()
        if is_relative_to(resolved, base):
            return bucket, resolved.relative_to(base)

    raise ArchiveError(f"transcript is outside Codex session directories: {resolved}")


def read_metadata(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def append_manifest(path: Path, record: Dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as handle:
        os.chmod(path, 0o600)
        handle.write(json.dumps(record, ensure_ascii=True, sort_keys=True))
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())


def archive_one(
    source: Path,
    archive_root: Path,
    codex_home: Path,
    event: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    bucket, relative = classify_source(source, codex_home)
    archive_root = archive_root.expanduser().resolve()
    ensure_private_dir(archive_root)
    destination = archive_root / "transcripts" / bucket / relative
    metadata_path = destination.with_suffix(destination.suffix + ".meta.json")

    lock_path = archive_root / ".archive.lock"
    with lock_path.open("a+", encoding="utf-8") as lock:
        os.chmod(lock_path, 0o600)
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        digest, size = copy_with_hash(source.expanduser().resolve(strict=True), destination)
        previous = read_metadata(metadata_path)

        metadata: Dict[str, Any] = {
            "archive_path": str(destination.relative_to(archive_root)),
            "archived_at": utc_now(),
            "bytes": size,
            "sha256": digest,
            "source_bucket": bucket,
            "source_relative_path": str(relative),
        }
        if event:
            for key in ("hook_event_name", "model", "session_id", "turn_id"):
                value = event.get(key)
                if isinstance(value, str) and value:
                    metadata[key] = value

        atomic_write_json(metadata_path, metadata)
        changed = previous.get("sha256") != digest
        if changed:
            append_manifest(archive_root / "manifest.jsonl", metadata)

    return {"changed": changed, **metadata}


def inspect_transcript(path: Path) -> Dict[str, Any]:
    models = set()
    session_id = None
    started_at = None
    with path.open("r", encoding="utf-8") as handle:
        for raw_line in handle:
            try:
                item = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            if not isinstance(item, dict):
                continue
            item_type = item.get("type")
            payload = item.get("payload")
            if not isinstance(payload, dict):
                continue
            if item_type == "turn_context" and isinstance(payload.get("model"), str):
                models.add(payload["model"])
            elif item_type == "session_meta":
                if isinstance(payload.get("id"), str):
                    session_id = payload["id"]
                if isinstance(payload.get("timestamp"), str):
                    started_at = payload["timestamp"]
    return {
        "models": sorted(models),
        "session_id": session_id,
        "started_at": started_at,
    }


def transcript_paths(codex_home: Path) -> Iterable[Path]:
    for bucket in ("sessions", "archived_sessions"):
        base = codex_home / bucket
        if base.is_dir():
            yield from sorted(base.rglob("*.jsonl"))


def backfill(
    codex_home: Path, archive_root: Path, model_prefix: Optional[str] = None
) -> Dict[str, Any]:
    archived = 0
    unchanged = 0
    skipped = 0
    failures: List[Dict[str, str]] = []

    for source in transcript_paths(codex_home):
        try:
            details = inspect_transcript(source)
            models = details["models"]
            if model_prefix and not any(model.startswith(model_prefix) for model in models):
                skipped += 1
                continue
            event = {
                "hook_event_name": "Backfill",
                "model": ",".join(models),
                "session_id": details.get("session_id") or "",
            }
            result = archive_one(source, archive_root, codex_home, event)
            if result["changed"]:
                archived += 1
            else:
                unchanged += 1
        except (ArchiveError, OSError) as error:
            failures.append({"path": str(source), "error": str(error)})

    return {
        "archive_root": str(archive_root.expanduser().resolve()),
        "archived": archived,
        "failures": failures,
        "model_prefix": model_prefix,
        "skipped": skipped,
        "unchanged": unchanged,
    }


def archive_status(archive_root: Path) -> Dict[str, Any]:
    root = archive_root.expanduser().resolve()
    transcript_root = root / "transcripts"
    files = list(transcript_root.rglob("*.jsonl")) if transcript_root.is_dir() else []
    return {
        "archive_root": str(root),
        "bytes": sum(path.stat().st_size for path in files),
        "exists": root.is_dir(),
        "transcripts": len(files),
    }


def verify_archive(archive_root: Path) -> Dict[str, Any]:
    root = archive_root.expanduser().resolve()
    metadata_files = sorted((root / "transcripts").rglob("*.jsonl.meta.json"))
    checked = 0
    failures: List[Dict[str, str]] = []

    for metadata_path in metadata_files:
        metadata = read_metadata(metadata_path)
        relative = metadata.get("archive_path")
        if not isinstance(relative, str):
            failures.append({"path": str(metadata_path), "error": "missing archive_path"})
            continue
        transcript = (root / relative).resolve()
        if not is_relative_to(transcript, root):
            failures.append({"path": str(metadata_path), "error": "archive_path escapes root"})
            continue
        try:
            digest, size = sha256_file(transcript)
        except OSError as error:
            failures.append({"path": str(transcript), "error": str(error)})
            continue
        checked += 1
        if digest != metadata.get("sha256") or size != metadata.get("bytes"):
            failures.append({"path": str(transcript), "error": "checksum or size mismatch"})

    return {
        "archive_root": str(root),
        "checked": checked,
        "failures": failures,
        "ok": not failures,
    }


def hook_command(args: argparse.Namespace) -> int:
    try:
        event = json.load(sys.stdin)
        if not isinstance(event, dict):
            raise ArchiveError("hook input must be a JSON object")
        transcript_path = event.get("transcript_path")
        if transcript_path is None:
            return 0
        if not isinstance(transcript_path, str):
            raise ArchiveError("transcript_path must be a string or null")
        if args.model_prefix:
            model = event.get("model")
            if not isinstance(model, str) or not model.startswith(args.model_prefix):
                return 0
        archive_one(
            Path(transcript_path),
            Path(args.archive_dir),
            Path(args.codex_home),
            event,
        )
        return 0
    except (ArchiveError, OSError, json.JSONDecodeError) as error:
        message = f"Chat transcript archive failed: {error}"
        print(json.dumps({"continue": True, "systemMessage": message}))
        return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    hook = subparsers.add_parser("hook", help="Read a Codex hook event from stdin")
    hook.add_argument("--archive-dir", default=str(default_archive_root()))
    hook.add_argument("--codex-home", default=str(default_codex_home()))
    hook.add_argument("--model-prefix")

    fill = subparsers.add_parser("backfill", help="Archive existing session transcripts")
    fill.add_argument("--archive-dir", default=str(default_archive_root()))
    fill.add_argument("--codex-home", default=str(default_codex_home()))
    fill.add_argument("--model-prefix")

    status = subparsers.add_parser("status", help="Show archive statistics")
    status.add_argument("--archive-dir", default=str(default_archive_root()))

    verify = subparsers.add_parser("verify", help="Verify archived transcript checksums")
    verify.add_argument("--archive-dir", default=str(default_archive_root()))
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.command == "hook":
        return hook_command(args)
    if args.command == "backfill":
        result = backfill(Path(args.codex_home), Path(args.archive_dir), args.model_prefix)
        print(json.dumps(result, ensure_ascii=True, indent=2, sort_keys=True))
        return 1 if result["failures"] else 0
    if args.command == "status":
        print(json.dumps(archive_status(Path(args.archive_dir)), indent=2, sort_keys=True))
        return 0
    if args.command == "verify":
        result = verify_archive(Path(args.archive_dir))
        print(json.dumps(result, ensure_ascii=True, indent=2, sort_keys=True))
        return 0 if result["ok"] else 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
