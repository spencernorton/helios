"""Discover Antigravity subscription models without enabling API billing.

The Gemini CLI is a different transport and is not interchangeable with agy.
These bounded synchronous probes belong on a background worker, never GTK's
main loop. Catalog availability does not by itself prove account sign-in.
"""

from __future__ import annotations

import base64
import json
import os
import re
from datetime import datetime, timezone
import shutil
import sqlite3
import subprocess
import threading
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from helios.backend.process.env_scrub import scrubbed_child_env
from helios.log import get_logger

_log = get_logger("google-env")


class GoogleBinaryNotFound(FileNotFoundError):
    """The Antigravity CLI binary could not be found."""


class GoogleSubscriptionRequired(ValueError):
    """A configured API provider would bypass the subscription route."""


@dataclass(slots=True)
class GoogleBinary:
    path: Path
    version: str = ""
    source: str = ""


def find_google_binary() -> GoogleBinary:
    """Locate Antigravity CLI; never substitute the Gemini CLI."""
    env_override = os.environ.get("HELIOS_GOOGLE_BINARY")
    if env_override:
        p = Path(env_override).expanduser()
        if p.is_file() and os.access(p, os.X_OK):
            return GoogleBinary(p, source="$HELIOS_GOOGLE_BINARY")
        raise GoogleBinaryNotFound("$HELIOS_GOOGLE_BINARY is not an executable file.")

    candidates = [
        ("agy", "PATH"),
        (str(Path.home() / ".gemini" / "antigravity" / "bin" / "agy"), "~/.gemini"),
        (str(Path.home() / ".local" / "bin" / "agy"), "~/.local/bin"),
        ("/opt/homebrew/bin/agy", "/opt/homebrew/bin"),
        ("/usr/local/bin/agy", "/usr/local/bin"),
    ]

    for name_or_path, src in candidates:
        which = shutil.which(name_or_path)
        if which:
            p = Path(which)
            if p.is_file() and os.access(p, os.X_OK):
                return GoogleBinary(p, source=src)
        p = Path(name_or_path).expanduser()
        if p.is_file() and os.access(p, os.X_OK):
            return GoogleBinary(p, source=src)

    raise GoogleBinaryNotFound(
        "Antigravity CLI (agy) not found on PATH. "
        "Install it or set $HELIOS_GOOGLE_BINARY."
    )


@dataclass(frozen=True, slots=True)
class GoogleModel:
    id: str
    label: str
    reasoning_efforts: tuple[tuple[str, str], ...] = ()
    default_effort: str = ""


@dataclass(slots=True)
class GoogleAuthStatus:
    """Observed CLI catalog and account status, without credential contents."""

    logged_in: bool
    email: str = ""
    plan_name: str = ""
    auth_method: str = ""
    binary_path: str = ""
    models: tuple[GoogleModel, ...] = ()
    catalog_status: str = "unavailable"
    auth_warning: str = ""
    error: str = ""
    raw: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.error


def subscription_configuration_error() -> str:
    """Reject settings that explicitly select an API/custom provider."""
    path = Path.home() / ".gemini" / "antigravity-cli" / "settings.json"
    if not path.exists():
        return ""
    try:
        settings = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "Cannot verify Antigravity settings. Check ~/.gemini/antigravity-cli/settings.json."
    if not isinstance(settings, dict):
        return "Antigravity settings must be a JSON object."
    if "useG1Credits" in settings and settings["useG1Credits"] is not False:
        return (
            "Antigravity AI-credit overages are enabled or invalid. "
            "Turn off AI-credit fallback before using Helios."
        )
    provider = settings.get("modelProvider", "")
    if not isinstance(provider, str) or provider not in {"", "antigravity"}:
        return (
            "Antigravity is configured for an API/custom provider. "
            "Choose the Antigravity Google-account provider before using Helios."
        )
    return ""


def google_subscription_env(*, keep: tuple[str, ...] | frozenset[str] = ()) -> dict[str, str]:
    """Return a scrubbed account-only environment, refusing API settings."""
    error = subscription_configuration_error()
    if error:
        raise GoogleSubscriptionRequired(error)
    env = scrubbed_child_env(keep=keep)
    for name in tuple(env):
        upper = name.upper()
        if upper.startswith((
            "GEMINI_", "GOOGLE_", "ANTHROPIC_", "OPENAI_", "ANTIGRAVITY_",
            "CLAUDE_", "CODEX_", "HELIOS_", "CLOUDSDK_", "AGY_",
        )) or upper == "GCLOUD_PROJECT":
            del env[name]
    return env


