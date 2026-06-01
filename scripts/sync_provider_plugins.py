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
        description="Synchronize Codex provider metadata, repair chat records, and restore local plugin enablement."
    )
    parser.add_argument("--codex-home", default=str(Path.home() / ".codex"))
    parser.add_argument("--provider", help="Target provider. Defaults to latest thread provider.")
    parser.add_argument("--backup-dir", help="Backup directory. Defaults under ~/.codex/backups_state/provider-sync.")
    parser.add_argument("--dry-run", action="store_true", help="Report changes without writing.")
    parser.add_argument("--skip-plugins", action="store_true", help="Only synchronize provider and chat metadata.")
    parser.add_argument(
        "--skip-chat-records",
        action="store_true",
        help="Only synchronize provider/plugin state; do not repair chat visibility fields or session_index.jsonl.",
    )
    parser.add_argument("--verify-delay", type=float, default=2.0)
    return parser.parse_args()


def timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def sql_quote(path: Path) -> str:
    return path.as_posix().replace("'", "''")


def iso_from_thread(row: sqlite3.Row) -> str:
    value = row["updated_at_ms"] or (row["updated_at"] * 1000)
    return iso_from_ms(int(value))


def iso_from_ms(value: int) -> str:
    return datetime.fromtimestamp(value / 1000, timezone.utc).isoformat().replace("+00:00", "Z")


def parse_iso_ms(value: str) -> int | None:
    if not value:
        return None
    try:
        return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        return None


def session_time_bounds(path: Path) -> tuple[int | None, int | None]:
    first_ms: int | None = None
    last_ms: int | None = None
    if not path.exists():
        return None, None

    with path.open("r", encoding="utf-8") as f:
        for line in f:
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            value = item.get("timestamp")
            if not isinstance(value, str):
                continue
            ms = parse_iso_ms(value)
            if ms is None:
                continue
            first_ms = ms if first_ms is None else min(first_ms, ms)
            last_ms = ms if last_ms is None else max(last_ms, ms)
    return first_ms, last_ms


def is_recent_session(last_ms: int, minutes: int = 10) -> bool:
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    return last_ms > now_ms - minutes * 60 * 1000


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


def update_db_provider(state_db: Path, provider: str, dry_run: bool) -> int:
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


def update_db_chat_records(state_db: Path, dry_run: bool) -> int:
    with sqlite3.connect(state_db) as conn:
        before = conn.execute(
            """
            select count(*)
            from threads
            where source = 'vscode'
              and archived = 0
              and (thread_source is null or thread_source = '')
            """
        ).fetchone()[0]
        if not dry_run:
            conn.execute(
                """
                update threads
                set thread_source = 'user'
                where source = 'vscode'
                  and archived = 0
                  and (thread_source is null or thread_source = '')
                """
            )
            conn.commit()
    return int(before)


def rewrite_session_file(
    path: Path,
    sessions_dir: Path,
    backup_sessions_dir: Path,
    provider: str,
    sync_chat_records: bool,
    dry_run: bool,
) -> tuple[bool, bool, bool]:
    changed = False
    provider_changed = False
    chat_changed = False
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
                    provider_changed = True
                    line_changed = True

                nested = payload.get("payload")
                if isinstance(nested, dict) and nested.get("model_provider") != provider:
                    nested["model_provider"] = provider
                    provider_changed = True
                    line_changed = True

                if sync_chat_records and payload.get("source") == "vscode" and not payload.get("thread_source"):
                    payload["thread_source"] = "user"
                    chat_changed = True
                    line_changed = True

                if (
                    sync_chat_records
                    and isinstance(nested, dict)
                    and nested.get("source") == "vscode"
                    and not nested.get("thread_source")
                ):
                    nested["thread_source"] = "user"
                    chat_changed = True
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

    return changed, provider_changed, chat_changed


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


def count_jsonl_chat_records(sessions_dir: Path) -> tuple[int, int]:
    user_session_meta = 0
    missing_thread_source = 0
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
                if payload.get("source") == "vscode":
                    user_session_meta += 1
                    if not payload.get("thread_source"):
                        missing_thread_source += 1
                break
    return user_session_meta, missing_thread_source


def read_session_index(index_path: Path) -> tuple[dict[str, dict], int, int]:
    entries_by_id: dict[str, dict] = {}
    duplicate_ids = 0
    bad_lines = 0
    if not index_path.exists():
        return entries_by_id, duplicate_ids, bad_lines

    for line in index_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            bad_lines += 1
            continue
        if not isinstance(item, dict) or not item.get("id"):
            bad_lines += 1
            continue
        if item["id"] in entries_by_id:
            duplicate_ids += 1
        entries_by_id[item["id"]] = item
    return entries_by_id, duplicate_ids, bad_lines


