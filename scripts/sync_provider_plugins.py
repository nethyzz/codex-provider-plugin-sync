#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


@dataclass(frozen=True)
class Marketplace:
    name: str
    source: Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Synchronize Codex provider metadata and restore local plugin enablement."
    )
    parser.add_argument("--codex-home", default=str(Path.home() / ".codex"))
    parser.add_argument("--provider", help="Target provider. Defaults to latest thread provider.")
    parser.add_argument("--backup-dir", help="Backup directory. Defaults under ~/.codex/backups_state/provider-sync.")
    parser.add_argument("--dry-run", action="store_true", help="Report changes without writing.")
    parser.add_argument("--skip-plugins", action="store_true", help="Only synchronize provider metadata.")
    parser.add_argument("--verify-delay", type=float, default=2.0)
    return parser.parse_args()


def timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def sql_quote(path: Path) -> str:
    return path.as_posix().replace("'", "''")


def latest_provider(state_db: Path) -> str:
    with sqlite3.connect(state_db) as conn:
        row = conn.execute(
            """
            select model_provider
            from threads
            where model_provider is not null and model_provider <> ''
            order by coalesce(updated_at_ms, updated_at * 1000, created_at_ms, created_at * 1000) desc
            limit 1
            """
        ).fetchone()
    if not row or not row[0]:
        raise SystemExit("Could not infer provider from latest thread. Pass --provider explicitly.")
    return str(row[0])


def db_summary(state_db: Path) -> tuple[list[tuple[str | None, str | None, int]], int]:
    with sqlite3.connect(state_db) as conn:
        groups = conn.execute(
            """
            select model_provider, model, count(*)
            from threads
            group by model_provider, model
            order by count(*) desc, model_provider, model
            """
        ).fetchall()
        count = conn.execute("select count(*) from threads").fetchone()[0]
    return groups, int(count)


def update_db(state_db: Path, provider: str, dry_run: bool) -> int:
    with sqlite3.connect(state_db) as conn:
        before = conn.execute(
            "select count(*) from threads where model_provider <> ? or model_provider is null",
            (provider,),
        ).fetchone()[0]
        if not dry_run:
            conn.execute(
                "update threads set model_provider = ? where model_provider <> ? or model_provider is null",
                (provider, provider),
            )
            conn.commit()
    return int(before)


def rewrite_session_file(path: Path, sessions_dir: Path, backup_sessions_dir: Path, provider: str, dry_run: bool) -> bool:
    changed = False
    output: list[str] = []

    with path.open("r", encoding="utf-8") as f:
        for line in f:
            newline = "\n" if line.endswith("\n") else ""
            stripped = line[:-1] if newline else line
            try:
                item = json.loads(stripped)
            except json.JSONDecodeError:
                output.append(line)
                continue

            line_changed = False
            payload = item.get("payload")
            if item.get("type") == "session_meta" and isinstance(payload, dict):
                if payload.get("model_provider") != provider:
                    payload["model_provider"] = provider
                    line_changed = True
                nested = payload.get("payload")
                if isinstance(nested, dict) and nested.get("model_provider") != provider:
                    nested["model_provider"] = provider
                    line_changed = True

            if line_changed:
                changed = True
                output.append(json.dumps(item, ensure_ascii=False, separators=(",", ":")) + newline)
            else:
                output.append(line)

    if changed and not dry_run:
        rel = path.relative_to(sessions_dir)
        backup = backup_sessions_dir / rel
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, backup)
        path.write_text("".join(output), encoding="utf-8")

    return changed


def count_jsonl_provider(sessions_dir: Path, provider: str) -> tuple[int, int]:
    session_meta = 0
    mismatched = 0
    for path in sessions_dir.rglob("*.jsonl"):
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                payload = item.get("payload")
                if item.get("type") != "session_meta" or not isinstance(payload, dict):
                    continue
                session_meta += 1
                if payload.get("model_provider") != provider:
                    mismatched += 1
                break
    return session_meta, mismatched


def table_present(config_text: str, table: str) -> bool:
    return f"[{table}]" in config_text


def marketplace_candidates(codex_home: Path) -> list[Marketplace]:
    home = Path.home()
    candidates = [
        Marketplace("openai-bundled", codex_home / ".tmp" / "bundled-marketplaces" / "openai-bundled"),
        Marketplace(
            "openai-primary-runtime",
            home / ".cache" / "codex-runtimes" / "codex-primary-runtime" / "plugins" / "openai-primary-runtime",
        ),
    ]

    plugin_cache = codex_home / "plugins" / "cache"
    if plugin_cache.exists():
        for child in sorted(plugin_cache.iterdir()):
            if child.is_dir():
                candidates.append(Marketplace(child.name, child))

    seen: set[str] = set()
    unique: list[Marketplace] = []
    for candidate in candidates:
        key = candidate.name
        if key in seen or not candidate.source.exists():
            continue
        seen.add(key)
        unique.append(candidate)
    return unique


