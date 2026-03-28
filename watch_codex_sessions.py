from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Tuple

SESSION_ID_RE = re.compile(
    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
    re.IGNORECASE,
)
PLAN_BLOCK_RE = re.compile(r"<proposed_plan>\s*(.*?)\s*</proposed_plan>", re.DOTALL | re.IGNORECASE)
HEADING_RE = re.compile(r"^\s*#\s+(.+?)\s*$", re.MULTILINE)
INVALID_FILENAME_RE = re.compile(r'[<>:"/\\|?*#`]+')
TRAILING_DOT_RE = re.compile(r"[. ]+$")
SPACE_RE = re.compile(r"\s+")

MANIFEST_VERSION = 2
ORCHESTRATION_TOOLS = {"spawn_agent", "send_input", "wait_agent", "close_agent"}
TERMINAL_TASK_EVENTS = {"task_complete", "task_failed", "task_cancelled"}
DEFAULT_EVENTS_MAX_BYTES = 32 * 1024 * 1024
DEFAULT_TRANSCRIPT_MAX_BYTES = 16 * 1024 * 1024
DEFAULT_PLAN_MAX_BYTES = 8 * 1024 * 1024


@dataclass(frozen=True)
class ThreadInfo:
    session_id: str
    thread_name: str
    thread_title: str
    source_kind: str
    parent_thread_id: Optional[str]
    agent_nickname: Optional[str]
    agent_role: Optional[str]
    depth: int
    rollout_path: Optional[str]
    archived: bool


@dataclass(frozen=True)
class RolloutSource:
    session_id: str
    path: Path
    origin: str
    archived: bool


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def default_codex_home() -> Path:
    codex_home = os.environ.get("CODEX_HOME")
    if codex_home:
        return Path(codex_home).expanduser().resolve()
    return Path("~/.codex").expanduser().resolve()


def read_json_lines(path: Path) -> Iterator[Dict[str, Any]]:
    if not path.exists():
        return
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for raw_line in handle:
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            try:
                payload = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                yield payload