_MODEL_ID = re.compile(r"gemini-[a-zA-Z0-9][a-zA-Z0-9._-]*\Z")
_EFFORT_SUFFIX_RE = re.compile(r"-(low|medium|high|xhigh|max)\Z")
_LABEL_EFFORT_RE = re.compile(r"\s*\((Low|Medium|High|X-High|Max)\)\Z")
_EFFORT_RANK = {"off": 0, "low": 1, "medium": 2, "high": 3, "xhigh": 4, "max": 5}


def parse_model_catalog(output: str) -> tuple[GoogleModel, ...]:
    """Read agy models id/label output, collapsing effort variants into base models."""
    grouped: dict[str, dict] = {}
    for line in output.splitlines():
        parts = line.split("\t", 1)
        if len(parts) != 2:
            continue
        mid, label = (part.strip() for part in parts)
        if not _MODEL_ID.fullmatch(mid) or not label:
            continue
        effort_m = _EFFORT_SUFFIX_RE.search(mid)
        if effort_m:
            effort_key = effort_m.group(1)
            base_id = mid[:effort_m.start()]
            label_m = _LABEL_EFFORT_RE.search(label)
            if label_m:
                base_label = label[:label_m.start()].strip()
                effort_label = label_m.group(1)
            else:
                base_label = label
                effort_label = effort_key.capitalize()
        else:
            effort_key = None
            effort_label = None
            base_id = mid
            base_label = label

        if base_id not in grouped:
            grouped[base_id] = {"label": base_label, "efforts": {}}
        if effort_key and effort_label:
            grouped[base_id]["efforts"][effort_key] = effort_label

    result: list[GoogleModel] = []
    for base_id, data in grouped.items():
        efforts_dict = data["efforts"]
        if efforts_dict:
            sorted_efforts = tuple(
                sorted(efforts_dict.items(), key=lambda item: _EFFORT_RANK.get(item[0], 99))
            )
            effort_keys = {k for k, _ in sorted_efforts}
            if "high" in effort_keys:
                default_effort = "high"
            elif "medium" in effort_keys:
                default_effort = "medium"
            else:
                default_effort = sorted_efforts[-1][0]
        else:
            sorted_efforts = ()
            default_effort = ""
        result.append(
            GoogleModel(
                id=base_id,
                label=data["label"],
                reasoning_efforts=sorted_efforts,
                default_effort=default_effort,
            )
        )
    return tuple(result)


def account_quota_verified(output: str) -> bool:
    """Validate the native /usage envelope without retaining account data."""
    try:
        result = json.loads(output)
    except (ValueError, TypeError):
        return False
    if not isinstance(result, dict) or result.get("status") != "SUCCESS":
        return False
    command = result.get("command")
    if not isinstance(command, dict) or command.get("name") != "usage":
        return False
    data = command.get("data")
    if not isinstance(data, dict):
        return False
    groups = data.get("groups")
    return isinstance(groups, list) and any(
        isinstance(group, dict)
        and isinstance(group.get("name"), str)
        and group["name"]
        and isinstance(group.get("buckets"), list)
        and group["buckets"]
        and all(isinstance(bucket, dict) for bucket in group["buckets"])
        for group in groups
    )


_CACHE_SECONDS = 60.0
_CACHE_LOCK = threading.Lock()
_AUTH_CACHE: tuple[tuple, float, GoogleAuthStatus] | None = None


def _stat_key(path: Path) -> tuple:
    try:
        stat = path.stat()
        return str(path), stat.st_mtime_ns, stat.st_size
    except OSError:
        return str(path), None, None


def _cache_key(binary: GoogleBinary) -> tuple:
    return (
        _stat_key(binary.path),
        _stat_key(Path.home() / ".gemini" / "antigravity-cli" / "settings.json"),
        str(Path.home()),
    )