def discover_plugins(marketplace: Marketplace) -> list[str]:
    plugin_names: set[str] = set()
    for plugin_json in marketplace.source.rglob(".codex-plugin/plugin.json"):
        try:
            data = json.loads(plugin_json.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        name = data.get("name")
        if isinstance(name, str) and name:
            plugin_names.add(name)
    return sorted(plugin_names)


def restore_plugins(codex_home: Path, backup_dir: Path, dry_run: bool) -> tuple[int, int, list[str], list[str]]:
    config = codex_home / "config.toml"
    if not config.exists():
        return 0, 0, [], []

    text = config.read_text(encoding="utf-8")
    additions: list[str] = []
    marketplace_tables: list[str] = []
    plugin_tables: list[str] = []

    for marketplace in marketplace_candidates(codex_home):
        marketplace_table = f"marketplaces.{marketplace.name}"
        if not table_present(text, marketplace_table):
            marketplace_tables.append(f"[{marketplace_table}]")
            additions.append(
                "\n"
                f"[{marketplace_table}]\n"
                f'last_updated = "{datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")}"\n'
                'source_type = "local"\n'
                f'source = "{marketplace.source}"\n'
            )

        for plugin_name in discover_plugins(marketplace):
            plugin_id = f"{plugin_name}@{marketplace.name}"
            plugin_table = f'plugins."{plugin_id}"'
            if not table_present(text, plugin_table):
                plugin_tables.append(f"[{plugin_table}]")
                additions.append("\n" f"[{plugin_table}]\n" "enabled = true\n")

    if additions and not dry_run:
        backup_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(config, backup_dir / "config.toml")
        config.write_text(text.rstrip() + "\n" + "".join(additions), encoding="utf-8")

    return len(marketplace_tables), len(plugin_tables), marketplace_tables, plugin_tables


def skill_count(codex_home: Path) -> int:
    skills_dir = codex_home / "skills"
    if not skills_dir.exists():
        return 0
    return sum(1 for _ in skills_dir.glob("*/SKILL.md"))


def main() -> None:
    args = parse_args()
    codex_home = Path(args.codex_home).expanduser()
    state_db = codex_home / "state_5.sqlite"
    sessions_dir = codex_home / "sessions"

    if not state_db.exists():
        raise SystemExit(f"Missing state database: {state_db}")
    if not sessions_dir.exists():
        raise SystemExit(f"Missing sessions directory: {sessions_dir}")

    provider = args.provider or latest_provider(state_db)
    backup_dir = (
        Path(args.backup_dir).expanduser()
        if args.backup_dir
        else codex_home / "backups_state" / "provider-sync" / timestamp()
    )

    before_rows, thread_count = db_summary(state_db)
    print(f"target_provider={provider}")
    print(f"threads={thread_count}")
    print(f"before_db_groups={before_rows!r}")
    print(f"backup_dir={backup_dir}")
    print(f"dry_run={args.dry_run}")

    if not args.dry_run:
        backup_dir.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(state_db) as conn:
            conn.execute("pragma wal_checkpoint(full)")
            conn.execute(f"vacuum main into '{sql_quote(backup_dir / 'state_5.sqlite')}'")

    changed_files: list[Path] = []
    for path in sorted(sessions_dir.rglob("*.jsonl")):
        if rewrite_session_file(path, sessions_dir, backup_dir / "sessions", provider, args.dry_run):
            changed_files.append(path)

    db_rows_before = update_db(state_db, provider, args.dry_run)

    if args.skip_plugins:
        marketplace_added = 0
        plugin_added = 0
        marketplace_tables = []
        plugin_tables = []
    else:
        marketplace_added, plugin_added, marketplace_tables, plugin_tables = restore_plugins(
            codex_home, backup_dir / "config", args.dry_run
        )

    if args.verify_delay > 0:
        time.sleep(args.verify_delay)

    after_rows, _ = db_summary(state_db)
    session_meta_count, mismatched_jsonl = count_jsonl_provider(sessions_dir, provider)
    with sqlite3.connect(state_db) as conn:
        db_remaining = conn.execute(
            "select count(*) from threads where model_provider <> ? or model_provider is null",
            (provider,),
        ).fetchone()[0]

    print(f"changed_session_files={len(changed_files)}")
    for path in changed_files:
        print(f"changed={path}")
    print(f"db_rows_needing_update_before={db_rows_before}")
    print(f"after_db_groups={after_rows!r}")
    print(f"session_meta_files={session_meta_count}")
    print(f"jsonl_remaining_not_{provider}={mismatched_jsonl}")
    print(f"db_remaining_not_{provider}={db_remaining}")
    print(f"marketplace_sections_to_add={marketplace_added if args.dry_run else 0}")
    for table in marketplace_tables:
        print(f"marketplace_section={table}")
    print(f"plugin_sections_to_add={plugin_added if args.dry_run else 0}")
    for table in plugin_tables:
        print(f"plugin_section={table}")
    print(f"config_marketplace_sections_added={0 if args.dry_run else marketplace_added}")
    print(f"config_plugin_sections_added={0 if args.dry_run else plugin_added}")
    print(f"skill_count={skill_count(codex_home)}")

    if not args.dry_run and (mismatched_jsonl or db_remaining):
        raise SystemExit("Provider sync verification failed; inspect output and backups.")


if __name__ == "__main__":
    main()
