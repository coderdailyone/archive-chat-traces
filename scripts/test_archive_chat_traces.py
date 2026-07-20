#!/usr/bin/env python3

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPT_DIR = Path(__file__).resolve().parent
ARCHIVER = SCRIPT_DIR / "archive_trace.py"
INSTALLER = SCRIPT_DIR / "install.py"


def write_transcript(codex_home: Path, model: str = "gpt-5.6-sol") -> Path:
    path = codex_home / "sessions/2026/07/20/rollout-test-session.jsonl"
    path.parent.mkdir(parents=True)
    records = [
        {
            "type": "session_meta",
            "payload": {"id": "test-session", "timestamp": "2026-07-20T00:00:00Z"},
        },
        {"type": "turn_context", "payload": {"model": model}},
        {"type": "event_msg", "payload": {"type": "user_message", "message": "hi"}},
        {
            "type": "event_msg",
            "payload": {"type": "agent_message", "message": "hello"},
        },
    ]
    path.write_text("".join(json.dumps(item) + "\n" for item in records), encoding="utf-8")
    return path


class ArchiveChatTracesTest(unittest.TestCase):
    def run_script(self, script: Path, *args: str, stdin: str | None = None, env=None):
        return subprocess.run(
            [sys.executable, str(script), *args],
            input=stdin,
            text=True,
            capture_output=True,
            check=False,
            env=env,
        )

    def test_backfill_and_verify(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            codex_home = root / "codex"
            archive = root / "archive"
            source = write_transcript(codex_home)

            result = self.run_script(
                ARCHIVER,
                "backfill",
                "--codex-home",
                str(codex_home),
                "--archive-dir",
                str(archive),
                "--model-prefix",
                "gpt-5.6",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            summary = json.loads(result.stdout)
            self.assertEqual(summary["archived"], 1)

            destination = archive / "transcripts/sessions/2026/07/20/rollout-test-session.jsonl"
            self.assertEqual(destination.read_bytes(), source.read_bytes())
            self.assertEqual(
                hashlib.sha256(destination.read_bytes()).hexdigest(),
                json.loads(
                    destination.with_suffix(".jsonl.meta.json").read_text(encoding="utf-8")
                )["sha256"],
            )

            verified = self.run_script(
                ARCHIVER, "verify", "--archive-dir", str(archive)
            )
            self.assertEqual(verified.returncode, 0, verified.stdout + verified.stderr)
            self.assertTrue(json.loads(verified.stdout)["ok"])

    def test_hook_rejects_paths_outside_codex_home_without_failing_turn(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            outside = root / "outside.jsonl"
            outside.write_text("{}\n", encoding="utf-8")
            event = {
                "hook_event_name": "Stop",
                "model": "gpt-5.6-sol",
                "session_id": "test-session",
                "transcript_path": str(outside),
            }
            result = self.run_script(
                ARCHIVER,
                "hook",
                "--codex-home",
                str(root / "codex"),
                "--archive-dir",
                str(root / "archive"),
                stdin=json.dumps(event),
            )
            self.assertEqual(result.returncode, 0)
            self.assertIn("systemMessage", json.loads(result.stdout))

    def test_install_is_idempotent_and_preserves_other_hooks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            codex_home = root / "codex"
            archive = root / "archive"
            codex_home.mkdir()
            existing = {
                "hooks": {
                    "Stop": [
                        {
                            "hooks": [
                                {"type": "command", "command": "/usr/bin/true"}
                            ]
                        }
                    ]
                }
            }
            (codex_home / "hooks.json").write_text(
                json.dumps(existing), encoding="utf-8"
            )

            for _ in range(2):
                result = self.run_script(
                    INSTALLER,
                    "install",
                    "--codex-home",
                    str(codex_home),
                    "--archive-dir",
                    str(archive),
                )
                self.assertEqual(result.returncode, 0, result.stderr)

            config = json.loads((codex_home / "hooks.json").read_text(encoding="utf-8"))
            commands = [
                handler["command"]
                for group in config["hooks"]["Stop"]
                for handler in group["hooks"]
            ]
            self.assertEqual(commands.count("/usr/bin/true"), 1)
            self.assertEqual(
                sum("archive-chat-traces/scripts/archive_trace.py" in cmd for cmd in commands),
                1,
            )

            removed = self.run_script(
                INSTALLER, "uninstall", "--codex-home", str(codex_home)
            )
            self.assertEqual(removed.returncode, 0, removed.stderr)
            config = json.loads((codex_home / "hooks.json").read_text(encoding="utf-8"))
            remaining = [
                handler["command"]
                for group in config["hooks"]["Stop"]
                for handler in group["hooks"]
            ]
            self.assertEqual(remaining, ["/usr/bin/true"])


if __name__ == "__main__":
    unittest.main()