def cached_auth_status() -> GoogleAuthStatus | None:
    """Return a fresh cached observation without waiting for a worker probe."""
    if subscription_configuration_error():
        return None
    try:
        key = _cache_key(find_google_binary())
    except GoogleBinaryNotFound:
        return None
    if not _CACHE_LOCK.acquire(blocking=False):
        return None
    try:
        if _AUTH_CACHE and _AUTH_CACHE[0] == key:
            if time.monotonic() - _AUTH_CACHE[1] < _CACHE_SECONDS:
                return replace(_AUTH_CACHE[2], raw=dict(_AUTH_CACHE[2].raw))
        return None
    finally:
        _CACHE_LOCK.release()


def fetch_auth_status(timeout: float = 8.0, *, force: bool = False) -> GoogleAuthStatus:
    """Discover live models and account quotas; failures never imply sign-in."""
    global _AUTH_CACHE
    try:
        env = google_subscription_env()
        binary = find_google_binary()
    except (GoogleBinaryNotFound, GoogleSubscriptionRequired) as exc:
        return GoogleAuthStatus(logged_in=False, error=str(exc))
    key = _cache_key(binary)
    with _CACHE_LOCK:
        if not force and _AUTH_CACHE and _AUTH_CACHE[0] == key:
            if time.monotonic() - _AUTH_CACHE[1] < _CACHE_SECONDS:
                return replace(_AUTH_CACHE[2], raw=dict(_AUTH_CACHE[2].raw))
        status = _probe_account(binary, env, timeout)
        _AUTH_CACHE = key, time.monotonic(), status
        return replace(status, raw=dict(status.raw))


def _probe_account(binary: GoogleBinary, env: dict[str, str], timeout: float) -> GoogleAuthStatus:
    status = GoogleAuthStatus(
        logged_in=False,
        binary_path=str(binary.path),
        auth_method="oauth-unverified",
    )
    deadline = time.monotonic() + timeout
    try:
        proc = subprocess.run(
            [str(binary.path), "models"], capture_output=True, text=True,
            timeout=timeout, env=env,
        )
    except (subprocess.SubprocessError, OSError):
        status.error = "Antigravity model discovery failed. Check `agy models` in a terminal."
        return status
    if proc.returncode:
        status.error = "Antigravity could not list models. Open `agy` to check account sign-in."
        return status
    status.models = parse_model_catalog(proc.stdout)
    if not status.models:
        status.error = "Antigravity returned no Google Gemini models."
        return status
    status.catalog_status = "agy-models"
    status.auth_warning = "Account sign-in is not verified. Open `agy` to check sign-in."
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return status
    try:
        usage = subprocess.run(
            [str(binary.path), "--print", "/usage", "--output-format", "json"],
            capture_output=True, text=True, timeout=remaining, env=env,
        )
    except (subprocess.SubprocessError, OSError):
        return status
    if usage.returncode == 0 and account_quota_verified(usage.stdout):
        status.logged_in = True
        status.auth_method = "oauth-quota"
        status.plan_name = "Google account (tier not reported)"
        status.auth_warning = ""
        status.email = signed_in_email()
    return status


_AUTH_EMAIL_RE = re.compile(r"applyAuthResult: email=([^,\s]+)")


