
# codex-provider-plugin-sync

Repair Codex Desktop after changing model providers by synchronizing provider metadata and restoring local plugin enablement.

This repository packages a Codex skill plus a standalone Python repair script. It is intentionally conservative: it updates only structured provider metadata fields and Codex plugin configuration tables, and it creates backups before writing.

## What It Fixes

After switching providers in Codex Desktop, older conversations or installed plugins may disappear from the UI because local provider metadata and plugin marketplace configuration no longer line up.

This skill handles two related repairs:

- Synchronizes `model_provider` metadata across `~/.codex/state_5.sqlite` and `~/.codex/sessions/**/*.jsonl`.
- Restores missing local marketplace and plugin enablement sections in `~/.codex/config.toml` so plugins and skills can reappear in the composer `@` picker.

Use [`codex-provider-sync`](https://github.com/Dailin521/codex-provider-sync) when you only need conversation provider metadata repair. Use this skill when provider metadata and plugin visibility both need repair.

## Files

- `SKILL.md` - Codex skill instructions and guardrails.
- `agents/openai.yaml` - Codex Desktop display metadata.
- `scripts/sync_provider_plugins.py` - Standalone repair script.

## Usage

Run a dry run first:

```bash
python3 scripts/sync_provider_plugins.py --dry-run
```

Review the reported target provider and planned changes:

- `target_provider`
- `changed_session_files`
- `db_rows_needing_update_before`
- `marketplace_sections_to_add`
- `plugin_sections_to_add`
- `skill_count`

Then run the repair:

```bash
python3 scripts/sync_provider_plugins.py
```

You can also pass a provider explicitly:

```bash
python3 scripts/sync_provider_plugins.py --provider openai
```

## Options

```text
--provider ai                 use a specific target provider
--codex-home ~/.codex         override Codex home
--backup-dir /path/to/backup  override backup destination
--dry-run                     report changes without writing
--skip-plugins                provider-only sync
--verify-delay 2              wait before final verification
```

## Safety

The script creates backups before writing:

- `state_5.sqlite` is backed up with SQLite `VACUUM INTO`.
- Changed session JSONL files are copied to the backup directory before editing.
- `config.toml` is copied before adding missing marketplace/plugin sections.

Guardrails:

- It does not replace arbitrary `openai` strings.
- It does not edit conversation content.
- It only rewrites JSON fields named `model_provider` inside `session_meta` records.
- It does not delete plugin caches or skill folders.

## Install As A Codex Skill

Clone this repository into your Codex skills directory:

```bash
git clone https://github.com/Dailin521/codex-provider-plugin-sync.git ~/.codex/skills/codex-provider-plugin-sync
```

Restart Codex Desktop if the skill does not appear immediately.

## License

MIT