def user_thread_rows(state_db: Path) -> list[sqlite3.Row]:
    conn = sqlite3.connect(state_db)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            """
            select id, title, rollout_path, updated_at, updated_at_ms
            from threads
            where source = 'vscode'
              and archived = 0
              and (thread_source = 'user' or thread_source is null or thread_source = '')
            order by coalesce(updated_at_ms, updated_at * 1000, created_at_ms, created_at * 1000), id
            """
        ).fetchall()
    finally:
        conn.close()


def expected_thread_timestamps(state_db: Path) -> dict[str, int]:
    expected: dict[str, int] = {}
    for row in user_thread_rows(state_db):
        _, last_ms = session_time_bounds(Path(row["rollout_path"]))
        if last_ms is not None and not is_recent_session(last_ms):
            expected[row["id"]] = last_ms
    return expected


def sync_thread_timestamps(state_db: Path, dry_run: bool) -> int:
    rows = user_thread_rows(state_db)
    updates: list[tuple[int, str]] = []
    for row in rows:
        _, last_ms = session_time_bounds(Path(row["rollout_path"]))
        if last_ms is None:
            continue
        if is_recent_session(last_ms):
            continue
        current_ms = row["updated_at_ms"] or (row["updated_at"] * 1000)
        if abs(int(current_ms) - last_ms) >= 1000:
            updates.append((last_ms, row["id"]))

    if updates and not dry_run:
        with sqlite3.connect(state_db) as conn:
            conn.executemany(
                "update threads set updated_at = ?, updated_at_ms = ? where id = ?",
                [(ms // 1000, ms, thread_id) for ms, thread_id in updates],
            )
            conn.commit()
    return len(updates)


def session_index_status(state_db: Path, index_path: Path) -> tuple[int, int, int, dict[str, dict], list[sqlite3.Row]]:
    entries_by_id, duplicate_ids, bad_lines = read_session_index(index_path)
    rows = user_thread_rows(state_db)
    missing_rows = [row for row in rows if row["id"] not in entries_by_id]
    return len(missing_rows), duplicate_ids, bad_lines, entries_by_id, missing_rows


def sync_session_index(state_db: Path, index_path: Path, backup_dir: Path, dry_run: bool) -> tuple[int, int, int, int]:
    missing_count, duplicate_ids, bad_lines, entries_by_id, missing_rows = session_index_status(state_db, index_path)
    expected_times = expected_thread_timestamps(state_db)
    timestamp_updates = 0
    for thread_id, expected_ms in expected_times.items():
        entry = entries_by_id.get(thread_id)
        if not entry:
            continue
        if parse_iso_ms(str(entry.get("updated_at", ""))) != expected_ms:
            timestamp_updates += 1

    if dry_run or (not missing_rows and duplicate_ids == 0 and timestamp_updates == 0):
        return missing_count, duplicate_ids, bad_lines, timestamp_updates

    backup_dir.mkdir(parents=True, exist_ok=True)
    if index_path.exists():
        shutil.copy2(index_path, backup_dir / "session_index.jsonl")

    for row in missing_rows:
        entries_by_id[row["id"]] = {
            "id": row["id"],
            "thread_name": row["title"] or row["id"],
            "updated_at": iso_from_thread(row),
        }

    for thread_id, expected_ms in expected_times.items():
        entry = entries_by_id.get(thread_id)
        if entry:
            entry["updated_at"] = iso_from_ms(expected_ms)

    entries = sorted(entries_by_id.values(), key=lambda item: item.get("updated_at", ""))
    index_path.write_text(
        "".join(json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n" for item in entries),
        encoding="utf-8",
    )
    return missing_count, duplicate_ids, bad_lines, timestamp_updates


def timestamp_mismatch_counts(state_db: Path, index_path: Path) -> tuple[int, int]:
    db_mismatches = 0
    expected_times = expected_thread_timestamps(state_db)
    for row in user_thread_rows(state_db):
        expected_ms = expected_times.get(row["id"])
        if expected_ms is None:
            continue
        current_ms = row["updated_at_ms"] or (row["updated_at"] * 1000)
        if abs(int(current_ms) - expected_ms) >= 1000:
            db_mismatches += 1

    entries_by_id, _, _ = read_session_index(index_path)
    index_mismatches = 0
    for thread_id, expected_ms in expected_times.items():
        entry = entries_by_id.get(thread_id)
        if entry and parse_iso_ms(str(entry.get("updated_at", ""))) != expected_ms:
            index_mismatches += 1
    return db_mismatches, index_mismatches


def db_chat_remaining(state_db: Path) -> int:
    with sqlite3.connect(state_db) as conn:
        return int(
            conn.execute(
                """
                select count(*)
                from threads
                where source = 'vscode'
                  and archived = 0
                  and (thread_source is null or thread_source = '')
                """
            ).fetchone()[0]
        )


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
    index_path = codex_home / "session_index.jsonl"
    sync_chat_records = not args.skip_chat_records

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
    print(f"sync_chat_records={sync_chat_records}")

    if not args.dry_run:
        backup_dir.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(state_db) as conn:
            conn.execute("pragma wal_checkpoint(full)")
            conn.execute(f"vacuum main into '{sql_quote(backup_dir / 'state_5.sqlite')}'")

    changed_files: list[Path] = []
    provider_session_changes = 0
    chat_session_changes = 0
    for path in sorted(sessions_dir.rglob("*.jsonl")):
        changed, provider_changed, chat_changed = rewrite_session_file(
            path, sessions_dir, backup_dir / "sessions", provider, sync_chat_records, args.dry_run
        )
        if changed:
            changed_files.append(path)
        if provider_changed:
            provider_session_changes += 1
        if chat_changed:
            chat_session_changes += 1

    db_rows_before = update_db_provider(state_db, provider, args.dry_run)
    chat_db_rows_before = update_db_chat_records(state_db, args.dry_run) if sync_chat_records else 0
    timestamp_db_rows_before = sync_thread_timestamps(state_db, args.dry_run) if sync_chat_records else 0
    index_missing_before, index_duplicates_before, index_bad_lines, index_timestamp_updates_before = sync_session_index(
        state_db, index_path, backup_dir / "index", args.dry_run
    ) if sync_chat_records else (0, 0, 0, 0)

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

    if sync_chat_records:
        user_session_meta, missing_jsonl_thread_source = count_jsonl_chat_records(sessions_dir)
        remaining_empty_thread_source = db_chat_remaining(state_db)
        index_missing_after, index_duplicates_after, index_bad_lines_after, _, _ = session_index_status(state_db, index_path)
        db_timestamp_remaining, index_timestamp_remaining = timestamp_mismatch_counts(state_db, index_path)
    else:
        user_session_meta = 0
        missing_jsonl_thread_source = 0
        remaining_empty_thread_source = 0
        index_missing_after = 0
        index_duplicates_after = 0
        index_bad_lines_after = 0
        db_timestamp_remaining = 0
        index_timestamp_remaining = 0

    print(f"changed_session_files={len(changed_files)}")
    for path in changed_files:
        print(f"changed={path}")
    print(f"provider_session_files_changed={provider_session_changes}")
    print(f"chat_session_files_changed={chat_session_changes}")
    print(f"db_rows_needing_update_before={db_rows_before}")
    print(f"db_chat_rows_needing_update_before={chat_db_rows_before}")
    print(f"db_timestamp_rows_needing_update_before={timestamp_db_rows_before}")
    print(f"after_db_groups={after_rows!r}")
    print(f"session_meta_files={session_meta_count}")
    print(f"jsonl_remaining_not_{provider}={mismatched_jsonl}")
    print(f"db_remaining_not_{provider}={db_remaining}")
    print(f"chat_session_meta_files={user_session_meta}")
    print(f"jsonl_remaining_missing_thread_source={missing_jsonl_thread_source}")
    print(f"db_remaining_empty_thread_source={remaining_empty_thread_source}")
    print(f"session_index_missing_user_threads_before={index_missing_before}")
    print(f"session_index_duplicate_ids_before={index_duplicates_before}")
    print(f"session_index_bad_lines_before={index_bad_lines}")
    print(f"session_index_timestamp_rows_needing_update_before={index_timestamp_updates_before}")
    print(f"session_index_missing_user_threads_after={index_missing_after}")
    print(f"session_index_duplicate_ids_after={index_duplicates_after}")
    print(f"session_index_bad_lines_after={index_bad_lines_after}")
    print(f"db_timestamp_remaining_mismatch={db_timestamp_remaining}")
    print(f"session_index_timestamp_remaining_mismatch={index_timestamp_remaining}")
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
    if (
        not args.dry_run
        and sync_chat_records
        and (
            missing_jsonl_thread_source
            or remaining_empty_thread_source
            or index_missing_after
            or index_duplicates_after
            or index_bad_lines_after
            or db_timestamp_remaining
            or index_timestamp_remaining
        )
    ):
        raise SystemExit("Chat record sync verification failed; inspect output and backups.")


if __name__ == "__main__":
    main()
