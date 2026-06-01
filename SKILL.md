---
name: codex-provider-plugin-sync
description: Repair Codex Desktop after changing model providers by synchronizing all thread/session model_provider metadata, repairing project chat-record visibility, and restoring cached plugin marketplace/plugin enablement so plugins and installed skills can appear in the composer @ picker. Use when the user says they changed provider, old chats show the wrong provider, old project chats are missing, plugins or skills from another provider no longer appear under @, or asks to unify provider, plugin, skill, marketplace, session, chat history, or Codex Desktop local state.
---

# Codex Provider Plugin Sync

## Overview

Use this skill for Codex Desktop local state repair after provider changes. It combines provider metadata synchronization, project chat-record visibility and timestamp repair, and plugin configuration restoration, while avoiding unsafe global replacement of strings such as `openai-curated` or `openai-primary-runtime`.

## Workflow

1. Run a dry run first:

```bash
python3 /path/to/codex-provider-plugin-sync/scripts/sync_provider_plugins.py --dry-run
```

2. Review the output:
   - `target_provider` should be the user's current provider, usually inferred from the latest thread.
   - `changed_session_files` and `db_rows_needing_update_before` show provider metadata work.
   - `db_chat_rows_needing_update_before`, `chat_session_files_changed`, `session_index_missing_user_threads_before`, and timestamp mismatch counts show chat-record visibility/index/time work.
   - `plugin_sections_to_add` and `marketplace_sections_to_add` show config restoration work.
   - `skill_count` confirms installed global skills are discoverable from `~/.codex/skills`.

3. Explain the target provider and planned changes. The script creates backups before writing.

4. Run the real sync with approval if writing outside the workspace is required:

```bash
python3 /path/to/codex-provider-plugin-sync/scripts/sync_provider_plugins.py
```

5. Verify the final output includes:
   - `jsonl_remaining_not_<provider>=0`
   - `db_remaining_not_<provider>=0`
   - `db_remaining_empty_thread_source=0`
   - `jsonl_remaining_missing_thread_source=0`
   - `session_index_missing_user_threads_after=0`
   - `session_index_duplicate_ids_after=0`
   - `db_timestamp_remaining_mismatch=0`
   - `session_index_timestamp_remaining_mismatch=0`
   - `after_db_groups` contains only the target provider, though different models may remain.
   - `config_plugin_sections_added` is nonzero when plugin enablement was missing, or zero if already restored.

## Script

Use `scripts/sync_provider_plugins.py`.

Important options:

```bash
--provider ai                 # use a specific target provider
--codex-home ~/.codex         # override Codex home
--backup-dir /path/to/backup  # override backup destination
--dry-run                     # no writes
--skip-plugins                # provider-only sync
--skip-chat-records           # skip thread_source/session_index repair
--verify-delay 2              # wait before final verification
```

The script:

- infers the provider from the latest `threads.model_provider` unless `--provider` is passed
- backs up `state_5.sqlite` with SQLite `VACUUM INTO`
- backs up each changed session JSONL before editing
- rewrites only structured `session_meta.payload.model_provider` fields
- repairs old normal chats by setting `threads.thread_source='user'` and `session_meta.payload.thread_source='user'` when `source='vscode'`
- backs up and deduplicates `session_index.jsonl`, adding any missing unarchived user-thread entries
- restores stable history timestamps from rollout JSONL event times into `threads.updated_at(_ms)` and `session_index.updated_at`; sessions updated in the last 10 minutes are treated as active and skipped to avoid racing the current chat
- updates `threads.model_provider`
- backs up `config.toml` before adding missing marketplace/plugin enablement sections
- discovers installed plugins from local caches:
  - `~/.codex/.tmp/bundled-marketplaces/openai-bundled`
  - `~/.cache/codex-runtimes/codex-primary-runtime/plugins/openai-primary-runtime`
  - `~/.codex/plugins/cache/*`
- reports the global skill count from `~/.codex/skills`

## Guardrails

- Do not replace arbitrary `openai` text in files. Marketplace names such as `openai-bundled`, `openai-primary-runtime`, and `openai-curated` are plugin source identifiers, not model providers.
- Do not edit conversation content except JSON fields named `model_provider` and `thread_source` inside `session_meta` records.
- Do not mark subagent/guardian records as user chats; only repair records with `source='vscode'`.
- Do not delete plugin caches or skill folders as part of this repair.
- Keep backups until the user confirms that Codex UI behavior is correct.
- If verification flips back after a delay, rerun with a longer `--verify-delay` and inspect for another state source before claiming success.
