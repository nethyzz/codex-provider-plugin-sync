---
name: codex-provider-plugin-sync
description: Repair Codex Desktop after changing model providers by synchronizing all thread/session model_provider metadata to the current provider and restoring cached plugin marketplace/plugin enablement so plugins and installed skills can appear in the composer @ picker. Use when the user says they changed provider, old chats show the wrong provider, plugins or skills from another provider no longer appear under @, or asks to unify provider, plugin, skill, marketplace, session, or Codex Desktop local state.
---

# Codex Provider Plugin Sync

## Overview

Use this skill for Codex Desktop local state repair after provider changes. It combines provider metadata synchronization with plugin configuration restoration, while avoiding unsafe global replacement of strings such as `openai-curated` or `openai-primary-runtime`.

## Workflow

1. Run a dry run first:

```bash
python3 /path/to/codex-provider-plugin-sync/scripts/sync_provider_plugins.py --dry-run
```

2. Review the output:
   - `target_provider` should be the user's current provider, usually inferred from the latest thread.
   - `changed_session_files` and `db_rows_needing_update_before` show provider metadata work.
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
--verify-delay 2              # wait before final verification
```

The script:

- infers the provider from the latest `threads.model_provider` unless `--provider` is passed
- backs up `state_5.sqlite` with SQLite `VACUUM INTO`
- backs up each changed session JSONL before editing
- rewrites only structured `session_meta.payload.model_provider` fields
- updates `threads.model_provider`
- backs up `config.toml` before adding missing marketplace/plugin enablement sections
- discovers installed plugins from local caches:
  - `~/.codex/.tmp/bundled-marketplaces/openai-bundled`
  - `~/.cache/codex-runtimes/codex-primary-runtime/plugins/openai-primary-runtime`
  - `~/.codex/plugins/cache/*`
- reports the global skill count from `~/.codex/skills`

## Guardrails

- Do not replace arbitrary `openai` text in files. Marketplace names such as `openai-bundled`, `openai-primary-runtime`, and `openai-curated` are plugin source identifiers, not model providers.
- Do not edit conversation content except JSON fields named `model_provider` inside `session_meta` records.
- Do not delete plugin caches or skill folders as part of this repair.
- Keep backups until the user confirms that Codex UI behavior is correct.
- If verification flips back after a delay, rerun with a longer `--verify-delay` and inspect for another state source before claiming success.
