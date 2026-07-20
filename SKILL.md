---
name: archive-chat-traces
description: Install, manage, verify, or remove automatic local archiving of Codex chat transcripts. Use when a user wants every Codex chat saved by default, wants to backfill existing GPT model sessions such as gpt-5.6, inspect archive status, verify checksums, change the archive directory, or uninstall the chat-archive hook without deleting archived data.
---

# Archive Chat Traces

Use the bundled scripts to preserve Codex session JSONL files independently of
the normal session and archived-session directories. The installed `Stop` hook
updates the archive after every completed agent turn.

## Install

Run:

```bash
python3 scripts/install.py install --backfill
```

The default archive directory is `~/CodexChatArchive`. Override it with
`--archive-dir /absolute/path`. To backfill only a model family while still
archiving every future chat, add `--backfill-model-prefix gpt-5.6`.

Tell the user to restart Codex and approve the new hook once through `/hooks`.
Do not bypass or modify Codex's hook trust store.

## Manage

Inspect configuration and archive statistics:

```bash
python3 scripts/install.py status
python3 scripts/archive_trace.py status
```

Verify every archived transcript against its SHA-256 sidecar:

```bash
python3 scripts/archive_trace.py verify
```

Backfill again safely; existing destinations are atomically refreshed:

```bash
python3 scripts/archive_trace.py backfill
```

Filter a historical backfill without filtering future hook events:

```bash
python3 scripts/archive_trace.py backfill --model-prefix gpt-5.6
```

## Uninstall

Remove only this skill's hook and preserve other hooks and archived data:

```bash
python3 scripts/install.py uninstall
```

Delete archived data only when the user explicitly requests the exact target.

## Boundaries

- Archive only transcript paths beneath `$CODEX_HOME/sessions` or
  `$CODEX_HOME/archived_sessions`.
- Never copy `auth.json`, `.env`, credentials, or the full `$CODEX_HOME` tree.
- Preserve the whole session when any historical turn matches a model filter;
  sessions can switch models.
- Treat transcripts as sensitive. Keep archive directories and files private.
- A transcript includes recorded messages, tool evidence, metadata, and returned
  reasoning summaries. It cannot expose private hidden chain-of-thought.
- `codex exec --ephemeral` has no persisted transcript and cannot be archived.
- The transcript JSONL format is useful archival data but is not a stable public
  integration schema. Preserve raw files and build derived indexes separately.