def parse_json_maybe(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    text = value.strip()
    if not text or text[0] not in "[{":
        return value
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return value


def infer_session_id(path: Path) -> Optional[str]:
    match = SESSION_ID_RE.search(path.name)
    if match:
        return match.group(1)
    for row in read_json_lines(path):
        if row.get("type") == "session_meta":
            payload = row.get("payload", {})
            session_id = payload.get("id")
            if isinstance(session_id, str) and session_id:
                return session_id
    return None


def load_session_index(session_index_path: Path) -> Dict[str, Dict[str, Any]]:
    index: Dict[str, Dict[str, Any]] = {}
    for row in read_json_lines(session_index_path):
        session_id = row.get("id")
        if isinstance(session_id, str) and session_id:
            index[session_id] = row
    return index


def determine_origin(codex_home: Path, rollout_path: Path) -> Tuple[str, bool]:
    try:
        rel_path = rollout_path.resolve().relative_to(codex_home.resolve())
    except ValueError:
        return "external", False
    if rel_path.parts and rel_path.parts[0] == "archived_sessions":
        return "archived_sessions", True
    if rel_path.parts and rel_path.parts[0] == "sessions":
        return "sessions", False
    return "external", False


def choose_better_source(existing: Optional[RolloutSource], candidate: RolloutSource) -> RolloutSource:
    if existing is None:
        return candidate
    if existing.origin == "archived_sessions" and candidate.origin == "sessions":
        return candidate
    if existing.origin == "external" and candidate.origin in {"sessions", "archived_sessions"}:
        return candidate
    return existing


def collect_message_text(content: Any) -> str:
    if not isinstance(content, list):
        return ""
    parts: List[str] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        for key in ("output_text", "text"):
            value = item.get(key)
            if isinstance(value, str) and value:
                parts.append(value)
                break
    return "\n".join(parts).strip()


def normalize_filename(title: str) -> str:
    cleaned = INVALID_FILENAME_RE.sub(" ", title)
    cleaned = cleaned.replace("\u3000", " ")
    cleaned = SPACE_RE.sub(" ", cleaned).strip()
    cleaned = TRAILING_DOT_RE.sub("", cleaned)
    return cleaned or "untitled-plan"


def extract_plan_title(plan_body: str) -> str:
    heading = HEADING_RE.search(plan_body)
    if heading:
        return heading.group(1).strip()
    for line in plan_body.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return "untitled-plan"


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return default


def yaml_escape(value: Optional[str]) -> str:
    return json.dumps("" if value is None else value, ensure_ascii=False)


def write_chunked_lines(directory: Path, lines: Iterable[str], max_bytes: int, suffix: str) -> List[Dict[str, Any]]:
    directory.mkdir(parents=True, exist_ok=True)
    parts: List[List[str]] = []
    part_lines: List[str] = []
    part_bytes = 0

    def flush() -> None:
        nonlocal part_lines, part_bytes
        if not part_lines:
            return
        parts.append(part_lines)
        part_lines = []
        part_bytes = 0

    for line in lines:
        encoded = line.encode("utf-8")
        if part_lines and part_bytes + len(encoded) > max_bytes:
            flush()
        if len(encoded) > max_bytes:
            flush()
            parts.append([line])
            continue
        part_lines.append(line)
        part_bytes += len(encoded)
    flush()

    if not parts:
        parts = [[""]]

    for old in directory.glob(f"part-*{suffix}"):
        old.unlink()

    metadata: List[Dict[str, Any]] = []
    for index, bucket in enumerate(parts, start=1):
        path = directory / f"part-{index:04d}{suffix}"
        content = "".join(bucket)
        path.write_text(content, encoding="utf-8")
        metadata.append({"path": str(path), "size_bytes": path.stat().st_size})
    return metadata


def run_git_command(repo_root: Path, args: List[str], check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(args, cwd=repo_root, text=True, capture_output=True, check=False)
    if check and result.returncode != 0:
        raise RuntimeError(
            f"git command failed ({' '.join(args)}):\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result


def is_filename_component_valid(name: str) -> bool:
    if not name or name != name.strip():
        return False
    if name.endswith("."):
        return False
    if "  " in name:
        return False
    return INVALID_FILENAME_RE.search(name) is None


def set_file_timestamps(path: Path, iso_timestamp: str) -> None:
    try:
        dt = datetime.fromisoformat(iso_timestamp.replace("Z", "+00:00"))
    except ValueError:
        return
    epoch = dt.timestamp()
    try:
        os.utime(path, (epoch, epoch))
    except OSError:
        pass
    if os.name != "nt":
        return
    try:
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.CreateFileW(
            str(path),
            0x0100,
            0x00000001 | 0x00000002 | 0x00000004,
            None,
            3,
            0x02000000,
            None,
        )
        if handle == -1 or handle == 0:
            return
        windows_epoch = int((epoch + 11644473600) * 10_000_000)

        class FILETIME(ctypes.Structure):
            _fields_ = [("dwLowDateTime", ctypes.c_uint32), ("dwHighDateTime", ctypes.c_uint32)]

        ft = FILETIME(windows_epoch & 0xFFFFFFFF, windows_epoch >> 32)
        kernel32.SetFileTime(handle, ctypes.byref(ft), ctypes.byref(ft), ctypes.byref(ft))
        kernel32.CloseHandle(handle)
    except Exception:
        return


class CodexArchiveWatcher:
    def __init__(
        self,
        codex_home: Path,
        output_dir: Path,
        poll_seconds: float = 1.0,
        events_max_bytes: int = DEFAULT_EVENTS_MAX_BYTES,
        transcript_max_bytes: int = DEFAULT_TRANSCRIPT_MAX_BYTES,
        plan_max_bytes: int = DEFAULT_PLAN_MAX_BYTES,
        auto_git: bool = False,
        git_remote: str = "origin",
        git_commit_interval_seconds: int = 300,
    ) -> None:
        self.codex_home = codex_home
        self.output_dir = output_dir
        self.poll_seconds = poll_seconds
        self.events_max_bytes = events_max_bytes
        self.transcript_max_bytes = transcript_max_bytes
        self.plan_max_bytes = plan_max_bytes
        self.auto_git = auto_git
        self.git_remote = git_remote
        self.git_commit_interval_seconds = git_commit_interval_seconds

        self.state_dir = output_dir / "_state"
        self.sessions_dir = output_dir / "sessions"
        self.plans_dir = output_dir / "plans"
        self.reports_dir = output_dir / "reports"
        self.thread_index_path = output_dir / "thread_index.json"
        self.manifest_path = self.state_dir / "manifest.json"
        self.plans_state_path = self.state_dir / "plans.json"
        self.session_states_dir = self.state_dir / "sessions"
        self.retention_report_path = self.reports_dir / "retention-audit.md"
        self.archive_audit_report_path = self.reports_dir / "archive-audit.md"
        self.filename_audit_report_path = self.reports_dir / "filename-audit.md"
        self.session_index_path = self.codex_home / "session_index.jsonl"
        self.state_db_path = self.codex_home / "state_5.sqlite"

        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        self.plans_dir.mkdir(parents=True, exist_ok=True)
        self.reports_dir.mkdir(parents=True, exist_ok=True)
        self.session_states_dir.mkdir(parents=True, exist_ok=True)

        self._thread_registry_cache: Optional[Dict[str, ThreadInfo]] = None
        self._thread_registry_mtime_ns: Optional[int] = None
        self.manifest = self._load_manifest()
        self.plan_manifest = self._load_plan_manifest()
        self._migrate_legacy_manifest_if_needed()

    def _load_manifest(self) -> Dict[str, Any]:
        manifest = load_json(
            self.manifest_path,
            {
                "version": MANIFEST_VERSION,
                "generated_at": "",
                "session_count": 0,
                "plan_count": 0,
                "last_git_sync_at": None,
                "last_session_index_offset": 0,
                "last_state_db_mtime_ns": 0,
                "known_sources": {},
                "last_discovery_mode": "",
                "last_full_scan_at": "",
                "last_incremental_scan_at": "",
                "last_discovered_source_count": 0,
                "last_new_source_count": 0,
                "last_processed_source_count": 0,
            },
        )
        manifest.setdefault("version", MANIFEST_VERSION)
        manifest.setdefault("generated_at", "")
        manifest.setdefault("session_count", 0)
        manifest.setdefault("plan_count", 0)
        manifest.setdefault("last_git_sync_at", None)
        manifest.setdefault("last_session_index_offset", 0)
        manifest.setdefault("last_state_db_mtime_ns", 0)
        manifest.setdefault("last_discovery_mode", "")
        manifest.setdefault("last_full_scan_at", "")
        manifest.setdefault("last_incremental_scan_at", "")
        manifest.setdefault("last_discovered_source_count", 0)
        manifest.setdefault("last_new_source_count", 0)
        manifest.setdefault("last_processed_source_count", 0)
        if not isinstance(manifest.get("known_sources"), dict):
            manifest["known_sources"] = {}
        return manifest

    def _load_plan_manifest(self) -> Dict[str, Any]:
        payload = load_json(self.plans_state_path, {})
        if not isinstance(payload, dict):
            return {}
        return payload

    def _migrate_legacy_manifest_if_needed(self) -> None:
        legacy_sessions = self.manifest.pop("sessions", None)
        legacy_plans = self.manifest.pop("plans", None)
        migrated = False

        if isinstance(legacy_sessions, dict):
            for session_id, session_state in legacy_sessions.items():
                self.write_session_state(session_id, session_state)
            migrated = True

        if isinstance(legacy_plans, dict):
            for plan_hash, plan_meta in legacy_plans.items():
                self.plan_manifest.setdefault(plan_hash, plan_meta)
            migrated = True

        if self.manifest.get("version", 0) < MANIFEST_VERSION:
            self.manifest["version"] = MANIFEST_VERSION
            migrated = True

        if migrated:
            self.manifest["session_count"] = len(list(self.session_states_dir.glob("*.json")))
            self.manifest["plan_count"] = len(self.plan_manifest)
            self.save_manifest()
            self.save_plan_manifest()

    def save_manifest(self) -> None:
        self.manifest["generated_at"] = utc_now()
        self.manifest["session_count"] = len(list(self.session_states_dir.glob("*.json")))
        self.manifest["plan_count"] = len(self.plan_manifest)
        write_json(self.manifest_path, self.manifest)

    def save_plan_manifest(self) -> None:
        write_json(self.plans_state_path, self.plan_manifest)

    def session_state_path(self, session_id: str) -> Path:
        return self.session_states_dir / f"{session_id}.json"

    def load_session_state(self, session_id: str) -> Dict[str, Any]:
        state = load_json(self.session_state_path(session_id), {})
        if not isinstance(state, dict):
            state = {}
        state.setdefault("source_rollout", "")
        state.setdefault("origin", "")
        state.setdefault("archived", False)
        state.setdefault("last_offset", 0)
        state.setdefault("current_turn_id", None)
        state.setdefault("current_turn_mode", None)
        state.setdefault("call_names", {})
        state.setdefault("first_seen_at", utc_now())
        state.setdefault("last_seen_at", utc_now())
        return state

    def write_session_state(self, session_id: str, state: Dict[str, Any]) -> None:
        write_json(self.session_state_path(session_id), state)

    def load_thread_registry(self, force_refresh: bool = False) -> Dict[str, ThreadInfo]:
        session_index = load_session_index(self.session_index_path)
        registry: Dict[str, ThreadInfo] = {}
        if not self.state_db_path.exists():
            return registry
        state_db_mtime_ns = self.state_db_path.stat().st_mtime_ns
        if (
            not force_refresh
            and self._thread_registry_cache is not None
            and self._thread_registry_mtime_ns == state_db_mtime_ns
        ):
            return dict(self._thread_registry_cache)

        query = (
            "SELECT id, title, source, rollout_path, agent_nickname, agent_role, archived "
            "FROM threads"
        )
        conn = sqlite3.connect(self.state_db_path)
        try:
            for row in conn.execute(query):
                session_id, title, source, rollout_path, agent_nickname, agent_role, archived = row
                if not isinstance(session_id, str) or not session_id:
                    continue
                parsed_source = parse_json_maybe(source)
                spawn_info = {}
                if isinstance(parsed_source, dict):
                    spawn_info = parsed_source.get("subagent", {}).get("thread_spawn", {})
                source_kind = "subagent" if isinstance(parsed_source, dict) and "subagent" in parsed_source else "main"
                parent_thread_id = spawn_info.get("parent_thread_id")
                depth = int(spawn_info.get("depth") or 0)
                index_name = session_index.get(session_id, {}).get("thread_name")
                thread_name = index_name or agent_nickname or title or session_id
                registry[session_id] = ThreadInfo(
                    session_id=session_id,
                    thread_name=str(thread_name),
                    thread_title=str(title or ""),
                    source_kind=source_kind,
                    parent_thread_id=str(parent_thread_id) if parent_thread_id else None,
                    agent_nickname=str(agent_nickname) if agent_nickname else None,
                    agent_role=str(agent_role) if agent_role else None,
                    depth=depth,
                    rollout_path=str(rollout_path) if rollout_path else None,
                    archived=bool(archived),
                )
        finally:
            conn.close()

        self._thread_registry_cache = dict(registry)
        self._thread_registry_mtime_ns = state_db_mtime_ns
        self.manifest["last_state_db_mtime_ns"] = state_db_mtime_ns
        return registry

    def should_full_scan(self) -> bool:
        known_sources = self.manifest.get("known_sources")
        if isinstance(known_sources, dict) and known_sources:
            return False
        return not any(self.session_states_dir.glob("*.json"))

    def load_known_sources(self) -> Dict[str, RolloutSource]:
        payload = self.manifest.get("known_sources", {})
        if not isinstance(payload, dict):
            return {}
        sources: Dict[str, RolloutSource] = {}
        for session_id, raw in payload.items():
            if not isinstance(session_id, str) or not session_id or not isinstance(raw, dict):
                continue
            path_value = raw.get("path")
            if not isinstance(path_value, str) or not path_value:
                continue
            sources[session_id] = RolloutSource(
                session_id=session_id,
                path=Path(path_value).expanduser(),
                origin=str(raw.get("origin") or "external"),
                archived=bool(raw.get("archived")),
            )
        return sources

    def remember_sources(self, sources: Dict[str, RolloutSource]) -> None:
        known_sources: Dict[str, Any] = {}
        for session_id, source in sorted(sources.items()):
            known_sources[session_id] = {
                "path": str(source.path),
                "origin": source.origin,
                "archived": source.archived,
            }
        self.manifest["known_sources"] = known_sources

    def record_discovery_stats(
        self,
        *,
        mode: str,
        source_count: int,
        new_source_count: int,
    ) -> None:
        now = utc_now()
        self.manifest["last_discovery_mode"] = mode
        self.manifest["last_discovered_source_count"] = source_count
        self.manifest["last_new_source_count"] = new_source_count
        if mode == "full":
            self.manifest["last_full_scan_at"] = now
        else:
            self.manifest["last_incremental_scan_at"] = now

    def update_session_index_checkpoint(self) -> None:
        try:
            size = self.session_index_path.stat().st_size
        except FileNotFoundError:
            size = 0
        self.manifest["last_session_index_offset"] = size

    def discover_session_index_updates(self) -> Dict[str, Dict[str, Any]]:
        updates: Dict[str, Dict[str, Any]] = {}
        if not self.session_index_path.exists():
            self.manifest["last_session_index_offset"] = 0
            return updates
        last_offset = int(self.manifest.get("last_session_index_offset", 0) or 0)
        file_size = self.session_index_path.stat().st_size
        if file_size < last_offset:
            last_offset = 0
        with self.session_index_path.open("rb") as handle:
            handle.seek(last_offset)
            while True:
                raw_line = handle.readline()
                if not raw_line:
                    break
                new_offset = handle.tell()
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line:
                    self.manifest["last_session_index_offset"] = new_offset
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    self.manifest["last_session_index_offset"] = new_offset
                    continue
                session_id = row.get("id")
                if isinstance(session_id, str) and session_id:
                    updates[session_id] = row
                self.manifest["last_session_index_offset"] = new_offset
        return updates

    def find_rollout_source_for_session(self, session_id: str) -> Optional[RolloutSource]:
        best: Optional[RolloutSource] = None
        for folder_name in ("sessions", "archived_sessions"):
            base_dir = self.codex_home / folder_name
            if not base_dir.exists():
                continue
            for rollout_path in base_dir.rglob(f"*{session_id}*.jsonl"):
                inferred_id = infer_session_id(rollout_path)
                if inferred_id != session_id:
                    continue
                origin, archived = determine_origin(self.codex_home, rollout_path)
                candidate = RolloutSource(session_id, rollout_path.resolve(), origin, archived)
                best = choose_better_source(best, candidate)
        return best

    def discover_rollout_sources(
        self, thread_registry: Dict[str, ThreadInfo], full_scan: Optional[bool] = None
    ) -> Dict[str, RolloutSource]:
        do_full_scan = self.should_full_scan() if full_scan is None else full_scan
        previous_sources = self.load_known_sources()
        sources: Dict[str, RolloutSource] = {} if do_full_scan else self.load_known_sources()

        if do_full_scan:
            for folder_name in ("sessions", "archived_sessions"):
                base_dir = self.codex_home / folder_name
                if not base_dir.exists():
                    continue
                for rollout_path in base_dir.rglob("*.jsonl"):
                    session_id = infer_session_id(rollout_path)
                    if not session_id:
                        continue
                    origin, archived = determine_origin(self.codex_home, rollout_path)
                    candidate = RolloutSource(session_id, rollout_path.resolve(), origin, archived)
                    sources[session_id] = choose_better_source(sources.get(session_id), candidate)
            self.update_session_index_checkpoint()
        else:
            for session_id in self.discover_session_index_updates():
                candidate = self.find_rollout_source_for_session(session_id)
                if candidate is not None:
                    sources[session_id] = choose_better_source(sources.get(session_id), candidate)

        for session_id, thread_info in thread_registry.items():
            if not thread_info.rollout_path:
                continue
            rollout_path = Path(thread_info.rollout_path).expanduser()
            if not rollout_path.exists():
                continue
            origin, archived = determine_origin(self.codex_home, rollout_path)
            candidate = RolloutSource(session_id, rollout_path.resolve(), origin, archived)
            sources[session_id] = choose_better_source(sources.get(session_id), candidate)

        for session_id, source in list(sources.items()):
            if source.path.exists():
                continue
            replacement = self.find_rollout_source_for_session(session_id)
            if replacement is not None:
                sources[session_id] = replacement

        self.remember_sources(sources)
        previous_keys = set(previous_sources.keys())
        current_keys = set(sources.keys())
        new_source_count = len(current_keys - previous_keys)
        self.record_discovery_stats(
            mode="full" if do_full_scan else "incremental",
            source_count=len(sources),
            new_source_count=new_source_count,
        )
        return sources

    def _session_manifest(self, source: RolloutSource) -> Dict[str, Any]:
        session_state = self.load_session_state(source.session_id)
        session_state["source_rollout"] = str(source.path)
        session_state["origin"] = source.origin
        session_state["archived"] = source.archived
        session_state["last_seen_at"] = utc_now()
        self.write_session_state(source.session_id, session_state)
        return session_state

    def seed_follow_only(self, sources: Dict[str, RolloutSource]) -> None:
        seeded = False
        for source in sources.values():
            session_state = self._session_manifest(source)
            if session_state.get("last_offset"):
                continue
            if self.session_events_parts_dir(source.session_id).exists():
                continue
            try:
                session_state["last_offset"] = source.path.stat().st_size
            except FileNotFoundError:
                continue
            self.write_session_state(source.session_id, session_state)
            seeded = True
        if seeded:
            self.save_manifest()

    def session_dir(self, session_id: str) -> Path:
        return self.sessions_dir / session_id

    def session_meta_path(self, session_id: str) -> Path:
        return self.session_dir(session_id) / "meta.json"

    def session_events_parts_dir(self, session_id: str) -> Path:
        return self.session_dir(session_id) / "events"

    def legacy_session_events_path(self, session_id: str) -> Path:
        return self.session_dir(session_id) / "events.jsonl"

    def session_transcript_parts_dir(self, session_id: str) -> Path:
        return self.session_dir(session_id) / "transcript"

    def legacy_session_transcript_path(self, session_id: str) -> Path:
        return self.session_dir(session_id) / "transcript.md"

    def reset_session_outputs(self, session_id: str) -> None:
        for path in (
            self.legacy_session_events_path(session_id),
            self.legacy_session_transcript_path(session_id),
            self.session_meta_path(session_id),
        ):
            if path.exists():
                path.unlink()
        for directory in (self.session_events_parts_dir(session_id), self.session_transcript_parts_dir(session_id)):
            if directory.exists():
                for child in directory.glob("*"):
                    if child.is_file():
                        child.unlink()

    def process_all_sources(self, full_scan: Optional[bool] = None) -> int:
        thread_registry = self.load_thread_registry(force_refresh=bool(full_scan))
        sources = self.discover_rollout_sources(thread_registry, full_scan=full_scan)
        processed = 0
        for session_id in sorted(sources):
            self.process_source(sources[session_id], thread_registry.get(session_id))
            processed += 1
        self.manifest["last_processed_source_count"] = processed
        self.write_thread_index(thread_registry)
        self.save_manifest()
        self.save_plan_manifest()
        self.maybe_sync_git()
        return processed

    def process_source(self, source: RolloutSource, thread_info: Optional[ThreadInfo]) -> None:
        session_state = self._session_manifest(source)
        try:
            file_size = source.path.stat().st_size
        except FileNotFoundError:
            return

        last_offset = int(session_state.get("last_offset", 0) or 0)
        if file_size < last_offset:
            session_state["last_offset"] = 0
            session_state["current_turn_id"] = None
            session_state["current_turn_mode"] = None
            session_state["call_names"] = {}
            last_offset = 0
            self.reset_session_outputs(source.session_id)

        normalized_events: List[Dict[str, Any]] = []
        with source.path.open("rb") as handle:
            handle.seek(last_offset)
            while True:
                raw_line = handle.readline()
                if not raw_line:
                    break
                new_offset = handle.tell()
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line:
                    session_state["last_offset"] = new_offset
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    session_state["last_offset"] = new_offset
                    continue
                normalized_events.extend(self.normalize_row(row, source, thread_info, session_state))
                session_state["last_offset"] = new_offset

        self.write_session_state(source.session_id, session_state)
        if normalized_events:
            self.append_normalized_events(source.session_id, normalized_events)
        resolved_thread = thread_info or self.fallback_thread_info(source.session_id, source)
        self.write_meta(source, resolved_thread)
        self.render_transcript(source.session_id, resolved_thread)
        self.write_meta(source, resolved_thread)

    def fallback_thread_info(self, session_id: str, source: RolloutSource) -> ThreadInfo:
        session_index_name = load_session_index(self.session_index_path).get(session_id, {}).get("thread_name")
        return ThreadInfo(
            session_id=session_id,
            thread_name=str(session_index_name or session_id),
            thread_title="",
            source_kind="main",
            parent_thread_id=None,
            agent_nickname=None,
            agent_role=None,
            depth=0,
            rollout_path=str(source.path),
            archived=source.archived,
        )

    def normalize_row(
        self,
        row: Dict[str, Any],
        source: RolloutSource,
        thread_info: Optional[ThreadInfo],
        session_state: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        timestamp = row.get("timestamp")
        row_type = row.get("type")
        payload = row.get("payload", {})
        normalized: List[Dict[str, Any]] = []

        if row_type == "event_msg":
            event_type = payload.get("type")
            turn_id = payload.get("turn_id")
            if event_type == "task_started" and turn_id:
                session_state["current_turn_id"] = turn_id
                session_state["current_turn_mode"] = payload.get("collaboration_mode_kind")
            elif event_type in TERMINAL_TASK_EVENTS and turn_id and turn_id == session_state.get("current_turn_id"):
                session_state["current_turn_id"] = None
                session_state["current_turn_mode"] = None
            return normalized

        if row_type != "response_item" or not isinstance(payload, dict):
            return normalized

        item_type = payload.get("type")
        current_turn_id = session_state.get("current_turn_id")
        current_turn_mode = session_state.get("current_turn_mode")
        base = {"timestamp": timestamp, "session_id": source.session_id, "turn_id": current_turn_id}

        if item_type == "message":
            role = payload.get("role")
            text = collect_message_text(payload.get("content"))
            if not text:
                return normalized
            if role == "user":
                normalized.append({**base, "event_kind": "user_message", "role": "user", "content": text})
            elif role == "assistant":
                phase = payload.get("phase")
                event_kind = "assistant_commentary" if phase == "commentary" else "assistant_message"
                normalized.append(
                    {
                        **base,
                        "event_kind": event_kind,
                        "role": "assistant",
                        "phase": phase,
                        "content": text,
                    }
                )
                for plan_body in self.extract_plan_bodies(text):
                    self.persist_plan(
                        source=source,
                        thread_info=thread_info or self.fallback_thread_info(source.session_id, source),
                        source_turn_id=current_turn_id,
                        plan_mode_confirmed=current_turn_mode == "plan",
                        plan_generated_at=timestamp,
                        plan_body=plan_body,
                    )
            return normalized

        if item_type == "reasoning":
            normalized.append(
                {**base, "event_kind": "reasoning_event", "has_encrypted_content": bool(payload.get("encrypted_content"))}
            )
            return normalized

        if item_type == "function_call":
            call_id = payload.get("call_id")
            name = payload.get("name")
            arguments = parse_json_maybe(payload.get("arguments"))
            if call_id and name:
                session_state.setdefault("call_names", {})[call_id] = name
            event_kind = "orchestration_event" if name in ORCHESTRATION_TOOLS else "tool_call"
            normalized.append(
                {
                    **base,
                    "event_kind": event_kind,
                    "stage": "call",
                    "call_id": call_id,
                    "name": name,
                    "arguments": arguments,
                }
            )
            return normalized

        if item_type == "function_call_output":
            call_id = payload.get("call_id")
            name = session_state.setdefault("call_names", {}).get(call_id)
            event_kind = "orchestration_event" if name in ORCHESTRATION_TOOLS else "tool_result"
            normalized.append(
                {
                    **base,
                    "event_kind": event_kind,
                    "stage": "output",
                    "call_id": call_id,
                    "name": name,
                    "output": payload.get("output"),
                }
            )
            return normalized

        return normalized

    def migrate_legacy_events_if_needed(self, session_id: str) -> None:
        parts_dir = self.session_events_parts_dir(session_id)
        legacy_path = self.legacy_session_events_path(session_id)
        if parts_dir.exists() or not legacy_path.exists():
            return
        lines = legacy_path.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
        write_chunked_lines(parts_dir, lines, self.events_max_bytes, ".jsonl")
        legacy_path.unlink()

    def append_normalized_events(self, session_id: str, events: Iterable[Dict[str, Any]]) -> None:
        self.migrate_legacy_events_if_needed(session_id)
        parts_dir = self.session_events_parts_dir(session_id)
        existing_lines: List[str] = []
        if parts_dir.exists():
            for path in sorted(parts_dir.glob("part-*.jsonl")):
                existing_lines.extend(path.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True))
        for event in events:
            existing_lines.append(json.dumps(event, ensure_ascii=False) + "\n")
        write_chunked_lines(parts_dir, existing_lines, self.events_max_bytes, ".jsonl")

    def read_normalized_events(self, session_id: str) -> List[Dict[str, Any]]:
        self.migrate_legacy_events_if_needed(session_id)
        events: List[Dict[str, Any]] = []
        parts_dir = self.session_events_parts_dir(session_id)
        if parts_dir.exists():
            paths = sorted(parts_dir.glob("part-*.jsonl"))
        else:
            legacy = self.legacy_session_events_path(session_id)
            paths = [legacy] if legacy.exists() else []
        for path in paths:
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        payload = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(payload, dict):
                        events.append(payload)
        return events

    def collect_part_metadata(self, directory: Path, suffix: str) -> List[Dict[str, Any]]:
        if not directory.exists():
            return []
        return [
            {"path": str(path), "size_bytes": path.stat().st_size}
            for path in sorted(directory.glob(f"part-*{suffix}"))
        ]

    def write_meta(self, source: RolloutSource, thread_info: ThreadInfo) -> None:
        session_state = self.load_session_state(source.session_id)
        payload = {
            "session_id": source.session_id,
            "thread_name": thread_info.thread_name,
            "thread_title": thread_info.thread_title,
            "source_kind": thread_info.source_kind,
            "parent_thread_id": thread_info.parent_thread_id,
            "agent_nickname": thread_info.agent_nickname,
            "agent_role": thread_info.agent_role,
            "depth": thread_info.depth,
            "rollout_path": str(source.path),
            "origin": source.origin,
            "archived": source.archived,
            "first_seen_at": session_state.get("first_seen_at"),
            "last_seen_at": session_state.get("last_seen_at"),
            "events_parts": self.collect_part_metadata(self.session_events_parts_dir(source.session_id), ".jsonl"),
            "transcript_parts": self.collect_part_metadata(self.session_transcript_parts_dir(source.session_id), ".md"),
        }
        write_json(self.session_meta_path(source.session_id), payload)

    def migrate_legacy_transcript_if_needed(self, session_id: str) -> None:
        parts_dir = self.session_transcript_parts_dir(session_id)
        legacy_path = self.legacy_session_transcript_path(session_id)
        if parts_dir.exists() or not legacy_path.exists():
            return
        lines = legacy_path.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
        write_chunked_lines(parts_dir, lines, self.transcript_max_bytes, ".md")
        legacy_path.unlink()

    def render_transcript(self, session_id: str, thread_info: ThreadInfo) -> None:
        events = self.read_normalized_events(session_id)
        lines = [
            "# Codex Session Transcript\n",
            "\n",
            f"- Session Name: {thread_info.thread_name}\n",
            f"- Session ID: {session_id}\n",
            f"- Source Kind: {thread_info.source_kind}\n",
            f"- Parent Thread ID: {thread_info.parent_thread_id or '-'}\n",
            f"- Agent Nickname: {thread_info.agent_nickname or '-'}\n",
            f"- Agent Role: {thread_info.agent_role or '-'}\n",
            f"- Source Rollout: {thread_info.rollout_path or '-'}\n",
            "\n",
        ]
        for event in events:
            title = {
                "user_message": "User",
                "assistant_message": "Assistant",
                "assistant_commentary": "Assistant Commentary",
                "tool_call": "Tool Call",
                "tool_result": "Tool Result",
                "orchestration_event": "Orchestration Event",
                "reasoning_event": "Reasoning Event",
            }.get(event.get("event_kind"), "Event")
            lines.extend(
                [
                    f"## {title}\n",
                    "\n",
                    f"- Timestamp: {event.get('timestamp') or '-'}\n",
                    f"- Turn ID: {event.get('turn_id') or '-'}\n",
                ]
            )
            if event.get("event_kind") in {"tool_call", "tool_result", "orchestration_event"}:
                lines.append(f"- Name: {event.get('name') or '-'}\n")
                if event.get("call_id"):
                    lines.append(f"- Call ID: {event.get('call_id')}\n")
                if event.get("stage"):
                    lines.append(f"- Stage: {event.get('stage')}\n")
            elif event.get("event_kind") == "reasoning_event":
                lines.append(
                    f"- Has Encrypted Content: {'true' if event.get('has_encrypted_content') else 'false'}\n"
                )
            lines.append("\n")
            if event.get("event_kind") in {"user_message", "assistant_message", "assistant_commentary"}:
                lines.append((event.get("content") or "") + "\n")
            elif event.get("event_kind") in {"tool_call", "orchestration_event"} and event.get("stage") == "call":
                lines.extend(["```json\n", json.dumps(event.get("arguments"), ensure_ascii=False, indent=2) + "\n", "```\n"])
            elif event.get("event_kind") == "tool_result" or (
                event.get("event_kind") == "orchestration_event" and event.get("stage") == "output"
            ):
                lines.extend(["```text\n", str(event.get("output") or "") + "\n", "```\n"])
            elif event.get("event_kind") == "reasoning_event":
                lines.append("Detected a reasoning event. Hidden raw reasoning is not exported.\n")
            lines.append("\n")
        self.migrate_legacy_transcript_if_needed(session_id)
        write_chunked_lines(self.session_transcript_parts_dir(session_id), lines, self.transcript_max_bytes, ".md")

    def extract_plan_bodies(self, text: str) -> List[str]:
        return [match.group(1).strip() for match in PLAN_BLOCK_RE.finditer(text)]

    def compute_plan_hash(self, session_id: str, source_turn_id: Optional[str], plan_body: str) -> str:
        digest = hashlib.sha256()
        digest.update(session_id.encode("utf-8"))
        digest.update(b"\n")
        digest.update((source_turn_id or "").encode("utf-8"))
        digest.update(b"\n")
        digest.update(plan_body.encode("utf-8"))
        return digest.hexdigest()

    def allocate_plan_base_path(self, title: str) -> Path:
        base_name = normalize_filename(title)
        candidate = self.plans_dir / f"{base_name}.md"
        occupied = {Path(meta.get("path", "")).resolve() for meta in self.plan_manifest.values() if meta.get("path")}
        if candidate.resolve() not in occupied and not candidate.exists():
            return candidate
        index = 2
        while True:
            candidate = self.plans_dir / f"{base_name}-{index}.md"
            if candidate.resolve() not in occupied and not candidate.exists():
                return candidate
            index += 1

    def write_plan_content(self, base_path: Path, content: str) -> List[str]:
        prefix = base_path.stem
        suffix = base_path.suffix
        for old in base_path.parent.glob(f"{prefix}.part-*{suffix}"):
            old.unlink()
        lines = content.splitlines(keepends=True)
        if len(content.encode("utf-8")) <= self.plan_max_bytes:
            base_path.write_text(content, encoding="utf-8")
            return [str(base_path)]

        if base_path.exists():
            base_path.unlink()
        metadata = write_chunked_lines(base_path.parent, lines, self.plan_max_bytes, suffix)
        paths: List[str] = []
        for index, meta in enumerate(metadata, start=1):
            original = Path(meta["path"])
            target = original.with_name(f"{prefix}.part-{index:04d}{suffix}")
            if target.exists():
                target.unlink()
            original.rename(target)
            paths.append(str(target))
        return paths

    def persist_plan(
        self,
        source: RolloutSource,
        thread_info: ThreadInfo,
        source_turn_id: Optional[str],
        plan_mode_confirmed: bool,
        plan_generated_at: Optional[str],
        plan_body: str,
    ) -> None:
        plan_hash = self.compute_plan_hash(source.session_id, source_turn_id, plan_body)
        if plan_hash in self.plan_manifest:
            return
        title = extract_plan_title(plan_body)
        base_path = self.allocate_plan_base_path(title)
        generated_at = plan_generated_at or utc_now()
        front_matter = [
            "---\n",
            f"title: {yaml_escape(title)}\n",
            f"session_id: {yaml_escape(source.session_id)}\n",
            f"thread_name: {yaml_escape(thread_info.thread_name)}\n",
            f"source_kind: {yaml_escape(thread_info.source_kind)}\n",
            f"parent_thread_id: {yaml_escape(thread_info.parent_thread_id)}\n",
            f"agent_nickname: {yaml_escape(thread_info.agent_nickname)}\n",
            f"agent_role: {yaml_escape(thread_info.agent_role)}\n",
            f"source_turn_id: {yaml_escape(source_turn_id)}\n",
            f"plan_mode_confirmed: {'true' if plan_mode_confirmed else 'false'}\n",
            f"plan_generated_at: {yaml_escape(generated_at)}\n",
            f"source_rollout: {yaml_escape(str(source.path))}\n",
            f"extracted_at: {yaml_escape(utc_now())}\n",
            "---\n",
            "\n",
        ]
        paths = self.write_plan_content(base_path, "".join(front_matter) + plan_body.rstrip() + "\n")
        for path_str in paths:
            set_file_timestamps(Path(path_str), generated_at)
        self.plan_manifest[plan_hash] = {
            "path": paths[0],
            "paths": paths,
            "session_id": source.session_id,
            "thread_name": thread_info.thread_name,
            "source_kind": thread_info.source_kind,
            "parent_thread_id": thread_info.parent_thread_id,
            "agent_nickname": thread_info.agent_nickname,
            "agent_role": thread_info.agent_role,
            "source_turn_id": source_turn_id,
            "plan_mode_confirmed": plan_mode_confirmed,
            "plan_generated_at": generated_at,
            "source_rollout": str(source.path),
            "title": title,
        }

    def write_thread_index(self, thread_registry: Dict[str, ThreadInfo]) -> None:
        threads_payload: Dict[str, Any] = {}
        children_by_parent: Dict[str, List[str]] = {}
        session_ids = {path.stem for path in self.session_states_dir.glob("*.json")} | set(thread_registry.keys())
        for session_id in sorted(session_ids):
            session_state = self.load_session_state(session_id)
            thread_info = thread_registry.get(session_id)
            if thread_info is None:
                rollout = session_state.get("source_rollout") or ""
                thread_info = self.fallback_thread_info(
                    session_id,
                    RolloutSource(
                        session_id,
                        Path(rollout) if rollout else Path(),
                        str(session_state.get("origin") or ""),
                        bool(session_state.get("archived")),
                    ),
                )
            threads_payload[session_id] = {
                "session_id": session_id,
                "thread_name": thread_info.thread_name,
                "thread_title": thread_info.thread_title,
                "source_kind": thread_info.source_kind,
                "parent_thread_id": thread_info.parent_thread_id,
                "agent_nickname": thread_info.agent_nickname,
                "agent_role": thread_info.agent_role,
                "depth": thread_info.depth,
                "rollout_path": thread_info.rollout_path,
                "archived": thread_info.archived,
                "first_seen_at": session_state.get("first_seen_at"),
                "last_seen_at": session_state.get("last_seen_at"),
            }
            if thread_info.parent_thread_id:
                children_by_parent.setdefault(thread_info.parent_thread_id, []).append(session_id)
        payload = {
            "generated_at": utc_now(),
            "threads": threads_payload,
            "children_by_parent": {key: sorted(value) for key, value in sorted(children_by_parent.items())},
        }
        write_json(self.thread_index_path, payload)

    def audit_archive(self) -> int:
        thread_registry = self.load_thread_registry()
        sources = self.discover_rollout_sources(thread_registry, full_scan=True)
        actual_session_ids = {path.stem for path in self.session_states_dir.glob("*.json")}
        expected_session_ids = set(sources.keys())

        missing_sessions = sorted(expected_session_ids - actual_session_ids)
        orphan_sessions = sorted(actual_session_ids - expected_session_ids)
        incomplete_sessions: List[str] = []
        active_incomplete_sessions: List[str] = []
        missing_meta: List[str] = []

        for session_id, source in sorted(sources.items()):
            state = self.load_session_state(session_id)
            if int(state.get("last_offset", 0) or 0) != source.path.stat().st_size:
                if state.get("current_turn_id"):
                    active_incomplete_sessions.append(session_id)
                else:
                    incomplete_sessions.append(session_id)
            if not self.session_meta_path(session_id).exists():
                missing_meta.append(session_id)

        children_expected: Dict[str, List[str]] = {}
        for session_id, info in thread_registry.items():
            if info.parent_thread_id:
                children_expected.setdefault(info.parent_thread_id, []).append(session_id)
        children_expected = {key: sorted(value) for key, value in sorted(children_expected.items())}

        thread_index = load_json(self.thread_index_path, {"children_by_parent": {}})
        children_actual = thread_index.get("children_by_parent", {}) if isinstance(thread_index, dict) else {}
        parent_mismatches: List[str] = []
        for parent in sorted(set(children_expected) | set(children_actual)):
            if sorted(children_expected.get(parent, [])) != sorted(children_actual.get(parent, [])):
                parent_mismatches.append(parent)

        lines = [
            "# Archive Audit",
            "",
            f"- Generated At: {utc_now()}",
            f"- Last Discovery Mode: {self.manifest.get('last_discovery_mode') or 'unknown'}",
            f"- Last Full Scan At: {self.manifest.get('last_full_scan_at') or 'n/a'}",
            f"- Last Incremental Scan At: {self.manifest.get('last_incremental_scan_at') or 'n/a'}",
            f"- Last Discovered Source Count: {self.manifest.get('last_discovered_source_count', 0)}",
            f"- Last New Source Count: {self.manifest.get('last_new_source_count', 0)}",
            f"- Last Processed Source Count: {self.manifest.get('last_processed_source_count', 0)}",
            f"- Expected Sessions: {len(expected_session_ids)}",
            f"- Archived Sessions: {len(actual_session_ids)}",
            f"- Missing Sessions: {len(missing_sessions)}",
            f"- Orphan Sessions: {len(orphan_sessions)}",
            f"- Incomplete Sessions: {len(incomplete_sessions)}",
            f"- Active Incomplete Sessions: {len(active_incomplete_sessions)}",
            f"- Missing Meta Files: {len(missing_meta)}",
            f"- Parent Link Mismatches: {len(parent_mismatches)}",
            "",
        ]

        for title, values in (
            ("Missing Sessions", missing_sessions),
            ("Orphan Sessions", orphan_sessions),
            ("Incomplete Sessions", incomplete_sessions),
            ("Active Incomplete Sessions", active_incomplete_sessions),
            ("Missing Meta Files", missing_meta),
            ("Parent Link Mismatches", parent_mismatches),
        ):
            if values:
                lines.extend([f"## {title}", ""])
                lines.extend([f"- `{value}`" for value in values])
                lines.append("")

        if not any((missing_sessions, orphan_sessions, incomplete_sessions, missing_meta, parent_mismatches)):
            lines.extend(["Archive audit passed with no gaps detected.", ""])

        self.archive_audit_report_path.write_text("\n".join(lines), encoding="utf-8")
        return 1 if any((missing_sessions, orphan_sessions, incomplete_sessions, missing_meta, parent_mismatches)) else 0

    def audit_filenames(self) -> int:
        invalid_paths: List[str] = []
        for path in sorted(self.output_dir.rglob("*")):
            if not path.is_file():
                continue
            rel_parts = path.relative_to(self.output_dir).parts
            if any(not is_filename_component_valid(part) for part in rel_parts):
                invalid_paths.append(path.relative_to(self.output_dir).as_posix())

        lines = [
            "# Filename Audit",
            "",
            f"- Generated At: {utc_now()}",
            f"- Invalid Paths: {len(invalid_paths)}",
            "",
        ]
        if invalid_paths:
            lines.extend(["## Invalid Paths", ""])
            lines.extend([f"- `{path}`" for path in invalid_paths])
            lines.append("")
        else:
            lines.extend(["All archived file names passed validation.", ""])
        self.filename_audit_report_path.write_text("\n".join(lines), encoding="utf-8")
        return 1 if invalid_paths else 0

    def repair_filenames(self) -> int:
        audit_before = self.audit_filenames()
        used_paths: set[Path] = set()
        changed = 0
        for plan_hash, meta in sorted(self.plan_manifest.items()):
            title = str(meta.get("title") or "untitled-plan")
            old_paths = [Path(path) for path in meta.get("paths", []) if path]
            if not old_paths:
                old_paths = [Path(meta.get("path"))] if meta.get("path") else []
            if not old_paths:
                continue
            base_name = normalize_filename(title)
            target_base = self.plans_dir / f"{base_name}.md"
            suffix_index = 2
            while target_base in used_paths or (target_base.exists() and target_base not in old_paths):
                target_base = self.plans_dir / f"{base_name}-{suffix_index}.md"
                suffix_index += 1

            new_paths: List[Path] = []
            if len(old_paths) == 1:
                new_paths = [target_base]
            else:
                new_paths = [
                    target_base.with_name(f"{target_base.stem}.part-{index:04d}{target_base.suffix}")
                    for index in range(1, len(old_paths) + 1)
                ]

            if [path.resolve() for path in old_paths] != [path.resolve() for path in new_paths]:
                for old_path, new_path in zip(old_paths, new_paths):
                    if old_path.exists():
                        new_path.parent.mkdir(parents=True, exist_ok=True)
                        if new_path.exists() and new_path != old_path:
                            new_path.unlink()
                        old_path.rename(new_path)
                changed += 1

            for path in new_paths:
                used_paths.add(path)
            meta["path"] = str(new_paths[0])
            meta["paths"] = [str(path) for path in new_paths]

        self.save_plan_manifest()
        audit_after = self.audit_filenames()
        return 0 if audit_after == 0 else max(1, audit_before or changed)

    def verify_retention(self) -> int:
        if not self.plan_manifest:
            self.retention_report_path.write_text("# Retention Audit\n\nNo plans recorded in manifest.\n", encoding="utf-8")
            return 0
        source_hashes: Dict[str, set[str]] = {}
        missing: List[Tuple[str, Dict[str, Any]]] = []
        for plan_hash, meta in sorted(self.plan_manifest.items()):
            source_rollout = meta.get("source_rollout")
            if not isinstance(source_rollout, str) or not source_rollout:
                missing.append((plan_hash, meta))
                continue
            if source_rollout not in source_hashes:
                source_hashes[source_rollout] = self.extract_plan_hashes_from_rollout(Path(source_rollout))
            if plan_hash not in source_hashes[source_rollout]:
                missing.append((plan_hash, meta))
        lines = [
            "# Retention Audit",
            "",
            f"- Generated At: {utc_now()}",
            f"- Total Plans Checked: {len(self.plan_manifest)}",
            f"- Missing Plans: {len(missing)}",
            "",
        ]
        if missing:
            lines.extend(["## Missing Plans", ""])
            for plan_hash, meta in missing:
                lines.append(
                    f"- `{meta.get('title') or 'untitled-plan'}` "
                    f"(session `{meta.get('session_id')}`, hash `{plan_hash}`)"
                )
            lines.append("")
        else:
            lines.extend(["All tracked plans are still discoverable from their source rollout files.", ""])
        self.retention_report_path.write_text("\n".join(lines), encoding="utf-8")
        return 1 if missing else 0

    def extract_plan_hashes_from_rollout(self, rollout_path: Path) -> set[str]:
        hashes: set[str] = set()
        if not rollout_path.exists():
            return hashes
        session_id = infer_session_id(rollout_path) or ""
        current_turn_id: Optional[str] = None
        with rollout_path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                payload = row.get("payload", {})
                if row.get("type") == "event_msg":
                    event_type = payload.get("type")
                    turn_id = payload.get("turn_id")
                    if event_type == "task_started" and turn_id:
                        current_turn_id = turn_id
                    elif event_type in TERMINAL_TASK_EVENTS and turn_id and turn_id == current_turn_id:
                        current_turn_id = None
                    continue
                if row.get("type") != "response_item" or payload.get("type") != "message" or payload.get("role") != "assistant":
                    continue
                text = collect_message_text(payload.get("content"))
                if not text:
                    continue
                for plan_body in self.extract_plan_bodies(text):
                    hashes.add(self.compute_plan_hash(session_id, current_turn_id, plan_body))
        return hashes

    def maybe_sync_git(self) -> bool:
        if not self.auto_git:
            return False

        last_sync = self.manifest.get("last_git_sync_at")
        if isinstance(last_sync, str):
            try:
                last_epoch = datetime.fromisoformat(last_sync.replace("Z", "+00:00")).timestamp()
            except ValueError:
                last_epoch = 0.0
            if time.time() - last_epoch < self.git_commit_interval_seconds:
                return False

        repo_root = Path(
            run_git_command(self.output_dir, ["git", "rev-parse", "--show-toplevel"]).stdout.strip()
        )
        output_rel = self.output_dir.resolve().relative_to(repo_root.resolve()).as_posix()

        pre_staged = run_git_command(repo_root, ["git", "diff", "--cached", "--name-only"]).stdout.splitlines()
        if any(path.strip() and not path.strip().startswith(output_rel + "/") and path.strip() != output_rel for path in pre_staged):
            raise RuntimeError("refusing auto git sync because staged changes exist outside output/codex-archive")

        archive_status = run_git_command(repo_root, ["git", "status", "--porcelain", "--", output_rel]).stdout.strip()
        if not archive_status:
            return False

        run_git_command(repo_root, ["git", "add", "-A", "--", output_rel])
        staged = [line.strip() for line in run_git_command(repo_root, ["git", "diff", "--cached", "--name-only"]).stdout.splitlines() if line.strip()]
        if any(not path.startswith(output_rel + "/") and path != output_rel for path in staged):
            raise RuntimeError("refusing auto git sync because staged set escaped output/codex-archive")
        if not staged:
            return False

        commit_message = f"codex-archive: sync {utc_now()}"
        run_git_command(repo_root, ["git", "commit", "-m", commit_message])
        run_git_command(repo_root, ["git", "push", self.git_remote, "HEAD"])
        self.manifest["last_git_sync_at"] = utc_now()
        self.save_manifest()
        return True


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Archive Codex Desktop sessions and proposed plans")
    parser.add_argument("--codex-home", type=Path, default=default_codex_home(), help="Codex home path")
    parser.add_argument("--output-dir", type=Path, default=Path("output/codex-archive"), help="Archive output directory")
    parser.add_argument("--poll-seconds", type=float, default=1.0, help="Polling interval for follow mode")
    parser.add_argument("--events-max-bytes", type=int, default=DEFAULT_EVENTS_MAX_BYTES, help="Max bytes per events part")
    parser.add_argument("--transcript-max-bytes", type=int, default=DEFAULT_TRANSCRIPT_MAX_BYTES, help="Max bytes per transcript part")
    parser.add_argument("--plan-max-bytes", type=int, default=DEFAULT_PLAN_MAX_BYTES, help="Max bytes per plan file part")
    parser.add_argument("--auto-git", action="store_true", help="Auto commit and push archive changes")
    parser.add_argument("--git-remote", default="origin", help="Remote to push when --auto-git is enabled")
    parser.add_argument("--git-commit-interval-seconds", type=int, default=300, help="Minimum seconds between auto git sync operations")
    parser.add_argument("--backfill-only", action="store_true", help="Process historical sessions then exit")
    parser.add_argument("--follow-only", action="store_true", help="Watch for new data only")
    parser.add_argument("--verify-retention", action="store_true", help="Verify that tracked plan hashes still exist")
    parser.add_argument("--audit-archive", action="store_true", help="Audit whether all source sessions are archived completely")
    parser.add_argument("--audit-filenames", action="store_true", help="Audit archive file names for invalid characters")
    parser.add_argument("--repair-filenames", action="store_true", help="Repair invalid dynamic file names under the archive path")
    parser.add_argument("--rescan", action="store_true", help="Force a full rollout source rescan before processing")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    mode_count = sum(
        bool(flag)
        for flag in (
            args.backfill_only,
            args.follow_only,
            args.verify_retention,
            args.audit_archive,
            args.audit_filenames,
            args.repair_filenames,
        )
    )
    if mode_count > 1:
        parser.error(
            "only one of --backfill-only, --follow-only, --verify-retention, --audit-archive, "
            "--audit-filenames, or --repair-filenames may be set"
        )

    watcher = CodexArchiveWatcher(
        codex_home=args.codex_home.expanduser().resolve(),
        output_dir=args.output_dir.expanduser().resolve(),
        poll_seconds=args.poll_seconds,
        events_max_bytes=args.events_max_bytes,
        transcript_max_bytes=args.transcript_max_bytes,
        plan_max_bytes=args.plan_max_bytes,
        auto_git=args.auto_git,
        git_remote=args.git_remote,
        git_commit_interval_seconds=args.git_commit_interval_seconds,
    )

    if args.verify_retention:
        return watcher.verify_retention()
    if args.audit_archive:
        return watcher.audit_archive()
    if args.audit_filenames:
        return watcher.audit_filenames()
    if args.repair_filenames:
        return watcher.repair_filenames()

    if args.follow_only:
        watcher.seed_follow_only(
            watcher.discover_rollout_sources(
                watcher.load_thread_registry(force_refresh=args.rescan),
                full_scan=True if args.rescan else None,
            )
        )
        watcher.write_thread_index(watcher.load_thread_registry())
        watcher.save_manifest()
        watcher.save_plan_manifest()
        try:
            while True:
                watcher.process_all_sources(full_scan=False)
                time.sleep(args.poll_seconds)
        except KeyboardInterrupt:
            return 0

    watcher.process_all_sources(full_scan=True if args.rescan else None)
    if args.backfill_only:
        return 0

    try:
        while True:
            watcher.process_all_sources(full_scan=False)
            time.sleep(args.poll_seconds)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