def signed_in_email() -> str:
    """The Google account agy is signed in as, for display only.

    agy has no status command that names the account. A file-backed sign-in
    keeps an id_token whose `email` claim we decode (never the access or
    refresh token); a keyring-backed sign-in (the default after `/login`)
    leaves no file, so fall back to agy's own log, which records the account
    on every authenticated start.
    """
    root = Path.home() / ".gemini" / "antigravity-cli"
    try:
        id_token = json.loads(
            (root / "antigravity-oauth-token").read_text(encoding="utf-8")
        ).get("id_token", "")
        payload = id_token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        email = json.loads(base64.urlsafe_b64decode(payload)).get("email", "")
        if isinstance(email, str) and email:
            return email
    except (OSError, ValueError, IndexError, AttributeError):
        pass
    try:
        logs = sorted((root / "log").glob("cli-*.log"), key=lambda f: f.stat().st_mtime)
    except OSError:
        return ""
    # newest 10 runs; Helios itself runs agy every catalog refresh.
    for log in reversed(logs[-10:]):
        try:
            match = _AUTH_EMAIL_RE.search(log.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
        if match:
            return match.group(1)
    return ""


def list_mcp_servers() -> list[dict[str, str]]:
    """Return configured MCP servers from ~/.gemini/config/mcp_config.json."""
    mcp_path = Path.home() / ".gemini" / "config" / "mcp_config.json"
    if not mcp_path.is_file():
        return []
    try:
        data = json.loads(mcp_path.read_text(encoding="utf-8"))
        servers = data.get("mcpServers", {})
        result = []
        for name, cfg in servers.items():
            cmd = cfg.get("command") or cfg.get("serverUrl") or ""
            result.append({"name": name, "command": cmd, "status": "configured"})
        return result
    except (OSError, json.JSONDecodeError) as e:
        _log.warning("could not read %s: %s", mcp_path, e)
        return []


def _antigravity_dir() -> Path:
    """Antigravity's own state directory (resolved per call for tests)."""
    return Path.home() / ".gemini" / "antigravity-cli"


@dataclass(frozen=True, slots=True)
class ConversationIdleSnapshot:
    """Read-only snapshot of an Antigravity conversation from its summary DB."""

    conversation_id: str
    status: str
    not_fully_idle: bool
    killed: bool
    step_count: int
    last_user_input_step_index: int
    last_modified_epoch: float


_FRACTIONAL_SECONDS_RE = re.compile(r"\.(\d+)")


def _parse_antigravity_timestamp(raw: Any) -> float | None:
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return float(raw)
    text = str(raw or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    text = _FRACTIONAL_SECONDS_RE.sub(lambda m: "." + m.group(1)[:6].ljust(6, "0"), text, count=1)
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def probe_conversation_idle(
    conversation_id: str,
    *,
    db_path: Path | None = None,
) -> ConversationIdleSnapshot | None:
    """Read Antigravity's summary SQLite DB read-only for one conversation.

    Returns None when the DB or row is absent or unreadable. Safe to call
    from a background worker thread; never mutates the DB.
    """
    cid = str(conversation_id or "").strip()
    if not cid or "/" in cid or "\\" in cid:
        return None
    path = db_path if db_path is not None else (
        _antigravity_dir() / "conversation_summaries.db"
    )
    if not path.is_file():
        return None
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=0.5)
        row = conn.execute(
            "SELECT status, not_fully_idle, killed, step_count, "
            "last_user_input_step_index, last_modified_time "
            "FROM conversation_summaries WHERE conversation_id = ? LIMIT 1",
            (cid,),
        ).fetchone()
    except (sqlite3.Error, OSError):
        return None
    finally:
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass
    if row is None:
        return None
    status, not_fully_idle, killed, step_count, last_user_step, raw_mtime = row
    mtime_epoch = _parse_antigravity_timestamp(raw_mtime)
    if mtime_epoch is None:
        return None
    try:
        steps = int(step_count)
        user_step = int(last_user_step)
    except (TypeError, ValueError):
        return None
    return ConversationIdleSnapshot(
        conversation_id=cid,
        status=str(status or ""),
        not_fully_idle=bool(not_fully_idle),
        killed=bool(killed),
        step_count=steps,
        last_user_input_step_index=user_step,
        last_modified_epoch=mtime_epoch,
    )


# ── Turn-completion evidence ────────────────────────────────────────────────
#
# Antigravity keeps two records of a conversation, and only one of them can
# say whether a turn is over:
#
# * ``conversation_summaries.db`` is an index rewritten in batches. Its row
#   routinely trails the conversation by tens of steps, even long after the
#   conversation finished, and it reads CASCADE_RUN_STATUS_IDLE in the middle
#   of a long generation and while a subagent works. Treating that row as
#   proof of an idle turn let Helios close live turns mid-answer.
# * ``conversations/<id>.db`` is the trajectory itself: one row per step with
#   that step's own status, written while the step runs. A finished chat turn
#   ends on a completed planner response that follows its user input.
#
# The summary row may therefore veto a recovery, but only the trajectory (and
# the trajectories of the conversation's subagents) can confirm one.
#
# Values are Antigravity's CortexStepType and CortexStepStatus enums, as
# compiled into the agy CLI.
STEP_TYPE_USER_INPUT = 14
STEP_TYPE_PLANNER_RESPONSE = 15
STEP_STATUS_DONE = 3
_STEP_STATUS_NAMES = {
    0: "unspecified", 1: "pending", 2: "running", 3: "done", 4: "invalid",
    5: "cleared", 6: "canceled", 7: "error", 8: "generating", 9: "waiting",
    11: "queued", 12: "interrupted",
}
# PENDING, RUNNING, GENERATING, WAITING and QUEUED: the step has not settled.
_STEP_STATUS_IN_PROGRESS = frozenset({1, 2, 8, 9, 11})
# Clock skew allowed between Helios's wall clock and Antigravity's writes.
_EVIDENCE_SKEW_SECONDS = 2.0
# A conversation's subagents accumulate over its whole life. Past this many,
# coverage is refused rather than truncated: an omitted child could be the
# one still working.
_MAX_SUBAGENTS = 4096
_CONVERSATION_DB_NAME_RE = re.compile(
    r"([0-9A-Za-z][0-9A-Za-z._-]*)\.db(?:-wal|-shm|-journal)?\Z"
)


def _safe_conversation_id(conversation_id: Any) -> str:
    cid = str(conversation_id or "").strip()
    if not cid or cid in {".", ".."} or any(c in cid for c in "/\\\x00"):
        return ""
    return cid


def _db_last_write_epoch(path: Path) -> float | None:
    """Newest write to an SQLite database: the file or its non-empty WAL.

    Stat before connecting. A read-only reader can create an empty ``-wal`` and
    rewrites ``-shm``, so neither of those counts as a write by Antigravity.
    """
    try:
        newest = path.stat().st_mtime
    except OSError:
        return None
    try:
        wal = path.with_name(path.name + "-wal").stat()
    except OSError:
        return newest
    if wal.st_size > 0:
        newest = max(newest, wal.st_mtime)
    return newest


@dataclass(frozen=True, slots=True)
class TrajectorySnapshot:
    """The newest step of an Antigravity trajectory (``conversations/<id>.db``)."""

    conversation_id: str
    newest_index: int
    newest_type: int
    newest_status: int
    last_user_input_index: int  # -1 when the trajectory holds no user input
    last_write_epoch: float

    @property
    def in_progress(self) -> bool:
        return self.newest_status in _STEP_STATUS_IN_PROGRESS

    @property
    def ends_with_final_response(self) -> bool:
        return (
            self.newest_type == STEP_TYPE_PLANNER_RESPONSE
            and self.newest_status == STEP_STATUS_DONE
            and self.newest_index > self.last_user_input_index
        )

    @property
    def newest_status_name(self) -> str:
        return _STEP_STATUS_NAMES.get(self.newest_status, f"status {self.newest_status}")


def _trajectory_path(conversation_id: str, conversations_dir: Path | None) -> Path | None:
    cid = _safe_conversation_id(conversation_id)
    if not cid:
        return None
    base = conversations_dir if conversations_dir is not None else (
        _antigravity_dir() / "conversations"
    )
    return base / f"{cid}.db"


def probe_conversation_trajectory(
    conversation_id: str,
    *,
    conversations_dir: Path | None = None,
) -> TrajectorySnapshot | None:
    """Read the newest step of one Antigravity trajectory, read-only.

    Returns None when the trajectory is absent, locked or unreadable; callers
    treat that as "not proven", never as finished. Safe on a worker thread.
    """
    path = _trajectory_path(conversation_id, conversations_dir)
    if path is None:
        return None
    cid = path.name[: -len(".db")]
    last_write = _db_last_write_epoch(path)
    if last_write is None:
        return None
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(
            f"{path.absolute().as_uri()}?mode=ro", uri=True, timeout=0.5
        )
        newest = conn.execute(
            "SELECT idx, step_type, status FROM steps ORDER BY idx DESC LIMIT 1"
        ).fetchone()
        last_user = conn.execute(
            "SELECT MAX(idx) FROM steps WHERE step_type = ?", (STEP_TYPE_USER_INPUT,)
        ).fetchone()
    except (sqlite3.Error, OSError, ValueError):
        return None
    finally:
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass
    if newest is None:
        return None
    values = (*newest, last_user[0] if last_user and last_user[0] is not None else -1)
    if not all(isinstance(v, int) and not isinstance(v, bool) for v in values):
        return None
    index, step_type, status, user_index = values
    return TrajectorySnapshot(
        conversation_id=cid,
        newest_index=index,
        newest_type=step_type,
        newest_status=status,
        last_user_input_index=user_index,
        last_write_epoch=last_write,
    )


def child_conversation_ids(
    conversation_id: str,
    *,
    db_path: Path | None = None,
) -> tuple[str, ...] | None:
    """Every subagent conversation under ``conversation_id``, nearest first.

    Parent links are written when a subagent is created, so they hold even
    when the rest of a summary row is stale. The whole tree is returned or
    nothing: None when the summary DB cannot be read or the tree is larger
    than Helios will inspect, which callers treat as "not proven".
    """
    cid = _safe_conversation_id(conversation_id)
    if not cid:
        return None
    path = db_path if db_path is not None else (
        _antigravity_dir() / "conversation_summaries.db"
    )
    if not path.is_file():
        return None
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(f"{path.absolute().as_uri()}?mode=ro", uri=True, timeout=0.5)
        columns = {
            row[1] for row in conn.execute("PRAGMA table_info(conversation_summaries)")
        }
        if "conversation_id" not in columns:
            return None
        if "parent_conversation_id" not in columns:
            return ()  # an agy without subagents records no parent links
        rows = conn.execute(
            "SELECT conversation_id, parent_conversation_id FROM conversation_summaries "
            "WHERE parent_conversation_id IS NOT NULL AND parent_conversation_id != ''"
        ).fetchall()
    except (sqlite3.Error, OSError, ValueError):
        return None
    finally:
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass
    by_parent: dict[str, list[str]] = {}
    for child, parent in rows:
        if isinstance(child, str) and isinstance(parent, str):
            by_parent.setdefault(parent, []).append(child)
    found: list[str] = []
    seen = {cid}
    frontier = [cid]
    while frontier:
        nxt: list[str] = []
        for parent in frontier:
            for child in by_parent.get(parent, ()):
                if child in seen:
                    continue
                if not _safe_conversation_id(child):
                    return None  # a child Helios cannot inspect
                seen.add(child)
                found.append(child)
                nxt.append(child)
                if len(found) > _MAX_SUBAGENTS:
                    return None
        frontier = nxt
    return tuple(found)


def open_conversation_ids(
    pid: int,
    *,
    proc_root: Path | None = None,
    conversations_dir: Path | None = None,
) -> frozenset[str] | None:
    """Conversation databases an agy process currently holds open.

    agy keeps a trajectory open while its cascade runs (a working subagent's
    as much as the conversation's own) and closes it once the cascade settles,
    so any open trajectory means work is still in flight. Returns None when the
    process's descriptors cannot be listed.
    """
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return None
    root = proc_root if proc_root is not None else Path("/proc")
    base = conversations_dir if conversations_dir is not None else (
        _antigravity_dir() / "conversations"
    )
    prefix = os.path.realpath(base) + os.sep
    fd_dir = root / str(pid) / "fd"
    try:
        entries = list(os.scandir(fd_dir))
    except OSError:
        return None
    ids: set[str] = set()
    for entry in entries:
        try:
            target = os.readlink(entry.path)
        except OSError:
            continue  # closed between the listing and the read
        if not target.startswith(prefix):
            continue
        match = _CONVERSATION_DB_NAME_RE.fullmatch(target[len(prefix):])
        if match:
            ids.add(match.group(1))
    return frozenset(ids)


def accepted_input_index(accepted_turn_id: Any, conversation_id: str) -> int | None:
    """The trajectory step index Helios recorded as a turn's accepted input.

    Google acceptance ids are ``<conversation>/user_input/<step index>``.
    """
    cid = _safe_conversation_id(conversation_id)
    prefix = f"{cid}/user_input/"
    text = str(accepted_turn_id or "")
    if not cid or not text.startswith(prefix):
        return None
    tail = text[len(prefix):]
    return int(tail) if tail.isdigit() else None


@dataclass(frozen=True, slots=True)
class TurnEvidence:
    """Whether Antigravity's own records prove a turn finished.

    ``progress_epoch`` is the newest trajectory write in the conversation and
    the subagents it started during the turn, so a caller can count writes the
    stdout stream never mentions (long generations, subagent work) as
    progress. 0.0 when unknown.
    """

    finished: bool
    reason: str
    progress_epoch: float = 0.0
    trajectory: TrajectorySnapshot | None = None
    summary: ConversationIdleSnapshot | None = None


def assess_turn_completion(
    conversation_id: str,
    *,
    turn_started_epoch: float,
    accepted_input_index: int | None,
    quiet_seconds: float,
    now_epoch: float | None = None,
    db_path: Path | None = None,
    conversations_dir: Path | None = None,
) -> TurnEvidence:
    """Decide from Antigravity's records alone whether a turn has finished.

    Finished means: the trajectory's newest step is a completed planner
    response that follows this turn's own user input; nothing in the
    conversation or any subagent it started this turn is still running or was
    written within ``quiet_seconds``; and the summary row, read last, does not
    disagree. Anything unreadable is "not proven". Safe on a worker thread.
    """
    now = time.time() if now_epoch is None else float(now_epoch)
    quiet = max(0.0, float(quiet_seconds))
    trajectory = probe_conversation_trajectory(
        conversation_id, conversations_dir=conversations_dir
    )
    if trajectory is None:
        return TurnEvidence(False, "its trajectory could not be read")
    progress = trajectory.last_write_epoch
    children = child_conversation_ids(conversation_id, db_path=db_path)
    busy_child = ""
    for child in children or ():
        path = _trajectory_path(child, conversations_dir)
        written = _db_last_write_epoch(path) if path is not None else None
        if written is None:
            # No trajectory to stat: not yet created, removed, or out of
            # reach. Only the subagent's own summary row can show it belongs
            # to an earlier turn; otherwise its state is unproven.
            row = probe_conversation_idle(child, db_path=db_path)
            if row is not None and row.last_modified_epoch < turn_started_epoch - _EVIDENCE_SKEW_SECONDS:
                continue
            if not busy_child:
                busy_child = f"subagent {child[:8]} has no trajectory to check"
            continue
        if written < turn_started_epoch - _EVIDENCE_SKEW_SECONDS:
            continue  # untouched since this turn began
        progress = max(progress, written)
        if busy_child:
            continue
        snap = probe_conversation_trajectory(child, conversations_dir=conversations_dir)
        if snap is None:
            # Touched this turn but unreadable (locked mid-write, or a layout
            # this Helios does not know): not settled.
            busy_child = f"subagent {child[:8]} was written this turn but could not be read"
        elif snap.in_progress or now - snap.last_write_epoch < quiet:
            busy_child = f"subagent {child[:8]} is still working"

    def not_finished(reason: str) -> TurnEvidence:
        return TurnEvidence(False, reason, progress, trajectory)

    if children is None:
        return not_finished("its subagent list could not be read")
    if trajectory.in_progress:
        return not_finished(
            f"step {trajectory.newest_index} is still {trajectory.newest_status_name}"
        )
    if busy_child:
        return not_finished(busy_child)
    if not trajectory.ends_with_final_response:
        return not_finished(
            f"its newest step ({trajectory.newest_index}, "
            f"{trajectory.newest_status_name}) is not a finished response"
        )
    if accepted_input_index is None or trajectory.last_user_input_index != accepted_input_index:
        return not_finished("its newest user input is not this turn's prompt")
    if now - trajectory.last_write_epoch < quiet:
        return not_finished("its trajectory was written moments ago")
    summary = probe_conversation_idle(conversation_id, db_path=db_path)
    if summary is None:
        return not_finished("it has no summary record")
    if (
        summary.status != "CASCADE_RUN_STATUS_IDLE"
        or summary.not_fully_idle
        or summary.killed
    ):
        return not_finished(
            f"its summary reports {summary.status or 'no status'}"
            + (" with work outstanding" if summary.not_fully_idle else "")
            + (" (killed)" if summary.killed else "")
        )
    if summary.last_modified_epoch < turn_started_epoch - _EVIDENCE_SKEW_SECONDS:
        return not_finished("its summary record predates this turn")
    if now - summary.last_modified_epoch < quiet:
        return not_finished("its summary record was written moments ago")
    return TurnEvidence(True, "", progress, trajectory, summary)
