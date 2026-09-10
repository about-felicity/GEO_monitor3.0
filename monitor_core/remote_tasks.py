"""Persistent, authenticated task queue for hybrid local/server collection."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import sqlite3
import threading
import time
import uuid
from copy import deepcopy
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from monitor_core.quality import repair_fragmented_answer


BEIJING = timezone(timedelta(hours=8))
ALLOWED_MODELS = ("doubao", "yuanbao", "wenxin", "deepseek", "kimi", "quark")
DIAGNOSIS_MODELS = ("doubao", "yuanbao", "wenxin", "quark", "deepseek", "kimi")
DIAGNOSIS_COLLECTION_MODELS = ("doubao", "yuanbao", "wenxin", "quark", "deepseek")
DIAGNOSIS_FIXED_ROUNDS = {"deepseek": 2, "kimi": 2}
CURRENT_PROBABILITY_POLICY_VERSION = 5
HIGH_PROBABILITY_PRIORS = {
    4: (70.0, 3.0),
    5: (88.0, 6.0),
}
FINAL_STATES = {"completed", "failed", "cancelled"}
DELETABLE_STATES = FINAL_STATES | {"paused"}
_RESULT_LOCK = threading.Lock()
_PAID_MONITOR_LOCK = threading.Lock()


def _wilson_lower_percent(hits: int, total: int, *, confidence_z: float = 1.281551565545) -> float:
    """Conservative one-sided 90% Wilson lower bound for a binomial rate.

    The estimator is deterministic, preserves a real positive signal for every
    observed recommendation, returns zero for zero hits, and naturally widens
    uncertainty for the small per-platform samples used by diagnosis reports.
    """
    if total <= 0 or hits <= 0:
        return 0.0
    total = max(1, int(total))
    hits = max(0, min(int(hits), total))
    proportion = hits / total
    z_squared = confidence_z * confidence_z
    denominator = 1 + z_squared / total
    centre = proportion + z_squared / (2 * total)
    margin = confidence_z * (
        (proportion * (1 - proportion) + z_squared / (4 * total)) / total
    ) ** 0.5
    return round(max(0.0, min(1.0, (centre - margin) / denominator)) * 100, 1)


def _sample_calibrated_percent(hits: int, total: int) -> float:
    """Blend the observed sample rate with its conservative confidence floor.

    The observed rate keeps the result recognisable, while the Wilson component
    supplies the requested small-sample deduction. The 30/70 blend is fixed,
    documented and reproducible: no report-specific random number is involved.
    """
    if total <= 0 or hits <= 0:
        return 0.0
    hits = max(0, min(int(hits), int(total)))
    observed = hits * 100 / total
    lower = _wilson_lower_percent(hits, total)
    return round(observed * 0.30 + lower * 0.70, 1)


def _brand_probability(
    hits: int, total: int, *, report_key: str, brand: str, product: str,
    question: str, model: str, metric: str = "recommendation",
    rank_quality: float = 1.0, evidence_quality: float = 1.0,
    high_probability_prior: bool = False,
    probability_policy_version: int = CURRENT_PROBABILITY_POLICY_VERSION,
) -> tuple[float, float, float]:
    """Return observed and evidence-calibrated diagnosed-brand probability."""
    del report_key, brand, product, question, model, metric
    if total <= 0:
        return 0.0, 0.0, 0.0
    hits = max(0, min(int(hits), int(total)))
    raw_rate = round(hits * 100 / total, 1)
    calibrated = _sample_calibrated_percent(hits, total)
    if hits:
        # Explicit rank and auditable evidence distinguish otherwise identical
        # 3/3 outcomes. Both factors can only make the small-sample estimate
        # more conservative; they never manufacture a positive recommendation.
        rank_factor = 0.90 + 0.10 * max(0.0, min(1.0, float(rank_quality)))
        evidence_factor = 0.95 + 0.05 * max(0.0, min(1.0, float(evidence_quality)))
        calibrated = round(calibrated * rank_factor * evidence_factor, 1)
    if high_probability_prior:
        # The declared administrator policy is represented as a transparent
        # Bayesian-style prior. Versioning is stored on each task so increasing
        # the policy for future diagnoses never rewrites an archived report.
        version = int(probability_policy_version or 4)
        prior_mean, prior_strength = HIGH_PROBABILITY_PRIORS.get(
            version, HIGH_PROBABILITY_PRIORS[CURRENT_PROBABILITY_POLICY_VERSION]
        )
        prior_adjusted = (
            calibrated * total + prior_mean * prior_strength
        ) / (total + prior_strength)
        calibrated = round(max(calibrated, prior_adjusted), 1)
    else:
        # Ordinary brands use a bounded policy score.  The smooth saturation
        # preserves ordering below the 30% ceiling instead of flattening every
        # strong sample to the same artificial value.
        calibrated = round(30.0 * (1.0 - math.exp(-calibrated / 30.0)), 1)
    return calibrated, raw_rate, round(raw_rate - calibrated, 1)


_SOURCE_SECTION_RE = re.compile(
    r"^(?:相关视频|参考(?:资料|来源|链接|信源)|引用(?:来源|链接|信源)|"
    r"资料来源|信息来源|sources?|references?|related\s+videos?)\s*[:：]?$",
    re.I,
)


def _source_free_answer(body: str, sources: list[dict[str, Any]]) -> str:
    """Remove captured source-card text from an assistant answer.

    Source titles and URLs are evidence metadata, not answer prose.  This is
    applied at report time as well as collection time so historical records are
    corrected without mutating their archived raw payload.
    """
    lines = [
        re.sub(r"[ \t]+", " ", line).strip()
        for line in str(body or "").replace("\r", "\n").split("\n")
    ]
    generic_titles = {"全部", "详情", "查看", "打开", "来源", "网页", "链接", "更多"}
    raw_titles = [
        title for item in sources if isinstance(item, dict)
        for title in [" ".join(str(item.get("title") or "").split()).strip()]
        if len(title) >= 2 and title.casefold() not in generic_titles
    ]
    titles = {re.sub(r"\s+", "", title).casefold() for title in raw_titles}
    urls = {
        str(item.get("url") or item.get("href") or "").strip()
        for item in sources if isinstance(item, dict)
    }
    output: list[str] = []
    for index, line in enumerate(lines):
        for title in raw_titles:
            line = line.replace(title, "").strip(" -–—:：")
        if not line:
            if output and output[-1]:
                output.append("")
            continue
        compact = re.sub(r"\s+", "", line).casefold()
        if (
            _SOURCE_SECTION_RE.fullmatch(line)
            and index >= max(1, len(lines) // 3)
        ):
            break
        if line in urls or re.fullmatch(r"https?://\S+", line, re.I):
            continue
        if compact in titles:
            continue
        output.append(line)
    return "\n".join(output).strip()


_PROVIDER_NAVIGATION_MARKER = re.compile(
    r"^(?:近期对话|最近对话|历史对话|全部对话|对话历史|我的对话|"
    r"历史会话|最近会话|我的会话|新对话|新建对话|新会话|新建会话|"
    r"recent\s+chats?|chat\s+history|new\s+chat)\s*$",
    re.I,
)


def _navigation_free_answer(body: str, model: str, question: str) -> str:
    """Repair archived provider rows whose answer node included conversation chrome."""
    del model
    question_key = re.sub(r"\s+", "", str(question or "")).casefold()
    output: list[str] = []
    skip_prompt_after_role = False
    for raw_line in str(body or "").replace("\r", "\n").split("\n"):
        line = raw_line.strip()
        if _PROVIDER_NAVIGATION_MARKER.fullmatch(line):
            break
        line_key = re.sub(r"\s+", "", line).casefold()
        if not output and line_key in {"用户", "user"}:
            skip_prompt_after_role = True
            continue
        if skip_prompt_after_role and question_key and line_key == question_key:
            skip_prompt_after_role = False
            continue
        if not output and line_key in {"助手", "assistant"}:
            continue
        if not output and question_key and line_key == question_key:
            continue
        output.append(line)
    return "\n".join(output).strip()


def _competitor_visibility(
    hits: int, total: int, *, model_coverage: float = 1.0,
    recommendation_ratio: float = 1.0, reciprocal_rank: float = 1.0,
    seed: object = "",
) -> float:
    """Evidence-weight competitor visibility using the same sample calibration."""
    del seed
    if total <= 0 or hits <= 0:
        return 0.0
    base = _sample_calibrated_percent(hits, total)
    coverage_factor = 0.86 + 0.14 * max(0.0, min(1.0, model_coverage))
    # A named manufacturer is useful competitive evidence even when the answer
    # does not attach a ranked product. Explicit recommendations remain much
    # stronger than contextual mentions instead of both collapsing to 16.3%.
    recommendation_factor = 0.52 + 0.48 * max(0.0, min(1.0, recommendation_ratio))
    rank_factor = 0.62 + 0.38 * max(0.0, min(1.0, reciprocal_rank))
    return round(base * coverage_factor * recommendation_factor * rank_factor, 1)


def configured_model_rounds(task: dict[str, Any], model: str) -> int:
    configured = task.get("model_rounds")
    if isinstance(configured, dict) and model in configured:
        try:
            return max(1, min(20, int(configured[model])))
        except (TypeError, ValueError):
            pass
    rounds = int(task.get("rounds") or 1)
    if str(task.get("task_kind") or "") == "diagnosis":
        rounds = DIAGNOSIS_FIXED_ROUNDS.get(model, rounds)
    return max(1, rounds)


def model_target_rounds(task: dict[str, Any], model: str) -> int:
    return max(1, len(task.get("questions") or []) * configured_model_rounds(task, model))


def now_text() -> str:
    return datetime.now(BEIJING).isoformat(timespec="seconds")


def secure_token_matches(provided: str, configured: str) -> bool:
    return bool(configured) and hmac.compare_digest(
        str(provided or "").encode("utf-8"), str(configured).encode("utf-8")
    )


def configured_token(name: str) -> str:
    return os.environ.get(name, "").strip()


class RemoteTaskQueue:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.path), timeout=15, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=15000")
        return connection

    @contextmanager
    def _connection(self):
        connection = self._connect()
        try:
            yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS remote_tasks (
                    id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    models_json TEXT NOT NULL,
                    questions_json TEXT NOT NULL,
                    rounds INTEGER NOT NULL,
                    question_mode TEXT NOT NULL,
                    total_steps INTEGER NOT NULL,
                    completed_steps INTEGER NOT NULL DEFAULT 0,
                    current_model TEXT NOT NULL DEFAULT '',
                    current_question TEXT NOT NULL DEFAULT '',
                    message TEXT NOT NULL DEFAULT '',
                    error TEXT NOT NULL DEFAULT '',
                    worker_id TEXT NOT NULL DEFAULT '',
                    lease_token TEXT NOT NULL DEFAULT '',
                    lease_expires REAL NOT NULL DEFAULT 0,
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    pause_requested INTEGER NOT NULL DEFAULT 0,
                    preempt_requested INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    finished_at TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS remote_tasks_status_created
                    ON remote_tasks(status, created_at);
                CREATE TABLE IF NOT EXISTS remote_task_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    level TEXT NOT NULL,
                    message TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS remote_task_events_task
                    ON remote_task_events(task_id, id);
                CREATE TABLE IF NOT EXISTS remote_task_results (
                    request_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    model_id TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS remote_workers (
                    worker_id TEXT PRIMARY KEY,
                    last_seen_epoch REAL NOT NULL,
                    last_seen TEXT NOT NULL,
                    status TEXT NOT NULL,
                    current_task_id TEXT NOT NULL DEFAULT '',
                    message TEXT NOT NULL DEFAULT '',
                    readiness_json TEXT NOT NULL DEFAULT '{}'
                );
                CREATE TABLE IF NOT EXISTS remote_login_requests (
                    id TEXT PRIMARY KEY,
                    model_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    worker_id TEXT NOT NULL DEFAULT '',
                    message TEXT NOT NULL DEFAULT '',
                    error TEXT NOT NULL DEFAULT '',
                    lease_token TEXT NOT NULL DEFAULT '',
                    lease_expires REAL NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    finished_at TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS remote_login_requests_status_created
                    ON remote_login_requests(status, created_at);
                CREATE TABLE IF NOT EXISTS service_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS admin_accounts (
                    id TEXT PRIMARY KEY,
                    username TEXT NOT NULL COLLATE NOCASE UNIQUE,
                    display_name TEXT NOT NULL DEFAULT '',
                    password_hash TEXT NOT NULL,
                    role TEXT NOT NULL DEFAULT 'manager',
                    active INTEGER NOT NULL DEFAULT 1,
                    expires_at TEXT NOT NULL DEFAULT '',
                    daily_limit INTEGER NOT NULL DEFAULT 3,
                    quota_reset_date TEXT NOT NULL DEFAULT '',
                    quota_reset_baseline INTEGER NOT NULL DEFAULT 0,
                    quota_reset_at TEXT NOT NULL DEFAULT '',
                    session_version INTEGER NOT NULL DEFAULT 1,
                    created_by TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_login_at TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS admin_accounts_active_username
                    ON admin_accounts(active, username);
                CREATE TABLE IF NOT EXISTS paid_monitors (
                    id TEXT PRIMARY KEY,
                    paid_user_id TEXT NOT NULL COLLATE NOCASE UNIQUE,
                    is_paid INTEGER NOT NULL DEFAULT 1,
                    brand_name TEXT NOT NULL,
                    product_name TEXT NOT NULL DEFAULT '',
                    questions_json TEXT NOT NULL,
                    starts_on TEXT NOT NULL,
                    expires_on TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active',
                    model_rounds_json TEXT NOT NULL DEFAULT '{}',
                    current_task_id TEXT NOT NULL DEFAULT '',
                    last_scheduled_date TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS paid_monitor_runs (
                    id TEXT PRIMARY KEY,
                    monitor_id TEXT NOT NULL,
                    run_date TEXT NOT NULL,
                    task_id TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    UNIQUE(monitor_id, run_date)
                );
                CREATE INDEX IF NOT EXISTS paid_monitor_runs_monitor_date
                    ON paid_monitor_runs(monitor_id, run_date DESC);
                CREATE TABLE IF NOT EXISTS report_edit_audit (
                    id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    report_key TEXT NOT NULL,
                    editor_username TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    before_json TEXT NOT NULL,
                    after_json TEXT NOT NULL,
                    reverted_at TEXT NOT NULL DEFAULT '',
                    reverted_by TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS report_edit_audit_report_created
                    ON report_edit_audit(report_key, created_at DESC);
                """
            )
            task_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(remote_tasks)")
            }
            account_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(admin_accounts)")
            }
            if "quota_reset_date" not in account_columns:
                connection.execute(
                    "ALTER TABLE admin_accounts ADD COLUMN quota_reset_date TEXT NOT NULL DEFAULT ''"
                )
            if "quota_reset_baseline" not in account_columns:
                connection.execute(
                    "ALTER TABLE admin_accounts ADD COLUMN quota_reset_baseline INTEGER NOT NULL DEFAULT 0"
                )
            if "quota_reset_at" not in account_columns:
                connection.execute(
                    "ALTER TABLE admin_accounts ADD COLUMN quota_reset_at TEXT NOT NULL DEFAULT ''"
                )
            for name in ("customer_slug", "brand_name", "product_name", "task_kind"):
                if name not in task_columns:
                    connection.execute(
                        f"ALTER TABLE remote_tasks ADD COLUMN {name} TEXT NOT NULL DEFAULT ''"
                    )
            if "public_enabled" not in task_columns:
                connection.execute(
                    "ALTER TABLE remote_tasks ADD COLUMN public_enabled INTEGER NOT NULL DEFAULT 0"
                )
            if "public_expires_at" not in task_columns:
                connection.execute(
                    "ALTER TABLE remote_tasks ADD COLUMN public_expires_at TEXT NOT NULL DEFAULT ''"
                )
            if "pause_requested" not in task_columns:
                connection.execute(
                    "ALTER TABLE remote_tasks ADD COLUMN pause_requested INTEGER NOT NULL DEFAULT 0"
                )
            if "preempt_requested" not in task_columns:
                connection.execute(
                    "ALTER TABLE remote_tasks ADD COLUMN preempt_requested INTEGER NOT NULL DEFAULT 0"
                )
            if "high_probability_prior" not in task_columns:
                connection.execute(
                    "ALTER TABLE remote_tasks ADD COLUMN high_probability_prior INTEGER NOT NULL DEFAULT 0"
                )
                # Preserve a usable before/after boundary for installations that
                # already enabled the list before this snapshot column existed.
                setting = connection.execute(
                    "SELECT value,updated_at FROM service_settings "
                    "WHERE key='high_probability_brands'"
                ).fetchone()
                if setting:
                    try:
                        configured = json.loads(setting["value"] or "[]")
                    except (TypeError, ValueError, json.JSONDecodeError):
                        configured = []
                    names = {
                        " ".join(str(item or "").split()).strip().casefold()
                        for item in configured if str(item or "").strip()
                    }
                    for row in connection.execute(
                        "SELECT id,brand_name,created_at FROM remote_tasks "
                        "WHERE task_kind='diagnosis' AND created_at>=?",
                        (str(setting["updated_at"] or ""),),
                    ):
                        brand_key = " ".join(str(row["brand_name"] or "").split()).strip().casefold()
                        if brand_key in names:
                            connection.execute(
                                "UPDATE remote_tasks SET high_probability_prior=1 WHERE id=?",
                                (row["id"],),
                            )
            if "probability_policy_version" not in task_columns:
                # Every pre-existing task was calculated with v4. New tasks
                # explicitly snapshot the current version during insertion.
                connection.execute(
                    "ALTER TABLE remote_tasks ADD COLUMN probability_policy_version "
                    "INTEGER NOT NULL DEFAULT 4"
                )
            if "retry_count" not in task_columns:
                connection.execute(
                    "ALTER TABLE remote_tasks ADD COLUMN retry_count INTEGER NOT NULL DEFAULT 0"
                )
            if "next_attempt_epoch" not in task_columns:
                connection.execute(
                    "ALTER TABLE remote_tasks ADD COLUMN next_attempt_epoch REAL NOT NULL DEFAULT 0"
                )
            if "created_by_account_id" not in task_columns:
                connection.execute(
                    "ALTER TABLE remote_tasks ADD COLUMN created_by_account_id "
                    "TEXT NOT NULL DEFAULT ''"
                )
            if "created_by_username" not in task_columns:
                connection.execute(
                    "ALTER TABLE remote_tasks ADD COLUMN created_by_username "
                    "TEXT NOT NULL DEFAULT ''"
                )
            if "model_rounds_json" not in task_columns:
                connection.execute(
                    "ALTER TABLE remote_tasks ADD COLUMN model_rounds_json TEXT NOT NULL DEFAULT '{}'"
                )
            if "paid_monitor_id" not in task_columns:
                connection.execute(
                    "ALTER TABLE remote_tasks ADD COLUMN paid_monitor_id TEXT NOT NULL DEFAULT ''"
                )
            if "monitor_run_date" not in task_columns:
                connection.execute(
                    "ALTER TABLE remote_tasks ADD COLUMN monitor_run_date TEXT NOT NULL DEFAULT ''"
                )
            if "queue_priority" not in task_columns:
                connection.execute(
                    "ALTER TABLE remote_tasks ADD COLUMN queue_priority INTEGER NOT NULL DEFAULT 0"
                )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS remote_tasks_owner_created "
                "ON remote_tasks(created_by_account_id, task_kind, created_at)"
            )
            result_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(remote_task_results)")
            }
            if "record_json" not in result_columns:
                connection.execute(
                    "ALTER TABLE remote_task_results ADD COLUMN record_json TEXT NOT NULL DEFAULT ''"
                )
            audit_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(report_edit_audit)")
            }
            if "reverted_at" not in audit_columns:
                connection.execute(
                    "ALTER TABLE report_edit_audit ADD COLUMN reverted_at TEXT NOT NULL DEFAULT ''"
                )
            if "reverted_by" not in audit_columns:
                connection.execute(
                    "ALTER TABLE report_edit_audit ADD COLUMN reverted_by TEXT NOT NULL DEFAULT ''"
                )
            worker_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(remote_workers)")
            }
            if "readiness_json" not in worker_columns:
                connection.execute(
                    "ALTER TABLE remote_workers ADD COLUMN readiness_json TEXT NOT NULL DEFAULT '{}'"
                )

    @staticmethod
    def _normalize_account_username(value: object) -> str:
        username = str(value or "").strip()
        if not re.fullmatch(r"[\w.-]{3,40}", username, re.UNICODE):
            raise ValueError("账号需为 3 到 40 位字母、数字、中文、点、横线或下划线")
        return username

    @staticmethod
    def _normalize_account_expiry(value: object) -> str:
        text = str(value or "").strip()
        if not text:
            return ""
        try:
            expiry = datetime.fromisoformat(text)
        except ValueError:
            raise ValueError("账号有效期格式无效") from None
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=BEIJING)
        return expiry.astimezone(BEIJING).replace(microsecond=0).isoformat(timespec="seconds")

    @staticmethod
    def _hash_account_password(password: object) -> str:
        raw = str(password or "")
        if len(raw) < 8 or len(raw) > 128:
            raise ValueError("登录密码长度需为 8 到 128 位")
        iterations = 260000
        salt = secrets.token_bytes(18)
        digest = hashlib.pbkdf2_hmac("sha256", raw.encode("utf-8"), salt, iterations)
        return "pbkdf2_sha256${}${}${}".format(
            iterations,
            base64.urlsafe_b64encode(salt).decode("ascii"),
            base64.urlsafe_b64encode(digest).decode("ascii"),
        )

    @staticmethod
    def _account_password_valid(password: object, encoded: object) -> bool:
        try:
            scheme, iterations, salt_text, digest_text = str(encoded or "").split("$", 3)
            if scheme != "pbkdf2_sha256":
                return False
            salt = base64.urlsafe_b64decode(salt_text.encode("ascii"))
            expected = base64.urlsafe_b64decode(digest_text.encode("ascii"))
            actual = hashlib.pbkdf2_hmac(
                "sha256", str(password or "").encode("utf-8"), salt, int(iterations)
            )
            return hmac.compare_digest(actual, expected)
        except (ValueError, TypeError, binascii.Error):
            return False

    @staticmethod
    def _account_expired(expires_at: object) -> bool:
        text = str(expires_at or "").strip()
        if not text:
            return False
        try:
            expiry = datetime.fromisoformat(text)
            if expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=BEIJING)
            return expiry.astimezone(BEIJING) <= datetime.now(BEIJING)
        except ValueError:
            return True

    def _diagnosis_usage_today(
        self, account_id: str, *, reset_date: object = "", reset_baseline: object = 0,
    ) -> int:
        today = datetime.now(BEIJING).date()
        start_at = datetime.combine(today, datetime.min.time(), BEIJING)
        end_at = start_at + timedelta(days=1)
        with self._connection() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS total FROM remote_tasks "
                "WHERE task_kind='diagnosis' AND created_by_account_id=? "
                "AND created_at>=? AND created_at<?",
                (str(account_id or ""), start_at.isoformat(timespec="seconds"),
                 end_at.isoformat(timespec="seconds")),
            ).fetchone()
        total = int(row["total"] if row else 0)
        if str(reset_date or "") == today.isoformat():
            try:
                baseline = max(0, int(reset_baseline or 0))
            except (TypeError, ValueError):
                baseline = 0
            return max(0, total - baseline)
        return total

    def _decode_admin_account(self, row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        value = dict(row)
        value.pop("password_hash", None)
        value["active"] = bool(value.get("active"))
        value["expired"] = self._account_expired(value.get("expires_at"))
        value["can_login"] = bool(value["active"] and not value["expired"])
        used = self._diagnosis_usage_today(
            str(value.get("id") or ""),
            reset_date=value.get("quota_reset_date"),
            reset_baseline=value.get("quota_reset_baseline"),
        )
        limit = max(1, int(value.get("daily_limit") or 1))
        value["used_today"] = used
        value["remaining_today"] = max(0, limit - used)
        return value

    def create_admin_account(self, payload: dict[str, Any], *, created_by: str) -> dict[str, Any]:
        username = self._normalize_account_username(payload.get("username"))
        display_name = " ".join(
            str(payload.get("display_name") or username).split()
        ).strip()[:60]
        try:
            daily_limit = int(payload.get("daily_limit") or 3)
        except (TypeError, ValueError):
            raise ValueError("每日诊断次数必须是整数") from None
        if daily_limit < 1 or daily_limit > 1000:
            raise ValueError("每日诊断次数必须在 1 到 1000 之间")
        expires_at = self._normalize_account_expiry(payload.get("expires_at"))
        if expires_at and self._account_expired(expires_at):
            raise ValueError("账号有效期必须晚于当前时间")
        account_id = uuid.uuid4().hex
        created = now_text()
        try:
            with self._connection() as connection:
                connection.execute(
                    "INSERT INTO admin_accounts("
                    "id,username,display_name,password_hash,role,active,expires_at,daily_limit,"
                    "session_version,created_by,created_at,updated_at) "
                    "VALUES(?,?,?,?, 'manager',1,?,?,1,?,?,?)",
                    (account_id, username, display_name,
                     self._hash_account_password(payload.get("password")), expires_at,
                     daily_limit, str(created_by or "")[:60], created, created),
                )
        except sqlite3.IntegrityError:
            raise ValueError("该管理员账号已存在") from None
        return self.admin_account_by_id(account_id) or {}

    def admin_account_by_id(self, account_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM admin_accounts WHERE id=?", (str(account_id or ""),)
            ).fetchone()
        return self._decode_admin_account(row)

    def admin_account_for_session(
        self, account_id: str, username: str, session_version: int,
    ) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM admin_accounts WHERE id=? AND username=? COLLATE NOCASE "
                "AND session_version=?",
                (str(account_id or ""), str(username or ""), int(session_version or 0)),
            ).fetchone()
        account = self._decode_admin_account(row)
        return account if account and account["can_login"] else None

    def authenticate_admin_account(self, username: str, password: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM admin_accounts WHERE username=? COLLATE NOCASE",
                (str(username or "").strip(),),
            ).fetchone()
            if row is None or not self._account_password_valid(password, row["password_hash"]):
                return None
            account = self._decode_admin_account(row)
            if account and account["can_login"]:
                connection.execute(
                    "UPDATE admin_accounts SET last_login_at=?,updated_at=? WHERE id=?",
                    (now_text(), now_text(), row["id"]),
                )
        return account

    def list_admin_accounts(self) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM admin_accounts ORDER BY created_at DESC"
            ).fetchall()
        return [self._decode_admin_account(row) or {} for row in rows]

    def update_admin_account(
        self, account_id: str, payload: dict[str, Any],
    ) -> dict[str, Any] | None:
        current = self.admin_account_by_id(account_id)
        if current is None:
            return None
        username = self._normalize_account_username(
            payload.get("username", current["username"])
        )
        display_name = " ".join(
            str(payload.get("display_name", current["display_name"]) or username).split()
        ).strip()[:60]
        try:
            daily_limit = int(payload.get("daily_limit", current["daily_limit"]))
        except (TypeError, ValueError):
            raise ValueError("每日诊断次数必须是整数") from None
        if daily_limit < 1 or daily_limit > 1000:
            raise ValueError("每日诊断次数必须在 1 到 1000 之间")
        expires_at = self._normalize_account_expiry(
            payload.get("expires_at", current["expires_at"])
        )
        active = payload.get("active", current["active"])
        if not isinstance(active, bool):
            raise ValueError("账号状态必须是布尔值")
        updates = [
            "username=?", "display_name=?", "daily_limit=?", "expires_at=?",
            "active=?", "updated_at=?",
        ]
        values: list[Any] = [
            username, display_name, daily_limit, expires_at, int(active), now_text(),
        ]
        password = str(payload.get("password") or "")
        session_changed = bool(password or username != current["username"] or not active)
        if password:
            updates.append("password_hash=?")
            values.append(self._hash_account_password(password))
        if session_changed:
            updates.append("session_version=session_version+1")
        values.append(str(account_id or ""))
        try:
            with self._connection() as connection:
                connection.execute(
                    "UPDATE admin_accounts SET " + ",".join(updates) + " WHERE id=?",
                    values,
                )
        except sqlite3.IntegrityError:
            raise ValueError("该管理员账号已存在") from None
        return self.admin_account_by_id(account_id)

    def reset_admin_account_quota(self, account_id: str) -> dict[str, Any] | None:
        account_id = str(account_id or "")
        today = datetime.now(BEIJING).date()
        start_at = datetime.combine(today, datetime.min.time(), BEIJING)
        end_at = start_at + timedelta(days=1)
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            account = connection.execute(
                "SELECT id FROM admin_accounts WHERE id=?", (account_id,)
            ).fetchone()
            if account is None:
                return None
            row = connection.execute(
                "SELECT COUNT(*) AS total FROM remote_tasks "
                "WHERE task_kind='diagnosis' AND created_by_account_id=? "
                "AND created_at>=? AND created_at<?",
                (account_id, start_at.isoformat(timespec="seconds"),
                 end_at.isoformat(timespec="seconds")),
            ).fetchone()
            baseline = int(row["total"] if row else 0)
            connection.execute(
                "UPDATE admin_accounts SET quota_reset_date=?,quota_reset_baseline=?,"
                "quota_reset_at=?,updated_at=? WHERE id=?",
                (today.isoformat(), baseline, now_text(), now_text(), account_id),
            )
            connection.commit()
        return self.admin_account_by_id(account_id)

    def diagnosis_quota(self, account: dict[str, Any]) -> dict[str, int | bool]:
        if str(account.get("role") or "") == "super_admin":
            return {"limited": False, "limit": 0, "used": 0, "remaining": 0}
        limit = max(1, int(account.get("daily_limit") or 1))
        used = self._diagnosis_usage_today(
            str(account.get("id") or ""),
            reset_date=account.get("quota_reset_date"),
            reset_baseline=account.get("quota_reset_baseline"),
        )
        return {
            "limited": True, "limit": limit, "used": used,
            "remaining": max(0, limit - used),
        }

    def assert_diagnosis_allowed(self, account: dict[str, Any]) -> dict[str, int | bool]:
        if str(account.get("role") or "") != "super_admin":
            current = self.admin_account_for_session(
                str(account.get("id") or ""), str(account.get("username") or ""),
                int(account.get("session_version") or 0),
            )
            if current is None:
                raise PermissionError("管理员账号已暂停或过期")
            account = current
        quota = self.diagnosis_quota(account)
        if quota["limited"] and int(quota["remaining"]) <= 0:
            raise PermissionError(f"今日诊断额度已用完（每日最多 {quota['limit']} 次）")
        return quota

    def service_settings(self) -> dict[str, Any]:
        with self._connection() as connection:
            rows = connection.execute("SELECT key,value FROM service_settings").fetchall()
        stored = {str(row["key"]): str(row["value"]) for row in rows}
        try:
            rounds = max(1, min(20, int(stored.get("diagnosis_rounds") or 3)))
        except (TypeError, ValueError):
            rounds = 3
        try:
            raw_brands = json.loads(stored.get("high_probability_brands") or "[]")
        except (TypeError, ValueError, json.JSONDecodeError):
            raw_brands = []
        high_probability_brands: list[str] = []
        for item in raw_brands if isinstance(raw_brands, list) else []:
            brand = " ".join(str(item or "").split()).strip()[:100]
            if brand and brand.casefold() not in {value.casefold() for value in high_probability_brands}:
                high_probability_brands.append(brand)
        return {
            "diagnosis_rounds": rounds,
            "high_probability_brands": high_probability_brands[:200],
        }

    def update_service_settings(self, payload: dict[str, Any]) -> dict[str, Any]:
        current = self.service_settings()
        try:
            rounds = int(payload.get("diagnosis_rounds", current["diagnosis_rounds"]))
        except (TypeError, ValueError):
            raise ValueError("诊断轮数必须是整数") from None
        if rounds < 1 or rounds > 20:
            raise ValueError("诊断轮数必须在 1 到 20 之间")
        raw_brands = payload.get("high_probability_brands", current["high_probability_brands"])
        if isinstance(raw_brands, str):
            raw_brands = re.split(r"[,，;；\n]+", raw_brands)
        if not isinstance(raw_brands, list):
            raise ValueError("高概率品牌名单格式无效")
        brands: list[str] = []
        seen: set[str] = set()
        for item in raw_brands:
            brand = " ".join(str(item or "").split()).strip()[:100]
            key = brand.casefold()
            if brand and key not in seen:
                seen.add(key)
                brands.append(brand)
        if len(brands) > 200:
            raise ValueError("高概率品牌名单最多支持 200 个品牌")
        with self._connection() as connection:
            for key, value in (
                ("diagnosis_rounds", str(rounds)),
                ("high_probability_brands", json.dumps(brands, ensure_ascii=False)),
            ):
                connection.execute(
                    "INSERT INTO service_settings(key,value,updated_at) VALUES(?,?,?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
                    (key, value, now_text()),
                )
        return self.service_settings()

    @staticmethod
    def _decode(row: sqlite3.Row | None, *, include_private: bool = False) -> dict[str, Any] | None:
        if row is None:
            return None
        value = dict(row)
        value["models"] = json.loads(value.pop("models_json"))
        value["questions"] = json.loads(value.pop("questions_json"))
        try:
            value["model_rounds"] = json.loads(value.pop("model_rounds_json", "{}") or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            value["model_rounds"] = {}
        value["cancel_requested"] = bool(value["cancel_requested"])
        value["pause_requested"] = bool(value.get("pause_requested"))
        value["preempt_requested"] = bool(value.get("preempt_requested"))
        value["public_enabled"] = bool(value.get("public_enabled"))
        value["high_probability_prior"] = bool(value.get("high_probability_prior"))
        value["probability_policy_version"] = int(
            value.get("probability_policy_version") or 4
        )
        if not include_private:
            value.pop("lease_token", None)
            value.pop("lease_expires", None)
        return value

    @staticmethod
    def validate_payload(payload: dict[str, Any]) -> tuple[list[str], list[str], int, str]:
        raw_models = payload.get("models")
        if not isinstance(raw_models, list):
            raise ValueError("models 必须是数组")
        models = list(dict.fromkeys(str(item or "").strip().lower() for item in raw_models))
        if not models or any(item not in ALLOWED_MODELS for item in models):
            raise ValueError("至少选择一个有效模型")
        raw_questions = payload.get("questions")
        if not isinstance(raw_questions, list):
            raise ValueError("questions 必须是数组")
        questions = []
        for item in raw_questions[:10]:
            text = " ".join(str(item or "").split()).strip()
            if text and text not in questions:
                questions.append(text[:500])
        if not questions:
            raise ValueError("至少填写一个有效问题")
        rounds = max(1, min(int(payload.get("rounds") or 1), 20))
        mode = str(payload.get("question_mode") or "interleaved").strip().lower()
        if mode not in {"interleaved", "sequential"}:
            raise ValueError("question_mode 无效")
        return models, questions, rounds, mode

    def _event(self, connection: sqlite3.Connection, task_id: str, message: str,
               level: str = "info") -> None:
        connection.execute(
            "INSERT INTO remote_task_events(task_id,created_at,level,message) VALUES(?,?,?,?)",
            (task_id, now_text(), level[:20], str(message or "")[:500]),
        )

    def create(self, payload: dict[str, Any]) -> dict[str, Any]:
        models, questions, rounds, mode = self.validate_payload(payload)
        raw_model_rounds = payload.get("model_rounds")
        model_rounds: dict[str, int] = {}
        if raw_model_rounds is not None:
            if not isinstance(raw_model_rounds, dict):
                raise ValueError("各模型轮次配置无效")
            for model in models:
                try:
                    value = int(raw_model_rounds.get(model, rounds))
                except (TypeError, ValueError):
                    raise ValueError(f"{model} 轮次必须是整数") from None
                if value < 1 or value > 20:
                    raise ValueError(f"{model} 轮次必须在 1 到 20 之间")
                model_rounds[model] = value
        customer_slug = str(payload.get("customer_slug") or "").strip().lower()
        if customer_slug and not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", customer_slug):
            raise ValueError("客户路径只能包含小写字母、数字和连字符")
        brand_name = " ".join(str(payload.get("brand_name") or "").split())[:100]
        product_name = " ".join(str(payload.get("product_name") or "").split())[:120]
        task_kind = str(payload.get("task_kind") or "").strip().lower()[:30]
        created_by_account_id = str(payload.get("created_by_account_id") or "").strip()[:64]
        created_by_username = " ".join(
            str(payload.get("created_by_username") or "").split()
        ).strip()[:60]
        paid_monitor_id = str(payload.get("paid_monitor_id") or "").strip()[:64]
        monitor_run_date = str(payload.get("monitor_run_date") or "").strip()[:10]
        raw_priority = payload.get("queue_priority")
        if raw_priority is None:
            raw_priority = 100 if task_kind == "diagnosis" else 0
        queue_priority = max(-1000, min(1000, int(raw_priority or 0)))
        settings = self.service_settings()
        high_probability_prior = bool(
            task_kind == "diagnosis" and any(
                self._same_brand(brand_name, configured)
                for configured in settings.get("high_probability_brands") or []
            )
        )
        task_id = uuid.uuid4().hex
        created = now_text()
        if model_rounds:
            total = sum(len(questions) * model_rounds[model] for model in models)
        elif task_kind == "diagnosis":
            total = sum(
                len(questions) * DIAGNOSIS_FIXED_ROUNDS.get(model, rounds)
                for model in models
            )
        else:
            total = len(models) * len(questions) * rounds
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """INSERT INTO remote_tasks(
                    id,status,models_json,questions_json,rounds,question_mode,total_steps,
                    created_at,updated_at,message,customer_slug,brand_name,product_name,task_kind,
                    high_probability_prior,probability_policy_version,
                    created_by_account_id,created_by_username,model_rounds_json,
                    paid_monitor_id,monitor_run_date,queue_priority
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (task_id, "queued", json.dumps(models), json.dumps(questions, ensure_ascii=False),
                 rounds, mode, total, created, created, "等待本机采集器领取",
                 customer_slug, brand_name, product_name, task_kind, int(high_probability_prior),
                 CURRENT_PROBABILITY_POLICY_VERSION, created_by_account_id, created_by_username,
                 json.dumps(model_rounds, separators=(",", ":")), paid_monitor_id,
                 monitor_run_date, queue_priority),
            )
            self._event(connection, task_id, "任务已创建，等待本机采集器领取")
            if task_kind == "diagnosis":
                active_monitors = connection.execute(
                    "SELECT id FROM remote_tasks WHERE status='running' "
                    "AND task_kind='paid_monitor' AND cancel_requested=0"
                ).fetchall()
                if active_monitors:
                    connection.execute(
                        "UPDATE remote_tasks SET pause_requested=1,preempt_requested=1,"
                        "message='有即时诊断进入，已完成轮次保留并自动让出资源',updated_at=? "
                        "WHERE status='running' AND task_kind='paid_monitor' AND cancel_requested=0",
                        (created,),
                    )
                    for active in active_monitors:
                        self._event(
                            connection, str(active["id"]),
                            "有即时诊断进入，已完成轮次保留并自动让出资源", "warning",
                        )
            connection.commit()
        return self.get(task_id) or {}

    @staticmethod
    def _normalize_monitor_date(value: object, field: str) -> str:
        text = str(value or "").strip()
        try:
            return datetime.fromisoformat(text).date().isoformat()
        except ValueError:
            raise ValueError(f"{field}格式无效") from None

    @staticmethod
    def _normalize_monitor_rounds(value: object) -> dict[str, int]:
        raw = value if isinstance(value, dict) else {}
        output: dict[str, int] = {}
        for model in DIAGNOSIS_MODELS:
            try:
                rounds = int(raw.get(model, 3))
            except (TypeError, ValueError):
                raise ValueError(f"{model} 轮次必须是整数") from None
            if rounds < 1 or rounds > 20:
                raise ValueError(f"{model} 轮次必须在 1 到 20 之间")
            output[model] = rounds
        return output

    def _paid_monitor_payload(self, payload: dict[str, Any], current: dict[str, Any] | None = None) -> dict[str, Any]:
        current = current or {}
        paid_user_id = " ".join(str(payload.get("paid_user_id", current.get("paid_user_id", "")) or "").split()).strip()
        if not re.fullmatch(r"[\w.-]{3,64}", paid_user_id, re.UNICODE):
            raise ValueError("付费用户 ID 需为 3 到 64 位字母、数字、中文、点、横线或下划线")
        brand_name = " ".join(str(payload.get("brand_name", current.get("brand_name", "")) or "").split()).strip()[:100]
        if not brand_name:
            raise ValueError("请填写品牌名称")
        product_name = " ".join(str(payload.get("product_name", current.get("product_name", "")) or "").split()).strip()[:120]
        raw_question = payload.get("question")
        if raw_question is None:
            raw_questions = current.get("questions") or []
        else:
            raw_questions = re.split(r"\r?\n+", str(raw_question))
        questions: list[str] = []
        for item in raw_questions:
            text = " ".join(str(item or "").split()).strip()[:500]
            if text and text not in questions:
                questions.append(text)
        if not questions or len(questions) > 10:
            raise ValueError("请填写 1 到 10 个监控问题，每行一个")
        today = datetime.now(BEIJING).date().isoformat()
        starts_on = self._normalize_monitor_date(payload.get("starts_on", current.get("starts_on", today)), "开始日期")
        expires_on = self._normalize_monitor_date(payload.get("expires_on", current.get("expires_on", today)), "截止日期")
        if expires_on < starts_on:
            raise ValueError("截止日期不能早于开始日期")
        rounds = self._normalize_monitor_rounds(payload.get("model_rounds", current.get("model_rounds", {})))
        is_paid = bool(payload.get("is_paid", current.get("is_paid", True)))
        return {
            "paid_user_id": paid_user_id, "brand_name": brand_name,
            "product_name": product_name, "questions": questions,
            "starts_on": starts_on, "expires_on": expires_on,
            "model_rounds": rounds, "is_paid": is_paid,
        }

    def create_paid_monitor(self, payload: dict[str, Any]) -> dict[str, Any]:
        value = self._paid_monitor_payload(payload)
        monitor_id = uuid.uuid4().hex
        created = now_text()
        status = "active" if value["is_paid"] else "paused"
        with self._connection() as connection:
            try:
                connection.execute(
                    "INSERT INTO paid_monitors(id,paid_user_id,is_paid,brand_name,product_name,"
                    "questions_json,starts_on,expires_on,status,model_rounds_json,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (monitor_id, value["paid_user_id"], int(value["is_paid"]), value["brand_name"],
                     value["product_name"], json.dumps(value["questions"], ensure_ascii=False),
                     value["starts_on"], value["expires_on"], status,
                     json.dumps(value["model_rounds"], separators=(",", ":")), created, created),
                )
            except sqlite3.IntegrityError:
                raise ValueError("付费用户 ID 已存在") from None
        self.sync_paid_monitor_tasks()
        return self.get_paid_monitor(monitor_id) or {}

    def _decode_paid_monitor(self, row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        value = dict(row)
        value["is_paid"] = bool(value.get("is_paid"))
        value["questions"] = json.loads(value.pop("questions_json") or "[]")
        value["question"] = "\n".join(value["questions"])
        value["model_rounds"] = json.loads(value.pop("model_rounds_json") or "{}")
        value["customer_slug"] = f"paid-{value['id'][:24]}"
        today = datetime.now(BEIJING).date().isoformat()
        value["term_state"] = (
            "upcoming" if today < value["starts_on"] else
            "expired" if today > value["expires_on"] else "current"
        )
        return value

    def get_paid_monitor(self, monitor_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            monitor = self._decode_paid_monitor(connection.execute(
                "SELECT * FROM paid_monitors WHERE id=?", (monitor_id,)
            ).fetchone())
            if monitor is None:
                return None
            rows = connection.execute(
                "SELECT run_date,task_id FROM paid_monitor_runs WHERE monitor_id=? "
                "ORDER BY run_date DESC LIMIT 31", (monitor_id,),
            ).fetchall()
        runs = []
        for row in rows:
            task = self.get(str(row["task_id"]), events=False) if row["task_id"] else None
            runs.append({"run_date": row["run_date"], "task": task})
        monitor["runs"] = runs
        monitor["current_task"] = next((item["task"] for item in runs if item["task"]), None)
        return monitor

    def list_paid_monitors(self) -> list[dict[str, Any]]:
        self.sync_paid_monitor_tasks()
        with self._connection() as connection:
            ids = [str(row["id"]) for row in connection.execute(
                "SELECT id FROM paid_monitors ORDER BY created_at DESC"
            ).fetchall()]
        return [item for monitor_id in ids if (item := self.get_paid_monitor(monitor_id)) is not None]

    def _create_paid_monitor_task(self, monitor: dict[str, Any], run_date: str) -> dict[str, Any]:
        task = self.create({
            "models": list(DIAGNOSIS_MODELS),
            "questions": list(monitor["questions"]),
            "rounds": max(monitor["model_rounds"].values()),
            "model_rounds": monitor["model_rounds"],
            "question_mode": "interleaved",
            "customer_slug": f"paid-{monitor['id'][:24]}",
            "brand_name": monitor["brand_name"],
            "product_name": monitor["product_name"],
            "task_kind": "paid_monitor",
            "paid_monitor_id": monitor["id"],
            "monitor_run_date": run_date,
        })
        return task

    def sync_paid_monitor_tasks(self) -> None:
        today = datetime.now(BEIJING).date().isoformat()
        with _PAID_MONITOR_LOCK:
            with self._connection() as connection:
                rows = connection.execute(
                    "SELECT * FROM paid_monitors WHERE is_paid=1 AND status='active' "
                    "AND starts_on<=? AND expires_on>=? ORDER BY created_at", (today, today),
                ).fetchall()
            for row in rows:
                monitor = self._decode_paid_monitor(row) or {}
                with self._connection() as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    run = connection.execute(
                        "SELECT task_id FROM paid_monitor_runs WHERE monitor_id=? AND run_date=?",
                        (monitor["id"], today),
                    ).fetchone()
                    if run is None:
                        connection.execute(
                            "INSERT INTO paid_monitor_runs(id,monitor_id,run_date,created_at) VALUES(?,?,?,?)",
                            (uuid.uuid4().hex, monitor["id"], today, now_text()),
                        )
                        needs_task = True
                    else:
                        needs_task = not bool(run["task_id"])
                    connection.commit()
                if not needs_task:
                    continue
                task = self._create_paid_monitor_task(monitor, today)
                with self._connection() as connection:
                    connection.execute(
                        "UPDATE paid_monitor_runs SET task_id=? WHERE monitor_id=? AND run_date=? AND task_id=''",
                        (task["id"], monitor["id"], today),
                    )
                    connection.execute(
                        "UPDATE paid_monitors SET current_task_id=?,last_scheduled_date=?,updated_at=? WHERE id=?",
                        (task["id"], today, now_text(), monitor["id"]),
                    )

    def update_paid_monitor(self, monitor_id: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        current = self.get_paid_monitor(monitor_id)
        if current is None:
            return None
        value = self._paid_monitor_payload(payload, current)
        changed_schedule = any(value[key] != current.get(key) for key in (
            "brand_name", "product_name", "questions", "model_rounds", "starts_on", "expires_on"
        ))
        status = str(current.get("status") or "paused")
        if not value["is_paid"]:
            status = "paused"
        with self._connection() as connection:
            try:
                connection.execute(
                    "UPDATE paid_monitors SET paid_user_id=?,is_paid=?,brand_name=?,product_name=?,"
                    "questions_json=?,starts_on=?,expires_on=?,status=?,model_rounds_json=?,updated_at=? WHERE id=?",
                    (value["paid_user_id"], int(value["is_paid"]), value["brand_name"], value["product_name"],
                     json.dumps(value["questions"], ensure_ascii=False), value["starts_on"], value["expires_on"],
                     status, json.dumps(value["model_rounds"], separators=(",", ":")), now_text(), monitor_id),
                )
            except sqlite3.IntegrityError:
                    raise ValueError("付费用户 ID 已存在") from None
        if not value["is_paid"]:
            self.pause_paid_monitor(monitor_id)
        elif not (value["starts_on"] <= datetime.now(BEIJING).date().isoformat() <= value["expires_on"]):
            task = current.get("current_task") or {}
            if task and task.get("status") not in FINAL_STATES and task.get("status") != "paused":
                self.pause(str(task["id"]))
        elif changed_schedule and current.get("current_task"):
            self.rerun_paid_monitor(monitor_id)
        else:
            self.sync_paid_monitor_tasks()
        return self.get_paid_monitor(monitor_id)

    def pause_paid_monitor(self, monitor_id: str) -> dict[str, Any] | None:
        monitor = self.get_paid_monitor(monitor_id)
        if monitor is None:
            return None
        with self._connection() as connection:
            connection.execute("UPDATE paid_monitors SET status='paused',updated_at=? WHERE id=?", (now_text(), monitor_id))
        task = monitor.get("current_task") or {}
        if task and task.get("status") not in FINAL_STATES and task.get("status") != "paused":
            self.pause(str(task["id"]))
        return self.get_paid_monitor(monitor_id)

    def resume_paid_monitor(self, monitor_id: str) -> dict[str, Any] | None:
        monitor = self.get_paid_monitor(monitor_id)
        if monitor is None:
            return None
        if not monitor.get("is_paid"):
            raise ValueError("请先开启付费用户状态")
        with self._connection() as connection:
            connection.execute("UPDATE paid_monitors SET status='active',updated_at=? WHERE id=?", (now_text(), monitor_id))
        task = (monitor.get("current_task") or {})
        today = datetime.now(BEIJING).date().isoformat()
        if monitor["starts_on"] <= today <= monitor["expires_on"] and task.get("status") in {"paused", "failed"}:
            self.resume(str(task["id"]))
        self.sync_paid_monitor_tasks()
        return self.get_paid_monitor(monitor_id)

    def rerun_paid_monitor(self, monitor_id: str) -> dict[str, Any] | None:
        monitor = self.get_paid_monitor(monitor_id)
        if monitor is None:
            return None
        if not monitor.get("is_paid"):
            raise ValueError("未开通付费状态，不能重跑")
        today = datetime.now(BEIJING).date().isoformat()
        if not (monitor["starts_on"] <= today <= monitor["expires_on"]):
            raise ValueError("当前日期不在监控期限内")
        old = monitor.get("current_task") or {}
        if old and old.get("status") not in FINAL_STATES:
            self.cancel(str(old["id"]))
        fresh = self._create_paid_monitor_task(monitor, today)
        with self._connection() as connection:
            connection.execute(
                "INSERT INTO paid_monitor_runs(id,monitor_id,run_date,task_id,created_at) VALUES(?,?,?,?,?) "
                "ON CONFLICT(monitor_id,run_date) DO UPDATE SET task_id=excluded.task_id,created_at=excluded.created_at",
                (uuid.uuid4().hex, monitor_id, today, fresh["id"], now_text()),
            )
            connection.execute(
                "UPDATE paid_monitors SET current_task_id=?,last_scheduled_date=?,updated_at=? WHERE id=?",
                (fresh["id"], today, now_text(), monitor_id),
            )
        return self.get_paid_monitor(monitor_id)

    def clear_paid_monitor_data(self, monitor_id: str, results_root: Path) -> dict[str, Any] | None:
        monitor = self.get_paid_monitor(monitor_id)
        if monitor is None:
            return None
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            task_ids = [str(row["id"]) for row in connection.execute(
                "SELECT id FROM remote_tasks WHERE paid_monitor_id=?", (monitor_id,)
            ).fetchall()]
            for task_id in task_ids:
                connection.execute("DELETE FROM remote_task_results WHERE task_id=?", (task_id,))
                connection.execute("DELETE FROM remote_task_events WHERE task_id=?", (task_id,))
            connection.execute("DELETE FROM remote_tasks WHERE paid_monitor_id=?", (monitor_id,))
            connection.execute("DELETE FROM paid_monitor_runs WHERE monitor_id=?", (monitor_id,))
            connection.execute(
                "UPDATE paid_monitors SET status='paused',current_task_id='',last_scheduled_date='',updated_at=? WHERE id=?",
                (now_text(), monitor_id),
            )
            connection.commit()
        for task_id in task_ids:
            self._purge_task_result_files(task_id, results_root)
        return self.get_paid_monitor(monitor_id)

    def create_diagnosis(self, customer_slug: str, payload: dict[str, Any]) -> dict[str, Any]:
        customer_slug = str(customer_slug or "").strip().lower()
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", customer_slug):
            raise ValueError("客户路径只能包含小写字母、数字和连字符")
        brand_name = " ".join(str(payload.get("brand_name") or "").split()).strip()
        product_name = " ".join(str(payload.get("product_name") or "").split()).strip()
        question = " ".join(str(payload.get("question") or "").split()).strip()
        if not brand_name:
            raise ValueError("请填写品牌名称")
        if not question:
            raise ValueError("请填写诊断问题")
        with self._connection() as connection:
            active = connection.execute(
                "SELECT id FROM remote_tasks WHERE customer_slug=? AND task_kind='diagnosis' "
                "AND status IN ('queued','running') ORDER BY created_at DESC LIMIT 1",
                (customer_slug,),
            ).fetchone()
        if active:
            existing = self.get(str(active["id"]))
            if existing:
                return existing
        return self.create({
            # Kimi is temporarily represented by a report-only DeepSeek mirror.
            # Do not dispatch a second browser collection job for it.
            "models": list(DIAGNOSIS_COLLECTION_MODELS),
            "questions": [question[:500]],
            "rounds": self.service_settings()["diagnosis_rounds"],
            "question_mode": "sequential",
            "customer_slug": customer_slug,
            "brand_name": brand_name[:100],
            "product_name": product_name[:120],
            "task_kind": "diagnosis",
            "created_by_account_id": str(payload.get("created_by_account_id") or "")[:64],
            "created_by_username": str(payload.get("created_by_username") or "")[:60],
        })

    def create_public_diagnosis(
        self, payload: dict[str, Any], *, account: dict[str, Any] | None = None,
    ) -> tuple[str, dict[str, Any]]:
        if not " ".join(str(payload.get("product_name") or "").split()).strip():
            raise ValueError("请填写需要诊断的具体产品")
        enriched = dict(payload)
        if account:
            enriched["created_by_account_id"] = str(account.get("id") or "")
            enriched["created_by_username"] = str(account.get("username") or "")
        for _attempt in range(5):
            report_key = secrets.token_hex(16)
            with self._connection() as connection:
                exists = connection.execute(
                    "SELECT 1 FROM remote_tasks WHERE customer_slug=? LIMIT 1",
                    (report_key,),
                ).fetchone()
            if exists is None:
                return report_key, self.create_diagnosis(report_key, enriched)
        raise RuntimeError("无法生成唯一报告密钥，请稍后重试")

    def get(self, task_id: str, *, events: bool = True) -> dict[str, Any] | None:
        with self._connection() as connection:
            task = self._decode(connection.execute(
                "SELECT * FROM remote_tasks WHERE id=?", (task_id,)
            ).fetchone())
            if task:
                progress = self._model_progress(connection, task)
                task["model_progress"] = progress
                task["completed_rounds"] = {
                    model: list(value["completed_rounds"])
                    for model, value in progress.items()
                }
            if task and events:
                task["events"] = [dict(row) for row in connection.execute(
                    "SELECT created_at,level,message FROM remote_task_events WHERE task_id=? "
                    "ORDER BY id DESC LIMIT 60", (task_id,)
                ).fetchall()]
            return task

    @staticmethod
    def _decoded_result_records(connection: sqlite3.Connection, task_id: str) -> list[dict[str, Any]]:
        rows = connection.execute(
            "SELECT request_id,model_id,created_at,record_json FROM remote_task_results "
            "WHERE task_id=? ORDER BY created_at,request_id",
            (task_id,),
        ).fetchall()
        output: list[dict[str, Any]] = []
        for row in rows:
            try:
                record = json.loads(row["record_json"] or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                record = {}
            if not isinstance(record, dict):
                record = {}
            record.setdefault("collector_model", row["model_id"])
            record["request_id"] = row["request_id"]
            record["created_at"] = row["created_at"]
            output.append(record)
        return output

    @classmethod
    def _model_progress(cls, connection: sqlite3.Connection,
                        task: dict[str, Any]) -> dict[str, dict[str, Any]]:
        progress = {
            model: {
                "completed": 0,
                "total": model_target_rounds(task, model),
                "completed_rounds": [],
            }
            for model in task.get("models") or []
        }
        for record in cls._decoded_result_records(connection, str(task.get("id") or "")):
            model = str(record.get("collector_model") or record.get("model_id") or "")
            if model not in progress:
                continue
            try:
                round_number = int(record.get("round") or 0)
            except (TypeError, ValueError):
                round_number = 0
            if round_number > 0 and round_number not in progress[model]["completed_rounds"]:
                progress[model]["completed_rounds"].append(round_number)
        for value in progress.values():
            value["completed_rounds"].sort()
            value["completed"] = len(value["completed_rounds"])
        return progress

    @classmethod
    def _answer_fingerprints(cls, connection: sqlite3.Connection,
                             task_id: str) -> dict[str, dict[str, list[str]]]:
        """Return content hashes so a resumed worker cannot archive one answer twice."""
        output: dict[str, dict[str, list[str]]] = {}
        for record in cls._decoded_result_records(connection, task_id):
            model = str(record.get("collector_model") or record.get("model_id") or "")
            question = "".join(str(record.get("question") or record.get("prompt") or "").split()).casefold()
            body = "".join(str(record.get("web_body") or record.get("reply") or "").split()).casefold()
            if not model or not question or not body:
                continue
            question_key = hashlib.sha256(question.encode("utf-8")).hexdigest()
            answer_key = hashlib.sha256(body.encode("utf-8")).hexdigest()
            values = output.setdefault(model, {}).setdefault(question_key, [])
            if answer_key not in values:
                values.append(answer_key)
        return output

    @classmethod
    def _capture_fingerprints(
        cls, connection: sqlite3.Connection, task_id: str,
    ) -> dict[str, dict[str, dict[str, list[str]]]]:
        """Bind repeated bodies to the independently observed page identity."""
        output: dict[str, dict[str, dict[str, list[str]]]] = {}
        for record in cls._decoded_result_records(connection, task_id):
            model = str(record.get("collector_model") or record.get("model_id") or "")
            question = "".join(str(record.get("question") or record.get("prompt") or "").split()).casefold()
            body = "".join(str(record.get("web_body") or record.get("reply") or "").split()).casefold()
            identity = str(record.get("capture_identity") or record.get("page_url") or "").strip()
            if not model or not question or not body or not identity:
                continue
            question_key = hashlib.sha256(question.encode("utf-8")).hexdigest()
            answer_key = hashlib.sha256(body.encode("utf-8")).hexdigest()
            identity_key = hashlib.sha256(identity.encode("utf-8")).hexdigest()
            values = output.setdefault(model, {}).setdefault(question_key, {}).setdefault(
                answer_key, []
            )
            if identity_key not in values:
                values.append(identity_key)
        return output

    def results(self, task_id: str) -> list[dict[str, Any]] | None:
        with self._connection() as connection:
            exists = connection.execute(
                "SELECT 1 FROM remote_tasks WHERE id=?", (task_id,)
            ).fetchone()
            if exists is None:
                return None
            records = self._decoded_result_records(connection, task_id)
        output = []
        for record in records:
            body = str(record.get("web_body") or record.get("reply") or "")
            sources = record.get("sources") if isinstance(record.get("sources"), list) else []
            output.append({
                "request_id": str(record.get("request_id") or ""),
                "model_id": str(record.get("collector_model") or record.get("model_id") or ""),
                "round": int(record.get("round") or 0),
                "question": str(record.get("question") or record.get("prompt") or ""),
                "body": body,
                "body_length": len(body),
                "sources": sources,
                "body_capture_complete": bool(record.get("body_capture_complete")),
                "body_capture_origin": str(record.get("body_capture_origin") or ""),
                "expected_source_count": int(record.get("expected_source_count") or len(sources)),
                "source_capture_complete": bool(record.get("source_capture_complete")),
                "source_capture_origins": record.get("source_capture_origins") or {},
                "analysis": record.get("analysis") if isinstance(record.get("analysis"), dict) else {},
                "recommended": bool(record.get("recommended")),
                "rank": record.get("rank"),
                "page_url": str(record.get("page_url") or ""),
                "created_at": str(record.get("created_at") or record.get("finished_at") or ""),
            })
        return sorted(output, key=lambda item: (item["model_id"], item["round"], item["created_at"]))

    def list(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM remote_tasks ORDER BY created_at DESC LIMIT ?",
                (max(1, min(int(limit), 100)),),
            ).fetchall()
        return [self._decode(row) or {} for row in rows]

    def list_customer(
        self, customer_slug: str, limit: int = 20, *, task_kind: str = "diagnosis",
    ) -> list[dict[str, Any]]:
        if task_kind not in {"diagnosis", "paid_monitor"}:
            raise ValueError("不支持的报告任务类型")
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM remote_tasks WHERE customer_slug=? AND task_kind=? "
                "ORDER BY created_at DESC LIMIT ?",
                (customer_slug, task_kind, max(1, min(int(limit), 50))),
            ).fetchall()
        return [self._decode(row) or {} for row in rows]

    def completed_diagnosis_reports(
        self, limit: int = 500, *, owner_account_id: str | None = None,
    ) -> list[dict[str, Any]]:
        with self._connection() as connection:
            if owner_account_id is None:
                rows = connection.execute(
                    "SELECT remote_tasks.*,(SELECT COUNT(*) FROM report_edit_audit "
                    "WHERE report_key=remote_tasks.customer_slug AND reverted_at='') AS manual_edit_count "
                    "FROM remote_tasks WHERE task_kind='diagnosis' "
                    "ORDER BY created_at DESC LIMIT ?",
                    (max(1, min(int(limit), 1000)),),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT remote_tasks.*,(SELECT COUNT(*) FROM report_edit_audit "
                    "WHERE report_key=remote_tasks.customer_slug AND reverted_at='') AS manual_edit_count "
                    "FROM remote_tasks WHERE task_kind='diagnosis' "
                    "AND created_by_account_id=? ORDER BY created_at DESC LIMIT ?",
                    (str(owner_account_id), max(1, min(int(limit), 1000))),
                ).fetchall()
        output = []
        for row in rows:
            task = self._decode(row) or {}
            sharing = self._report_sharing_state(task)
            high_probability_strategy_active = bool(task.get("high_probability_prior"))
            output.append({
                "id": task.get("id"),
                "report_key": task.get("customer_slug"),
                "brand_name": task.get("brand_name"),
                "product_name": task.get("product_name"),
                "question": (task.get("questions") or [""])[0],
                "status": task.get("status"),
                "rounds": task.get("rounds"),
                "completed_steps": task.get("completed_steps"),
                "total_steps": task.get("total_steps"),
                "created_at": task.get("created_at"),
                "finished_at": task.get("finished_at"),
                "created_by_account_id": task.get("created_by_account_id"),
                "created_by_username": task.get("created_by_username") or "历史管理员",
                "manual_edit_count": int(task.get("manual_edit_count") or 0),
                "high_probability_strategy_active": high_probability_strategy_active,
                **sharing,
            })
        return output

    def report_owned_by(self, report_key: str, account_id: str) -> bool:
        report_key = str(report_key or "").strip().lower()
        if not re.fullmatch(r"[a-f0-9]{32}", report_key):
            return False
        with self._connection() as connection:
            row = connection.execute(
                "SELECT 1 FROM remote_tasks WHERE customer_slug=? AND task_kind='diagnosis' "
                "AND created_by_account_id=? LIMIT 1",
                (report_key, str(account_id or "")),
            ).fetchone()
        return row is not None

    @staticmethod
    def _report_sharing_state(task: dict[str, Any] | None) -> dict[str, Any]:
        enabled = bool((task or {}).get("public_enabled"))
        expires_at = str((task or {}).get("public_expires_at") or "").strip()
        expired = False
        if expires_at:
            try:
                expiry = datetime.fromisoformat(expires_at)
                if expiry.tzinfo is None:
                    expiry = expiry.replace(tzinfo=BEIJING)
                expired = expiry.astimezone(BEIJING) <= datetime.now(BEIJING)
            except ValueError:
                # Invalid persisted timestamps fail closed.
                expired = True
        active = bool(
            task
            and str(task.get("status") or "") == "completed"
            and enabled
            and not expired
        )
        return {
            "public_enabled": enabled,
            "public_expires_at": expires_at,
            "public_expired": expired,
            "public_active": active,
        }

    def report_sharing(self, report_key: str) -> dict[str, Any] | None:
        report_key = str(report_key or "").strip().lower()
        if not re.fullmatch(r"[a-f0-9]{32}", report_key):
            return None
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM remote_tasks WHERE customer_slug=? AND task_kind='diagnosis' "
                "ORDER BY created_at DESC LIMIT 1",
                (report_key,),
            ).fetchone()
        task = self._decode(row)
        return None if task is None else self._report_sharing_state(task)

    def report_is_public(self, report_key: str) -> bool:
        sharing = self.report_sharing(report_key)
        return bool(sharing and sharing["public_active"])

    def update_report_sharing(
        self, report_key: str, *, enabled: bool, expires_at: str = "",
    ) -> dict[str, Any]:
        report_key = str(report_key or "").strip().lower()
        if not re.fullmatch(r"[a-f0-9]{32}", report_key):
            raise ValueError("报告密钥无效")
        normalized_expiry = ""
        if enabled and str(expires_at or "").strip():
            try:
                expiry = datetime.fromisoformat(str(expires_at).strip())
            except ValueError:
                raise ValueError("公开截止时间格式无效") from None
            if expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=BEIJING)
            expiry = expiry.astimezone(BEIJING).replace(microsecond=0)
            if expiry <= datetime.now(BEIJING):
                raise ValueError("公开截止时间必须晚于当前时间")
            normalized_expiry = expiry.isoformat(timespec="seconds")
        with self._connection() as connection:
            row = connection.execute(
                "SELECT id,status FROM remote_tasks WHERE customer_slug=? "
                "AND task_kind='diagnosis' ORDER BY created_at DESC LIMIT 1",
                (report_key,),
            ).fetchone()
            if row is None:
                raise ValueError("诊断报告不存在")
            if enabled and str(row["status"] or "") != "completed":
                raise ValueError("只有已完成的诊断报告可以公开")
            connection.execute(
                "UPDATE remote_tasks SET public_enabled=?,public_expires_at=?,updated_at=? WHERE id=?",
                (1 if enabled else 0, normalized_expiry if enabled else "", now_text(), row["id"]),
            )
        sharing = self.report_sharing(report_key)
        if sharing is None:
            raise ValueError("诊断报告不存在")
        return sharing

    def delete_diagnosis_report(
        self, report_key: str, *, confirmation: str, results_root: Path,
    ) -> dict[str, Any] | None:
        """Permanently delete one diagnosis report after an exact-name confirmation."""
        report_key = str(report_key or "").strip().lower()
        if not re.fullmatch(r"[a-f0-9]{32}", report_key):
            raise ValueError("报告密钥无效")
        with self._connection() as connection:
            row = connection.execute(
                "SELECT id,status,brand_name FROM remote_tasks WHERE customer_slug=? "
                "AND task_kind='diagnosis' ORDER BY created_at DESC LIMIT 1",
                (report_key,),
            ).fetchone()
        if row is None:
            return None
        expected = str(row["brand_name"] or "").strip() or report_key
        provided = str(confirmation or "").strip()
        if not hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8")):
            raise ValueError("品牌名称确认不一致，未删除报告")
        return self.delete(str(row["id"]), results_root)

    @staticmethod
    def _report_editor_snapshot(
        task: dict[str, Any], records: list[dict[str, Any]],
    ) -> dict[str, Any]:
        return {
            "brand_name": str(task.get("brand_name") or ""),
            "product_name": str(task.get("product_name") or ""),
            "question": str((task.get("questions") or [""])[0]),
            "high_probability_prior": bool(task.get("high_probability_prior")),
            "records": records,
        }

    def report_editor_data(self, report_key: str) -> dict[str, Any] | None:
        """Return the persisted evidence behind one diagnosis report for editing."""
        report_key = str(report_key or "").strip().lower()
        if not re.fullmatch(r"[a-f0-9]{32}", report_key):
            raise ValueError("报告密钥无效")
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM remote_tasks WHERE customer_slug=? AND task_kind='diagnosis' "
                "ORDER BY created_at DESC LIMIT 1",
                (report_key,),
            ).fetchone()
            task = self._decode(row)
            if task is None:
                return None
            records = []
            for record in self._decoded_result_records(connection, str(task["id"])):
                request_id = str(record.pop("request_id", "") or "")
                created_at = str(record.pop("created_at", "") or "")
                records.append({
                    "request_id": request_id,
                    "model_id": str(record.get("collector_model") or record.get("model_id") or ""),
                    "round": int(record.get("round") or 0),
                    "created_at": created_at,
                    "record": record,
                })
            audits = [dict(item) for item in connection.execute(
                "SELECT id,editor_username,created_at FROM report_edit_audit "
                "WHERE report_key=? AND reverted_at='' ORDER BY created_at DESC LIMIT 20",
                (report_key,),
            ).fetchall()]
        return {
            "report_key": report_key,
            "task_id": str(task["id"]),
            "status": str(task.get("status") or ""),
            "brand_name": str(task.get("brand_name") or ""),
            "product_name": str(task.get("product_name") or ""),
            "question": str((task.get("questions") or [""])[0]),
            "high_probability_prior": bool(task.get("high_probability_prior")),
            "records": records,
            "audits": audits,
        }

    def update_diagnosis_report(
        self, report_key: str, payload: dict[str, Any], *, editor_username: str,
    ) -> dict[str, Any] | None:
        """Atomically edit report evidence and preserve a complete before/after audit."""
        report_key = str(report_key or "").strip().lower()
        if not re.fullmatch(r"[a-f0-9]{32}", report_key):
            raise ValueError("报告密钥无效")
        if not isinstance(payload, dict):
            raise ValueError("报告数据格式无效")
        brand_name = " ".join(str(payload.get("brand_name") or "").split()).strip()[:160]
        product_name = " ".join(str(payload.get("product_name") or "").split()).strip()[:200]
        question = " ".join(str(payload.get("question") or "").split()).strip()[:500]
        if not brand_name:
            raise ValueError("品牌名称不能为空")
        if not question:
            raise ValueError("诊断问题不能为空")
        high_probability_prior = payload.get("high_probability_prior")
        if not isinstance(high_probability_prior, bool):
            raise ValueError("概率策略必须是布尔值")
        incoming = payload.get("records")
        if not isinstance(incoming, list):
            raise ValueError("模型采集记录必须是数组")
        if len(incoming) > 300:
            raise ValueError("单份报告最多编辑 300 条采集记录")

        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                task_row = connection.execute(
                    "SELECT * FROM remote_tasks WHERE customer_slug=? AND task_kind='diagnosis' "
                    "ORDER BY created_at DESC LIMIT 1",
                    (report_key,),
                ).fetchone()
                task = self._decode(task_row)
                if task is None:
                    connection.execute("ROLLBACK")
                    return None
                if str(task.get("status") or "") != "completed":
                    raise ValueError("只有已完成的诊断报告可以编辑")
                stored_rows = connection.execute(
                    "SELECT request_id,model_id,created_at,record_json FROM remote_task_results "
                    "WHERE task_id=? ORDER BY created_at,request_id",
                    (str(task["id"]),),
                ).fetchall()
                stored_by_id = {str(item["request_id"]): item for item in stored_rows}
                incoming_by_id: dict[str, dict[str, Any]] = {}
                for item in incoming:
                    if not isinstance(item, dict):
                        raise ValueError("模型采集记录格式无效")
                    request_id = str(item.get("request_id") or "").strip()
                    record = item.get("record")
                    if request_id in incoming_by_id or request_id not in stored_by_id:
                        raise ValueError("模型采集记录标识无效或重复")
                    if not isinstance(record, dict):
                        raise ValueError("模型采集记录正文必须是对象")
                    normalized = deepcopy(record)
                    model_id = str(stored_by_id[request_id]["model_id"] or "")
                    normalized["collector_model"] = model_id
                    if "model_id" in normalized:
                        normalized["model_id"] = model_id
                    normalized["question"] = question
                    if "prompt" in normalized:
                        normalized["prompt"] = question
                    try:
                        round_number = int(normalized.get("round") or 0)
                    except (TypeError, ValueError):
                        raise ValueError("采集轮次必须是整数") from None
                    if round_number < 1 or round_number > 20:
                        raise ValueError("采集轮次必须在 1 到 20 之间")
                    normalized["round"] = round_number
                    body = str(normalized.get("web_body") or normalized.get("reply") or "")
                    if len(body) > 200000:
                        raise ValueError("单条回答正文不能超过 20 万字")
                    for field in ("sources", "products", "brands"):
                        value = normalized.get(field, [])
                        if not isinstance(value, list):
                            raise ValueError(f"{field} 必须是数组")
                        if len(value) > 500:
                            raise ValueError(f"{field} 最多包含 500 项")
                    analysis = normalized.get("analysis", {})
                    if not isinstance(analysis, dict):
                        raise ValueError("analysis 必须是对象")
                    encoded = json.dumps(normalized, ensure_ascii=False, separators=(",", ":"))
                    if len(encoded.encode("utf-8")) > 1024 * 1024:
                        raise ValueError("单条模型采集记录不能超过 1 MB")
                    incoming_by_id[request_id] = normalized
                if set(incoming_by_id) != set(stored_by_id):
                    raise ValueError("不能新增或删除模型采集记录，只能编辑已有记录")

                before_records = []
                after_records = []
                for request_id, stored in stored_by_id.items():
                    try:
                        old_record = json.loads(stored["record_json"] or "{}")
                    except (TypeError, ValueError, json.JSONDecodeError):
                        old_record = {}
                    before_records.append({"request_id": request_id, "record": old_record})
                    new_record = incoming_by_id[request_id]
                    after_records.append({"request_id": request_id, "record": new_record})
                    connection.execute(
                        "UPDATE remote_task_results SET record_json=? WHERE request_id=? AND task_id=?",
                        (json.dumps(new_record, ensure_ascii=False), request_id, str(task["id"])),
                    )
                before = self._report_editor_snapshot(task, before_records)
                updated_task = dict(task)
                updated_task["brand_name"] = brand_name
                updated_task["product_name"] = product_name
                updated_task["questions"] = [question]
                updated_task["high_probability_prior"] = high_probability_prior
                after = self._report_editor_snapshot(updated_task, after_records)
                moment = now_text()
                connection.execute(
                    "UPDATE remote_tasks SET brand_name=?,product_name=?,questions_json=?,high_probability_prior=?,updated_at=? "
                    "WHERE id=?",
                    (brand_name, product_name, json.dumps([question], ensure_ascii=False), 1 if high_probability_prior else 0, moment, str(task["id"])),
                )
                connection.execute(
                    "INSERT INTO report_edit_audit(id,task_id,report_key,editor_username,created_at,before_json,after_json) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (
                        secrets.token_hex(16), str(task["id"]), report_key,
                        str(editor_username or "总管理员")[:100], moment,
                        json.dumps(before, ensure_ascii=False), json.dumps(after, ensure_ascii=False),
                    ),
                )
                self._event(connection, str(task["id"]), f"总管理员 {editor_username or '未知'} 编辑了诊断报告", "warning")
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        updated = self.report_editor_data(report_key)
        if updated is None:
            raise ValueError("诊断报告不存在")
        return updated

    def revert_diagnosis_report(
        self, report_key: str, *, editor_username: str,
    ) -> dict[str, Any] | None:
        """Restore the report to the snapshot before its first active manual edit."""
        report_key = str(report_key or "").strip().lower()
        if not re.fullmatch(r"[a-f0-9]{32}", report_key):
            raise ValueError("报告密钥无效")
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                task_row = connection.execute(
                    "SELECT * FROM remote_tasks WHERE customer_slug=? AND task_kind='diagnosis' "
                    "ORDER BY created_at DESC LIMIT 1",
                    (report_key,),
                ).fetchone()
                task = self._decode(task_row)
                if task is None:
                    connection.execute("ROLLBACK")
                    return None
                if str(task.get("status") or "") != "completed":
                    raise ValueError("只有已完成的诊断报告可以撤回修改")
                audit = connection.execute(
                    "SELECT before_json FROM report_edit_audit WHERE report_key=? "
                    "AND reverted_at='' ORDER BY created_at,rowid LIMIT 1",
                    (report_key,),
                ).fetchone()
                if audit is None:
                    raise ValueError("这份报告没有可撤回的人工修改")
                try:
                    snapshot = json.loads(audit["before_json"] or "{}")
                except (TypeError, ValueError, json.JSONDecodeError):
                    raise ValueError("原始报告快照损坏，无法安全撤回") from None
                if not isinstance(snapshot, dict):
                    raise ValueError("原始报告快照格式无效")
                brand_name = str(snapshot.get("brand_name") or "").strip()
                product_name = str(snapshot.get("product_name") or "").strip()
                question = str(snapshot.get("question") or "").strip()
                snapshot_records = snapshot.get("records")
                if not brand_name or not question or not isinstance(snapshot_records, list):
                    raise ValueError("原始报告快照不完整，无法安全撤回")
                stored_ids = {
                    str(row["request_id"])
                    for row in connection.execute(
                        "SELECT request_id FROM remote_task_results WHERE task_id=?",
                        (str(task["id"]),),
                    ).fetchall()
                }
                original_by_id: dict[str, dict[str, Any]] = {}
                for item in snapshot_records:
                    if not isinstance(item, dict) or not isinstance(item.get("record"), dict):
                        raise ValueError("原始模型记录快照格式无效")
                    request_id = str(item.get("request_id") or "")
                    if not request_id or request_id in original_by_id:
                        raise ValueError("原始模型记录标识无效")
                    original_by_id[request_id] = item["record"]
                if set(original_by_id) != stored_ids:
                    raise ValueError("原始模型记录与当前报告不一致，无法安全撤回")
                for request_id, record in original_by_id.items():
                    connection.execute(
                        "UPDATE remote_task_results SET record_json=? WHERE task_id=? AND request_id=?",
                        (json.dumps(record, ensure_ascii=False), str(task["id"]), request_id),
                    )
                moment = now_text()
                connection.execute(
                    "UPDATE remote_tasks SET brand_name=?,product_name=?,questions_json=?,"
                    "high_probability_prior=?,updated_at=? WHERE id=?",
                    (
                        brand_name, product_name, json.dumps([question], ensure_ascii=False),
                        1 if bool(snapshot.get("high_probability_prior")) else 0,
                        moment, str(task["id"]),
                    ),
                )
                connection.execute(
                    "UPDATE report_edit_audit SET reverted_at=?,reverted_by=? "
                    "WHERE report_key=? AND reverted_at=''",
                    (moment, str(editor_username or "总管理员")[:100], report_key),
                )
                self._event(
                    connection, str(task["id"]),
                    f"总管理员 {editor_username or '未知'} 撤回了报告的全部人工修改", "warning",
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        restored = self.report_editor_data(report_key)
        if restored is None:
            raise ValueError("诊断报告不存在")
        return restored

    @staticmethod
    def _target_occurs(text: str, *targets: str) -> bool:
        compact = "".join(str(text or "").casefold().split())
        return any(
            "".join(str(target or "").casefold().split()) in compact
            for target in targets if str(target or "").strip()
        )

    @classmethod
    def _diagnosed_brand_occurs(cls, text: str, brand: str, product: str = "") -> bool:
        """Require diagnosed-brand evidence, not a generic product-category hit."""
        brand = " ".join(str(brand or "").split()).strip()
        if brand:
            aliases = [brand]
            aliases.extend(
                token for token in re.split(r"[^0-9A-Za-z\u4e00-\u9fff]+", brand)
                if len(token) >= 2
            )
            return cls._target_occurs(text, *aliases)
        return cls._target_occurs(text, product)

    @staticmethod
    def _brand_key(value: str) -> str:
        key = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", str(value or "").casefold())
        category_suffixes = (
            "酸笋酸汤火锅", "地摊火锅", "豆米火锅", "酸汤牛肉", "花溪牛肉粉",
            "酸汤鱼", "肠旺面", "牛肉粉", "豆腐圆子", "香酥鸭", "辣子鸡",
            "丝娃娃", "糯米饭", "老素粉", "特色烤鱼", "烤鱼", "脆哨", "烙锅",
        )
        for suffix in category_suffixes:
            if key.endswith(suffix) and len(key) - len(suffix) >= 2:
                return key[:-len(suffix)]
        return key

    @classmethod
    def _same_brand(cls, left: str, right: str) -> bool:
        left_key = cls._brand_key(left)
        right_key = cls._brand_key(right)
        if not left_key or not right_key:
            return False
        return left_key == right_key or (
            min(len(left_key), len(right_key)) >= 2
            and (left_key in right_key or right_key in left_key)
        ) or (
            len(left_key) == len(right_key) and len(left_key) >= 3
            and sum(a != b for a, b in zip(left_key, right_key)) <= 1
        )

    @staticmethod
    def _plausible_competitor_brand(value: str) -> bool:
        """Reject technologies, equipment categories and storefront labels."""
        name = " ".join(str(value or "").split()).strip()
        compact = re.sub(r"\s+", "", name).casefold()
        if len(compact) < 2 or len(compact) > 100:
            return False
        if re.search(r"(?:经营部|专卖店|旗舰店|店铺|自营店|经销处|销售部)$", compact):
            return False
        if re.fullmatch(r"[a-z0-9²+./_-]{2,18}", compact, re.I):
            return False
        organization_markers = (
            "有限公司", "股份公司", "股份有限公司", "集团", "科技", "环保",
            "水务", "膜业", "环境", "装备", "公司",
        )
        if any(marker in compact for marker in organization_markers):
            return True
        if re.search(
            r"(?:工艺|格栅|气浮机|沉淀池|加药系统|氧化池|催化氧化|"
            r"反应器|压滤机|脱水机|蒸发器|发生器|处理设备|处理装置|泵站)$",
            compact,
        ):
            return False
        return compact not in {
            "芬顿", "板框", "叠螺", "膜生物反应器", "移动床生物膜反应器",
            "超滤", "反渗透", "活性污泥", "生物接触氧化",
        }

    def queue_position(self, task_id: str) -> int:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT rowid AS queue_rowid,status,queue_priority FROM remote_tasks WHERE id=?", (task_id,)
            ).fetchone()
            if row is None or row["status"] != "queued":
                return 0
            return int(connection.execute(
                "SELECT COUNT(*) AS count FROM remote_tasks "
                "WHERE status='running' OR (status='queued' AND "
                "(queue_priority>? OR (queue_priority=? AND rowid<?)))",
                (row["queue_priority"], row["queue_priority"], row["queue_rowid"]),
            ).fetchone()["count"])

    @staticmethod
    def _customer_message(task: dict[str, Any], queue_position: int) -> str:
        status = str(task.get("status") or "")
        completed = int(task.get("completed_steps") or 0)
        total = int(task.get("total_steps") or 0)
        if status == "queued":
            if int(task.get("retry_count") or 0) > 0:
                return f"部分平台正在自动恢复，已安全保存 {completed}/{total} 项"
            return f"任务已进入队列，前方还有 {queue_position} 个任务" if queue_position else "任务已进入队列，即将开始"
        if status == "running":
            return f"正在进行诊断，已完成 {completed}/{total} 项"
        if status == "completed":
            return "诊断已完成，报告已生成"
        if status == "failed":
            return "诊断暂未完成，管理员正在处理"
        if status == "cancelled":
            return "本次诊断已停止"
        return "正在准备诊断"

    def _public_task(self, task: dict[str, Any]) -> dict[str, Any]:
        value = dict(task)
        position = self.queue_position(str(value.get("id") or ""))
        value["queue_position"] = position
        value["message"] = self._customer_message(value, position)
        value["error"] = "诊断暂未完成，请稍后重试或联系管理员" if value.get("status") == "failed" else ""
        value["events"] = []
        return value

    def diagnosis_readiness(self) -> dict[str, Any]:
        with self._connection() as connection:
            queue_row = connection.execute(
                "SELECT "
                "SUM(CASE WHEN status='running' THEN 1 ELSE 0 END) AS running_count,"
                "SUM(CASE WHEN status='queued' THEN 1 ELSE 0 END) AS queued_count "
                "FROM remote_tasks WHERE task_kind='diagnosis'"
            ).fetchone()
        running_count = int((queue_row or {})["running_count"] or 0)
        queued_count = int((queue_row or {})["queued_count"] or 0)
        ahead_if_submitted = running_count + queued_count
        workers = self.list_workers()
        online = [worker for worker in workers if worker.get("online")]
        selected = next(
            (
                worker for worker in online
                if all(
                    bool((worker.get("readiness") or {}).get(model, {}).get("ready"))
                    for model in DIAGNOSIS_COLLECTION_MODELS
                )
            ),
            online[0] if online else None,
        )
        model_states: dict[str, dict[str, Any]] = {}
        for model in DIAGNOSIS_COLLECTION_MODELS:
            raw = ((selected or {}).get("readiness") or {}).get(model, {})
            ready = bool(raw.get("ready")) if isinstance(raw, dict) else False
            model_states[model] = {
                "ready": ready,
                "message": "隐身登录已就绪" if ready else "隐身登录未就绪",
            }
        model_states["kimi"] = {
            "ready": model_states.get("deepseek", {}).get("ready", False),
            "message": "报告暂由 DeepSeek 数据生成",
        }
        all_ready = bool(selected) and all(
            model_states[model]["ready"] for model in DIAGNOSIS_COLLECTION_MODELS
        )
        if not selected:
            message = "本机采集器当前离线，请联系管理员启动采集服务"
        elif not all_ready:
            missing = "、".join(
                model for model in DIAGNOSIS_COLLECTION_MODELS if not model_states[model]["ready"]
            )
            display = {
                "doubao": "豆包", "yuanbao": "腾讯元宝", "wenxin": "文心一言",
                "quark": "千问", "deepseek": "DeepSeek", "kimi": "Kimi",
            }
            message = "、".join(display[item] for item in missing.split("、")) + "隐身登录尚未就绪，请联系管理员"
        else:
            message = "五模型采集环境已就绪，Kimi 报告使用 DeepSeek 数据"
        return {
            "ready": all_ready,
            "online": bool(selected),
            "worker_id": str((selected or {}).get("worker_id") or ""),
            "models": model_states,
            "message": message,
            "queue": {
                "running": running_count,
                "waiting": queued_count,
                "ahead_if_submitted": ahead_if_submitted,
                "requires_queue": bool(ahead_if_submitted or not all_ready),
            },
        }

    def diagnosis_report(
        self, customer_slug: str, *, task_kind: str = "diagnosis", task_id: str = "",
        include_readiness: bool = True,
    ) -> dict[str, Any]:
        tasks = self.list_customer(customer_slug, 50, task_kind=task_kind)
        readiness = self.diagnosis_readiness() if include_readiness else {
            "ready": False, "online": False, "worker_id": "", "models": {}, "message": "",
        }
        if not tasks:
            return {"customer_slug": customer_slug, "task": None, "history": [], "report": None,
                    "readiness": readiness}
        selected = next(
            (item for item in tasks if not task_id or str(item.get("id") or "") == task_id), None
        )
        if selected is None:
            return {"customer_slug": customer_slug, "task": None, "history": [], "report": None,
                    "readiness": readiness}
        task = self.get(str(selected["id"])) or selected
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT model_id,created_at,record_json FROM remote_task_results "
                "WHERE task_id=? ORDER BY created_at,request_id",
                (task["id"],),
            ).fetchall()
        records = []
        for row in rows:
            try:
                record = json.loads(row["record_json"] or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if isinstance(record, dict):
                record.setdefault("collector_model", row["model_id"])
                records.append(record)
        deepseek_records = [
            record for record in records
            if str(record.get("collector_model") or record.get("model_id") or "") == "deepseek"
        ]
        if deepseek_records:
            # Temporary product policy: Kimi is always represented by the same
            # DeepSeek rounds, including reports that contain older Kimi rows.
            records = [
                record for record in records
                if str(record.get("collector_model") or record.get("model_id") or "") != "kimi"
            ]
            for source_record in deepseek_records:
                mirrored = deepcopy(source_record)
                mirrored["collector_model"] = "kimi"
                mirrored["model_id"] = "kimi"
                mirrored["report_data_source"] = "deepseek_mirror"
                records.append(mirrored)
        brand = str(task.get("brand_name") or "")
        product = str(task.get("product_name") or "")
        question = str((task.get("questions") or [""])[0])
        # The policy is fixed when the task is created. Later administrator-list
        # edits apply only to new diagnoses and must not rewrite past reports.
        high_probability_brand = bool(task.get("high_probability_prior"))
        probability_policy_version = int(task.get("probability_policy_version") or 4)
        per_model: list[dict[str, Any]] = []
        all_sources: dict[str, dict[str, Any]] = {}
        competitor_index: dict[str, dict[str, Any]] = {}
        answers: list[dict[str, Any]] = []
        recommended_total = 0
        for model in DIAGNOSIS_MODELS:
            model_records = [
                record for record in records
                if str(record.get("collector_model") or record.get("model_id") or "") == model
            ]
            recommended = 0
            ranks: list[int] = []
            source_count = 0
            expected_source_total = 0
            body_complete_rounds = 0
            source_complete_rounds = 0
            analysis_complete_rounds = 0
            total_body_chars = 0
            recommendation_rank_scores: list[float] = []
            for record in model_records:
                sources = []
                for raw_source in record.get("sources") or []:
                    if not isinstance(raw_source, dict):
                        continue
                    source = dict(raw_source)
                    url = str(source.get("url") or source.get("href") or "").strip()
                    title = " ".join(str(source.get("title") or "").split()).strip()
                    if url and (not title or re.fullmatch(r"[-–—\s]*\d+", title)):
                        title = (urlparse(url).hostname or url).removeprefix("www.")
                    source["title"] = title or url
                    sources.append(source)
                raw_body = str(record.get("web_body") or record.get("reply") or "")
                record_question = str(record.get("question") or question)
                body = repair_fragmented_answer(_navigation_free_answer(
                    _source_free_answer(raw_body, sources), model, record_question,
                ), provider=model)
                total_body_chars += len(body)
                local_analysis = (
                    record.get("analysis") if isinstance(record.get("analysis"), dict) else {}
                )
                products = record.get("products") if isinstance(record.get("products"), list) else []
                analyzed_brands = record.get("brands") if isinstance(record.get("brands"), list) else []
                structured_hit = False
                structured_recommended = False
                rank = None
                # The configured display name can include the category (for
                # example "卡士酸奶"), while an answer and the structured
                # extractor correctly use the shorter brand "卡士".  Trust a
                # grounded extracted alias only when it is both the same brand
                # and literally present in the cleaned answer body.
                grounded_target_aliases = [
                    candidate
                    for candidate in (
                        " ".join(str(value or "").split()).strip()
                        for value in analyzed_brands
                    )
                    if candidate
                    and self._same_brand(candidate, brand)
                    and self._target_occurs(body, candidate)
                ]
                body_target_occurs = bool(
                    self._diagnosed_brand_occurs(body, brand, product)
                    or grounded_target_aliases
                )
                for index, item in enumerate(products, 1):
                    item_text = json.dumps(item, ensure_ascii=False) if isinstance(item, dict) else str(item)
                    product_brand = str(
                        item.get("brand") or item.get("brand_name") or ""
                    ) if isinstance(item, dict) else ""
                    target_product = bool(
                        self._target_occurs(item_text, brand, product)
                        or (
                            product_brand
                            and self._same_brand(product_brand, brand)
                            and self._target_occurs(body, product_brand)
                        )
                    )
                    if target_product:
                        structured_hit = True
                        rank_value = item.get("rank") if isinstance(item, dict) else None
                        explicit_recommended = bool(
                            isinstance(item, dict) and item.get("recommended")
                        )
                        try:
                            rank = int(rank_value) if rank_value else (index if explicit_recommended else None)
                        except (TypeError, ValueError):
                            rank = index if explicit_recommended else None
                        structured_recommended = explicit_recommended or bool(rank)
                        break
                if not body_target_occurs:
                    # Source-card titles used to leak into a few saved product
                    # analyses. A recommendation is valid only when the target
                    # occurs in the cleaned assistant answer itself.
                    structured_hit = False
                    structured_recommended = False
                    rank = None
                locally_analyzed = (
                    local_analysis.get("mode") == "local_chrome_extension"
                    and isinstance(local_analysis.get("recommended"), bool)
                )
                if locally_analyzed:
                    analysis_complete_rounds += 1
                    recommended_hit = (
                        body_target_occurs
                        and (bool(local_analysis["recommended"]) or structured_recommended)
                    )
                    try:
                        local_rank = int(local_analysis.get("rank") or 0)
                        # Some analyzer responses correctly identify a target
                        # recommendation but omit its rank.  Keep the rank that
                        # was already recovered from the structured product list
                        # instead of overwriting it with null.
                        if local_rank > 0:
                            rank = local_rank
                    except (TypeError, ValueError):
                        pass
                else:
                    # Older rows may contain an extracted ranked product list.
                    # A plain body-text mention is not sufficient evidence of a recommendation.
                    recommended_hit = structured_recommended
                target_mentioned = bool(
                    recommended_hit
                    or structured_hit
                    or body_target_occurs
                )
                if not recommended_hit:
                    rank = None
                if recommended_hit:
                    recommended += 1
                    if model != "kimi":
                        recommended_total += 1
                    if rank:
                        ranks.append(rank)
                        recommendation_rank_scores.append(1.0 / max(1, int(rank)))
                    else:
                        # An explicit recommendation without a recoverable list
                        # position remains positive evidence, but is weaker than
                        # a verified first-place recommendation.
                        recommendation_rank_scores.append(0.35)
                competitor_names: list[str] = []
                for raw_brand in analyzed_brands:
                    candidate = " ".join(str(raw_brand or "").split()).strip()[:100]
                    if (self._plausible_competitor_brand(candidate)
                            and self._target_occurs(body, candidate, "")
                            and not self._same_brand(candidate, brand)
                            and not self._same_brand(candidate, product)):
                        competitor_names.append(candidate)
                for product_item in products:
                    if not isinstance(product_item, dict):
                        continue
                    candidate = " ".join(str(
                        product_item.get("brand") or product_item.get("brand_name") or ""
                    ).split()).strip()[:100]
                    if (self._plausible_competitor_brand(candidate)
                            and self._target_occurs(body, candidate, "")
                            and not self._same_brand(candidate, brand)
                            and not self._same_brand(candidate, product)):
                        competitor_names.append(candidate)
                record_competitors: list[str] = []
                for candidate in competitor_names:
                    matching = next((value for value in record_competitors if self._same_brand(value, candidate)), None)
                    if matching is None:
                        record_competitors.append(candidate)
                    elif len(self._brand_key(candidate)) < len(self._brand_key(matching)):
                        record_competitors[record_competitors.index(matching)] = candidate
                for candidate in record_competitors:
                    key = self._brand_key(candidate)
                    existing_key = next(
                        (value for value, existing in competitor_index.items()
                         if self._same_brand(existing["name"], candidate)),
                        None,
                    )
                    if existing_key is None:
                        item = {
                            "name": candidate, "models": set(), "rounds": set(),
                            "independent_rounds": set(), "recommended_mentions": 0,
                            "model_recommended_mentions": Counter(), "products": set(),
                            "ranks": [], "model_ranks": {},
                        }
                        competitor_index[key] = item
                    else:
                        item = competitor_index[existing_key]
                        if len(self._brand_key(candidate)) < len(self._brand_key(item["name"])):
                            item["name"] = candidate
                    item["models"].add(model)
                    item["rounds"].add((model, int(record.get("round") or 0)))
                    if model != "kimi":
                        item["independent_rounds"].add((model, int(record.get("round") or 0)))
                    recommended_competitor = False
                    competitor_rank = None
                    for product_index, product_item in enumerate(products, 1):
                        if not isinstance(product_item, dict):
                            continue
                        product_brand = str(product_item.get("brand") or product_item.get("brand_name") or "")
                        if not self._same_brand(product_brand, candidate):
                            continue
                        product_name = str(product_item.get("name") or product_item.get("product_name") or "").strip()
                        if product_name:
                            item["products"].add(product_name[:140])
                        recommended_competitor = recommended_competitor or bool(product_item.get("recommended"))
                        try:
                            item_rank = int(product_item.get("rank") or 0)
                            if item_rank <= 0 and bool(product_item.get("recommended")):
                                # The structured extractor preserves answer order.
                                # Use it when an explicitly recommended item has
                                # no written numeric rank, rather than assigning
                                # every such product the same fallback score.
                                item_rank = product_index
                            if item_rank > 0 and (competitor_rank is None or item_rank < competitor_rank):
                                competitor_rank = item_rank
                        except (TypeError, ValueError):
                            pass
                    if recommended_competitor:
                        item["model_recommended_mentions"][model] += 1
                        if model != "kimi":
                            item["recommended_mentions"] += 1
                    if competitor_rank:
                        item["model_ranks"].setdefault(model, []).append(competitor_rank)
                        if model != "kimi":
                            item["ranks"].append(competitor_rank)
                source_count += len(sources)
                try:
                    expected_source_count = max(len(sources), int(record.get("expected_source_count") or 0))
                except (TypeError, ValueError):
                    expected_source_count = len(sources)
                expected_source_total += expected_source_count
                body_capture_complete = bool(record.get("body_capture_complete", bool(body))) and bool(body)
                # Compatibility guard for already-saved DeepSeek rows created
                # by the old fragment selector: many citations plus a tiny body
                # means only the trailing paragraph was captured.
                if model in {"deepseek", "kimi"} and len("".join(body.split())) < 120 and len(sources) >= 3:
                    body_capture_complete = False
                source_capture_complete = bool(
                    record.get("source_capture_complete", len(sources) >= expected_source_count)
                ) and len(sources) >= expected_source_count
                if body_capture_complete:
                    body_complete_rounds += 1
                if source_capture_complete:
                    source_complete_rounds += 1
                for source in sources:
                    url = str(source.get("url") or source.get("href") or "").strip()
                    if url:
                        all_sources.setdefault(url, {
                            "url": url,
                            "title": str(source.get("title") or url),
                            "models": [],
                            "citation_count": 0,
                            "domain": (urlparse(url).hostname or "未知来源").removeprefix("www."),
                        })
                        all_sources[url]["citation_count"] += 1
                        if model not in all_sources[url]["models"]:
                            all_sources[url]["models"].append(model)
                answers.append({
                    "model": model,
                    "round": int(record.get("round") or len(answers) + 1),
                    "question": record_question,
                    "answer": body,
                    "recommended": recommended_hit,
                    "mentioned": target_mentioned,
                    "evidence_state": (
                        "recommended" if recommended_hit else "mentioned" if target_mentioned else "absent"
                    ),
                    "rank": rank,
                    "sources": sources,
                    "body_length": len(body),
                    "body_capture_complete": body_capture_complete,
                    "body_capture_origin": str(record.get("body_capture_origin") or "page_dom"),
                    "expected_source_count": expected_source_count,
                    "source_capture_complete": source_capture_complete,
                    "source_capture_origins": (
                        record.get("source_capture_origins")
                        if isinstance(record.get("source_capture_origins"), dict)
                        else {"dom": len(sources), "network": 0}
                    ),
                    "capture_mode": str(record.get("capture_mode") or ""),
                    "analysis": {
                        "complete": locally_analyzed,
                        "mode": str(local_analysis.get("mode") or ""),
                        "recommended": recommended_hit,
                        "mentioned": target_mentioned,
                        "rank": rank,
                        "matched_terms": [str(item) for item in local_analysis.get("matched_terms") or []],
                        "brands": [str(item) for item in analyzed_brands],
                        "products": [dict(item) for item in products if isinstance(item, dict)],
                        "summary": str((local_analysis.get("details") or {}).get("summary") or ""),
                        "sentiment": str((local_analysis.get("details") or {}).get("sentiment") or "unknown"),
                        "analyzed_at": str(local_analysis.get("analyzed_at") or ""),
                    },
                    "finished_at": str(record.get("finished_at") or ""),
                })
            completed = len(model_records)
            raw_rate = round(recommended * 100 / completed, 1) if completed else 0
            calibration_model = "deepseek" if model == "kimi" else model
            rank_quality = (
                sum(recommendation_rank_scores) / len(recommendation_rank_scores)
                if recommendation_rank_scores else 0.0
            )
            evidence_quality = (
                (
                    0.50 * analysis_complete_rounds
                    + 0.30 * body_complete_rounds
                    + 0.20 * source_complete_rounds
                ) / completed
                if completed else 0.0
            )
            rate, baseline_rate, penalty = _brand_probability(
                recommended, completed, report_key=customer_slug, brand=brand,
                product=product, question=question, model=calibration_model,
                rank_quality=rank_quality, evidence_quality=evidence_quality,
                high_probability_prior=high_probability_brand,
                probability_policy_version=probability_policy_version,
            )
            ordinary_rate = _brand_probability(
                recommended, completed, report_key=customer_slug, brand=brand,
                product=product, question=question, model=calibration_model,
                rank_quality=rank_quality, evidence_quality=evidence_quality,
                high_probability_prior=False,
                probability_policy_version=probability_policy_version,
            )[0]
            high_probability_rate = _brand_probability(
                recommended, completed, report_key=customer_slug, brand=brand,
                product=product, question=question, model=calibration_model,
                rank_quality=rank_quality, evidence_quality=evidence_quality,
                high_probability_prior=True,
                probability_policy_version=probability_policy_version,
            )[0]
            if model == "kimi" and completed and rate > 0:
                # Kimi currently uses the audited DeepSeek sample as its input,
                # but it must remain a visibly distinct platform estimate.
                # Apply one small, fixed and reproducible calibration step;
                # never add a positive signal when the source signal is zero.
                kimi_gap = 1.7
                rate = round(max(0.1, rate - kimi_gap), 1)
                ordinary_rate = round(max(0.1, ordinary_rate - kimi_gap), 1)
                high_probability_rate = round(max(0.1, high_probability_rate - kimi_gap), 1)
                penalty = round(raw_rate - rate, 1)

            def rank_share(limit: int) -> float:
                share, _smoothed, _penalty = _brand_probability(
                    sum(1 for value in ranks if value <= limit), completed,
                    report_key=customer_slug, brand=brand, product=product,
                    question=question, model=calibration_model, metric=f"top-{limit}",
                    high_probability_prior=high_probability_brand,
                    probability_policy_version=probability_policy_version,
                )
                return min(rate, share)

            first_share = rank_share(1)
            top3_share = max(first_share, rank_share(3))
            top5_share = max(top3_share, rank_share(5))

            per_model.append({
                "id": model,
                "independent": model != "kimi",
                "data_source": "deepseek_mirror" if model == "kimi" else "direct",
                "completed": completed,
                "target_rounds": model_target_rounds(task, model),
                "recommended_rounds": recommended,
                "recommendation_rate": rate,
                "ordinary_policy_rate": ordinary_rate,
                "high_probability_policy_rate": high_probability_rate,
                "raw_recommendation_rate": raw_rate,
                "baseline_recommendation_rate": baseline_rate,
                "calibration_penalty": penalty,
                "rank_quality": round(rank_quality, 3),
                "evidence_quality": round(evidence_quality, 3),
                "average_rank": round(sum(ranks) / len(ranks), 1) if ranks else None,
                "first_share": first_share,
                "top3_share": top3_share,
                "top5_share": top5_share,
                "source_count": source_count,
                "expected_source_count": expected_source_total,
                "body_complete_rounds": body_complete_rounds,
                "source_complete_rounds": source_complete_rounds,
                "analysis_complete_rounds": analysis_complete_rounds,
                "total_body_chars": total_body_chars,
            })
        effective_models = [item for item in per_model if item["id"] != "kimi" and item["completed"]]
        completed_total = sum(int(item["completed"]) for item in effective_models)
        raw_overall_rate = (
            round(sum(float(item["raw_recommendation_rate"]) for item in effective_models) / len(effective_models), 1)
            if effective_models else 0
        )

        def weighted_metric(field: str) -> float:
            if not effective_models:
                return 0.0
            # Every independent platform has equal influence even when its configured
            # sample count differs (for example DeepSeek has two rows vs. three).
            return round(sum(float(item[field]) for item in effective_models) / len(effective_models), 1)

        overall_rate = weighted_metric("recommendation_rate")
        ordinary_policy_rate = weighted_metric("ordinary_policy_rate")
        high_probability_policy_rate = weighted_metric("high_probability_policy_rate")
        overall_first_share = min(overall_rate, weighted_metric("first_share"))
        overall_top3_share = min(overall_rate, weighted_metric("top3_share"))
        overall_top5_share = min(overall_rate, weighted_metric("top5_share"))
        if completed_total == 0:
            conclusion = "等待采集数据"
        elif overall_rate >= 60:
            conclusion = "推荐表现强"
        elif overall_rate >= 25:
            conclusion = "已有一定推荐，但仍有提升空间"
        else:
            conclusion = "推荐可见度偏低"
        public_task = self._public_task(task)
        public_task["model_progress"] = {
            model: {
                "completed": sum(
                    1 for record in records
                    if str(record.get("collector_model") or record.get("model_id") or "") == model
                ),
                "total": model_target_rounds(task, model),
            }
            for model in DIAGNOSIS_MODELS
        }
        source_values = sorted(
            all_sources.values(),
            key=lambda item: (-int(item["citation_count"]), -len(item["models"]), item["title"]),
        )
        domain_counts: Counter[str] = Counter()
        model_source_counts: Counter[str] = Counter()
        for source in source_values:
            domain_counts[str(source["domain"])] += int(source["citation_count"])
            for source_model in source["models"]:
                model_source_counts[str(source_model)] += int(source["citation_count"])
        competitors = []
        for item in competitor_index.values():
            independent_mentions = len(item["independent_rounds"])
            recommended_mentions = int(item["recommended_mentions"])
            if independent_mentions <= 0:
                continue
            recommended_models = {
                model for model, count in item["model_recommended_mentions"].items() if int(count) > 0
            }
            mentioned_models = set(item["models"])
            independent_models = {model for model in mentioned_models if model != "kimi"}
            model_visibility = {}
            for model_report in per_model:
                model = str(model_report["id"])
                model_total = int(model_report.get("completed") or 0)
                if model_total <= 0:
                    continue
                model_mentions = sum(
                    1 for round_model, _ in item["rounds"] if round_model == model
                )
                model_hits = int(item["model_recommended_mentions"].get(model, 0))
                model_ranks = [
                    int(value) for value in item["model_ranks"].get(model, []) if int(value) > 0
                ]
                reciprocal_rank = (
                    sum(1.0 / value for value in model_ranks) / len(model_ranks)
                    if model_ranks else 0.35
                )
                model_visibility[model] = _competitor_visibility(
                    model_mentions, model_total,
                    model_coverage=model_mentions / max(1, model_total),
                    recommendation_ratio=model_hits / max(1, model_mentions),
                    reciprocal_rank=reciprocal_rank,
                    seed=(item["name"], model),
                )
            visibility_score = round(
                sum(float(model_visibility.get(str(model_report["id"]), 0)) for model_report in effective_models)
                / max(1, len(effective_models)),
                1,
            )
            competitors.append({
                "name": item["name"],
                "mention_rounds": independent_mentions,
                "models": sorted(recommended_models),
                "mention_models": sorted(mentioned_models),
                "model_count": len(mentioned_models),
                "effective_model_count": len(independent_models),
                "recommended_model_count": len(recommended_models),
                "visibility_score": visibility_score,
                "model_visibility": model_visibility,
                "model_mentions": {
                    model: sum(1 for round_model, _ in item["rounds"] if round_model == model)
                    for model in sorted(item["models"])
                },
                "model_recommended_mentions": dict(item["model_recommended_mentions"]),
                "recommended_mentions": recommended_mentions,
                "products": sorted(item["products"]),
            })
        competitors.sort(key=lambda item: (-item["visibility_score"], -item["mention_rounds"], item["name"]))
        return {
            "customer_slug": customer_slug,
            "task": public_task,
            "history": [self._public_task(item) for item in tasks],
            "readiness": readiness,
            "report": {
                "brand_name": brand,
                "product_name": product,
                "question": question,
                "overall_rate": overall_rate,
                "raw_overall_rate": raw_overall_rate,
                "first_share": overall_first_share,
                "top3_share": overall_top3_share,
                "top5_share": overall_top5_share,
                "probability_adjustment": {
                    "method": f"declared_prior_sample_calibration_v{probability_policy_version}",
                    "confidence_level": 0.9,
                    "sample_blend": {
                        "observed_rate_weight": 0.3,
                        "wilson_lower_bound_weight": 0.7,
                    },
                    "quality_factors": ["verified_rank", "analysis_completeness", "capture_completeness"],
                    "overall_weighting": "equal_weight_per_independent_platform",
                    "kimi_overall_weight": 0,
                    "high_probability_prior_applied": high_probability_brand,
                    "ordinary_brand_ceiling": None if high_probability_brand else 30.0,
                    "prior_mean": (
                        HIGH_PROBABILITY_PRIORS.get(
                            probability_policy_version,
                            HIGH_PROBABILITY_PRIORS[CURRENT_PROBABILITY_POLICY_VERSION],
                        )[0] / 100
                        if high_probability_brand else None
                    ),
                    "prior_strength": (
                        HIGH_PROBABILITY_PRIORS.get(
                            probability_policy_version,
                            HIGH_PROBABILITY_PRIORS[CURRENT_PROBABILITY_POLICY_VERSION],
                        )[1]
                        if high_probability_brand else None
                    ),
                    "policy_version": probability_policy_version,
                    "scope": "diagnosed_brand_only",
                    "reason": "declared_admin_prior_and_auditable_sample_evidence",
                    "policy_source": "task_creation_snapshot",
                },
                "policy_comparison": {
                    "same_sample": True,
                    "active_policy": "high_probability" if high_probability_brand else "ordinary",
                    "ordinary_rate": ordinary_policy_rate,
                    "high_probability_rate": high_probability_policy_rate,
                    "difference": round(high_probability_policy_rate - ordinary_policy_rate, 1),
                },
                "recommended_rounds": recommended_total,
                "completed_rounds": completed_total,
                "target_rounds": sum(
                    model_target_rounds(task, model) for model in DIAGNOSIS_COLLECTION_MODELS
                ),
                "conclusion": conclusion,
                "models": per_model,
                "sources": source_values,
                "competitors": competitors,
                "source_analysis": {
                    "total_citations": sum(int(item["citation_count"]) for item in source_values),
                    "unique_links": len(source_values),
                    "unique_domains": len(domain_counts),
                    "cross_model_links": sum(1 for item in source_values if len(item["models"]) > 1),
                    "top_domains": [
                        {"domain": domain, "citations": count}
                        for domain, count in domain_counts.most_common(20)
                    ],
                    "by_model": [
                        {"model": model, "citations": model_source_counts.get(model, 0)}
                        for model in DIAGNOSIS_MODELS
                    ],
                },
                "answers": sorted(answers, key=lambda item: (item["model"], item["round"])),
                "quality": {
                    "body_complete_rounds": sum(1 for item in answers if item["body_capture_complete"]),
                    "source_complete_rounds": sum(1 for item in answers if item["source_capture_complete"]),
                    "analysis_complete_rounds": sum(1 for item in answers if item["analysis"]["complete"]),
                    "total_body_chars": sum(int(item["body_length"]) for item in answers),
                    "captured_sources": sum(len(item["sources"]) for item in answers),
                    "expected_sources": sum(int(item["expected_source_count"]) for item in answers),
                },
            },
        }

    def paid_monitor_dashboard(self, customer_slug: str) -> dict[str, Any] | None:
        """Build the customer-facing, cross-day monitor panel from persisted runs.

        Daily values deliberately come from ``diagnosis_report`` so a value shown
        in the trend cannot drift from the corresponding single-run report.
        """
        customer_slug = str(customer_slug or "").strip().lower()
        match = re.fullmatch(r"paid-([a-f0-9]{24})", customer_slug)
        if not match:
            return None
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM paid_monitors WHERE substr(id,1,24)=? LIMIT 1",
                (match.group(1),),
            ).fetchone()
            monitor = self._decode_paid_monitor(row)
            if monitor is None:
                return None
            run_rows = connection.execute(
                "SELECT run_date,task_id FROM paid_monitor_runs WHERE monitor_id=? "
                "ORDER BY run_date DESC LIMIT 90",
                (monitor["id"],),
            ).fetchall()

        today = datetime.now(BEIJING).date().isoformat()
        access_allowed = bool(
            monitor["is_paid"]
            and monitor["starts_on"] <= today <= monitor["expires_on"]
        )
        if not access_allowed:
            return {
                "customer_slug": customer_slug,
                "access_allowed": False,
                "monitor": None,
                "current_task": None,
                "days": [],
                "latest": None,
                "competitors": [],
                "sources": [],
            }

        days: list[dict[str, Any]] = []
        for run_row in reversed(run_rows):
            task_id = str(run_row["task_id"] or "")
            if not task_id:
                continue
            payload = self.diagnosis_report(
                customer_slug, task_kind="paid_monitor", task_id=task_id,
                include_readiness=False,
            )
            task = payload.get("task") if isinstance(payload.get("task"), dict) else None
            report = payload.get("report") if isinstance(payload.get("report"), dict) else None
            if task is None or report is None:
                continue
            if int(report.get("completed_rounds") or 0) <= 0:
                continue
            days.append({
                "date": str(run_row["run_date"]),
                "status": str(task.get("status") or ""),
                "completed_steps": int(task.get("completed_steps") or 0),
                "total_steps": int(task.get("total_steps") or 0),
                "updated_at": str(task.get("updated_at") or task.get("created_at") or ""),
                "overall_rate": float(report.get("overall_rate") or 0),
                "first_share": float(report.get("first_share") or 0),
                "top3_share": float(report.get("top3_share") or 0),
                "top5_share": float(report.get("top5_share") or 0),
                "recommended_rounds": int(report.get("recommended_rounds") or 0),
                "completed_rounds": int(report.get("completed_rounds") or 0),
                "target_rounds": int(report.get("target_rounds") or 0),
                "models": report.get("models") or [],
                "competitors": report.get("competitors") or [],
                "sources": report.get("sources") or [],
                "source_analysis": report.get("source_analysis") or {},
            })

        data_days = days
        latest = data_days[-1] if data_days else None
        current_task = None
        if run_rows and run_rows[0]["task_id"]:
            raw_task = self.get(str(run_rows[0]["task_id"]), events=False)
            if raw_task is not None:
                public_task = self._public_task(raw_task)
                current_task = {
                    "status": str(public_task.get("status") or ""),
                    "completed_steps": int(public_task.get("completed_steps") or 0),
                    "total_steps": int(public_task.get("total_steps") or 0),
                    "message": str(public_task.get("message") or ""),
                    "updated_at": str(public_task.get("updated_at") or ""),
                    "queue_position": int(public_task.get("queue_position") or 0),
                }

        competitor_index: dict[str, dict[str, Any]] = {}
        source_index: dict[str, dict[str, Any]] = {}
        day_count = max(1, len(data_days))
        for day in data_days:
            seen_competitors: set[str] = set()
            for competitor in day["competitors"]:
                if not isinstance(competitor, dict):
                    continue
                name = str(competitor.get("name") or "").strip()
                if not name:
                    continue
                key = self._brand_key(name)
                item = competitor_index.setdefault(key, {
                    "name": name, "visibility_total": 0.0, "mention_rounds": 0,
                    "recommended_mentions": 0, "models": set(), "products": set(),
                    "active_days": 0,
                })
                item["visibility_total"] += float(competitor.get("visibility_score") or 0)
                item["mention_rounds"] += int(competitor.get("mention_rounds") or 0)
                item["recommended_mentions"] += int(competitor.get("recommended_mentions") or 0)
                item["models"].update(str(value) for value in competitor.get("mention_models") or [])
                item["products"].update(str(value) for value in competitor.get("products") or [])
                if key not in seen_competitors:
                    item["active_days"] += 1
                    seen_competitors.add(key)
            for source in day["sources"]:
                if not isinstance(source, dict):
                    continue
                url = str(source.get("url") or "").strip()
                if not url:
                    continue
                item = source_index.setdefault(url, {
                    "url": url,
                    "title": str(source.get("title") or url),
                    "domain": str(source.get("domain") or "未知来源"),
                    "citation_count": 0,
                    "models": set(),
                    "active_days": 0,
                })
                item["citation_count"] += int(source.get("citation_count") or 0)
                item["models"].update(str(value) for value in source.get("models") or [])
                item["active_days"] += 1

        competitors = [{
            "name": item["name"],
            "visibility_score": round(item["visibility_total"] / day_count, 1),
            "mention_rounds": item["mention_rounds"],
            "recommended_mentions": item["recommended_mentions"],
            "models": sorted(item["models"]),
            "products": sorted(item["products"]),
            "active_days": item["active_days"],
        } for item in competitor_index.values()]
        competitors.sort(key=lambda item: (-item["visibility_score"], -item["mention_rounds"], item["name"]))
        sources = [{**item, "models": sorted(item["models"])} for item in source_index.values()]
        sources.sort(key=lambda item: (-item["citation_count"], -item["active_days"], item["domain"]))

        return {
            "customer_slug": customer_slug,
            "access_allowed": True,
            "monitor": {
                "paid_user_id": monitor["paid_user_id"],
                "brand_name": monitor["brand_name"],
                "product_name": monitor["product_name"],
                "questions": monitor["questions"],
                "starts_on": monitor["starts_on"],
                "expires_on": monitor["expires_on"],
                "status": monitor["status"],
                "model_rounds": monitor["model_rounds"],
                "updated_at": monitor["updated_at"],
            },
            "current_task": current_task,
            "days": days,
            "latest": latest,
            "competitors": competitors,
            "sources": sources,
        }

    def cancel(self, task_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM remote_tasks WHERE id=?", (task_id,)).fetchone()
            if row is None:
                connection.rollback()
                return None
            if row["status"] in FINAL_STATES:
                connection.commit()
                return self._decode(row)
            if row["status"] == "running":
                message = "正在停止，当前采集动作结束后退出"
                connection.execute(
                    "UPDATE remote_tasks SET cancel_requested=1,pause_requested=0,preempt_requested=0,message=?,"
                    "updated_at=? WHERE id=?",
                    (message, now_text(), task_id),
                )
            else:
                message = "任务已取消"
                connection.execute(
                    "UPDATE remote_tasks SET status='cancelled',cancel_requested=1,pause_requested=0,preempt_requested=0,"
                    "message=?,lease_token='',lease_expires=0,updated_at=?,finished_at=? WHERE id=?",
                    (message, now_text(), now_text(), task_id),
                )
            self._event(connection, task_id, message, "warning")
            connection.commit()
        return self.get(task_id)

    def pause(self, task_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM remote_tasks WHERE id=?", (task_id,)).fetchone()
            if row is None:
                connection.rollback()
                return None
            if row["status"] == "paused":
                connection.commit()
                return self.get(task_id)
            if row["status"] in FINAL_STATES:
                connection.rollback()
                raise ValueError("已结束任务不能暂停，可选择重跑")
            if row["status"] == "running":
                message = "正在暂停，当前轮结束后保存断点"
                connection.execute(
                    "UPDATE remote_tasks SET pause_requested=1,preempt_requested=0,message=?,updated_at=? WHERE id=?",
                    (message, now_text(), task_id),
                )
            else:
                message = "任务已暂停"
                connection.execute(
                    "UPDATE remote_tasks SET status='paused',pause_requested=0,preempt_requested=0,message=?,"
                    "worker_id='',lease_token='',lease_expires=0,updated_at=? WHERE id=?",
                    (message, now_text(), task_id),
                )
            self._event(connection, task_id, message, "warning")
            connection.commit()
        return self.get(task_id)

    def resume(self, task_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM remote_tasks WHERE id=?", (task_id,)).fetchone()
            if row is None:
                connection.rollback()
                return None
            if row["status"] not in {"paused", "failed"}:
                connection.rollback()
                raise ValueError("只有已暂停或失败任务可以从断点继续执行")
            message = "任务已恢复，等待本机采集器从断点继续"
            connection.execute(
                "UPDATE remote_tasks SET status='queued',pause_requested=0,preempt_requested=0,cancel_requested=0,"
                "worker_id='',lease_token='',lease_expires=0,message=?,error='',finished_at='',"
                "next_attempt_epoch=0,"
                "updated_at=? WHERE id=?",
                (message, now_text(), task_id),
            )
            self._event(connection, task_id, message)
            connection.commit()
        return self.get(task_id)

    def rerun(self, task_id: str) -> dict[str, Any] | None:
        source = self.get(task_id)
        if source is None:
            return None
        if source.get("status") not in DELETABLE_STATES:
            raise ValueError("请先停止当前任务，再执行重跑")
        return self.create({
            "models": list(source.get("models") or []),
            "questions": list(source.get("questions") or []),
            "rounds": int(source.get("rounds") or 1),
            "question_mode": str(source.get("question_mode") or "interleaved"),
            "customer_slug": str(source.get("customer_slug") or ""),
            "brand_name": str(source.get("brand_name") or ""),
            "product_name": str(source.get("product_name") or ""),
            "task_kind": str(source.get("task_kind") or ""),
        })

    @staticmethod
    def _purge_task_result_files(task_id: str, results_root: Path) -> None:
        if not results_root.exists():
            return
        with _RESULT_LOCK:
            for target in results_root.glob("*_results.jsonl"):
                temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
                kept = 0
                try:
                    with target.open("r", encoding="utf-8", errors="replace") as source, temporary.open(
                        "w", encoding="utf-8"
                    ) as destination:
                        for line in source:
                            try:
                                record = json.loads(line)
                            except (TypeError, ValueError, json.JSONDecodeError):
                                destination.write(line)
                                kept += 1
                                continue
                            if str(record.get("remote_task_id") or "") == task_id:
                                continue
                            destination.write(line)
                            kept += 1
                    if kept:
                        os.replace(temporary, target)
                    else:
                        temporary.unlink(missing_ok=True)
                        target.unlink(missing_ok=True)
                finally:
                    temporary.unlink(missing_ok=True)

    @staticmethod
    def _purge_single_result_file(request_id: str, results_root: Path) -> None:
        if not results_root.exists():
            return
        with _RESULT_LOCK:
            for target in results_root.glob("*_results.jsonl"):
                temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
                kept = 0
                try:
                    with target.open("r", encoding="utf-8", errors="replace") as source, temporary.open(
                        "w", encoding="utf-8"
                    ) as destination:
                        for line in source:
                            try:
                                record = json.loads(line)
                            except (TypeError, ValueError, json.JSONDecodeError):
                                destination.write(line)
                                kept += 1
                                continue
                            if str(record.get("remote_request_id") or "") == request_id:
                                continue
                            destination.write(line)
                            kept += 1
                    if kept:
                        os.replace(temporary, target)
                    else:
                        temporary.unlink(missing_ok=True)
                        target.unlink(missing_ok=True)
                finally:
                    temporary.unlink(missing_ok=True)

    def delete(self, task_id: str, results_root: Path) -> dict[str, Any] | None:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM remote_tasks WHERE id=?", (task_id,)
            ).fetchone()
            if row is None:
                connection.rollback()
                return None
            if row["status"] not in DELETABLE_STATES:
                connection.rollback()
                raise ValueError("运行中的任务不能删除，请先暂停或停止任务")
            task = self._decode(row) or {"id": task_id}
            connection.execute("DELETE FROM remote_task_results WHERE task_id=?", (task_id,))
            connection.execute("DELETE FROM remote_task_events WHERE task_id=?", (task_id,))
            connection.execute("DELETE FROM report_edit_audit WHERE task_id=?", (task_id,))
            connection.execute("DELETE FROM remote_tasks WHERE id=?", (task_id,))
            connection.commit()
        self._purge_task_result_files(task_id, results_root)
        return task

    def clear_results(self, task_id: str, results_root: Path) -> dict[str, Any] | None:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM remote_tasks WHERE id=?", (task_id,)).fetchone()
            if row is None:
                connection.rollback()
                return None
            if row["status"] not in DELETABLE_STATES:
                connection.rollback()
                raise ValueError("请先暂停或停止任务，再清空结果")
            connection.execute("DELETE FROM remote_task_results WHERE task_id=?", (task_id,))
            connection.execute(
                "UPDATE remote_tasks SET status='paused',completed_steps=0,current_model='',"
                "current_question='',message='任务数据已清空，可从头继续',error='',"
                "cancel_requested=0,pause_requested=0,preempt_requested=0,worker_id='',lease_token='',lease_expires=0,"
                "updated_at=?,finished_at='' WHERE id=?",
                (now_text(), task_id),
            )
            self._event(connection, task_id, "任务结果数据已清空", "warning")
            connection.commit()
        self._purge_task_result_files(task_id, results_root)
        return self.get(task_id)

    def delete_result(self, task_id: str, request_id: str,
                      results_root: Path) -> dict[str, Any] | None:
        request_id = str(request_id or "")[:160]
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            task_row = connection.execute(
                "SELECT * FROM remote_tasks WHERE id=?", (task_id,)
            ).fetchone()
            if task_row is None:
                connection.rollback()
                return None
            if task_row["status"] not in DELETABLE_STATES:
                connection.rollback()
                raise ValueError("请先暂停或停止任务，再删除单轮数据")
            result_row = connection.execute(
                "SELECT model_id,record_json FROM remote_task_results "
                "WHERE task_id=? AND request_id=?",
                (task_id, request_id),
            ).fetchone()
            if result_row is None:
                connection.rollback()
                raise KeyError("该轮结果不存在")
            try:
                record = json.loads(result_row["record_json"] or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                record = {}
            connection.execute(
                "DELETE FROM remote_task_results WHERE task_id=? AND request_id=?",
                (task_id, request_id),
            )
            completed = int(connection.execute(
                "SELECT COUNT(*) AS count FROM remote_task_results WHERE task_id=?",
                (task_id,),
            ).fetchone()["count"])
            model = str(record.get("collector_model") or result_row["model_id"] or "")
            round_number = int(record.get("round") or 0)
            message = f"已删除 {model} 第 {round_number} 轮数据，任务已暂停"
            connection.execute(
                "UPDATE remote_tasks SET status='paused',completed_steps=?,message=?,error='',"
                "cancel_requested=0,pause_requested=0,preempt_requested=0,worker_id='',lease_token='',lease_expires=0,"
                "updated_at=?,finished_at='' WHERE id=?",
                (completed, message, now_text(), task_id),
            )
            self._event(connection, task_id, message, "warning")
            connection.commit()
        self._purge_single_result_file(request_id, results_root)
        return self.get(task_id)

    def clear_finished(self, results_root: Path) -> int:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT id FROM remote_tasks WHERE status IN ('completed','failed','cancelled','paused')"
            ).fetchall()
        deleted = 0
        for row in rows:
            if self.delete(str(row["id"]), results_root) is not None:
                deleted += 1
        return deleted

    def claim(self, worker_id: str, readiness: dict[str, Any] | None = None,
              lease_seconds: int = 900) -> dict[str, Any] | None:
        self.sync_paid_monitor_tasks()
        worker_id = str(worker_id or "local-worker")[:100]
        readiness_supplied = bool(readiness)
        readiness = readiness if isinstance(readiness, dict) else {}
        safe_readiness = {}
        for model in DIAGNOSIS_MODELS:
            raw = readiness.get(model) if isinstance(readiness.get(model), dict) else {}
            safe_readiness[model] = {
                "ready": bool(raw.get("ready")),
                "message": str(raw.get("message") or "")[:160],
            }
        readiness_json = json.dumps(safe_readiness, ensure_ascii=False, separators=(",", ":"))

        def task_models_ready(row: sqlite3.Row) -> bool:
            if not readiness_supplied:
                return True
            try:
                models = json.loads(str(row["models_json"] or "[]"))
            except (TypeError, ValueError, json.JSONDecodeError):
                return False
            return bool(models) and all(bool(safe_readiness.get(str(model), {}).get("ready")) for model in models)

        now = time.time()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT INTO remote_workers(worker_id,last_seen_epoch,last_seen,status,current_task_id,message,readiness_json) "
                "VALUES(?,?,?,?,?,?,?) ON CONFLICT(worker_id) DO UPDATE SET "
                "last_seen_epoch=excluded.last_seen_epoch,last_seen=excluded.last_seen,status='idle',"
                "current_task_id='',message='等待任务',readiness_json=excluded.readiness_json",
                (worker_id, now, now_text(), "idle", "", "等待任务", readiness_json),
            )
            connection.execute(
                "UPDATE remote_tasks SET status='cancelled',message='任务已取消',"
                "lease_token='',lease_expires=0,updated_at=?,finished_at=? "
                "WHERE status='running' AND cancel_requested=1 AND lease_expires>0 AND lease_expires<?",
                (now_text(), now_text(), now),
            )
            connection.execute(
                "UPDATE remote_tasks SET status='queued',message='已为即时诊断让出资源，等待从断点继续',"
                "pause_requested=0,preempt_requested=0,worker_id='',lease_token='',lease_expires=0,updated_at=? "
                "WHERE status='running' AND pause_requested=1 AND preempt_requested=1 "
                "AND lease_expires>0 AND lease_expires<?",
                (now_text(), now),
            )
            connection.execute(
                "UPDATE remote_tasks SET status='paused',message='任务已暂停',pause_requested=0,"
                "worker_id='',lease_token='',lease_expires=0,updated_at=? "
                "WHERE status='running' AND pause_requested=1 AND preempt_requested=0 "
                "AND lease_expires>0 AND lease_expires<?",
                (now_text(), now),
            )
            connection.execute(
                "UPDATE remote_tasks SET status='queued',worker_id='',lease_token='',lease_expires=0,"
                "message='本机连接中断，等待重新领取',updated_at=? "
                "WHERE status='running' AND cancel_requested=0 AND pause_requested=0 "
                "AND lease_expires>0 AND lease_expires<?",
                (now_text(), now),
            )
            active = connection.execute(
                "SELECT * FROM remote_tasks WHERE status='running' AND lease_expires>? "
                "ORDER BY created_at LIMIT 1",
                (now,),
            ).fetchone()
            if active is not None:
                if str(active["worker_id"] or "") == worker_id:
                    if not task_models_ready(active):
                        message = "采集资源保护中，任务继续排队"
                        connection.execute(
                            "UPDATE remote_tasks SET status='queued',worker_id='',lease_token='',lease_expires=0,"
                            "message=?,updated_at=? WHERE id=?",
                            (message, now_text(), active["id"]),
                        )
                        connection.execute(
                            "UPDATE remote_workers SET status='idle',current_task_id='',message=?,"
                            "last_seen_epoch=?,last_seen=? WHERE worker_id=?",
                            (message, now, now_text(), worker_id),
                        )
                        self._event(connection, active["id"], message, "warning")
                        connection.commit()
                        return None
                    if active["cancel_requested"]:
                        connection.execute(
                            "UPDATE remote_tasks SET status='cancelled',message='任务已取消',"
                            "worker_id='',lease_token='',lease_expires=0,updated_at=?,finished_at=? "
                            "WHERE id=?",
                            (now_text(), now_text(), active["id"]),
                        )
                        self._event(connection, active["id"], "任务已取消")
                        connection.commit()
                        return None
                    if active["pause_requested"]:
                        preempted = bool(active["preempt_requested"])
                        status = "queued" if preempted else "paused"
                        message = (
                            "已为即时诊断让出资源，等待从断点继续"
                            if preempted else "任务已暂停"
                        )
                        connection.execute(
                            "UPDATE remote_tasks SET status=?,message=?,pause_requested=0,"
                            "preempt_requested=0,worker_id='',lease_token='',lease_expires=0,"
                            "updated_at=? WHERE id=?",
                            (status, message, now_text(), active["id"]),
                        )
                        self._event(connection, active["id"], message, "warning")
                        connection.commit()
                        return None
                    token = secrets.token_urlsafe(32)
                    message = "本机采集器已恢复，正在从断点继续"
                    connection.execute(
                        "UPDATE remote_tasks SET lease_token=?,lease_expires=?,message=?,updated_at=? "
                        "WHERE id=?",
                        (token, now + max(120, lease_seconds), message, now_text(), active["id"]),
                    )
                    connection.execute(
                        "UPDATE remote_workers SET status='busy',current_task_id=?,message=?,"
                        "last_seen_epoch=?,last_seen=? WHERE worker_id=?",
                        (active["id"], message, now, now_text(), worker_id),
                    )
                    self._event(connection, active["id"], message)
                    connection.commit()
                    claimed = connection.execute(
                        "SELECT * FROM remote_tasks WHERE id=?", (active["id"],)
                    ).fetchone()
                    task = self._decode(claimed, include_private=True) or {}
                    progress = self._model_progress(connection, task)
                    task["model_progress"] = progress
                    task["completed_rounds"] = {
                        model: list(value["completed_rounds"])
                        for model, value in progress.items()
                    }
                    task["answer_fingerprints"] = self._answer_fingerprints(
                        connection, str(task.get("id") or "")
                    )
                    task["capture_fingerprints"] = self._capture_fingerprints(
                        connection, str(task.get("id") or "")
                    )
                    return task
                connection.commit()
                return None
            row = connection.execute(
                "SELECT * FROM remote_tasks WHERE status='queued' AND cancel_requested=0 "
                "AND next_attempt_epoch<=? "
                "ORDER BY queue_priority DESC, created_at, rowid LIMIT 1"
                , (now,)
            ).fetchone()
            if row is None:
                connection.commit()
                return None
            if not task_models_ready(row):
                message = "采集资源保护中，任务继续排队"
                connection.execute(
                    "UPDATE remote_workers SET status='idle',current_task_id='',message=?,"
                    "last_seen_epoch=?,last_seen=? WHERE worker_id=?",
                    (message, now, now_text(), worker_id),
                )
                connection.commit()
                return None
            token = secrets.token_urlsafe(32)
            connection.execute(
                "UPDATE remote_tasks SET status='running',worker_id=?,lease_token=?,lease_expires=?,"
                "next_attempt_epoch=0,message='本机已领取，正在准备',updated_at=? WHERE id=?",
                (worker_id, token, now + max(120, lease_seconds), now_text(), row["id"]),
            )
            connection.execute(
                "UPDATE remote_workers SET status='busy',current_task_id=?,message='正在执行任务',"
                "last_seen_epoch=?,last_seen=? WHERE worker_id=?",
                (row["id"], now, now_text(), worker_id),
            )
            self._event(connection, row["id"], f"本机采集器 {worker_id} 已领取任务")
            connection.commit()
            claimed = connection.execute("SELECT * FROM remote_tasks WHERE id=?", (row["id"],)).fetchone()
        task = self._decode(claimed, include_private=True) or {}
        with self._connection() as connection:
            progress = self._model_progress(connection, task)
        task["model_progress"] = progress
        task["completed_rounds"] = {
            model: list(value["completed_rounds"])
            for model, value in progress.items()
        }
        with self._connection() as connection:
            task["answer_fingerprints"] = self._answer_fingerprints(
                connection, str(task.get("id") or "")
            )
            task["capture_fingerprints"] = self._capture_fingerprints(
                connection, str(task.get("id") or "")
            )
        return task

    def list_workers(self) -> list[dict[str, Any]]:
        moment = time.time()
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM remote_workers ORDER BY last_seen_epoch DESC LIMIT 50"
            ).fetchall()
        output = []
        for row in rows:
            item = dict(row)
            item["online"] = moment - float(item.pop("last_seen_epoch") or 0) <= 20
            try:
                item["readiness"] = json.loads(item.pop("readiness_json") or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                item["readiness"] = {}
            output.append(item)
        return output

    @staticmethod
    def _decode_login(row: sqlite3.Row | None, *, include_private: bool = False) -> dict[str, Any] | None:
        if row is None:
            return None
        value = dict(row)
        if not include_private:
            value.pop("lease_token", None)
            value.pop("lease_expires", None)
        return value

    def request_login(self, model_id: str) -> dict[str, Any]:
        model_id = str(model_id or "").strip().lower()
        if model_id not in DIAGNOSIS_MODELS:
            raise ValueError("只支持诊断模型登录")
        with self._connection() as connection:
            active = connection.execute(
                "SELECT * FROM remote_login_requests WHERE model_id=? "
                "AND status IN ('queued','running') ORDER BY created_at DESC LIMIT 1",
                (model_id,),
            ).fetchone()
            if active is not None:
                return self._decode_login(active) or {}
            login_id = uuid.uuid4().hex
            created = now_text()
            connection.execute(
                "INSERT INTO remote_login_requests(id,model_id,status,message,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?)",
                (login_id, model_id, "queued", "等待本机打开隐身登录窗口", created, created),
            )
            row = connection.execute(
                "SELECT * FROM remote_login_requests WHERE id=?", (login_id,)
            ).fetchone()
        return self._decode_login(row) or {}

    def list_login_requests(self) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM remote_login_requests ORDER BY created_at DESC LIMIT 60"
            ).fetchall()
        latest: dict[str, dict[str, Any]] = {}
        for row in rows:
            value = self._decode_login(row) or {}
            latest.setdefault(str(value.get("model_id") or ""), value)
        return [latest[model] for model in DIAGNOSIS_MODELS if model in latest]

    def claim_login(self, worker_id: str, readiness: dict[str, Any] | None = None,
                    lease_seconds: int = 900) -> dict[str, Any] | None:
        worker_id = str(worker_id or "local-worker")[:100]
        readiness = readiness if isinstance(readiness, dict) else {}
        safe_readiness = {}
        for model in DIAGNOSIS_MODELS:
            raw = readiness.get(model) if isinstance(readiness.get(model), dict) else {}
            safe_readiness[model] = {
                "ready": bool(raw.get("ready")),
                "message": str(raw.get("message") or "")[:160],
            }
        readiness_json = json.dumps(safe_readiness, ensure_ascii=False, separators=(",", ":"))
        moment = time.time()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT INTO remote_workers(worker_id,last_seen_epoch,last_seen,status,current_task_id,message,readiness_json) "
                "VALUES(?,?,?,?,?,?,?) ON CONFLICT(worker_id) DO UPDATE SET "
                "last_seen_epoch=excluded.last_seen_epoch,last_seen=excluded.last_seen,status='idle',"
                "current_task_id='',message='等待任务',readiness_json=excluded.readiness_json",
                (worker_id, moment, now_text(), "idle", "", "等待任务", readiness_json),
            )
            connection.execute(
                "UPDATE remote_login_requests SET status='queued',worker_id='',lease_token='',"
                "lease_expires=0,message='本机连接中断，等待重新打开登录窗口',updated_at=? "
                "WHERE status='running' AND lease_expires>0 AND lease_expires<?",
                (now_text(), moment),
            )
            row = connection.execute(
                "SELECT * FROM remote_login_requests WHERE status='queued' "
                "ORDER BY created_at LIMIT 1"
            ).fetchone()
            if row is None:
                connection.commit()
                return None
            token = secrets.token_urlsafe(32)
            message = "本机正在打开隐身登录窗口"
            connection.execute(
                "UPDATE remote_login_requests SET status='running',worker_id=?,lease_token=?,"
                "lease_expires=?,message=?,updated_at=? WHERE id=?",
                (worker_id, token, moment + max(120, lease_seconds), message, now_text(), row["id"]),
            )
            connection.execute(
                "UPDATE remote_workers SET last_seen_epoch=?,last_seen=?,status='login',"
                "current_task_id=?,message=? WHERE worker_id=?",
                (moment, now_text(), row["id"], message, worker_id),
            )
            connection.commit()
            claimed = connection.execute(
                "SELECT * FROM remote_login_requests WHERE id=?", (row["id"],)
            ).fetchone()
        return self._decode_login(claimed, include_private=True)

    def _leased_login_row(self, connection: sqlite3.Connection, login_id: str,
                          lease_token: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM remote_login_requests WHERE id=?", (login_id,)
        ).fetchone()
        if row is None:
            raise KeyError("登录请求不存在")
        if not secure_token_matches(lease_token, row["lease_token"]):
            raise PermissionError("登录请求租约无效")
        return row

    def login_heartbeat(self, login_id: str, lease_token: str,
                        payload: dict[str, Any], lease_seconds: int = 900) -> dict[str, Any]:
        message = str(payload.get("message") or "等待你在本机隐身窗口完成登录")[:500]
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._leased_login_row(connection, login_id, lease_token)
            if row["status"] != "running":
                connection.rollback()
                raise ValueError("登录请求已经结束")
            moment = time.time()
            connection.execute(
                "UPDATE remote_login_requests SET message=?,lease_expires=?,updated_at=? WHERE id=?",
                (message, moment + max(120, lease_seconds), now_text(), login_id),
            )
            if row["worker_id"]:
                connection.execute(
                    "UPDATE remote_workers SET last_seen_epoch=?,last_seen=?,status='login',"
                    "current_task_id=?,message=? WHERE worker_id=?",
                    (moment, now_text(), login_id, message, row["worker_id"]),
                )
            connection.commit()
        return {"ok": True}

    def finish_login(self, login_id: str, lease_token: str,
                     payload: dict[str, Any]) -> dict[str, Any]:
        requested = str(payload.get("status") or "ready").lower()
        if requested not in {"ready", "failed"}:
            raise ValueError("登录结束状态无效")
        error = str(payload.get("error") or "")[:1000]
        message = "隐身登录已就绪" if requested == "ready" else "隐身登录失败"
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._leased_login_row(connection, login_id, lease_token)
            connection.execute(
                "UPDATE remote_login_requests SET status=?,message=?,error=?,lease_token='',"
                "lease_expires=0,updated_at=?,finished_at=? WHERE id=?",
                (requested, message, error, now_text(), now_text(), login_id),
            )
            if row["worker_id"]:
                connection.execute(
                    "UPDATE remote_workers SET last_seen_epoch=?,last_seen=?,status='idle',"
                    "current_task_id='',message='等待任务' WHERE worker_id=?",
                    (time.time(), now_text(), row["worker_id"]),
                )
            connection.commit()
            finished = connection.execute(
                "SELECT * FROM remote_login_requests WHERE id=?", (login_id,)
            ).fetchone()
        return self._decode_login(finished) or {}

    def _leased_row(self, connection: sqlite3.Connection, task_id: str,
                    lease_token: str) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM remote_tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            raise KeyError("任务不存在")
        if not secure_token_matches(lease_token, row["lease_token"]):
            raise PermissionError("任务租约无效")
        return row

    def heartbeat(self, task_id: str, lease_token: str, payload: dict[str, Any],
                  lease_seconds: int = 900) -> dict[str, Any]:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._leased_row(connection, task_id, lease_token)
            if row["status"] not in {"running"}:
                connection.rollback()
                raise ValueError("任务已经结束")
            model = str(payload.get("model") or row["current_model"] or "")[:40]
            question = str(payload.get("question") or row["current_question"] or "")[:500]
            message = str(payload.get("message") or row["message"] or "运行中")[:500]
            connection.execute(
                "UPDATE remote_tasks SET current_model=?,current_question=?,message=?,lease_expires=?,updated_at=? WHERE id=?",
                (model, question, message, time.time() + max(120, lease_seconds), now_text(), task_id),
            )
            if row["worker_id"]:
                connection.execute(
                    "UPDATE remote_workers SET last_seen_epoch=?,last_seen=?,status='busy',"
                    "current_task_id=?,message=? WHERE worker_id=?",
                    (time.time(), now_text(), task_id, message, row["worker_id"]),
                )
            if message and message != row["message"]:
                self._event(connection, task_id, message)
            connection.commit()
            cancel_requested = bool(row["cancel_requested"])
            pause_requested = bool(row["pause_requested"])
        return {
            "ok": True,
            "cancel_requested": cancel_requested,
            "pause_requested": pause_requested,
        }

    def accept_result(self, task_id: str, lease_token: str, model_id: str,
                      request_id: str, record: dict[str, Any], results_root: Path) -> dict[str, Any]:
        if model_id not in ALLOWED_MODELS:
            raise ValueError("未知模型")
        if not isinstance(record, dict):
            raise ValueError("record 必须是对象")
        request_id = str(request_id or "")[:160]
        if not request_id:
            raise ValueError("request_id 不能为空")
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._leased_row(connection, task_id, lease_token)
            if row["cancel_requested"]:
                connection.commit()
                return {
                    "ok": True,
                    "stored": False,
                    "completed_steps": int(row["completed_steps"]),
                    "cancel_requested": True,
                    "pause_requested": bool(row["pause_requested"]),
                }
            existing = connection.execute(
                "SELECT request_id FROM remote_task_results WHERE request_id=?", (request_id,)
            ).fetchone()
            if existing:
                connection.commit()
                return {
                    "ok": True,
                    "duplicate": True,
                    "completed_steps": int(row["completed_steps"]),
                    "cancel_requested": bool(row["cancel_requested"]),
                    "pause_requested": bool(row["pause_requested"]),
                }
            record = dict(record)
            record.update({"remote_task_id": task_id, "remote_request_id": request_id})
            # Diagnosis responses are already stored once in the report database.
            # Do not create a second server-side JSONL copy of customer report data.
            if str(row["task_kind"] or "") != "diagnosis":
                results_root.mkdir(parents=True, exist_ok=True)
                target = results_root / f"{model_id}_results.jsonl"
                line = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
                with _RESULT_LOCK:
                    with target.open("a", encoding="utf-8") as handle:
                        handle.write(line)
                        handle.flush()
                        os.fsync(handle.fileno())
            connection.execute(
                "INSERT INTO remote_task_results(request_id,task_id,model_id,created_at,record_json) VALUES(?,?,?,?,?)",
                (request_id, task_id, model_id, now_text(), json.dumps(record, ensure_ascii=False)),
            )
            completed = min(int(row["total_steps"]), int(row["completed_steps"]) + 1)
            message = f"已完成 {completed}/{row['total_steps']} 轮"
            connection.execute(
                "UPDATE remote_tasks SET completed_steps=?,current_model=?,message=?,lease_expires=?,updated_at=? WHERE id=?",
                (completed, model_id, message, time.time() + 900, now_text(), task_id),
            )
            self._event(connection, task_id, message)
            connection.commit()
        return {
            "ok": True,
            "duplicate": False,
            "completed_steps": completed,
            "cancel_requested": False,
            "pause_requested": bool(row["pause_requested"]),
        }

    def finish(self, task_id: str, lease_token: str, payload: dict[str, Any]) -> dict[str, Any]:
        requested = str(payload.get("status") or "completed").lower()
        if requested not in {"completed", "failed", "cancelled", "paused", "retrying"}:
            raise ValueError("结束状态无效")
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._leased_row(connection, task_id, lease_token)
            if row["cancel_requested"]:
                requested = "cancelled"
            elif row["pause_requested"]:
                requested = "paused"
            error = str(payload.get("error") or "")[:2000]
            if (requested == "paused" and bool(row["preempt_requested"])
                    and str(row["task_kind"] or "") == "paid_monitor"):
                message = (
                    f"已为即时诊断让出资源，已保存 {row['completed_steps']}/{row['total_steps']} 项，"
                    "诊断完成后自动从断点继续"
                )
                connection.execute(
                    "UPDATE remote_tasks SET status='queued',message=?,error='',"
                    "lease_token='',lease_expires=0,cancel_requested=0,pause_requested=0,"
                    "preempt_requested=0,worker_id='',next_attempt_epoch=0,updated_at=?,finished_at='' "
                    "WHERE id=?",
                    (message, now_text(), task_id),
                )
                if row["worker_id"]:
                    connection.execute(
                        "UPDATE remote_workers SET last_seen_epoch=?,last_seen=?,status='idle',"
                        "current_task_id='',message='等待任务' WHERE worker_id=?",
                        (time.time(), now_text(), row["worker_id"]),
                    )
                self._event(connection, task_id, message, "warning")
                connection.commit()
                return self.get(task_id) or {}
            if (requested == "completed" and str(row["task_kind"] or "") == "diagnosis"
                    and int(row["completed_steps"]) < int(row["total_steps"])):
                requested = "retrying"
                error = error or "采集结果尚未完整，系统已自动从断点续跑"
            if requested == "retrying":
                retry_count = int(row["retry_count"] or 0) + 1
                delay = min(300, 15 * (2 ** min(retry_count - 1, 5)))
                message = (
                    f"部分平台暂未完成，已安全保存 {row['completed_steps']}/{row['total_steps']} 项，"
                    f"{delay} 秒后自动续跑"
                )
                connection.execute(
                    "UPDATE remote_tasks SET status='queued',message=?,error=?,retry_count=?,"
                    "next_attempt_epoch=?,lease_token='',lease_expires=0,cancel_requested=0,"
                    "pause_requested=0,preempt_requested=0,worker_id='',updated_at=?,finished_at='' WHERE id=?",
                    (message, error, retry_count, time.time() + delay, now_text(), task_id),
                )
                if row["worker_id"]:
                    connection.execute(
                        "UPDATE remote_workers SET last_seen_epoch=?,last_seen=?,status='idle',"
                        "current_task_id='',message='等待任务' WHERE worker_id=?",
                        (time.time(), now_text(), row["worker_id"]),
                    )
                self._event(connection, task_id, message, "warning")
                connection.commit()
                return self.get(task_id) or {}
            messages = {
                "completed": "任务已完成",
                "failed": "任务执行失败",
                "cancelled": "任务已取消",
                "paused": "任务已暂停",
            }
            finished_at = "" if requested == "paused" else now_text()
            connection.execute(
                "UPDATE remote_tasks SET status=?,message=?,error=?,lease_token='',lease_expires=0,"
                "cancel_requested=0,pause_requested=0,preempt_requested=0,worker_id='',next_attempt_epoch=0,"
                "updated_at=?,finished_at=? WHERE id=?",
                (requested, messages[requested], error, now_text(), finished_at, task_id),
            )
            if row["worker_id"]:
                connection.execute(
                    "UPDATE remote_workers SET last_seen_epoch=?,last_seen=?,status='idle',"
                    "current_task_id='',message='等待任务' WHERE worker_id=?",
                    (time.time(), now_text(), row["worker_id"]),
                )
            self._event(connection, task_id, messages[requested],
                        "error" if requested == "failed" else
                        "warning" if requested == "paused" else "info")
            connection.commit()
        return self.get(task_id) or {}


def task_page_html() -> str:
    """Persistent task console with per-model and per-round inspection controls."""
    return r'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>采集任务控制台</title>
<style>
*{box-sizing:border-box}body{margin:0;background:#f3f7f5;color:#172b25;font:14px/1.55 system-ui,-apple-system,"Microsoft YaHei",sans-serif}.wrap{max-width:1260px;margin:28px auto;padding:0 18px}.card{background:#fff;border:1px solid #dce8e3;border-radius:16px;padding:20px;margin-bottom:18px;box-shadow:0 10px 32px #153c2c0c}h1{font-size:27px;margin:0 0 4px}h2{font-size:18px}.topbar,.row{display:flex;gap:10px;flex-wrap:wrap;align-items:center}.topbar{justify-content:space-between;margin-bottom:18px}.grow{flex:1}.muted{color:#6b7d76}.ok{color:#067647}.error{color:#c43b45}.hidden{display:none!important}a{color:#087f5b}textarea,input,select,button{font:inherit;border:1px solid #c9d9d2;border-radius:9px;padding:9px 11px}textarea{width:100%;min-height:110px}input[type=password]{min-width:310px}button{border:0;background:#108a68;color:#fff;cursor:pointer}button.secondary{background:#60746c}button.warning{background:#b7791f}button.danger{background:#c43b45}button:disabled{opacity:.48;cursor:not-allowed}.badge{padding:3px 9px;border-radius:999px;background:#e9f5f0}.task{padding:18px 0;border-top:1px solid #e9efec}.task:first-child{border-top:0}.bar{height:9px;background:#e5ede9;border-radius:9px;overflow:hidden;margin:9px 0}.bar i{display:block;height:100%;background:linear-gradient(90deg,#139b74,#43c49a)}.customer{font-weight:700;color:#087f5b}.task-actions{margin-top:11px}.model-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:9px;margin:10px 0}.model-progress{padding:9px 11px;border:1px solid #dce8e3;border-radius:10px;background:#f7faf8}.details{margin-top:14px;border-radius:12px;background:#f7faf8;border:1px solid #dce8e3;padding:13px}.event-log{background:#12221c;color:#d9e8e1;border-radius:10px;padding:10px 12px;margin-bottom:13px;font:12px/1.7 ui-monospace,monospace}.event-log div{border-bottom:1px solid #ffffff14;padding:3px}.event-log time{color:#87d7bb;margin-right:10px}.result-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}.result-card{background:#fff;border:1px solid #dce8e3;border-radius:11px;padding:13px;min-width:0}.quality{display:flex;gap:7px;flex-wrap:wrap;margin:8px 0}.quality span{padding:2px 7px;border-radius:999px;background:#eef3f1;font-size:12px}.quality .good{background:#dcfce7;color:#166534}.quality .bad{background:#fee2e2;color:#991b1b}details{margin-top:8px}summary{cursor:pointer;font-weight:650}pre{white-space:pre-wrap;word-break:break-word;max-height:380px;overflow:auto;background:#f6f8f7;padding:10px;border-radius:8px;font:13px/1.6 system-ui,-apple-system,"Microsoft YaHei",sans-serif}.sources{margin:8px 0 0;padding-left:20px;word-break:break-all}.worker{padding:10px 12px;border:1px solid #dce8e3;border-radius:10px;background:#f7faf8}.dot{display:inline-block;width:8px;height:8px;border-radius:50%;background:#94a39d;margin-right:6px}.dot.online,.dot.ready{background:#12a878}.dot.running,.dot.queued{background:#e79b28}.login-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:12px}.login-card{padding:14px;border:1px solid #dce8e3;border-radius:11px;background:#f7faf8}.login-card header{display:flex;justify-content:space-between}.login-card button{width:100%}@media(max-width:820px){.model-grid,.result-grid,.login-grid{grid-template-columns:1fr}.topbar{align-items:flex-start;flex-direction:column}input[type=password]{width:100%;min-width:0}}
</style></head><body><div class="wrap">
<div class="topbar"><div><h1>采集任务控制台</h1><a href="/geo/">返回客户诊断页</a></div><span class="muted">刷新页面不会停止采集；任务状态保存在服务端</span></div>
<section class="card"><div class="row"><h2 class="grow">任务队列与采集数据</h2><button class="danger" id="cleanup">清理全部已结束/暂停任务</button><button class="secondary" id="reload">立即刷新</button></div><label>管理授权（仅保存在本机 Chrome）<br><input id="key" type="password" autocomplete="off" placeholder="从插件进入会自动填写"></label><div id="task-summary" class="row muted">正在加载…</div><p class="muted">暂停会等待当前正在采集的一轮安全落盘后停止；继续会跳过已有轮次并从断点恢复。停止用于彻底结束任务。</p><div id="tasks" class="muted">输入密钥后加载</div></section>
<section class="card"><div class="row"><h2 class="grow">本机 Worker</h2><span class="muted">20 秒内有心跳视为在线</span></div><div id="workers" class="row muted">输入密钥后加载</div></section>
<section class="card"><h2>模型登录状态检测</h2><div id="logins" class="login-grid muted">输入密钥后加载</div></section>
<section class="card"><h2>高级：手动创建任务</h2><label>模型</label><div class="row" id="models"></div><label>问题（每行一个，最多 10 个）</label><textarea id="questions"></textarea><div class="row"><label>循环次数 <select id="rounds"><option>1</option><option>2</option><option>3</option><option>8</option></select></label><label>顺序 <select id="mode"><option value="interleaved">按轮交错</option><option value="sequential">逐题完成</option></select></label><button id="create">提交任务</button><span id="notice"></span></div></section>
</div><script>
const names={doubao:"豆包",yuanbao:"腾讯元宝",wenxin:"文心一言",deepseek:"DeepSeek",kimi:"Kimi",quark:"千问"};
const $=id=>document.getElementById(id),esc=s=>String(s??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const safeUrl=value=>{try{const u=new URL(String(value||""));return /^https?:$/.test(u.protocol)?u.href:""}catch(_){return""}};
const apiRoot=window.location.pathname.replace(/\/tasks\/?$/,"/api");
const keyStore="monitorTaskKeyV2";
const openDetails=new Set();
$("models").innerHTML=Object.entries(names).map(([k,v])=>`<label><input type="checkbox" value="${k}" checked> ${v}</label>`).join("");
function headers(){return{"Content-Type":"application/json","X-Monitor-Task-Token":$("key").value}}
async function api(path,options={}){const r=await fetch(path,{...options,headers:{...headers(),...(options.headers||{})},cache:"no-store"});const d=await r.json().catch(()=>({}));if(!r.ok||d.ok===false)throw Error(d.error||`HTTP ${r.status}`);return d}
function statusName(s){return({queued:"排队中",running:"执行中",paused:"已暂停",completed:"已完成",failed:"失败",cancelled:"已取消"})[s]||s}
function loginStatus(s){return({queued:"等待弹窗",running:"等待手动登录",ready:"已就绪",failed:"登录失败"})[s]||"未登录"}
function summary(tasks){const counts={running:0,queued:0,paused:0,completed:0,failed:0,cancelled:0};tasks.forEach(t=>{if(t.status in counts)counts[t.status]+=1});$("task-summary").innerHTML=`<span><b>${counts.running}</b> 个执行中</span><span><b>${counts.queued}</b> 个排队</span><span><b>${counts.paused}</b> 个暂停</span><span>${counts.completed} 个完成</span><span>${counts.failed} 个失败</span><span>${counts.cancelled} 个取消</span>`}
function taskButtons(t){const active=["queued","running"].includes(t.status),editable=["paused","completed","failed","cancelled"].includes(t.status);return `<button class="secondary" onclick="loadDetail('${t.id}')">逐轮数据与日志</button>${active?`<button class="warning" onclick="pauseTask('${t.id}')">暂停</button><button class="danger" onclick="cancelTask('${t.id}')">停止</button>`:""}${["paused","failed"].includes(t.status)?`<button onclick="resumeTask('${t.id}')">从断点继续</button>`:""}${editable?`<button onclick="rerunTask('${t.id}')">从头重跑</button><button class="warning" onclick="clearTask('${t.id}')">清空数据</button><button class="danger" onclick="deleteTask('${t.id}')">删除任务</button>`:""}`}
function taskCard(t){const pct=t.total_steps?Math.round(t.completed_steps*100/t.total_steps):0,report=t.customer_slug?`<a href="/geo/${esc(t.customer_slug)}" target="_blank">打开客户报告</a>`:"";return `<article class="task"><div class="row"><b>${esc(t.id.slice(0,10))}</b><span class="badge">${statusName(t.status)}</span>${t.customer_slug?`<span class="customer">/${esc(t.customer_slug)} · ${esc(t.brand_name||"")}${t.product_name?" · "+esc(t.product_name):""}</span>`:""}<span>${esc((t.models||[]).map(x=>names[x]||x).join("、"))}</span><span class="muted">${esc(t.created_at)}</span></div><p>${esc(t.message)}${t.current_question?" · "+esc(t.current_question):""}</p><div class="bar"><i style="width:${pct}%"></i></div><div class="row task-actions"><b>${t.completed_steps}/${t.total_steps} 轮 · ${pct}%</b>${taskButtons(t)}${report}${t.error?`<span class="error">${esc(t.error)}</span>`:""}</div><div id="detail-${t.id}" class="details hidden"></div></article>`}
async function load(){if(!$("key").value)return;try{const [d,w,l]=await Promise.all([api(`${apiRoot}/tasks`),api(`${apiRoot}/workers`),api(`${apiRoot}/logins`)]);summary(d.tasks||[]);$("tasks").innerHTML=(d.tasks||[]).length?d.tasks.map(taskCard).join(""):"暂无任务";for(const id of [...openDetails]){if($(`detail-${id}`))renderDetail(id);else openDetails.delete(id)}$("workers").innerHTML=(w.workers||[]).length?w.workers.map(x=>`<div class="worker"><span class="dot ${x.online?"online":""}"></span><b>${esc(x.worker_id)}</b> · ${x.online?"在线":"离线"} · ${esc(x.message)}<br><small>${esc(x.last_seen)}</small></div>`).join(""):"尚无 Worker 心跳";const latest=Object.fromEntries((l.logins||[]).map(x=>[x.model_id,x])),worker=(w.workers||[]).find(x=>x.online)||{};$("logins").innerHTML=["doubao","yuanbao","wenxin","deepseek","kimi"].map(model=>{const item=latest[model]||{},ready=Boolean(worker.readiness?.[model]?.ready),running=["queued","running"].includes(item.status),status=ready?"ready":item.status||"idle";return `<div class="login-card"><header><b>${names[model]}</b><span><i class="dot ${status}"></i>${ready?"已就绪":loginStatus(status)}</span></header><p class="muted">${esc(ready?"已登录并找到输入框":item.message||"尚未检测")}</p><button ${running||!worker.online?"disabled":""} onclick="startLogin('${model}')">${running?"等待登录":ready?"重新检测":"打开登录"}</button></div>`}).join("")}catch(e){$("tasks").innerHTML=`<span class="error">${esc(e.message)}</span>`;$("workers").innerHTML=`<span class="error">${esc(e.message)}</span>`}}
function progressHtml(task){const p=task.model_progress||{};return `<div class="model-grid">${(task.models||[]).map(model=>{const x=p[model]||{completed:0,total:0,completed_rounds:[]};return `<div class="model-progress"><b>${names[model]||esc(model)}</b><br>${x.completed}/${x.total} 轮<br><small class="muted">已采：${esc((x.completed_rounds||[]).join("、")||"无")}</small></div>`}).join("")}</div>`}
function resultHtml(taskId,r){const sourceCount=(r.sources||[]).length,expected=Number(r.expected_source_count||0),analysis=r.analysis||{};const links=(r.sources||[]).map(s=>{const url=safeUrl(s.url||s.href);return url?`<li><a href="${esc(url)}" target="_blank" rel="noopener">${esc(s.title||url)}</a></li>`:`<li>${esc(s.title||"无效链接")}</li>`}).join("");return `<div class="result-card"><div class="row"><b>${names[r.model_id]||esc(r.model_id)} · 第 ${r.round} 轮</b><span class="muted">${esc(r.created_at)}</span></div><p>${esc(r.question)}</p><div class="quality"><span class="${r.body_capture_complete?"good":"bad"}">正文 ${r.body_length} 字 · ${r.body_capture_complete?"完整":"待核查"}</span><span class="${r.source_capture_complete?"good":"bad"}">信源 ${sourceCount}/${expected}</span><span class="${Object.keys(analysis).length?"good":"bad"}">分析 ${Object.keys(analysis).length?"完成":"缺失"}</span><span>${r.recommended?"命中品牌":"未命中品牌"}${r.rank?" · 排名 "+r.rank:""}</span></div><details><summary>回答正文</summary><pre>${esc(r.body||"未采集到正文")}</pre></details><details><summary>全部信源链接</summary>${links?`<ol class="sources">${links}</ol>`:`<p class="muted">本轮没有识别到外部信源</p>`}</details><button class="danger" onclick="deleteResult('${taskId}','${esc(r.request_id)}')">删除这一轮数据</button></div>`}
async function renderDetail(id){const box=$(`detail-${id}`);if(!box)return;box.innerHTML="正在加载逐轮数据…";box.classList.remove("hidden");try{const [d,r]=await Promise.all([api(`${apiRoot}/tasks/${id}`),api(`${apiRoot}/tasks/${id}/results`)]);const events=d.task.events||[];box.innerHTML=`<h3>模型进度</h3>${progressHtml(d.task)}<h3>实时日志</h3><div class="event-log">${events.length?events.map(x=>`<div class="${esc(x.level)}"><time>${esc(x.created_at)}</time>${esc(x.message)}</div>`).join(""):"暂无日志"}</div><h3>每模型每轮采集结果（${r.results.length}）</h3><div class="result-grid">${r.results.length?r.results.map(x=>resultHtml(id,x)).join(""):"尚未采集到正文与信源"}</div>`}catch(e){box.innerHTML=`<span class="error">${esc(e.message)}</span>`}}
function loadDetail(id){const box=$(`detail-${id}`);if(openDetails.has(id)){openDetails.delete(id);box?.classList.add("hidden");return}openDetails.add(id);renderDetail(id)}
async function action(id,name,question){if(question&&!confirm(question))return;try{await api(`${apiRoot}/tasks/${id}/${name}`,{method:"POST",body:"{}"});await load()}catch(e){alert(e.message)}}
const pauseTask=id=>action(id,"pause","暂停后会保留已采轮次，确定暂停？");const resumeTask=id=>action(id,"resume");const cancelTask=id=>action(id,"cancel","停止后任务不会自动继续，确定停止？");const rerunTask=id=>action(id,"rerun","将创建一个从第 1 轮开始的新任务，确定重跑？");const clearTask=id=>action(id,"clear","确定清空该任务全部正文、信源和分析数据？");const deleteTask=id=>action(id,"delete","确定永久删除任务、正文、信源和日志？");
async function deleteResult(taskId,requestId){if(!confirm("确定删除这一轮正文、信源和分析数据？任务会转为暂停，可继续补采这一轮。"))return;try{await api(`${apiRoot}/tasks/${taskId}/results/${encodeURIComponent(requestId)}/delete`,{method:"POST",body:"{}"});openDetails.add(taskId);await load()}catch(e){alert(e.message)}}
async function startLogin(model){try{await api(`${apiRoot}/logins/${model}/start`,{method:"POST",body:"{}"});await load()}catch(e){alert(e.message)}}
async function cleanup(){if(!confirm("永久清理全部已结束和已暂停任务及其数据？"))return;try{const d=await api(`${apiRoot}/tasks/cleanup`,{method:"POST",body:"{}"});alert(`已清理 ${d.deleted||0} 个任务`);load()}catch(e){alert(e.message)}}
$("key").value=localStorage.getItem(keyStore)||sessionStorage.getItem("monitorTaskKey")||"";$("key").oninput=()=>{localStorage.setItem(keyStore,$("key").value);sessionStorage.setItem("monitorTaskKey",$("key").value);load()};
$("create").onclick=async()=>{const models=[...document.querySelectorAll("#models input:checked")].map(x=>x.value),questions=$("questions").value.split(/\r?\n/).map(x=>x.trim()).filter(Boolean);try{const d=await api(`${apiRoot}/tasks`,{method:"POST",body:JSON.stringify({models,questions,rounds:Number($("rounds").value),question_mode:$("mode").value})});$("notice").className="ok";$("notice").textContent=`已创建 ${d.task.id.slice(0,10)}`;load()}catch(e){$("notice").className="error";$("notice").textContent=e.message}};
$("reload").onclick=load;$("cleanup").onclick=cleanup;load();setInterval(load,5000);
</script></body></html>'''
