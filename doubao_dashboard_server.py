import csv
import base64
import binascii
import gzip
import hashlib
import hmac
import html
import importlib
import json
import math
import os
import re
import socket
import secrets
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, parse_qsl, urlencode, urlparse, urlunparse

import doubao_question_aliases as qa
import doubao_brand_settings as brand_settings
from monitor_core.plugins import discover_plugins
from monitor_core import database as monitor_database
from monitor_core.remote_tasks import (
    RemoteTaskQueue,
    configured_token,
    secure_token_matches,
    task_page_html,
)
from monitor_core.http_cache import HTTP_JSON_CACHE
from monitor_core.analytics import (
    _brand_matcher as analytics_brand_matcher,
    _mentions as analytics_brand_mentions,
    BRAND_MATCH_SCHEMA_VERSION,
    BRAND_CANONICAL_GROUPS,
    beijing_day as analytics_beijing_day,
    brand_alias_occurs,
    canonical_question as analytics_canonical_question,
    canonical_brand_name as analytics_canonical_brand_name,
    build_analytics,
    content_index as analytics_content_index,
    daily_owned_product_recommendations,
    owned_brand_vocabulary,
    prepare_analytics,
    valid_brand as analytics_valid_brand,
)
from monitor_core.owned_products import (
    OWN_PRODUCT_RULES,
    OWN_PRODUCT_SCHEMA_VERSION,
    brands_for_products,
    own_product_mentions,
)

# 固定使用中国时区 (UTC+8)，不受系统时区影响
CST = timezone(timedelta(hours=8))


def beijing_now():
    return datetime.now(CST).isoformat(sep=" ", timespec="seconds")


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CSV_PATH = os.path.join(BASE_DIR, "doubao_refs_result.csv")
PRODUCT_CSV_PATH = os.path.join(BASE_DIR, "doubao_products_result.csv")
ANSWER_CSV_PATH = os.path.join(BASE_DIR, "doubao_answers_result.csv")
CAPTURE_SKIP_CSV_PATH = os.path.join(BASE_DIR, "doubao_capture_skips.csv")
AI_CACHE_PATH = os.path.join(BASE_DIR, "doubao_source_ai_cache.json")
META_CACHE_PATH = os.path.join(BASE_DIR, "doubao_source_cache.json")
BRAND_AI_CACHE_PATH = os.path.join(BASE_DIR, "doubao_brand_ai_cache.json")
CONTENT_INDEX_PATH = os.path.join(BASE_DIR, "doubao_source_content_index.json")
VIEW_CACHE_PATH = os.path.join(BASE_DIR, "runtime", "dashboard_view_cache.json")
CONTENT_WORKER_PATH = os.path.join(BASE_DIR, "doubao_source_content_worker.py")
QUESTION_ALIASES_PATH = os.path.join(BASE_DIR, "doubao_question_aliases.py")
BRAND_SETTINGS_PATH = str(brand_settings.SETTINGS_PATH)
DEBUG_LOG_PATH = os.path.join(BASE_DIR, "doubao_run_debug.log")
RAG_ML_LAB_PATH = os.path.join(BASE_DIR, "doubao_rag_ml_lab.html")
CONTROL_RUNTIME_DIR = Path(BASE_DIR) / "runtime" / "unified_control"
HOST = os.environ.get("DOUBAO_DASHBOARD_HOST", "0.0.0.0")
PORT = int(os.environ.get("DOUBAO_DASHBOARD_PORT", "8765"))
REMOTE_TASKS_ENABLED = os.environ.get("MONITOR_REMOTE_TASKS_ENABLED", "").strip() == "1"
REMOTE_TASK_QUEUE = RemoteTaskQueue(
    os.environ.get(
        "MONITOR_REMOTE_TASK_DB",
        str(Path(BASE_DIR) / "runtime" / "remote_tasks.sqlite3"),
    )
)

MODEL_PLUGINS = discover_plugins()
MODEL_REGISTRY = {model_id: plugin.metadata() for model_id, plugin in MODEL_PLUGINS.items()}
RESERVED_MODEL_SLOTS = 1

_CONTROL_LOCK = threading.Lock()
_CONTROL_PROCESSES = {}
_DIAGNOSIS_CREATE_LOCK = threading.Lock()
_DIAGNOSIS_LAST_CREATED = {}
_ADMIN_LOGIN_LOCK = threading.Lock()
_ADMIN_LOGIN_ATTEMPTS = {}
_ANALYTICS_LOCK = threading.RLock()
_ANALYTICS_SNAPSHOT_LOCK = threading.Lock()
_ANALYTICS_CACHE = {}
_ANALYTICS_BUILDING = set()
_ANALYTICS_KEY_LOCKS = defaultdict(threading.Lock)
_ANALYTICS_SNAPSHOT = None
_ANALYTICS_CACHE_SCHEMA_VERSION = 33
_DATABASE_VERSION_CACHE = [0.0, 0]
_ANALYTICS_LAST_VALIDATED = {}
_ANALYTICS_VALIDATION_INTERVAL = 15.0
_ANALYTICS_BUILD_SEMAPHORE = threading.Semaphore(1)
_ANALYTICS_CACHE_MAX = max(
    4, min(64, int(os.environ.get("MONITOR_ANALYTICS_MEMORY_CACHE_MAX", "48") or 48))
)
_RETAIN_ANALYTICS_SNAPSHOT = (
    os.environ.get("MONITOR_RETAIN_GLOBAL_SNAPSHOT", "1").strip() != "0"
)
_OWNED_BOARD_CACHE = {}
_OWNED_BOARD_LOCK = threading.Lock()
_OWNED_BOARD_BUILDING = set()
_OWNED_BOARD_LAST_VALIDATED = {}
_OWNED_BOARD_VALIDATION_INTERVAL = 5.0
_SOURCE_INTERSECTION_CACHE = {}
_SOURCE_INTERSECTION_LOCKS = defaultdict(threading.Lock)
_SOURCE_INTERSECTION_BUILDING = set()
_SOURCE_INTERSECTION_LAST_VALIDATED = {}
_SOURCE_INTERSECTION_LOCK = threading.Lock()


def _process_alive(process):
    return process is not None and process.poll() is None


def _latest_control_log_message(path, offset=0):
    try:
        with Path(path).open("rb") as handle:
            handle.seek(max(0, int(offset or 0)))
            lines = handle.read().decode("utf-8", errors="replace").splitlines()
    except (OSError, TypeError):
        return ""
    for line in reversed(lines[-80:]):
        text = str(line or "").strip()
        if not text:
            continue
        text = re.sub(r"^\d{4}-\d{2}-\d{2}[^[]*\[(?:INFO|WARNING|ERROR|DEBUG)\]\s*", "", text)
        return text[:240]
    return ""


def _control_status():
    result = {}
    with _CONTROL_LOCK:
        for model, definition in MODEL_REGISTRY.items():
            item = _CONTROL_PROCESSES.get(model) or {}
            process = item.get("process")
            running = _process_alive(process)
            stopped_by_user = bool(item.get("stopped_by_user"))
            if running:
                phase = _latest_control_log_message(item.get("log"), item.get("log_offset")) or str(item.get("phase") or "运行中")
            elif item.get("starting") or item.get("startup_error"):
                phase = str(item.get("phase") or "")
            elif stopped_by_user:
                phase = "已手动停止"
            elif process is not None and process.returncode == 0:
                phase = "任务已完成"
            elif process is not None:
                phase = f"运行异常退出（退出码 {process.returncode}）"
            else:
                phase = str(item.get("phase") or "")
            result[model] = {
                "running": running,
                "starting": bool(item.get("starting")),
                "phase": phase,
                "startup_error": str(item.get("startup_error") or ""),
                "pid": process.pid if running else None,
                "started_at": item.get("started_at", ""),
                "last_exit_code": None if running or process is None or stopped_by_user else process.returncode,
                "log": str(item.get("log", "")),
                "ready": MODEL_PLUGINS[model].ready(),
                "name": definition["name"],
            }
    return {"ok": True, "generated_at": beijing_now(), "models": result}


def _launch_controlled_job(model, options, log_path):
    plugin = MODEL_PLUGINS[model]

    def progress(message):
        with _CONTROL_LOCK:
            item = _CONTROL_PROCESSES.get(model)
            if item and not item.get("cancel_requested"):
                item["phase"] = str(message)

    try:
        plugin.prepare(options, progress)
        with _CONTROL_LOCK:
            item = _CONTROL_PROCESSES.get(model) or {}
            if item.get("cancel_requested"):
                item.update({"starting": False, "phase": "启动已取消"})
                return
        command, cwd = plugin.command(options)
        env = os.environ.copy()
        env.update({"PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"})
        log_handle = open(log_path, "a", encoding="utf-8")
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            process = subprocess.Popen(
                command, cwd=str(cwd), stdin=subprocess.DEVNULL,
                stdout=log_handle, stderr=subprocess.STDOUT, env=env,
                creationflags=creationflags,
            )
        finally:
            log_handle.close()
        with _CONTROL_LOCK:
            item = _CONTROL_PROCESSES.get(model) or {}
            if item.get("cancel_requested"):
                process.terminate()
                item.update({"process": process, "starting": False, "phase": "启动已取消"})
            else:
                item.update({"process": process, "starting": False, "phase": "运行中",
                             "started_at": beijing_now(), "startup_error": ""})
            _CONTROL_PROCESSES[model] = item
    except Exception as exc:
        with open(log_path, "a", encoding="utf-8") as handle:
            handle.write(f"{beijing_now()} 启动失败：{type(exc).__name__}: {exc}\n")
        with _CONTROL_LOCK:
            item = _CONTROL_PROCESSES.get(model) or {}
            item.update({"starting": False, "phase": "启动失败", "startup_error": str(exc)})
            _CONTROL_PROCESSES[model] = item


def _start_controlled_job(model, options=None):
    options = options if isinstance(options, dict) else {}
    if model not in MODEL_REGISTRY or not MODEL_REGISTRY[model]["supports_control"]:
        raise ValueError("未知模型")
    with _CONTROL_LOCK:
        existing = _CONTROL_PROCESSES.get(model) or {}
        current = existing.get("process")
        if _process_alive(current):
            return {
                "running": True, "pid": current.pid,
                "started_at": existing.get("started_at", ""), "log": str(existing.get("log", "")),
            }
        if existing.get("starting"):
            return {"running": False, "starting": True, "phase": existing.get("phase", "正在启动"),
                    "started_at": existing.get("started_at", ""), "log": str(existing.get("log", ""))}
        CONTROL_RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        log_path = CONTROL_RUNTIME_DIR / f"{model}.log"
        try:
            log_offset = log_path.stat().st_size
        except OSError:
            log_offset = 0
        plugin = MODEL_PLUGINS[model]
        if not plugin.ready():
            raise FileNotFoundError(f"{plugin.name} 运行配置不存在。")
        _CONTROL_PROCESSES[model] = {
            "process": None, "starting": True, "phase": "启动请求已接收",
            "startup_error": "", "started_at": beijing_now(), "log": log_path,
            "stopped_by_user": False, "log_offset": log_offset,
        }
        threading.Thread(target=_launch_controlled_job, args=(model, dict(options), log_path),
                         name=f"{model}-control-launcher", daemon=True).start()
        return {
            "running": False, "starting": True, "phase": "启动请求已接收",
            "started_at": _CONTROL_PROCESSES[model]["started_at"], "log": str(log_path),
        }


def _stop_controlled_job(model):
    with _CONTROL_LOCK:
        item = _CONTROL_PROCESSES.get(model) or {}
        process = item.get("process")
        if item.get("starting"):
            item.update({"cancel_requested": True, "starting": False, "phase": "正在取消启动",
                         "stopped_by_user": True})
            return {"running": False, "starting": False, "message": "启动已取消。"}
        if not _process_alive(process):
            return {"running": False, "message": "当前没有由统一面板启动的任务。"}
        item.update({"stopped_by_user": True, "phase": "正在停止"})
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        else:
            process.terminate()
        try:
            process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            process.kill()
        return {"running": False, "message": "任务已停止，已经保存的轮次不会丢失。"}


def _yuanbao_stats():
    return MODEL_PLUGINS["yuanbao"].stats()


def _analytics_data_version(cache_key=None):
    if monitor_database.enabled():
        try:
            if cache_key is not None:
                scope, scoped_version = monitor_database.scope_version(cache_key)
                if scoped_version:
                    return (("postgresql", scope, scoped_version, _analytics_content_token()),)
            now = time.monotonic()
            if now - _DATABASE_VERSION_CACHE[0] >= 1.0:
                _DATABASE_VERSION_CACHE[:] = [now, monitor_database.global_version()]
            database_version = _DATABASE_VERSION_CACHE[1]
            if database_version:
                return (("postgresql", database_version, _analytics_content_token()),)
        except Exception:
            pass
    paths = [Path(CSV_PATH), Path(ANSWER_CSV_PATH), Path(PRODUCT_CSV_PATH),
             Path(CONTENT_INDEX_PATH), Path(BRAND_SETTINGS_PATH)]
    for plugin in MODEL_PLUGINS.values():
        for attribute in ("results", "collector_results", "dashboard"):
            path = getattr(plugin, attribute, None)
            if path:
                paths.append(Path(path))
    version = []
    for path in paths:
        try:
            stat = path.stat()
            version.append((str(path), stat.st_mtime_ns, stat.st_size))
        except OSError:
            version.append((str(path), 0, 0))
    return tuple(version)


def _analytics_content_token():
    version = [_ANALYTICS_CACHE_SCHEMA_VERSION, BRAND_MATCH_SCHEMA_VERSION,
               OWN_PRODUCT_SCHEMA_VERSION]
    # The content worker atomically republishes a multi-megabyte index every
    # batch. Invalidating every filter cache on every write kept the dashboard
    # in a permanent rebuild loop. Content enrichment may lag by at most this
    # small window; newly ingested runs still use their scoped DB versions.
    content_epoch_seconds = max(
        60, int(os.environ.get("MONITOR_ANALYTICS_CONTENT_EPOCH_SECONDS", "300") or 300)
    )
    try:
        stat = Path(CONTENT_INDEX_PATH).stat()
        version.append(stat.st_mtime_ns // (content_epoch_seconds * 1_000_000_000))
    except OSError:
        version.append(0)
    try:
        stat = Path(BRAND_SETTINGS_PATH).stat()
        version.extend((stat.st_mtime_ns, stat.st_size))
    except OSError:
        version.extend((0, 0))
    return hashlib.sha256(json.dumps(version).encode("utf-8")).hexdigest()[:24]


def _tail_result_days(path, question_filter="", max_bytes=262144):
    """Read recent JSONL dates without loading a multi-megabyte result file."""
    try:
        with Path(path).open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - max_bytes))
            raw = handle.read()
    except OSError:
        return set()
    lines = raw.decode("utf-8", errors="replace").splitlines()
    if size > max_bytes and lines:
        lines = lines[1:]
    days = set()
    canonical_filter = analytics_canonical_question(question_filter) if question_filter else ""
    for line in lines[-400:]:
        try:
            value = json.loads(line)
        except (ValueError, json.JSONDecodeError):
            continue
        if canonical_filter and analytics_canonical_question(
            value.get("question") or value.get("prompt") or ""
        ) != canonical_filter:
            continue
        day = str(value.get("day") or analytics_beijing_day(
            value.get("finished_at") or value.get("captured_at") or value.get("run_time") or ""
        ))
        if day:
            days.add(day)
    return days


def _cache_missing_available_day(result, model_filter="", question_filter=""):
    cached_days = set(result.get("dates") or [])
    model_ids = (model_filter,) if model_filter in MODEL_PLUGINS else tuple(MODEL_PLUGINS)
    available_days = set()
    for model_id in model_ids:
        path = getattr(MODEL_PLUGINS[model_id], "results", None)
        if path:
            available_days.update(_tail_result_days(path, question_filter))
    return bool(available_days - cached_days)


def _analytics_snapshot(version):
    global _ANALYTICS_SNAPSHOT
    # The snapshot contains every model, question and date. Its identity must
    # therefore use the global data version, not the active filter's scoped
    # version. Using a day/question scope here caused a full PostgreSQL reload
    # and re-enrichment of every archived source whenever the user switched a
    # filter, even when the underlying data had not changed.
    snapshot_version = _analytics_data_version()
    with _ANALYTICS_SNAPSHOT_LOCK:
        if (
            _RETAIN_ANALYTICS_SNAPSHOT
            and _ANALYTICS_SNAPSHOT
            and _ANALYTICS_SNAPSHOT[0] == snapshot_version
        ):
            return _ANALYTICS_SNAPSHOT
        runs_by_model = None
        if monitor_database.enabled():
            try:
                database_runs = monitor_database.load_runs_by_model()
                if database_runs:
                    runs_by_model = {
                        model_id: database_runs.get(model_id, [])
                        for model_id in MODEL_PLUGINS
                    }
            except Exception:
                runs_by_model = None
        if runs_by_model is None:
            runs_by_model = {
                model_id: plugin.analytics_runs()
                for model_id, plugin in MODEL_PLUGINS.items()
            }
        prepared = prepare_analytics(runs_by_model)
        snapshot = (snapshot_version, runs_by_model, prepared)
        # PostgreSQL and the persisted payload cache already retain completed
        # analytics. Keeping the full all-history source corpus in Python as
        # well costs hundreds of megabytes on production datasets.
        _ANALYTICS_SNAPSHOT = snapshot if _RETAIN_ANALYTICS_SNAPSHOT else None
        return snapshot


def _remember_analytics_cache_locked(cache_key, value):
    """Bound large JSON-ready analytics payloads kept in process memory."""
    _ANALYTICS_CACHE.pop(cache_key, None)
    _ANALYTICS_CACHE[cache_key] = value
    while len(_ANALYTICS_CACHE) > _ANALYTICS_CACHE_MAX:
        oldest_key = next(iter(_ANALYTICS_CACHE))
        _ANALYTICS_CACHE.pop(oldest_key, None)
        _ANALYTICS_LAST_VALIDATED.pop(oldest_key, None)
        _ANALYTICS_KEY_LOCKS.pop(oldest_key, None)


def _uses_scoped_database_load(cache_key):
    return bool(
        monitor_database.enabled()
        and cache_key[3] in {"overview", "compare", "sources", "brands", "brand-trends", "runs"}
    )


def _build_unified_analytics(cache_key, version):
    # Date-scoped dashboard views must not materialize every archived run and
    # all 400k+ source rows.  Cross-model intersections still need every model,
    # but only for the selected day/question.  Selector options come from two
    # indexed DISTINCT queries instead of the heavyweight snapshot.
    if _uses_scoped_database_load(cache_key):
        filter_options = monitor_database.analytics_filter_options(
            model=cache_key[2], question=cache_key[0],
        )
        load_scope = {"day": cache_key[1]} if cache_key[1] else {}
        source_summary = None
        detail_scope = None
        if not cache_key[1]:
            # All-date views use exact database totals for their KPI cards and
            # a bounded detail window appropriate to the visible component.
            # This prevents every navigation click from materializing the full
            # historical source corpus in Python.
            detail_limits = {
                "overview": 7, "brands": 2, "runs": 1,
                "compare": 1, "sources": 1,
            }
            detail_days = list(filter_options.get("dates") or [])[
                :detail_limits.get(cache_key[3], 1)
            ]
            if detail_days:
                load_scope = {
                    "day_from": min(detail_days),
                    "day_to": max(detail_days),
                }
            source_summary = monitor_database.analytics_source_scope_summary(
                question=cache_key[0], model=cache_key[2],
            )
            if cache_key[3] == "sources":
                detail_scope = {
                    "kind": "latest_day",
                    "dates": detail_days,
                    "date_from": min(detail_days) if detail_days else "",
                    "date_to": max(detail_days) if detail_days else "",
                }
        if cache_key[3] in {"brands", "brand-trends"} and cache_key[1]:
            # The cross-model brand overview needs to paint quickly. Loading
            # four models across the whole trend window makes the first request
            # contend with the per-model source drill-downs. Keep the combined
            # overview on the selected day; a selected model still gets the
            # complete 14-day trend window.
            selected_day = datetime.strptime(cache_key[1], "%Y-%m-%d").date()
            history_days = 13 if cache_key[3] == "brand-trends" and cache_key[2] else 0
            load_scope = {
                "day_from": (selected_day - timedelta(days=history_days)).isoformat(),
                "day_to": selected_day.isoformat(),
            }
        # Cross-model source intersections need all three models. Every other
        # single-model view should only materialize the selected model.
        load_model = cache_key[2] if cache_key[2] and cache_key[3] != "sources" else ""
        load_options = {**load_scope, "question": cache_key[0]}
        if load_model:
            load_options["model"] = load_model
        runs_by_model = monitor_database.load_runs_by_model(**load_options)
        runs_by_model = {
            model_id: runs_by_model.get(model_id, [])
            for model_id in MODEL_PLUGINS
        }
        prepared = prepare_analytics(
            runs_by_model,
            enrich_sources=cache_key[3] in {"sources", "brands", "brand-trends"},
        )
        return _build_unified_analytics_from_snapshot(
            cache_key, (version, runs_by_model, prepared),
            discard_building=True, cache_version=version,
            filter_options=filter_options,
            model_summary=source_summary,
            detail_scope=detail_scope,
        )
    snapshot_version, runs_by_model, prepared = _analytics_snapshot(version)
    return _build_unified_analytics_from_snapshot(
        cache_key, (snapshot_version, runs_by_model, prepared),
        discard_building=True, cache_version=version,
    )


def _build_unified_analytics_from_snapshot(
    cache_key, snapshot, *, discard_building, cache_version=None,
    filter_options=None, model_summary=None, detail_scope=None,
):
    snapshot_version, runs_by_model, prepared = snapshot
    result_version = cache_version if cache_version is not None else snapshot_version
    result = build_analytics(
        MODEL_REGISTRY,
        runs_by_model,
        question=cache_key[0], date=cache_key[1], model=cache_key[2],
        view=cache_key[3],
        prepared=prepared,
    )
    if filter_options:
        result["questions"] = list(filter_options.get("questions") or [])
        result["dates"] = list(filter_options.get("dates") or [])
    if model_summary is not None:
        for model_row in result.get("models") or []:
            summary = model_summary.get(str(model_row.get("id") or ""), {})
            for field, value in summary.items():
                model_row[field] = int(value or 0)
    if detail_scope is not None:
        result["detail_scope"] = detail_scope
    with _ANALYTICS_LOCK:
        _remember_analytics_cache_locked(cache_key, (result_version, result))
        if discard_building:
            _ANALYTICS_BUILDING.discard(cache_key)
    _persist_unified_analytics(cache_key, result)
    try:
        expected_scope_version = None
        if (
            len(result_version) == 1
            and len(result_version[0]) >= 4
            and result_version[0][0] == "postgresql"
        ):
            expected_scope_version = int(result_version[0][2])
        monitor_database.cache_put(
            cache_key, _analytics_content_token(), result,
            expected_scope_version=expected_scope_version,
        )
    except Exception:
        pass
    return result


def _unified_analytics_cache_path(cache_key):
    payload = json.dumps(
        (_ANALYTICS_CACHE_SCHEMA_VERSION, cache_key),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]
    return Path(BASE_DIR) / "runtime" / "unified_analytics_cache" / f"{digest}.json"


def _persist_unified_analytics(cache_key, result):
    path = _unified_analytics_cache_path(cache_key)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(result, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        temporary.replace(path)
    except OSError:
        pass


def _load_persisted_unified_analytics(cache_key):
    try:
        value = json.loads(_unified_analytics_cache_path(cache_key).read_text(encoding="utf-8"))
        return value if isinstance(value, dict) and isinstance(value.get("models"), list) else None
    except (OSError, ValueError, json.JSONDecodeError):
        return None


def _refresh_unified_analytics(cache_key, version):
    try:
        with _ANALYTICS_BUILD_SEMAPHORE:
            _build_unified_analytics(cache_key, version)
    except Exception:
        with _ANALYTICS_LOCK:
            _ANALYTICS_BUILDING.discard(cache_key)


def _build_interactive_unified_analytics(cache_key, version):
    """Prioritize small indexed UI scopes over serialized background warmup."""
    if _uses_scoped_database_load(cache_key):
        return _build_unified_analytics(cache_key, version)
    with _ANALYTICS_BUILD_SEMAPHORE:
        return _build_unified_analytics(cache_key, version)


def _validate_and_refresh_unified_analytics(cache_key):
    """Validate stale-while-revalidate caches without blocking a UI request."""
    try:
        version = _analytics_data_version(cache_key)
        with _ANALYTICS_LOCK:
            cached = _ANALYTICS_CACHE.get(cache_key)
            if cached and cached[0] == version:
                _ANALYTICS_BUILDING.discard(cache_key)
                _ANALYTICS_LAST_VALIDATED[cache_key] = time.monotonic()
                return
        _refresh_unified_analytics(cache_key, version)
    except Exception:
        with _ANALYTICS_LOCK:
            _ANALYTICS_BUILDING.discard(cache_key)


def _schedule_analytics_validation(cache_key):
    now = time.monotonic()
    selected_day = cache_key[1]
    today = datetime.now(CST).date().isoformat()
    validation_interval = (
        300.0 if cache_key[3] == "brand-trends" else
        _ANALYTICS_VALIDATION_INTERVAL if selected_day == today else
        300.0
    )
    with _ANALYTICS_LOCK:
        if cache_key in _ANALYTICS_BUILDING:
            return
        if now - _ANALYTICS_LAST_VALIDATED.get(cache_key, 0.0) < validation_interval:
            return
        _ANALYTICS_LAST_VALIDATED[cache_key] = now
        _ANALYTICS_BUILDING.add(cache_key)
    threading.Thread(
        target=_validate_and_refresh_unified_analytics,
        args=(cache_key,),
        name="unified-analytics-validate",
        daemon=True,
    ).start()


def _unified_analytics_without_coalescing(params):
    cache_key = (
        (params.get("question") or [""])[0],
        (params.get("date") or [""])[0],
        (params.get("model") or [""])[0],
        (params.get("view") or [""])[0],
    )
    # PostgreSQL scope versions are authoritative. Never serve an unversioned
    # disk snapshot first: ingest-only models (notably Quark) may have no JSONL
    # tail for `_cache_missing_available_day` to inspect, which previously made
    # “全部日期” smaller than a single day until a background refresh finished.
    if monitor_database.enabled():
        content_token = _analytics_content_token()
        # A completed date-scoped payload is safe to paint immediately after
        # a service restart. PostgreSQL remains authoritative: validation and
        # replacement happen in the background, while the user avoids paying
        # the 20-40 second cold aggregation cost on every restart or view
        # switch. All-date payloads keep the stricter database-first path.
        persisted = _load_persisted_unified_analytics(cache_key)
        if (
            (cache_key[1] or cache_key[3] == "sources")
            and persisted is not None
            and str((persisted.get("filters") or {}).get("date") or "") == cache_key[1]
            and str((persisted.get("filters") or {}).get("question") or "") == cache_key[0]
            and str((persisted.get("filters") or {}).get("model") or "") == cache_key[2]
            and str((persisted.get("filters") or {}).get("view") or "") == cache_key[3]
            and not _cache_missing_available_day(
                persisted, cache_key[2], cache_key[0]
            )
        ):
            with _ANALYTICS_LOCK:
                _remember_analytics_cache_locked(cache_key, ((), persisted))
            _schedule_analytics_validation(cache_key)
            return persisted
        try:
            database_cached = monitor_database.cache_get(cache_key, content_token)
            if isinstance(database_cached, dict) and isinstance(database_cached.get("models"), list):
                version = _analytics_data_version(cache_key)
                with _ANALYTICS_LOCK:
                    _remember_analytics_cache_locked(cache_key, (version, database_cached))
                    _ANALYTICS_LAST_VALIDATED[cache_key] = time.monotonic()
                    _ANALYTICS_BUILDING.discard(cache_key)
                return database_cached
        except Exception:
            pass
        version = _analytics_data_version(cache_key)
        with _ANALYTICS_LOCK:
            cached = _ANALYTICS_CACHE.get(cache_key)
            if cached and cached[0] == version:
                return cached[1]
            _ANALYTICS_BUILDING.add(cache_key)
        return _build_interactive_unified_analytics(cache_key, version)

    # Disk cache is local and cheap. Return it before touching PostgreSQL, then
    # validate in the background. This keeps a service restart or a busy DB
    # from turning an already-viewed filter into a cold request.
    persisted = _load_persisted_unified_analytics(cache_key)
    if persisted is not None and not _cache_missing_available_day(
        persisted, cache_key[2], cache_key[0]
    ):
        with _ANALYTICS_LOCK:
            _remember_analytics_cache_locked(cache_key, ((), persisted))
        _schedule_analytics_validation(cache_key)
        return persisted
    content_token = _analytics_content_token()
    try:
        database_cached = monitor_database.cache_get(cache_key, content_token)
        if isinstance(database_cached, dict) and isinstance(database_cached.get("models"), list):
            version = _analytics_data_version(cache_key)
            with _ANALYTICS_LOCK:
                _remember_analytics_cache_locked(cache_key, (version, database_cached))
                _ANALYTICS_LAST_VALIDATED[cache_key] = time.monotonic()
            return database_cached
    except Exception:
        pass
    version = _analytics_data_version(cache_key)
    build_now = False
    stale_snapshot = None
    with _ANALYTICS_LOCK:
        cached = _ANALYTICS_CACHE.get(cache_key)
        if cached and cached[0] == version:
            return cached[1]
        if cached:
            if _cache_missing_available_day(cached[1], cache_key[2], cache_key[0]):
                _ANALYTICS_BUILDING.add(cache_key)
                build_now = True
            else:
                if cache_key not in _ANALYTICS_BUILDING:
                    _ANALYTICS_BUILDING.add(cache_key)
                    threading.Thread(
                        target=_refresh_unified_analytics,
                        args=(cache_key, version),
                        name="unified-analytics-refresh",
                        daemon=True,
                    ).start()
                return cached[1]
        if build_now:
            pass
        else:
            persisted = persisted or _load_persisted_unified_analytics(cache_key)
            if persisted is not None:
                _remember_analytics_cache_locked(cache_key, ((), persisted))
                _ANALYTICS_BUILDING.add(cache_key)
                if _cache_missing_available_day(persisted, cache_key[2], cache_key[0]):
                    build_now = True
                else:
                    threading.Thread(
                        target=_refresh_unified_analytics,
                        args=(cache_key, version),
                        name="unified-analytics-refresh",
                        daemon=True,
                    ).start()
                    return persisted
            elif _ANALYTICS_SNAPSHOT is not None and not _uses_scoped_database_load(cache_key):
                _ANALYTICS_BUILDING.add(cache_key)
                stale_snapshot = _ANALYTICS_SNAPSHOT
            else:
                _ANALYTICS_BUILDING.add(cache_key)
                build_now = True
    if stale_snapshot is not None:
        result = _build_unified_analytics_from_snapshot(
            cache_key, stale_snapshot, discard_building=False
        )
        threading.Thread(
            target=_refresh_unified_analytics,
            args=(cache_key, version),
            name="unified-analytics-refresh",
            daemon=True,
        ).start()
        return result
    if build_now:
        return _build_interactive_unified_analytics(cache_key, version)
    raise RuntimeError("analytics build was not scheduled")


def _unified_analytics(params):
    cache_key = (
        (params.get("question") or [""])[0],
        (params.get("date") or [""])[0],
        (params.get("model") or [""])[0],
        (params.get("view") or [""])[0],
    )
    # A populated in-memory cache is always served immediately. Version checks
    # and refreshes happen out of band, so model/question switching never waits
    # behind PostgreSQL or a content-index rebuild.
    with _ANALYTICS_LOCK:
        cached = _ANALYTICS_CACHE.get(cache_key)
    # Never make a browser request wait for a live aggregation. Collector
    # callbacks can arrive every few seconds; synchronously rebuilding the
    # current day caused aborted browser requests to pile up as server threads.
    # Serve the latest complete snapshot immediately and coalesce one refresh
    # in the background. The next poll receives the refreshed counters.
    if cached and not _cache_missing_available_day(cached[1], cache_key[2], cache_key[0]):
        if monitor_database.enabled():
            try:
                if cached[0] == _analytics_data_version(cache_key):
                    return cached[1]
            except Exception:
                pass
        # Live collectors continuously advance PostgreSQL scope versions. The
        # visible request must still receive the latest completed payload;
        # validate and replace it in the background instead of synchronously
        # rebuilding the brand view on every five-second poll.
        _schedule_analytics_validation(cache_key)
        return cached[1]
    # Collapse a burst of identical cold requests into one aggregation. Requests
    # for different dates/models still execute in parallel.
    with _ANALYTICS_KEY_LOCKS[cache_key]:
        return _unified_analytics_without_coalescing(params)


def _source_intersection_cache_path(question, date):
    token = json.dumps(
        (_ANALYTICS_CACHE_SCHEMA_VERSION, "full-source-intersections", question, date),
        ensure_ascii=False, separators=(",", ":"),
    )
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()[:24]
    return Path(BASE_DIR) / "runtime" / "source_intersection_cache" / f"{digest}.json"


def _source_intersection_version(question, date):
    return (
        _analytics_data_version((question, date, "", "sources")),
        _analytics_content_token(),
    )


def _finalize_intersection_rows(grouped, exact_models, *, competitor=False):
    rows = []
    for row in grouped.values():
        matched = [model_id for model_id, count in row["model_counts"].items() if count]
        if len(matched) != exact_models:
            continue
        item = dict(row)
        item["matched_models"] = matched
        item["total_count"] = sum(item["model_counts"].values())
        if competitor:
            item["competitor_brands"] = sorted(item.get("competitor_brands") or [])
            if not item.get("competitor_brand"):
                item["competitor_brand"] = "、".join(item["competitor_brands"])
        else:
            item["owned_brands"] = sorted(item.get("owned_brands") or [])
            item["own_products"] = sorted(item.get("own_products") or [])
        rows.append(item)
    return sorted(
        rows,
        key=lambda item: (
            str(item.get("date") or ""), str(item.get("question") or ""),
            int(item.get("total_count") or 0), str(item.get("canonical_url") or ""),
        ),
        reverse=True,
    )


def _build_source_intersection_payload(question="", date=""):
    """Build full-filter intersections from unique URLs plus SQL citation counts.

    Source body/title classification runs once per canonical URL, while
    PostgreSQL counts citations per model/run. This preserves the exact
    all-date semantics without loading every source payload into Python.
    """
    catalog = monitor_database.source_intersection_catalog(question=question, day=date)
    index = analytics_content_index()
    eligible_competitors = {
        analytics_canonical_brand_name(item)
        for item in monitor_database.source_intersection_product_brands(
            question=question, day=date,
        )
        if analytics_canonical_brand_name(item)
    }
    owned_vocab = []
    brand_aliases = {brand: {brand} for brand in eligible_competitors}
    for item in owned_brand_vocabulary():
        name = analytics_canonical_brand_name(item.get("name") or "")
        if name:
            aliases = set(item.get("aliases") or []) | {name}
            owned_vocab.append((name, aliases))
            brand_aliases.setdefault(name, {name}).update(aliases)
    for item in brand_settings.vocabulary():
        name = analytics_canonical_brand_name(item.get("name") or "")
        if name in brand_aliases:
            brand_aliases[name].update(item.get("aliases") or [])
    for name, aliases in BRAND_CANONICAL_GROUPS.items():
        canonical = analytics_canonical_brand_name(name)
        if canonical in brand_aliases:
            brand_aliases[canonical].update(aliases)
    brand_matcher = analytics_brand_matcher(brand_aliases)
    labels = {}
    for raw in catalog:
        source = dict(raw)
        canonical_url = str(source.get("canonical_url") or "").strip()
        url = str(source.get("url") or canonical_url).strip()
        title = str(source.get("title") or "")
        is_article = "视频" not in str(source.get("source_type") or "")
        entry = (index.get(url) or index.get(canonical_url) or {}) if is_article else {}
        body_ready = bool(
            entry.get("status") == "ok"
            and entry.get("extraction_quality") in {"high", "medium"}
        )
        title_products = set(own_product_mentions(title))
        body_products = set(entry.get("own_product_mentions") or []) if body_ready else set()
        products = title_products | body_products
        title_brands = set(analytics_brand_mentions(title, brand_matcher))
        owned_names = {name for name, _aliases in owned_vocab}
        owned_brands = title_brands & owned_names
        if is_article:
            owned_brands.update(
                canonical
                for value in entry.get("owned_brand_mentions") or []
                if (canonical := analytics_canonical_brand_name(value))
            )
        owned_brands.update(
            analytics_canonical_brand_name(value)
            for value in brands_for_products(products)
            if analytics_canonical_brand_name(value)
        )
        competitor_brands = set()
        if body_ready:
            body_brands = {
                canonical
                for value in (
                    list(entry.get("brand_mentions") or [])
                    + list(entry.get("owned_brand_mentions") or [])
                )
                if (canonical := analytics_canonical_brand_name(value))
            }
            if not body_brands and entry.get("excerpt"):
                body_brands.update(
                    analytics_brand_mentions(str(entry.get("excerpt") or ""), brand_matcher)
                )
            owned_brands.update(body_brands & owned_names)
            competitor_brands = {
                brand for brand in body_brands
                if brand in eligible_competitors and brand not in owned_brands
            }
        if owned_brands or products or competitor_brands:
            labels[canonical_url] = {
                "owned_brands": owned_brands,
                "own_products": products,
                "competitor_brands": competitor_brands,
            }
    citations = monitor_database.source_intersection_citations(
        labels, question=question, day=date,
    )
    owned_grouped = {}
    competitor_grouped = {}
    all_competitor_grouped = {}
    model_ids = ("doubao", "yuanbao", "wenxin", "deepseek", "quark")
    for raw in citations:
        row = dict(raw)
        url = str(row.get("canonical_url") or "")
        label = labels.get(url) or {}
        base = {
            "date": str(row.get("date") or ""),
            "question": str(row.get("question") or ""),
            "title": str(row.get("title") or url),
            "url": str(row.get("url") or url),
            "canonical_url": url,
            "media": str(row.get("media") or ""),
            "type": str(row.get("source_type") or "文章"),
            "model_counts": {item: 0 for item in model_ids},
        }
        model_id = str(row.get("model_id") or "")
        count = int(row.get("citation_count") or 0)
        if label.get("owned_brands") or label.get("own_products"):
            key = (base["date"], base["question"], url)
            target = owned_grouped.setdefault(key, {
                **base, "owned_brands": set(), "own_products": set(),
            })
            target["model_counts"][model_id] += count
            target["owned_brands"].update(label.get("owned_brands") or [])
            target["own_products"].update(label.get("own_products") or [])
        competitors = set(label.get("competitor_brands") or [])
        if competitors:
            all_key = (base["date"], base["question"], url)
            all_target = all_competitor_grouped.setdefault(all_key, {
                **base, "competitor_brands": set(), "body_match_scope": "正文",
            })
            all_target["model_counts"][model_id] += count
            all_target["competitor_brands"].update(competitors)
            for brand in competitors:
                key = (base["date"], base["question"], brand, url)
                target = competitor_grouped.setdefault(key, {
                    **base, "competitor_brand": brand, "competitor_brands": {brand},
                    "body_match_scope": "正文",
                })
                target["model_counts"][model_id] += count
    payload = {
        "generated_at": datetime.now(CST).isoformat(timespec="seconds"),
        "filters": {"question": question, "date": date, "view": "source-intersections"},
        "competitor_brands": sorted({
            brand for label in labels.values()
            for brand in label.get("competitor_brands") or []
        }),
        "common_owned_sources": _finalize_intersection_rows(owned_grouped, len(model_ids)),
        "two_model_owned_sources": _finalize_intersection_rows(owned_grouped, 2),
        "common_competitor_sources": _finalize_intersection_rows(competitor_grouped, len(model_ids), competitor=True),
        "two_model_competitor_sources": _finalize_intersection_rows(competitor_grouped, 2, competitor=True),
        "common_all_competitor_sources": _finalize_intersection_rows(all_competitor_grouped, len(model_ids), competitor=True),
        "two_model_all_competitor_sources": _finalize_intersection_rows(all_competitor_grouped, 2, competitor=True),
    }
    return payload


def _persist_source_intersections(question, date, payload):
    path = _source_intersection_cache_path(question, date)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        temporary.replace(path)
    except OSError:
        pass


def _refresh_source_intersections(question, date, version):
    key = (question, date)
    try:
        payload = _build_source_intersection_payload(question, date)
        with _SOURCE_INTERSECTION_LOCK:
            _SOURCE_INTERSECTION_CACHE[key] = (version, payload)
            _SOURCE_INTERSECTION_LAST_VALIDATED[key] = time.monotonic()
        _persist_source_intersections(question, date, payload)
    finally:
        with _SOURCE_INTERSECTION_LOCK:
            _SOURCE_INTERSECTION_BUILDING.discard(key)


def _source_intersection_payload(params):
    question = str((params.get("question") or [""])[0]).strip()
    date = str((params.get("date") or [""])[0]).strip()
    key = (question, date)
    version = _source_intersection_version(question, date)
    with _SOURCE_INTERSECTION_LOCK:
        cached = _SOURCE_INTERSECTION_CACHE.get(key)
    if cached and cached[0] == version:
        return cached[1]
    if cached:
        with _SOURCE_INTERSECTION_LOCK:
            due = time.monotonic() - _SOURCE_INTERSECTION_LAST_VALIDATED.get(key, 0.0) >= 300.0
            if due and key not in _SOURCE_INTERSECTION_BUILDING:
                _SOURCE_INTERSECTION_BUILDING.add(key)
                _SOURCE_INTERSECTION_LAST_VALIDATED[key] = time.monotonic()
                threading.Thread(
                    target=_refresh_source_intersections,
                    args=(question, date, version), daemon=True,
                    name="source-intersections-refresh",
                ).start()
        return cached[1]
    path = _source_intersection_cache_path(question, date)
    try:
        persisted = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        persisted = None
    if isinstance(persisted, dict):
        with _SOURCE_INTERSECTION_LOCK:
            # The persisted full-history aggregate is safe to paint at once.
            # Treat it as current for five minutes; live ingestion otherwise
            # advances the global version every few seconds and would keep a
            # CPU-heavy historical regroup running continuously.
            _SOURCE_INTERSECTION_CACHE[key] = (version, persisted)
            _SOURCE_INTERSECTION_LAST_VALIDATED[key] = time.monotonic()
        return persisted
    with _SOURCE_INTERSECTION_LOCKS[key]:
        payload = _build_source_intersection_payload(question, date)
        with _SOURCE_INTERSECTION_LOCK:
            _SOURCE_INTERSECTION_CACHE[key] = (version, payload)
            _SOURCE_INTERSECTION_LAST_VALIDATED[key] = time.monotonic()
        _persist_source_intersections(question, date, payload)
        return payload


def _analytics_payload_for_client(result, include_runs=False, view=""):
    if view == "overview":
        result = _with_fresh_owned_product_board(result)
    owned_brands = [dict(item) for item in brand_settings.load_settings().get("owned_brands") or []]
    safe_models = []
    for raw_model in result.get("models") or []:
        model = dict(raw_model)
        for field in ("brand_daily", "brand_trend_daily"):
            days = []
            for raw_day in model.get(field) or []:
                day = dict(raw_day)
                day["items"] = [
                    item for item in day.get("items") or []
                    if analytics_valid_brand(item.get("name") or "")
                ]
                days.append(day)
            model[field] = days
        safe_models.append(model)
    result = {**result, "models": safe_models}
    if include_runs and not view:
        return {**result, "owned_brands": owned_brands}
    view_fields = {
        "overview": {"daily", "source_types"},
        "compare": {"source_types", "media", "top_articles", "top_videos"},
        "sources": {"top_articles", "top_videos", "owned_sources", "article_keywords", "video_keywords", "daily_source_top"},
        "brands": {"brand_trend_daily", "product_trend_daily", "brand_source_daily", "source_brand_daily", "article_keywords", "video_keywords"},
        "runs": {"recent_runs"},
        "control": set(),
    }
    selected_fields = view_fields.get(view)
    large_fields = {
        "daily", "questions", "source_types", "media", "top_articles", "top_videos",
        "owned_sources", "article_keywords", "video_keywords", "brand_daily", "product_daily",
        "brand_trend_daily", "product_trend_daily", "brand_source_daily",
        "source_brand_daily", "daily_source_top", "recent_runs",
    }
    models = []
    for model in result.get("models") or []:
        if selected_fields is None:
            models.append({key: value for key, value in model.items() if key != "recent_runs"})
            continue
        projected = {
            key: value
            for key, value in model.items()
            if key not in large_fields or key in selected_fields
        }
        # Keep a stable client shape while sending only the arrays used by the
        # active view. This prevents transient view changes from dereferencing
        # a missing field before the next response arrives.
        for field in large_fields:
            projected.setdefault(field, [])
        models.append(projected)
    return {**result, "models": models, "owned_brands": owned_brands}


def _brand_source_links_payload(params):
    model_id = str((params.get("model") or [""])[0]).strip()
    brand = str((params.get("brand") or [""])[0]).strip()
    question = str((params.get("question") or [""])[0]).strip()
    focus_date = str((params.get("date") or [""])[0]).strip()
    if model_id not in MODEL_PLUGINS:
        raise ValueError("请选择有效模型")
    if not brand:
        raise ValueError("请选择品牌")
    # Link drill-down is a read-only interaction and must stay responsive while
    # the content index is being refreshed. Reuse the already-enriched snapshot
    # shown by the dashboard; a background analytics refresh will replace it
    # atomically when new source content is ready.
    if monitor_database.enabled():
        load_options = {"model": model_id, "question": question}
        if focus_date:
            load_options.update({
                "day_from": focus_date,
                "day_to": focus_date,
            })
        runs_by_model = monitor_database.load_runs_by_model(**load_options)
        runs_by_model = {model_id: runs_by_model.get(model_id, [])}
        prepare_analytics(runs_by_model)
    else:
        snapshot = _ANALYTICS_SNAPSHOT
        if snapshot is None:
            snapshot = _analytics_snapshot(_analytics_data_version())
        _snapshot_version, runs_by_model, _prepared = snapshot
    selected_runs = [
        run for run in runs_by_model.get(model_id, [])
        if not question or run.get("question") == question
    ]
    if focus_date:
        selected_runs = [
            run for run in selected_runs
            if str(run.get("day") or "") == focus_date
        ]
    days = []
    for day in sorted({run.get("day") for run in selected_runs if run.get("day")}, reverse=True):
        day_runs = [run for run in selected_runs if run.get("day") == day]
        observed_sources = {}
        evaluable_sources = {}
        matched_sources = {}
        for run in day_runs:
            for source in run.get("sources") or []:
                key = str(source.get("canonical_url") or source.get("url") or "").strip()
                if not key:
                    continue
                observed_sources.setdefault(key, source)
                title_hit = brand in set(source.get("title_brand_mentions") or [])
                evaluable = bool(
                    source.get("type") == "视频"
                    or source.get("body_analysis_ready")
                    or title_hit
                )
                if evaluable:
                    evaluable_sources.setdefault(key, source)
                if brand in set(source.get("brand_mentions") or []):
                    matched_sources.setdefault(key, source)
        links = []
        for source in matched_sources.values():
            title_hit = brand in set(source.get("title_brand_mentions") or [])
            body_hit = brand in set(source.get("body_brand_mentions") or [])
            scope = "标题+正文" if title_hit and body_hit else "标题" if title_hit else "正文" if body_hit else "已识别"
            links.append({
                "title": source.get("title") or source.get("url") or "未命名信源",
                "url": source.get("url") or source.get("canonical_url") or "",
                "canonical_url": source.get("canonical_url") or source.get("url") or "",
                "media": source.get("media") or "",
                "type": source.get("type") or "",
                "match_scope": scope,
                "own_products": source.get("own_products") or [],
            })
        links.sort(key=lambda item: (item["type"], item["media"], item["title"]))
        total = len(observed_sources)
        eligible = len(evaluable_sources)
        failed = sum(
            key not in evaluable_sources
            and source.get("content_analysis_status") == "failed"
            for key, source in observed_sources.items()
        )
        matched = len(matched_sources)
        days.append({
            "date": day,
            "sources": total,
            "eligible_sources": eligible,
            "pending_sources": max(0, total - eligible - failed),
            "failed_sources": failed,
            "mentions": matched,
            "mention_rate": round(matched * 100 / eligible, 2) if eligible else 0,
            "links": links,
        })
    return {"ok": True, "model": model_id, "brand": brand, "question": question, "days": days}


CATEGORY_BASELINE_NAME = "品类全量基准"

OWN_TITLE_THEME_PATTERNS = {
    "榜单/推荐": ("推荐", "榜单", "排行", "排名", "top", "好物", "好用", "首选", "值得买", "闭眼入"),
    "测评/实测": ("测评", "实测", "亲测", "试用", "体验", "真实", "对比", "横评", "效果验证"),
    "安全/温和": ("安全", "温和", "不刺激", "无刺激", "敏感", "无激素", "无前列腺素", "副作用"),
    "功效结果": ("增长", "生长", "增密", "浓密", "变长", "强韧", "修护", "控油", "蓬松", "美白", "防晒", "祛痘", "防脱"),
    "成分/科学": ("成分", "配方", "多肽", "pdrn", "科学", "研究", "临床", "原理", "机制", "鱼子酱"),
    "教程/周期": ("怎么用", "用法", "教程", "正确使用", "坚持", "几天", "周期", "多久", "早晚"),
    "避雷/风险": ("避雷", "别买", "踩雷", "智商税", "风险", "慎用", "停用", "拔草"),
    "价格/性价比": ("平价", "价格", "性价比", "便宜", "贵", "大牌平替", "学生党"),
    "年份/新鲜度": ("2026", "2025", "最新", "新款", "今年", "年度"),
    "痛点场景": ("稀疏", "秃眉", "短睫毛", "断裂", "脱落", "出油", "扁塌", "敏感肌", "干枯", "毛躁"),
}
OWN_TITLE_KEYWORD_PHRASES = tuple(sorted({
    phrase
    for phrases in OWN_TITLE_THEME_PATTERNS.values()
    for phrase in phrases
} | {
    "野生眉", "原生眉", "短稀易断", "素颜", "空瓶", "回购", "红黑榜",
    "新手", "入门", "专业", "医生", "科普", "解析", "精选", "必入",
    "显白", "遮白发", "植物染", "敏感头皮", "油头", "细软塌", "头皮屑",
    "清爽", "轻盈", "防水", "不搓泥", "提亮", "保湿", "抗老", "修护屏障",
}, key=len, reverse=True))
OWN_TITLE_KEYWORD_STOPWORDS = {
    "梵玢", "科熙本", "姿生怡", "道和", "焕颜计", "茗媛萃",
    "产品", "品牌", "精华", "精华液", "推荐", "文章", "视频", "分享",
    "资讯", "综合", "日报", "新闻网", "健康网", "大河", "咸宁",
    "眉毛增长液", "睫毛增长液", "眉毛精华液", "睫毛精华液",
}


def owned_source_products(href, title, content_index=None):
    title_matches = set(own_product_mentions(title))
    entries = (
        content_index.get("entries", {})
        if isinstance(content_index, dict) and isinstance(content_index.get("entries"), dict)
        else {}
    )
    raw_href = str(href or "").strip()
    entry = entries.get(raw_href) or entries.get(canonical_source_url(raw_href)) or {}
    body_matches = set()
    if (
        isinstance(entry, dict)
        and entry.get("status") == "ok"
        and entry.get("extraction_quality") in ("high", "medium")
    ):
        if safe_int(entry.get("own_product_schema_version")) == OWN_PRODUCT_SCHEMA_VERSION:
            body_matches.update(entry.get("own_product_mentions") or [])
        else:
            body_matches.update(own_product_mentions(entry.get("excerpt") or ""))
    matches = sorted(title_matches | body_matches)
    if title_matches and body_matches:
        scope = "标题+正文"
    elif body_matches:
        scope = "正文"
    elif title_matches:
        scope = "标题"
    else:
        scope = ""
    return matches, scope


def owned_source_brands(href, title, content_index=None):
    configured = brand_settings.load_settings()
    owned_names = sorted({
        canonical_brand_name(item["name"])
        for item in brand_settings.vocabulary(configured)
        if item.get("group") == "owned" and item.get("name")
    })
    title_matches = {
        brand for brand in owned_names
        if title_mentions_brand(title, brand)
    }
    entries = (
        content_index.get("entries", {})
        if isinstance(content_index, dict) and isinstance(content_index.get("entries"), dict)
        else {}
    )
    raw_href = str(href or "").strip()
    entry = entries.get(raw_href) or entries.get(canonical_source_url(raw_href)) or {}
    body_matches = set()
    if (
        isinstance(entry, dict)
        and entry.get("status") == "ok"
        and entry.get("extraction_quality") in ("high", "medium")
    ):
        body_matches.update(
            canonical_brand_name(value)
            for value in entry.get("owned_brand_mentions") or []
            if value
        )
    matches = sorted(title_matches | body_matches)
    if title_matches and body_matches:
        scope = "标题+正文"
    elif body_matches:
        scope = "正文"
    elif title_matches:
        scope = "标题"
    else:
        scope = ""
    return matches, scope


def read_json(path):
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def read_csv_rows():
    if not os.path.exists(CSV_PATH):
        return []
    try:
        with open(CSV_PATH, "r", encoding="utf-8-sig", newline="") as f:
            return list(csv.DictReader(f))
    except Exception:
        return []


def read_product_rows():
    if not os.path.exists(PRODUCT_CSV_PATH):
        return []
    try:
        with open(PRODUCT_CSV_PATH, "r", encoding="utf-8-sig", newline="") as f:
            return list(csv.DictReader(f))
    except Exception:
        return []


def read_answer_rows():
    if not os.path.exists(ANSWER_CSV_PATH):
        return []
    try:
        with open(ANSWER_CSV_PATH, "r", encoding="utf-8-sig", newline="") as f:
            return list(csv.DictReader(f))
    except Exception:
        return []


def read_capture_skip_rows():
    if not os.path.exists(CAPTURE_SKIP_CSV_PATH):
        return []
    try:
        with open(CAPTURE_SKIP_CSV_PATH, "r", encoding="utf-8-sig", newline="") as f:
            return list(csv.DictReader(f))
    except Exception:
        return []


def safe_int(value, default=0):
    try:
        return int(str(value or "").strip())
    except Exception:
        return default


def file_info(path):
    if not os.path.exists(path):
        return {"exists": False, "mtime": "", "size": 0}
    stat = os.stat(path)
    return {
        "exists": True,
        "mtime": datetime.fromtimestamp(stat.st_mtime, tz=CST).strftime("%Y-%m-%d %H:%M:%S"),
        "size": stat.st_size,
    }


def log_tail(max_lines=60):
    if not os.path.exists(DEBUG_LOG_PATH):
        return []
    try:
        with open(DEBUG_LOG_PATH, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()
        return [line.rstrip("\n") for line in lines[-max_lines:]]
    except Exception as exc:
        return ["read log failed: " + repr(exc)]


@lru_cache(maxsize=20000)
def host_of(url):
    return urlparse(url or "").netloc.lower().split(":")[0]


VIDEO_HOSTS = {"www.iesdouyin.com", "iesdouyin.com", "douyin.com",
               "bilibili.com", "www.bilibili.com",
               "youtube.com", "www.youtube.com", "youtu.be",
               "kuaishou.com", "www.kuaishou.com"}


def link_type(href):
    host = host_of(href)
    if host in VIDEO_HOSTS:
        return "\u89c6\u9891"
    if "/video/" in (href or "").lower():
        return "\u89c6\u9891"
    return "\u6587\u7ae0"


def is_weak_media_name(media, host=""):
    text = str(media or "").strip().lower()
    host = str(host or "").strip().lower().split(":")[0]
    if not text:
        return True
    if "." in text:
        return True
    if host:
        labels = [part for part in host.split(".") if part and part not in ("www", "m", "wap", "pc", "post", "news", "baby")]
        if text == host or text in labels:
            return True
        if len(text) <= 10 and any(text == label or text in label for label in labels):
            return True
    return False


def domain_label(host):
    labels = [part for part in (host or "").split(".") if part and part not in ("www", "m", "wap", "pc", "post", "news", "baby", "detail")]
    return labels[0] if labels else (host or "无域名")


def title_has_product_signal(title):
    title = str(title or "")
    tokens = ("商品", "详情", "批发", "染发剂", "染发膏", "染发乳", "泡泡染", "ml", "盒", "家用", "价格", "包邮")
    return any(token.lower() in title.lower() for token in tokens)


def readable_source_label(source_type, media, host, title, href, note):
    url = (href or "").lower()
    host = host or ""
    media = str(media or "").strip()
    note = str(note or "").strip()
    title = str(title or "")
    label = domain_label(host)

    if "国家药监局" in title or "药监" in title or "抽检" in title or "禁用原料" in title:
        return "监管信息｜药监/化妆品抽检", "非媒体：监管公告或监管信息转载页"

    if any(token in host for token in ("1688.com", "taobao.com", "tmall.com", "jd.com", "pinduoduo.com")):
        platform = "1688" if "1688.com" in host else "淘宝" if "taobao.com" in host else "天猫" if "tmall.com" in host else "京东" if "jd.com" in host else "拼多多"
        return "商品页｜" + platform, "电商商品页"

    if title_has_product_signal(title) and ("/detail" in url or "/offer/" in url or "id=" in url):
        return "商品页｜独立商城（" + label + "）", "非媒体：独立站/小商城商品详情页"

    # 媒体名弱时，用域名生成可读标签，而不是"待识别"
    if is_weak_media_name(media, host):
        if source_type == "商品页":
            return "商品页｜独立商城（" + label + "）", "非媒体：商品页，旧缓存媒体名像域名短名"
        # 用域名标签代替"待识别"
        return "网站｜" + label, "域名兜底：" + host

    if source_type == "商品页":
        return "商品页｜" + media, note or "商品页"
    if source_type == "视频":
        return "视频｜" + media, note or "视频平台/视频来源"
    if source_type == "其他":
        return "其他｜" + media, note or "非文章/非视频/非商品页"
    return media, note


@lru_cache(maxsize=30000)
def _source_for_cached(href, row_title, ai_source_type, ai_media, ai_note, meta_title):
    host = host_of(href)
    source_type = str(ai_source_type or "").strip()
    media = str(ai_media or "").strip()
    note = str(ai_note or "").strip()
    title = row_title or meta_title or ""
    if is_weak_media_name(media, host):
        media = ""
        note = "待 AI 重新识别，当前旧缓存媒体名像域名短名"
    if not source_type:
        url = (href or "").lower()
        if any(token in url for token in ("douyin", "iesdouyin", "tiktok", "bilibili", "kuaishou", "youtube", "xigua", "haokan")):
            source_type = "视频"
        elif "国家药监局" in title or "药监" in title or "抽检" in title or "禁用原料" in title:
            source_type = "文章"
        elif any(token in url for token in ("taobao", "tmall", "jd.", "1688", "pinduoduo", "amazon")):
            source_type = "商品页"
        elif title_has_product_signal(title) and ("/detail" in url or "/offer/" in url or "id=" in url):
            source_type = "商品页"
        elif any(token in title for token in ("视频", "测评", "实测")) and any(token in url for token in ("video", "douyin")):
            source_type = "视频"
        else:
            source_type = "文章"
    media, note = readable_source_label(source_type, media, host, title, href, note)
    return source_type, media, host, note


def source_for(row, ai_cache, meta_cache):
    href = row.get("href", "")
    host = host_of(href)
    ai = ai_cache.get(host) or {}
    meta = meta_cache.get(href) or {}
    return _source_for_cached(
        href,
        str(row.get("title") or ""),
        str(ai.get("source_type") or ""),
        str(ai.get("media") or ""),
        str(ai.get("note") or ""),
        str(meta.get("title") or ""),
    )


def latest_run_rows(rows):
    latest = max((safe_int(row.get("run_no")) for row in rows), default=0)
    return latest, [row for row in rows if safe_int(row.get("run_no")) == latest]


def counter_items(counter, limit=None):
    rows = counter.most_common(limit) if limit else counter.most_common()
    return [
        {"name": name, "count": count}
        for name, count in rows
    ]


def rank_counter_items(counter, ranks_by_name, limit=None, runs_by_name=None, total_runs=0):
    rows = counter.most_common()
    if runs_by_name is not None:
        rows.sort(key=lambda pair: (len(runs_by_name.get(pair[0], set())), pair[1]), reverse=True)
    if limit:
        rows = rows[:limit]
    result = []
    for name, count in rows:
        ranks = [safe_int(rank) for rank in ranks_by_name.get(name, []) if safe_int(rank)]
        avg_rank = round(sum(ranks) / len(ranks), 2) if ranks else 0
        run_count = len(runs_by_name.get(name, set())) if runs_by_name is not None else 0
        rank_counts = Counter(ranks)
        occurrence_count = run_count if runs_by_name is not None else count
        item = {
            "name": name,
            "count": count,
            "run_count": run_count,
            "run_rate": round(run_count * 100 / total_runs, 2) if total_runs else 0,
            "avg_rank": avg_rank,
            "best_rank": min(ranks) if ranks else 0,
            "top1": sum(1 for rank in ranks if rank == 1),
            "top2": sum(1 for rank in ranks if rank == 2),
            "top3": sum(1 for rank in ranks if rank == 3),
            "rank_counts": {str(rank): rank_counts[rank] for rank in sorted(rank_counts)},
            "unranked_count": max(0, occurrence_count - sum(rank_counts.values())),
        }
        result.append(item)
    return result


def _build_owned_product_board(cache_key, selected_models, question, live_day, model):
    version = monitor_database.scope_version(cache_key)
    database_cached = monitor_database.cache_get(
        cache_key, _analytics_content_token(),
    )
    live_board = (
        list(database_cached.get("owned_product_daily") or [])
        if isinstance(database_cached, dict) else None
    )
    if live_board is None:
        runs_by_model = monitor_database.load_owned_product_runs(
            day=live_day, question=question, model=model,
        )
        live_board = daily_owned_product_recommendations(
            runs_by_model, selected_models, question=question, date=live_day,
        )
        monitor_database.cache_put(
            cache_key, _analytics_content_token(),
            {"owned_product_daily": live_board},
            expected_scope_version=int(version[1]),
        )
    with _OWNED_BOARD_LOCK:
        _OWNED_BOARD_CACHE[cache_key] = (version, live_board)
    return live_board


def _refresh_owned_product_board(cache_key, selected_models, question, live_day, model):
    try:
        _build_owned_product_board(
            cache_key, selected_models, question, live_day, model,
        )
    except Exception:
        pass
    finally:
        with _OWNED_BOARD_LOCK:
            _OWNED_BOARD_BUILDING.discard(cache_key)


def _schedule_owned_product_board_refresh(
    cache_key, selected_models, question, live_day, model,
):
    now = time.monotonic()
    with _OWNED_BOARD_LOCK:
        if cache_key in _OWNED_BOARD_BUILDING:
            return
        if now - _OWNED_BOARD_LAST_VALIDATED.get(cache_key, 0.0) < _OWNED_BOARD_VALIDATION_INTERVAL:
            return
        _OWNED_BOARD_LAST_VALIDATED[cache_key] = now
        _OWNED_BOARD_BUILDING.add(cache_key)
    threading.Thread(
        target=_refresh_owned_product_board,
        args=(cache_key, selected_models, question, live_day, model),
        name="owned-product-board-refresh",
        daemon=True,
    ).start()


def _with_fresh_owned_product_board(result):
    """Overlay the live owned-product matrix on a possibly stale full snapshot.

    Archive KPIs are intentionally served stale-while-revalidate so filter
    changes stay responsive.  The owned-product board is small and operationally
    time-sensitive, so it uses a source-free indexed query and its own scoped
    version instead of waiting for the heavyweight all-date snapshot.
    """
    if not monitor_database.enabled():
        return result
    filters = dict(result.get("filters") or {})
    question = str(filters.get("question") or "")
    requested_day = str(filters.get("date") or "")
    model = str(filters.get("model") or "")
    try:
        if requested_day:
            visible_days = [requested_day]
        else:
            # The complete snapshot already carries the date selector.  Reading
            # it here avoids an extra PostgreSQL query on every UI switch while
            # collectors may be writing.  Fall back only for a truly cold cache.
            visible_days = [
                str(item) for item in (result.get("dates") or [])[:7] if item
            ]
            if not visible_days:
                options = monitor_database.analytics_filter_options(
                    model=model, question=question,
                )
                visible_days = [
                    str(item) for item in (options.get("dates") or [])[:7] if item
                ]
        if not visible_days:
            return result
        selected_models = (
            [model] if model and model in MODEL_REGISTRY else list(MODEL_REGISTRY)
        )
        stale_board = list(result.get("owned_product_daily") or [])
        board = []
        for day_index, live_day in enumerate(visible_days):
            historical = [
                row for row in stale_board if str(row.get("date") or "") == live_day
            ]
            # For an all-date view, only the newest day changes continuously.
            # Reuse complete historical rows and refresh a historical day only
            # when the cached snapshot has no matrix for it.
            if not requested_day and day_index > 0 and historical:
                board.extend(historical)
                continue
            cache_key = (question, live_day, model, "owned_product_board")
            with _OWNED_BOARD_LOCK:
                cached = _OWNED_BOARD_CACHE.get(cache_key)
            if cached is not None:
                live_board = cached[1]
                _schedule_owned_product_board_refresh(
                    cache_key, selected_models, question, live_day, model,
                )
            elif historical:
                # The analytics payload already contains a complete matrix.
                # Paint it immediately and validate in the background; live
                # collection must never make a five-second UI poll wait on DB.
                live_board = historical
                with _OWNED_BOARD_LOCK:
                    _OWNED_BOARD_CACHE[cache_key] = ((), live_board)
                _schedule_owned_product_board_refresh(
                    cache_key, selected_models, question, live_day, model,
                )
            else:
                # A truly cold payload has no safe board to show yet. This path
                # runs once; subsequent requests use stale-while-revalidate.
                live_board = _build_owned_product_board(
                    cache_key, selected_models, question, live_day, model,
                )
            board.extend(live_board)
        return {**result, "owned_product_daily": board}
    except Exception:
        # A transient database problem must not take down the rest of the
        # dashboard; the last complete archive result remains auditable.
        return result


QUESTION_RULES = [
    ("染发剂推荐", ("染发", "白发", "染发剂", "染发膏", "染发霜", "遮白", "植物染")),
    ("祛痘推荐", ("祛痘", "痘痘", "痤疮", "闭口", "粉刺")),
    ("防晒推荐", ("防晒", "晒黑", "晒伤", "spf", "pa+++")),
    ("洗发水推荐", ("洗发水", "洗头", "头皮屑", "控油洗发")),
    ("护肤品推荐", ("护肤", "面霜", "精华", "乳液", "敏感肌")),
]


def question_summaries(rows, answer_rows=None):
    buckets = {}
    for row in rows:
        question = question_for(row)
        item = buckets.setdefault(question, {
            "question": question,
            "refs": 0,
            "unique_links": set(),
            "source_runs": set(),
            "answer_runs": set(),
            "latest_run_no": 0,
            "latest_run_time": "",
        })
        item["refs"] += 1
        if row.get("href"):
            item["unique_links"].add(row.get("href"))
        run_no = safe_int(row.get("run_no"))
        if run_no:
            item["source_runs"].add(run_no)
            item["answer_runs"].add(run_no)
            if run_no >= item["latest_run_no"]:
                item["latest_run_no"] = run_no
                item["latest_run_time"] = row.get("run_time", "")

    # Questions and runs without reference links must still appear in the
    # selector. Otherwise a valid answer from one account is incorrectly
    # hidden and the selector understates multi-account collection coverage.
    for row in (answer_rows or []):
        question = question_for(row)
        item = buckets.setdefault(question, {
            "question": question,
            "refs": 0,
            "unique_links": set(),
            "source_runs": set(),
            "answer_runs": set(),
            "latest_run_no": 0,
            "latest_run_time": "",
        })
        run_no = safe_int(row.get("run_no"))
        if run_no:
            item["answer_runs"].add(run_no)
            if run_no >= item["latest_run_no"]:
                item["latest_run_no"] = run_no
                item["latest_run_time"] = row.get("run_time", "")

    result = []
    for item in buckets.values():
        result.append({
            "question": item["question"],
            "refs": item["refs"],
            "unique_links": len(item["unique_links"]),
            "runs": len(item["answer_runs"]),
            "source_runs": len(item["source_runs"]),
            "latest_run_no": item["latest_run_no"],
            "latest_run_time": item["latest_run_time"],
        })
    return sorted(result, key=lambda x: (x["latest_run_no"], x["refs"]), reverse=True)


ALL_QUESTIONS = "\u5168\u90e8\u95ee\u9898"
UNKNOWN_QUESTION = "\u672a\u8bc6\u522b\u95ee\u9898"
QUESTION_RULES = [
    ("\u67d3\u53d1\u5242\u63a8\u8350", ("\u67d3\u53d1", "\u767d\u53d1", "\u67d3\u53d1\u5242", "\u67d3\u53d1\u818f", "\u67d3\u53d1\u971c", "\u906e\u767d", "\u690d\u7269\u67d3")),
    ("\u794d\u75d8\u63a8\u8350", ("\u794d\u75d8", "\u75d8\u75d8", "\u75e4\u75ae", "\u95ed\u53e3", "\u7c89\u523a")),
    ("\u9632\u6652\u63a8\u8350", ("\u9632\u6652", "\u6652\u9ed1", "\u6652\u4f24", "spf", "pa+++")),
    ("\u6d17\u53d1\u6c34\u63a8\u8350", ("\u6d17\u53d1\u6c34", "\u6d17\u5934", "\u5934\u76ae\u5c51", "\u63a7\u6cb9\u6d17\u53d1")),
    ("\u62a4\u80a4\u54c1\u63a8\u8350", ("\u62a4\u80a4", "\u9762\u971c", "\u7cbe\u534e", "\u4e73\u6db2", "\u654f\u611f\u808c")),
]


# 用于区分旧数据中 chat_title 相同但实际问题不同的情况
# 格式: { "chat_title": { "split_run_no": N } } 表示 run_no <= N 的是一个问题，> N 的是另一个
# 如果问题文本完全一致可留空 {}

DOUBAO_QUESTION_SPLITS = {
    # 前 100 个 run 是"推荐一款染发剂"，后面 44 个是"染发剂推荐"
    "染发剂推荐": 100,
}
NORMALIZATION_SCHEMA_VERSION = 3
ALL_DEVICES = "all"


@lru_cache(maxsize=8192)
def canonical_question_name(value):
    """Return the stable canonical question name for a surface form."""
    return qa.canonical_question_name(value)


def normalize_selected_question(value):
    question = str(value or ALL_QUESTIONS).strip() or ALL_QUESTIONS
    if question == ALL_QUESTIONS:
        return ALL_QUESTIONS
    return canonical_question_name(question) or question


def normalize_selected_device(value):
    device = str(value or ALL_DEVICES).strip()
    return device if device and device.casefold() != ALL_DEVICES else ALL_DEVICES


def row_device(row):
    return str((row or {}).get("mumu_instance") or "").strip()


def question_for(row):
    direct = canonical_question_name(row.get("question") or row.get("prompt") or row.get("query"))
    if direct:
        return direct

    chat_title = canonical_question_name(row.get("chat_title"))
    if chat_title:
        # 如果 chat_title 相同但不同 run_no 区间对应不同问题（旧数据没有 question 列）
        split_run_no = DOUBAO_QUESTION_SPLITS.get(chat_title)
        if split_run_no:
            run_no = safe_int(row.get("run_no"))
            if run_no <= split_run_no:
                old_question = "推荐一款" + chat_title.replace("推荐", "")
                return qa.QUESTION_ALIASES.get(old_question, old_question)
            else:
                return chat_title
        return chat_title

    text = " ".join([
        str(row.get("title") or ""),
        str(row.get("page_url") or ""),
        str(row.get("href") or ""),
    ]).lower()
    for label, keywords in QUESTION_RULES:
        if any(keyword.lower() in text for keyword in keywords):
            return label

    return UNKNOWN_QUESTION


def question_source_breakdown(rows, ai_cache, meta_cache):
    buckets = {}
    for row in rows:
        question = question_for(row)
        bucket = buckets.setdefault(question, {
            "question": question,
            "refs": 0,
            "unique_links": set(),
            "runs": set(),
            "latest_run_no": 0,
            "latest_run_time": "",
            "by_type": Counter(),
            "by_media": Counter(),
            "by_domain": Counter(),
        })
        bucket["refs"] += 1
        href = row.get("href", "")
        if href:
            bucket["unique_links"].add(href)
        run_no = safe_int(row.get("run_no"))
        if run_no:
            bucket["runs"].add(run_no)
            if run_no >= bucket["latest_run_no"]:
                bucket["latest_run_no"] = run_no
                bucket["latest_run_time"] = row.get("run_time", "")
        source_type, media, host, _note = source_for(row, ai_cache, meta_cache)
        bucket["by_type"][source_type] += 1
        bucket["by_media"][media] += 1
        if host:
            bucket["by_domain"][host] += 1

    result = []
    for bucket in buckets.values():
        result.append({
            "question": bucket["question"],
            "refs": bucket["refs"],
            "unique_links": len(bucket["unique_links"]),
            "runs": len(bucket["runs"]),
            "latest_run_no": bucket["latest_run_no"],
            "latest_run_time": bucket["latest_run_time"],
            "by_type": counter_items(bucket["by_type"]),
            "by_media": counter_items(bucket["by_media"]),
            "by_domain": counter_items(bucket["by_domain"]),
            "media_total": len(bucket["by_media"]),
            "domain_total": len(bucket["by_domain"]),
        })
    return sorted(result, key=lambda x: (x["latest_run_no"], x["refs"]), reverse=True)


def date_for(row):
    for key in ("run_time", "extracted_at"):
        value = str(row.get(key) or "").strip()
        if len(value) >= 10:
            return value[:10]
    return "未知日期"


def daily_question_source_breakdown(
    rows,
    ai_cache,
    meta_cache,
    content_index=None,
    answer_rows=None,
):
    def owned_products_for(href, title):
        return owned_source_products(href, title, content_index)

    def owned_brands_for(href, title):
        return owned_source_brands(href, title, content_index)

    buckets = {}
    all_dates = set()

    def get_bucket(question):
        return buckets.setdefault(question, {
            "question": question,
            "dates": set(),
            "refs_by_date": Counter(),
            "runs_by_date": {},
            "media_by_date": {},
            "type_by_date": {},
            "href_by_date": {},
            "href_titles": {},
        })

    for row in rows:
        question = question_for(row)
        day = date_for(row)
        all_dates.add(day)
        bucket = get_bucket(question)
        bucket["dates"].add(day)
        bucket["refs_by_date"][day] += 1
        run_no = safe_int(row.get("run_no"))
        if run_no:
            bucket["runs_by_date"].setdefault(day, set()).add(run_no)
        source_type, media, _host, _note = source_for(row, ai_cache, meta_cache)
        bucket["media_by_date"].setdefault(day, Counter())[media] += 1
        bucket["type_by_date"].setdefault(day, Counter())[source_type] += 1
        href = row.get("href", "")
        if href:
            bucket["href_by_date"].setdefault(day, Counter())[href] += 1
            if href not in bucket["href_titles"]:
                bucket["href_titles"][href] = (row.get("title") or "")[:100]

    # A completed answer can legitimately contain no source/reference cards.
    # Keep those runs on the daily timeline so a zero-reference day is visible
    # instead of disappearing from the dashboard entirely.
    for row in answer_rows or []:
        question = question_for(row)
        day = date_for(row)
        all_dates.add(day)
        bucket = get_bucket(question)
        bucket["dates"].add(day)
        run_no = safe_int(row.get("run_no"))
        if run_no:
            bucket["runs_by_date"].setdefault(day, set()).add(run_no)

    dates = sorted(all_dates)
    result = []
    for bucket in buckets.values():
        media_names = set()
        type_names = set()
        for counter in bucket["media_by_date"].values():
            media_names.update(counter.keys())
        for counter in bucket["type_by_date"].values():
            type_names.update(counter.keys())

        def matrix_rows(names, by_date):
            built = []
            for name in names:
                counts = [by_date.get(day, Counter()).get(name, 0) for day in dates]
                total = sum(counts)
                delta = counts[-1] - counts[-2] if len(counts) >= 2 else counts[-1]
                built.append({
                    "name": name,
                    "total": total,
                    "delta": delta,
                    "counts": counts,
                })
            return sorted(built, key=lambda item: (item["total"], item["name"]), reverse=True)

        # 每个日期的 top 10 链接（视频和文章分别取前10）
        top_links_by_date = {}
        for day in dates:
            day_counter = bucket["href_by_date"].get(day, Counter())
            top_links_by_date[day] = []
            seen = set()

            # 视频 top 10
            video_counter = Counter({h: c for h, c in day_counter.items() if link_type(h) == "\u89c6\u9891"})
            for h, c in video_counter.most_common(10):
                title = bucket["href_titles"].get(h, "")
                own_products, own_scope = owned_products_for(h, title)
                own_brands, own_brand_scope = owned_brands_for(h, title)
                top_links_by_date[day].append(
                    {
                        "href": h, "count": c, "title": title, "type": "\u89c6\u9891",
                        "own_products": own_products, "own_match_scope": own_scope,
                        "own_brands": own_brands, "own_brand_match_scope": own_brand_scope,
                    })
                seen.add(h)

            # 文章 top 10
            article_counter = Counter({h: c for h, c in day_counter.items() if link_type(h) == "\u6587\u7ae0"})
            for h, c in article_counter.most_common(10):
                title = bucket["href_titles"].get(h, "")
                own_products, own_scope = owned_products_for(h, title)
                own_brands, own_brand_scope = owned_brands_for(h, title)
                top_links_by_date[day].append(
                    {
                        "href": h, "count": c, "title": title, "type": "\u6587\u7ae0",
                        "own_products": own_products, "own_match_scope": own_scope,
                        "own_brands": own_brands, "own_brand_match_scope": own_brand_scope,
                    })
                seen.add(h)

        result.append({
            "question": bucket["question"],
            "dates": dates,
            "refs_by_date": [bucket["refs_by_date"].get(day, 0) for day in dates],
            "runs_by_date": [len(bucket["runs_by_date"].get(day, set())) for day in dates],
            "media_rows": matrix_rows(media_names, bucket["media_by_date"]),
            "type_rows": matrix_rows(type_names, bucket["type_by_date"]),
            "top_links_by_date": top_links_by_date,
        })
    return sorted(result, key=lambda item: sum(item["refs_by_date"]), reverse=True)


def product_question_for(row):
    direct = canonical_question_name(row.get("question") or row.get("prompt") or row.get("query"))
    if direct:
        return direct
    return canonical_question_name(row.get("chat_title")) or UNKNOWN_QUESTION


PRODUCT_CATEGORY_TOKENS = (
    "沐浴精油", "沐浴油", "染发剂", "染发膏", "染发霜", "泡沫染",
    "眉毛增长液", "眉毛精华液", "睫毛增长液", "睫毛精华液", "睫毛精华",
    "生发液", "育发液", "洗发水", "精华液", "精华", "面膜", "面霜", "防晒霜", "防晒乳",
)
PRODUCT_MODIFIER_TOKENS = (
    "甜扁桃", "果酸", "护理", "修护", "修护款", "泡沫", "怡然", "植物",
    "薰衣草", "经典", "日常", "清爽", "滋润", "温和", "低敏", "敏感肌", "干敏皮",
)

PRODUCT_NOISE_TOKENS = (
    "评测", "精选评测", "测评", "指南", "排行榜", "榜单", "红黑榜", "选购",
    "价格", "图片", "品牌", "怎么样", "京东商城", "淘宝网", "网易网", "手机网易网",
    "重要提醒", "必须", "否则", "极易", "反黑", "不是产品", "科普", "区别",
    "实测", "哪个", "哪款", "哪个好", "好闻", "好用", "热门", "清单", "法治安顺",
    "界面新闻", "给你挑了", "小提示", "小提醒", "小贴士", "提示", "贴士", "选购小贴士", "一句话",
)

BRAND_FORBIDDEN_WORDS = (
    "玻尿酸", "水杨酸", "烟酰胺", "氨基酸", "二硫化硒", "PCA", "款热门",
    "控油去", "热门", "实测", "清单", "哪个", "哪款", "好闻", "好用",
)

BRAND_ALIAS_RULES = (
    (("加利古",), "加利古"),
    (("DS实验室", "DS 实验室", "DS Laboratories"), "DS实验室"),
    (("CAVILLA", "Cavilla", "卡维拉", "卡薇拉"), "卡维拉"),
    (("GeraX", "Gerax"), "GeraX"),
    (("VSVE", "vsve", "威诗薇儿"), "VSVE"),
    (("OKSS", "OKSS+", "+OKSS", "+OKSS+", "+okss", "+okss+"), "OKSS"),
    (("Spes", "Spēs", "诗裴丝"), "Spes"),
    (("Freiol", "福来 Freiol", "福来"), "福来"),
    (("Fresh", "馥蕾诗"), "馥蕾诗"),
    (("Moroccanoil", "摩洛哥油"), "摩洛哥油"),
    (("PEACH JO", "PEACH JO+", "PEACH JO +"), "PEACH JO+"),
    (("伊丽莎白雅顿", "雅顿"), "伊丽莎白雅顿"),
    (("仁和匠心", "人仁和匠心", "仁和"), "仁和"),
    (("章华汉草", "章华"), "章华"),
    (("甘椰植萃", "甘椰"), "甘椰"),
    (("因士柔", "因士"), "因士"),
    (("优色林", "Eucerin"), "优色林"),
    (("欧舒丹", "L'OCCITANE", "L’occitane"), "欧舒丹"),
    (("浴见",), "浴见"),
    (("Diptyque", "蒂普提克", "杜桑"), "Diptyque"),
    (("KONO", "卡厘"), "KONO"),
    (("Off&Relax", "Off＆Relax", "Off"), "Off&Relax"),
    (("梵玢", "梵正", "FBCY"), "梵玢 FBCY"),
    (("欧莱雅",), "欧莱雅"),
    (("惊时",), "惊时"),
    (("伊帕尔汗", "伊帕尔"), "伊帕尔汗"),
    (("韩方五谷",), "韩方五谷"),
    (("TOCI",), "TOCI"),
    (("安安金纯",), "安安金纯"),
    (("百雀羚",), "百雀羚"),
    (("肌肤未来",), "肌肤未来"),
    (("妮维雅",), "妮维雅"),
    (("尊蓝",), "尊蓝"),
    (("EHD",), "EHD"),
    (("Nebe",), "Nebe"),
    (("极方防", "极方"), "极方"),
    (("多潘",), "多潘"),
    (("康如",), "康如"),
    (("苏玫氏",), "苏玫氏"),
    (("乐霖",), "乐霖"),
    (("乐霂",), "乐霂"),
    (("拜耳康王", "康王"), "康王"),
    (("花王莉婕", "花王莉", "花王"), "花王"),
    (("施华蔻",), "施华蔻"),
    (("三橡树", "三棵树", "橡树染发剂"), "三橡树"),
    (("高缇雅染发露", "高缇雅泡泡染发露", "高缇雅"), "高缇雅"),
    (("韩愢壹号", "韩愢壹", "韩愢"), "韩愢壹"),
    (("薇诺娜",), "薇诺娜"),
    (("溪木源",), "溪木源"),
    (("安修泽",), "安修泽"),
    (("毕生之研", "毕生之"), "毕生之研"),
    (("丽可植",), "丽可植"),
    (("肌漾美", "肌漾"), "肌漾"),
    (("ALRA",), "ALRA"),
    (("芙清",), "芙清"),
    (("大水滴",), "大水滴"),
    (("John",), "John Jeff"),
    (("卡诗", "Kérastase", "Kerastase"), "卡诗"),
    (("道和时尚", "道和时", "道和"), "道和"),
    (("韩束",), "韩束"),
    (("乐霖",), "乐霖"),
    (("域发",), "域发"),
    (("森之宣言",), "森之宣言"),
    (("植芙琳",), "植芙琳"),
    (("妍绮",), "妍绮"),
    (("OKSS",), "OKSS"),
    (("海飞丝",), "海飞丝"),
    (("KIMTRUE", "且初"), "KIMTRUE"),
    (("Spes",), "Spes"),
    (("自然堂",), "自然堂"),
    (("青植元",), "青植元"),
    (("焕颜计",), "焕颜计"),
    (("依漾",), "依漾"),
    (("高姿",), "高姿"),
    (("颐莲",), "颐莲"),
    (("瑷尔博士",), "瑷尔博士"),
    (("珀莱雅",), "珀莱雅"),
    (("理肤泉", "La Roche-Posay"), "理肤泉"),
    (("润百颜",), "润百颜"),
    (("RNW",), "RNW"),
    (("THESTARCHILD", "The Star Child"), "THESTARCHILD"),
    (("EIIO",), "EIIO"),
    (("GIK",), "GIK"),
    (("金妮雅",), "金妮雅"),
    (("凯膜",), "凯膜"),
    (("仁和匠心",), "仁和匠心"),
    (("澳贝妍",), "澳贝妍"),
    (("美美的天空",), "美美的天空"),
    (("可复美",), "可复美"),
    (("敷尔佳",), "敷尔佳"),
    (("神秘博士",), "神秘博士"),
    (("LiLiA",), "LiLiA"),
    (("GlashVista", "Glashvista"), "GlashVista"),
    (("REVITALASH", "RevitaLash", "Revitalash"), "RevitaLash"),
)

KNOWN_BRANDS = {brand for _, brand in BRAND_ALIAS_RULES}

INVALID_BRAND_TERMS = {
    "款高性", "遵循天然", "强韧丰盈系列", "染发剂", "款抖音", "市监小", "不存在纯",
    "祛痘", "红肿痘", "痘", "痘痘", "敏感红", "油痘", "淡化痘", "突发红肿",
    "油敏痘", "使用小贴士", "植祛小", "小提示", "小提醒", "小贴士", "面膜每周", "面膜每周2", "面膜一周",
    "高端沙龙卡诗", "高端沙龙级卡诗", "内蒙古", "韩愢单剂", "橡树",
    # AI/product-card descriptors and shop names are not brands.  These values
    # occurred in grounded recommendation text, so grounding alone cannot tell
    # them apart from a real brand.
    "12种氨基酸", "俄罗斯睫毛营养液", "新升级通用", "植萃", "植物精华",
    "泡泡染发剂", "高端理发店同款", "端享优选店铺", "专攻", "美白祛斑霜",
    "韩国三文鱼", "粉水光", "草本", "草本育发", "苗族传承", "水晶", "紫苏",
    "黄金", "黄金面膜", "黄金胶原蛋白", "黄金胶原蛋白面膜", "黄金胶原蛋白多肽发光面膜",
}

INVALID_PRODUCT_TERMS = {
    "款高性价比", "遵循天然", "强韧丰盈系列", "款抖音", "市监小", "不存在纯",
    "使用小贴士", "植祛小", "小提示", "小提醒", "小贴士", "面膜每周", "面膜一周", "内蒙古",
    "年控油洗发水推荐", "染发剂安全挑选四步法", "染发剂年度排行", "新手友好・三款精选染发剂",
    "植物染发剂[利用植物来源", "染发剂营销乱象", "一种净颜祛痘精华液及其制备方法",
    "种净颜祛痘精华液及其制备方法",
    "祛痘精华好物推荐",
}


def brand_ai_cache():
    data = read_json(BRAND_AI_CACHE_PATH)
    brands = data.get("brands") if isinstance(data.get("brands"), dict) else {}
    return brands


def brand_from_ai_cache(product_name):
    text = str(product_name or "").strip()
    if not text:
        return ""
    cache = brand_ai_cache()
    for key, value in cache.items():
        if not key or key not in text:
            continue
        if isinstance(value, str):
            return value.strip()
        if isinstance(value, dict) and value.get("is_brand") and value.get("brand"):
            return str(value.get("brand")).strip()
    return ""


def strip_brand_leading_noise(text):
    text = str(text or "").strip()
    # Remove repeated price/spec/symbol prefixes until a plausible brand token is
    # at the front. This keeps "440ml 💰 KONO ..." counted as KONO instead of
    # dropping the product or counting "440ml" as a brand.
    for _ in range(4):
        old = text
        text = re.sub(r"^[\s￥¥$]*\d+(?:\.\d+)?\s*/\s*\d+\s*(?:ml|mL|ML|g|G)\b", "", text).strip()
        text = re.sub(r"^\d+(?:\.\d+)?\s*(?:ml|mL|ML|g|G)\b", "", text).strip()
        text = re.sub(r"^[^\w\u4e00-\u9fff]+", "", text).strip()
        if text == old:
            break
    return text


def is_invalid_brand_candidate(brand):
    text = str(brand or "").strip()
    if not text:
        return True
    if text in INVALID_BRAND_TERMS:
        return True
    if len(text) > 14:
        return True
    if re.fullmatch(r"[\d.]+", text):
        return True
    # 规格单位必须和数字一起出现。旧规则把任意英文品牌中的字母
    # G/g 都当成“克”，导致 GeraX、GIK、DDG 等有效品牌被整批过滤。
    if re.search(r"\d+(?:\.\d+)?\s*(?:ml|g|kg)\b", text, re.IGNORECASE):
        return True
    if re.search(r"[元￥¥/]", text):
        return True
    # Allow spaces inside multi-token brand names such as "梵玢 FBCY".
    if re.search(r"[^\w\u4e00-\u9fff&＆'.’+\-\s]", text):
        return True
    if any(token in text for token in PRODUCT_NOISE_TOKENS):
        return True
    if any(token.lower() in text.lower() for token in BRAND_FORBIDDEN_WORDS):
        return True
    if text in PRODUCT_CATEGORY_TOKENS or text in PRODUCT_MODIFIER_TOKENS:
        return True
    return False


def brand_for_product(product_name):
    text = str(product_name or "").strip()
    if not text:
        return ""
    # Some extracted names start with price/spec/emoji, for example
    # "7.9/440ml 💰 KONO ..." or "💥 惊时 ...". Those are not brands.
    text = strip_brand_leading_noise(text)
    # Confirmed brand prefixes from AI-reviewed product names.  Unknown prefixes
    # are still rejected below; this small allow-list prevents a verified full
    # product name from being shown as "brand missing" merely because the brand
    # cache has not been generated yet.
    for prefix, brand in (
        ("谷雨", "谷雨"),
        ("HBN", "HBN"),
        ("焕颜计", "焕颜计"),
    ):
        if text.lower().startswith(prefix.lower()):
            return brand
    if any(token in text for token in PRODUCT_NOISE_TOKENS):
        # If this is an obvious page title/noise phrase, don't create a fake
        # brand. Known aliases later in the string only count when the text is
        # not a question/article title like "欧莱雅洗发水哪个味道好闻".
        for aliases, brand in BRAND_ALIAS_RULES:
            if any(alias and alias.lower() in text.lower() for alias in aliases) and not any(
                token in text for token in ("哪个", "哪款", "好闻", "好用", "实测", "清单", "评测", "测评")
            ):
                return brand
        return ""
    for aliases, brand in BRAND_ALIAS_RULES:
        if any(alias and alias.lower() in text.lower() for alias in aliases):
            return brand
    cached_brand = brand_from_ai_cache(text)
    if cached_brand and not is_invalid_brand_candidate(cached_brand):
        return cached_brand
    text = re.sub(r"[（(].*?[）)]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    first_token = re.split(r"[\s·\-_/]+", text)[0].strip()
    if first_token in KNOWN_BRANDS:
        return first_token
    if first_token in INVALID_BRAND_TERMS:
        return ""
    compact = re.sub(r"\s+", "", text)
    prefix = compact
    for token in PRODUCT_CATEGORY_TOKENS:
        pos = compact.find(token)
        if pos > 0:
            prefix = compact[:pos]
            break
    changed = True
    while changed:
        changed = False
        for token in PRODUCT_MODIFIER_TOKENS:
            if prefix.endswith(token) and len(prefix) > len(token):
                prefix = prefix[:-len(token)]
                changed = True
    prefix = strip_brand_leading_noise(prefix)
    cached_brand = brand_from_ai_cache(prefix)
    if cached_brand and not is_invalid_brand_candidate(cached_brand):
        return cached_brand
    if prefix in KNOWN_BRANDS:
        return prefix
    # Unknown prefixes are not allowed into the brand leaderboard directly.
    # They can be added through BRAND_ALIAS_RULES or doubao_brand_ai_cache.json
    # after AI/manual confirmation.
    return ""


@lru_cache(maxsize=8192)
def canonical_brand_name(brand):
    """Merge confirmed spelling variants before aggregating the leaderboard."""
    text = analytics_canonical_brand_name(brand)
    if not text:
        return ""
    # Explicit model brands should use the same case-insensitive alias table as
    # brands inferred from product names. This merges GlashVista/Glashvista and
    # REVITALASH/RevitaLash without merging those two distinct brands together.
    folded = text.casefold()
    for item in brand_settings.vocabulary():
        if any(
            folded == str(alias or "").strip().casefold()
            for alias in item.get("aliases") or ()
        ):
            return str(item.get("name") or text).strip()
    for aliases, canonical in BRAND_ALIAS_RULES:
        if any(folded == str(alias or "").strip().casefold() for alias in aliases):
            return canonical
    if text.startswith("梵玢"):
        return "梵玢 FBCY"
    # Liese（莉婕）is Kao's hair-color brand. The dashboard aggregates by
    # parent brand, so model outputs such as 花王莉婕/莉婕 must join 花王.
    if text.startswith("花王莉婕") or text in ("花王莉婕", "莉婕", "Liese"):
        return "花王"
    if text.casefold() == "ryo" or text in ("吕", "紫吕", "吕（Ryo）", "吕RYO"):
        return "吕RYO"
    if text.startswith("道和"):
        return "道和"
    # “拜耳康王”是“拜耳旗下康王”的营销写法。榜单按消费者实际看到
    # 的品牌“康王”聚合，不能把它改成母公司“拜耳”而造成正文命中消失。
    if text in ("康王", "拜耳康王") or text.startswith("拜耳康王"):
        return "康王"
    return text


SOURCE_TRACKING_PARAMS = {
    "from", "source", "share_token", "share_id", "timestamp", "refer", "ref",
    "spm", "scm", "scene", "enter_from", "enter_method", "share_app_id",
}


def canonical_source_url(href):
    """Normalize URLs for daily unique/new-source calculations.

    This is deliberately conservative: identity-bearing path/query parameters
    are preserved, while fragments and common tracking parameters are removed.
    """
    text = str(href or "").strip()
    if not text:
        return ""
    try:
        parsed = urlparse(text)
        query = []
        for key, value in parse_qsl(parsed.query, keep_blank_values=True):
            folded = key.casefold()
            if folded.startswith("utm_") or folded in SOURCE_TRACKING_PARAMS:
                continue
            query.append((key, value))
        path = re.sub(r"/{2,}", "/", parsed.path or "/")
        if path != "/":
            path = path.rstrip("/")
        scheme = (parsed.scheme or "https").casefold()
        netloc = parsed.netloc.casefold()
        if scheme in {"http", "https"}:
            scheme = "https"
            if netloc.startswith("www."):
                netloc = netloc[4:]
        return urlunparse((
            scheme,
            netloc,
            path,
            "",
            urlencode(sorted(query)),
            "",
        ))
    except Exception:
        return text.split("#", 1)[0]


@lru_cache(maxsize=512)
def aliases_for_brand(brand):
    canonical = canonical_brand_name(brand)
    aliases = {canonical}
    # Keep extraction/grounding aliases in sync with analytics canonicalization.
    # Previously a canonical value such as "TALIKA 塔莉卡" was known to the
    # dashboard, but its answer-side alias "塔莉卡" was not considered grounded,
    # which incorrectly cleared the brand after successful extraction.
    aliases.update(BRAND_CANONICAL_GROUPS.get(canonical, ()))
    for values, target in BRAND_ALIAS_RULES:
        if canonical_brand_name(target) == canonical:
            aliases.update(str(value or "").strip() for value in values)
    aliases.update(brand_settings.aliases_for_brand(canonical))
    aliases.discard("")
    # "Off" is too generic for title-level evidence and creates many false
    # matches in English titles; the full Off&Relax spelling remains usable.
    aliases.discard("Off")
    return sorted(aliases, key=lambda value: (-len(value), value.casefold()))


@lru_cache(maxsize=200000)
def title_mentions_brand(title, brand):
    text = str(title or "")
    folded = text.casefold()
    for alias in aliases_for_brand(brand):
        alias_folded = str(alias or "").casefold()
        if not alias_folded:
            continue
        if re.search(r"[\u3400-\u9fff]", alias_folded):
            if brand_alias_occurs(text, alias_folded):
                return True
            continue
        if re.search(r"(?<![a-z0-9])" + re.escape(alias_folded) + r"(?![a-z0-9])", folded):
            return True
    return False


def brand_for_row(row, normalized_product):
    """Prefer the brand explicitly returned by the product-review model."""
    model_brand = str((row or {}).get("brand_name") or "").strip()
    if model_brand == "拜耳" and "康王" in str(normalized_product or ""):
        return "康王"
    if model_brand and not is_invalid_brand_candidate(model_brand):
        return canonical_brand_name(model_brand)
    return canonical_brand_name(brand_for_product(normalized_product))


PRODUCT_ALIAS_RULES = (
    (("GlashVista",), "GlashVista 睫毛精华液"),
    (("RevitaLash", "眉毛"), "RevitaLash 眉毛精华液"),
    (("RevitaLash", "睫毛"), "RevitaLash 睫毛精华液"),
    (("瑷尔博士", "益生菌", "面膜"), "瑷尔博士益生菌面膜"),
    (("珀莱雅", "源力", "面膜"), "珀莱雅源力面膜"),
    (("珀莱雅", "双抗", "面膜"), "珀莱雅双抗面膜"),
    (("珀莱雅", "水母", "面膜"), "珀莱雅水母面膜"),
    (("欧莱雅", "黑精华", "面膜"), "欧莱雅黑精华面膜"),
    (("理肤泉", "B5", "面膜"), "理肤泉 B5 面膜"),
    (("高姿", "蜂巢", "水库", "面膜"), "高姿蜂巢水库面膜"),
    (("润百颜", "白纱布", "面膜"), "润百颜白纱布面膜"),
    (("RNW", "玻尿酸", "面膜"), "RNW 玻尿酸面膜"),
    (("RNW", "四季", "发光", "面膜"), "RNW 四季发光面膜"),
    (("THESTARCHILD", "积雪草", "面膜"), "THESTARCHILD 积雪草面膜"),
    (("EIIO", "水光", "面膜"), "EIIO 水光面膜"),
    (("GIK", "PRP", "面膜"), "GIK PRP 胶原面膜"),
    (("金妮雅", "玻尿酸", "面膜"), "金妮雅玻尿酸面膜"),
    (("凯膜", "玻尿酸", "面膜"), "凯膜玻尿酸冻干粉面膜"),
    (("仁和匠心", "玻尿酸", "面膜"), "仁和匠心多重玻尿酸面膜"),
    (("澳贝妍", "377", "面膜"), "澳贝妍 377 美白面膜"),
    (("澳贝妍", "八杯水", "面膜"), "澳贝妍八杯水玻尿酸面膜"),
    (("八杯水", "玻尿酸", "面膜"), "澳贝妍八杯水玻尿酸面膜"),
    (("韩方五谷", "377", "面膜"), "韩方五谷 377 美白面膜"),
    (("美美的天空", "积雪草", "面膜"), "美美的天空积雪草面膜"),
    (("薇诺娜", "屏障", "面膜"), "薇诺娜屏障修护面膜"),
    (("可复美", "胶原蛋白", "修护贴"), "可复美重组胶原蛋白修护贴"),
    (("玻尿酸", "冻干粉", "面膜"), "凯膜玻尿酸冻干粉面膜"),
    (("神秘博士", "二裂酵母", "面膜"), "神秘博士二裂酵母面膜"),
    (("LiLiA", "玻尿酸", "面膜"), "LiLiA 8D 玻尿酸面膜"),
)


def normalize_product_for_stats(product_name):
    text = str(product_name or "").strip()
    if not text:
        return ""
    text = strip_brand_leading_noise(text)
    text = re.sub(r"\s+", " ", text).strip(" -—：:，,。；;")
    for tokens, canonical in PRODUCT_ALIAS_RULES:
        if all(token.lower() in text.lower() for token in tokens):
            return canonical
    return text


def is_noisy_product_name(product_name):
    text = str(product_name or "").strip()
    if not text:
        return True
    if any(term and term in text for term in INVALID_PRODUCT_TERMS):
        return True
    noisy_patterns = (
        r"(?:实测|测评|评测|精选评测)",
        r"(?:哪个|哪款|哪个好|好闻|好用)",
        r"(?:界面新闻|给你挑了|小提示|选购小贴士|一句话|注意|提醒)",
        r"(?:价格|图片|品牌|怎么样).{0,12}(?:京东|淘宝|商城|网易)",
        r"(?:清单|指南|排行榜|榜单|红黑榜|选购|科普)",
    )
    return any(re.search(pattern, text) for pattern in noisy_patterns)


RECOMMEND_QUESTION_HINTS = (
    "推荐", "怎么选", "如何选", "哪款", "哪个牌子", "什么牌子",
    "排行榜", "排行", "榜单", "清单", "合集", "对比", "选购",
)
NON_RECOMMEND_QUESTION_HINTS = (
    "怎么样", "评价", "评测", "安全吗", "安全么", "好用吗", "好不好",
    "成分安全吗", "成分安全", "靠谱吗", "是不是",
)

# These questions have complete reference data, but the listed historical
# product rows were generated before the product extractor stabilized. Exclude
# only those old product/source-run slices; future clean rounds for the same
# questions remain visible.
PRODUCT_STATS_EXCLUDED_QUESTION_MAX_RUN = {}

# Raw capture is preserved for audit, but these source rows belong to a second,
# unrelated answer that was accidentally concatenated into the same page grab.
QUARANTINED_SOURCE_INDEX_FROM_RUN = {
    6354: 13,  # 护发精油正文后混入“成都龙泉露营地”信源 13-21
}


def is_quarantined_source_row(row):
    run_no = safe_int((row or {}).get("run_no"))
    first_bad_index = QUARANTINED_SOURCE_INDEX_FROM_RUN.get(run_no)
    return bool(first_bad_index and safe_int((row or {}).get("index")) >= first_bad_index)


def is_excluded_product_stat_row(row, question):
    max_run = PRODUCT_STATS_EXCLUDED_QUESTION_MAX_RUN.get(str(question or "").strip())
    if not max_run:
        return False
    run_no = safe_int(row.get("run_no"))
    return bool(run_no and run_no <= max_run)


def is_recommendation_question(question):
    text = str(question or "").strip()
    if not text:
        return False
    if any(hint in text for hint in RECOMMEND_QUESTION_HINTS):
        return True
    if any(hint in text for hint in NON_RECOMMEND_QUESTION_HINTS):
        return False
    return False


def is_ai_verified_product_row(row):
    """Optionally keep legacy/rule-only extraction out of product statistics."""
    # Show historical records by default.  They remain identifiable by their
    # empty/non-ai review_status and can be hidden at any time by setting this
    # environment variable to 1.
    strict = os.environ.get("DOUBAO_DASHBOARD_AI_VERIFIED_ONLY", "0").strip().lower()
    if strict in ("0", "false", "no"):
        return True
    return str(row.get("review_status") or "").strip() == "ai_verified"


def dedupe_product_rows_for_stats(product_rows):
    """Keep one model snapshot and one logical product per run.

    Foreground capture and the retry worker can occasionally finish the same
    answer a few seconds apart.  Historical files may therefore contain two
    complete snapshots whose product names differ only by spacing.  Product
    and brand counts must still be bounded by the number of unique runs.
    """
    latest_snapshot = {}
    for row in product_rows:
        run_no = safe_int(row.get("run_no"))
        answer_hash = str(row.get("answer_hash") or "").strip()
        if not run_no or not answer_hash:
            continue
        key = (run_no, answer_hash)
        reviewed_at = str(row.get("reviewed_at") or "")
        if reviewed_at >= latest_snapshot.get(key, ""):
            latest_snapshot[key] = reviewed_at

    result = []
    seen = set()
    for row in product_rows:
        run_no = safe_int(row.get("run_no"))
        answer_hash = str(row.get("answer_hash") or "").strip()
        snapshot_key = (run_no, answer_hash)
        reviewed_at = str(row.get("reviewed_at") or "")
        if run_no and answer_hash and reviewed_at != latest_snapshot.get(snapshot_key, reviewed_at):
            continue

        normalized = normalize_product_for_stats(row.get("product_name") or "")
        logical_product = re.sub(r"[\s\-_—·]+", "", normalized).casefold()
        logical_key = (
            run_no,
            answer_hash,
            logical_product,
            safe_int(row.get("product_index")),
        )
        if logical_key in seen:
            continue
        seen.add(logical_key)
        result.append(row)
    return result


def product_stats(product_rows, selected_question=ALL_QUESTIONS, source_rows=None):
    selected_question = (selected_question or ALL_QUESTIONS).strip()
    product_rows = dedupe_product_rows_for_stats(product_rows)
    source_runs_by_question = defaultdict(set)
    source_run_days_by_question = defaultdict(dict)
    for src in (source_rows or []):
        src_question = question_for(src)
        if is_excluded_product_stat_row(src, src_question):
            continue
        if not is_recommendation_question(src_question):
            continue
        if selected_question != ALL_QUESTIONS and src_question != selected_question:
            continue
        run_no = safe_int(src.get("run_no"))
        if run_no:
            source_runs_by_question[src_question].add(run_no)
            source_run_days_by_question[src_question][run_no] = date_for(src)

    rows = []
    unverified_rows = 0
    for row in product_rows:
        question = product_question_for(row)
        if is_excluded_product_stat_row(row, question):
            continue
        if not is_recommendation_question(question):
            continue
        if selected_question != ALL_QUESTIONS and question != selected_question:
            continue
        if not is_ai_verified_product_row(row):
            unverified_rows += 1
            continue
        raw_product = str(row.get("product_name") or "").strip()
        product = normalize_product_for_stats(raw_product)
        if not product or is_noisy_product_name(product):
            continue
        copied = dict(row)
        copied["_question"] = question
        copied["_product"] = product
        rows.append(copied)

    by_product = Counter()
    by_brand = Counter()
    product_ranks = defaultdict(list)
    brand_ranks = defaultdict(list)
    product_runs = defaultdict(set)
    brand_runs = defaultdict(set)
    by_question = {}
    runs = {}
    latest_run_no = 0
    latest_rows = []
    global_brand_seen = set()
    ordered_rows = sorted(rows, key=lambda row: (
        safe_int(row.get("run_no")),
        safe_int(row.get("product_index")) or 9999,
    ))
    for row in ordered_rows:
        product = row["_product"]
        brand = brand_for_row(row, product)
        question = row["_question"]
        rank = safe_int(row.get("product_index"))
        run_no = safe_int(row.get("run_no"))
        by_product[product] += 1
        if run_no:
            product_runs[product].add(run_no)
        if rank:
            product_ranks[product].append(rank)
        if brand:
            brand_run_key = (brand, run_no) if run_no else (brand, id(row))
            if brand_run_key not in global_brand_seen:
                global_brand_seen.add(brand_run_key)
                by_brand[brand] += 1
                if run_no:
                    brand_runs[brand].add(run_no)
                if rank:
                    brand_ranks[brand].append(rank)
        bucket = by_question.setdefault(question, {
            "question": question,
            "mentions": 0,
            "products": Counter(),
            "brands": Counter(),
            "product_ranks": defaultdict(list),
            "brand_ranks": defaultdict(list),
            "product_runs": defaultdict(set),
            "brand_runs": defaultdict(set),
            "brand_seen": set(),
            "runs": set(),
        })
        bucket["mentions"] += 1
        bucket["products"][product] += 1
        if run_no:
            bucket["product_runs"][product].add(run_no)
        if rank:
            bucket["product_ranks"][product].append(rank)
        if brand:
            brand_run_key = (brand, run_no) if run_no else (brand, id(row))
            if brand_run_key not in bucket["brand_seen"]:
                bucket["brand_seen"].add(brand_run_key)
                bucket["brands"][brand] += 1
                if run_no:
                    bucket["brand_runs"][brand].add(run_no)
                if rank:
                    bucket["brand_ranks"][brand].append(rank)
        if run_no:
            bucket["runs"].add(run_no)
            runs.setdefault(run_no, []).append(row)
            if run_no > latest_run_no:
                latest_run_no = run_no

    latest_rows = runs.get(latest_run_no, [])
    per_question = []
    for item in by_question.values():
        run_count = len(item["runs"])
        source_run_count = len(source_runs_by_question.get(item["question"], set())) or run_count
        source_days = [day for day in source_run_days_by_question.get(item["question"], {}).values() if day]
        today_date = max(source_days) if source_days else ""
        today_runs = sum(1 for day in source_days if day == today_date)
        per_question.append({
            "question": item["question"],
            "mentions": item["mentions"],
            "unique_products": len(item["products"]),
            "runs": run_count,
            "source_runs": source_run_count,
            "today_date": today_date,
            "today_runs": today_runs,
            "avg_per_run": round(item["mentions"] / run_count, 2) if run_count else 0,
            "top_products": rank_counter_items(
                item["products"], item["product_ranks"],
                runs_by_name=item["product_runs"], total_runs=run_count,
            ),
            "top_brands": rank_counter_items(
                item["brands"], item["brand_ranks"],
                runs_by_name=item["brand_runs"], total_runs=run_count,
            ),
        })

    latest_products = []
    for row in sorted(latest_rows, key=lambda r: safe_int(r.get("product_index"))):
        latest_products.append({
            "run_no": row.get("run_no", ""),
            "question": row.get("_question", ""),
            "product_index": row.get("product_index", ""),
            "product_name": row.get("_product", ""),
            "brand_name": brand_for_row(row, row.get("_product", "")),
            "evidence": row.get("evidence", ""),
            "run_time": row.get("run_time", ""),
            "review_status": row.get("review_status", ""),
            "extraction_method": row.get("extraction_method", ""),
        })

    return {
        "total_mentions": len(rows),
        "unverified_rows_excluded": unverified_rows,
        "total_product_runs": len(runs),
        "unique_products": len(by_product),
        "unique_brands": len(by_brand),
        "latest_product_run_no": latest_run_no,
        "latest_products": latest_products,
        "by_brand": rank_counter_items(
            by_brand, brand_ranks, runs_by_name=brand_runs, total_runs=len(runs),
        ),
        "by_product": rank_counter_items(
            by_product, product_ranks, runs_by_name=product_runs, total_runs=len(runs),
        ),
        "by_question": sorted(per_question, key=lambda x: (x["mentions"], x["unique_products"]), reverse=True),
    }


def daily_question_product_breakdown(
    product_rows, source_rows=None, selected_question=ALL_QUESTIONS, answer_rows=None
):
    selected_question = (selected_question or ALL_QUESTIONS).strip()
    product_rows = dedupe_product_rows_for_stats(product_rows)
    buckets = {}
    all_dates = set()

    def bucket_for(question):
        return buckets.setdefault(question, {
            "question": question,
            "mentions_by_date": Counter(),
            "runs_by_date": {},
            "brand_by_date": {},
            "product_by_date": {},
            "brand_seen_by_date": {},
            "product_seen_by_date": {},
        })

    # Index source runs first. We apply them only to question/date buckets that
    # have product data, so intentionally cleared historical product dates do
    # not reappear as empty columns.
    source_runs_by_question_date = {}
    # An archived answer is the authoritative record that a question actually
    # ran. Source rows are not a safe denominator because a completed answer
    # can have zero links, or its source write may need later repair.
    denominator_rows = answer_rows if answer_rows is not None else source_rows
    for row in denominator_rows or []:
        question = question_for(row)
        if not is_recommendation_question(question):
            continue
        if selected_question != ALL_QUESTIONS and question != selected_question:
            continue
        run_no = safe_int(row.get("run_no"))
        day = date_for(row)
        if not run_no or not day:
            continue
        source_runs_by_question_date.setdefault((question, day), set()).add(run_no)

    for row in product_rows:
        question = product_question_for(row)
        if is_excluded_product_stat_row(row, question):
            continue
        if not is_recommendation_question(question):
            continue
        if selected_question != ALL_QUESTIONS and question != selected_question:
            continue
        if not is_ai_verified_product_row(row):
            continue
        raw_product = str(row.get("product_name") or "").strip()
        product = normalize_product_for_stats(raw_product)
        if not product or is_noisy_product_name(product):
            continue
        brand = brand_for_row(row, product)
        day = date_for(row)
        all_dates.add(day)
        bucket = bucket_for(question)
        run_no = safe_int(row.get("run_no"))
        bucket["mentions_by_date"][day] += 1
        bucket["runs_by_date"].setdefault(day, set()).add(run_no)
        if brand:
            brand_seen = bucket["brand_seen_by_date"].setdefault(day, set())
            brand_key = (run_no, brand)
            if brand_key not in brand_seen:
                brand_seen.add(brand_key)
                bucket["brand_by_date"].setdefault(day, Counter())[brand] += 1
        product_seen = bucket["product_seen_by_date"].setdefault(day, set())
        product_key = (run_no, re.sub(r"[\s\-_—·]+", "", product).casefold())
        if product_key not in product_seen:
            product_seen.add(product_key)
            bucket["product_by_date"].setdefault(day, Counter())[product] += 1

    # "运行轮次" means every capture run for this question, including a run
    # whose product review is still pending. Product rows alone undercount the
    # denominator while the background AI worker is catching up.
    for question, bucket in buckets.items():
        for day in list(bucket["runs_by_date"]):
            source_runs = source_runs_by_question_date.get((question, day))
            if source_runs:
                bucket["runs_by_date"][day] = set(source_runs)

    dates = sorted(all_dates)

    def matrix_rows(names, by_date, totals_by_date, limit=None):
        ranks_by_date = {}
        for day in dates:
            counter = by_date.get(day, Counter())
            ordered = sorted(counter.items(), key=lambda item: (-item[1], item[0]))
            ranks = {}
            previous_count = None
            current_rank = 0
            for position, (name, count) in enumerate(ordered, 1):
                if count != previous_count:
                    current_rank = position
                    previous_count = count
                ranks[name] = current_rank
            ranks_by_date[day] = ranks
        built = []
        for name in names:
            counts = [by_date.get(day, Counter()).get(name, 0) for day in dates]
            ranks = [ranks_by_date.get(day, {}).get(name) if count else None for day, count in zip(dates, counts)]
            total = sum(counts)
            if not total:
                continue
            latest = counts[-1] if counts else 0
            prev = counts[-2] if len(counts) >= 2 else 0
            latest_total = totals_by_date.get(dates[-1], 0) if dates else 0
            prev_total = totals_by_date.get(dates[-2], 0) if len(dates) >= 2 else 0
            latest_pct = latest / latest_total * 100 if latest_total else 0
            prev_pct = prev / prev_total * 100 if prev_total else 0
            built.append({
                "name": name,
                "total": total,
                "delta": latest - prev,
                "pct_delta": round(latest_pct - prev_pct, 2),
                "counts": counts,
                "ranks": ranks,
                "latest_rank": ranks[-1] if ranks else None,
                "previous_rank": ranks[-2] if len(ranks) >= 2 else None,
            })
        built = sorted(built, key=lambda item: (item["total"], item["name"]), reverse=True)
        return built[:limit] if limit else built

    result = []
    for bucket in buckets.values():
        brand_names = set()
        product_names = set()
        for counter in bucket["brand_by_date"].values():
            brand_names.update(counter.keys())
        for counter in bucket["product_by_date"].values():
            product_names.update(counter.keys())
        mentions_by_date = bucket["mentions_by_date"]
        run_totals_by_date = Counter({
            day: len(bucket["runs_by_date"].get(day, set()))
            for day in dates
        })
        result.append({
            "question": bucket["question"],
            "dates": dates,
            "mentions_by_date": [mentions_by_date.get(day, 0) for day in dates],
            "runs_by_date": [len(bucket["runs_by_date"].get(day, set())) for day in dates],
            "brand_rows": matrix_rows(brand_names, bucket["brand_by_date"], run_totals_by_date),
            "product_rows": matrix_rows(product_names, bucket["product_by_date"], run_totals_by_date, limit=80),
        })
    return sorted(result, key=lambda item: sum(item["mentions_by_date"]), reverse=True)


def is_terminal_answer_review(row):
    status = str((row or {}).get("review_status") or "").strip().casefold()
    return status.startswith("ai_verified") or status in {
        "rule_unverified", "verified", "verified_no_products",
        "no_products", "confirmed_no_products",
    }


def brand_source_daily_analytics(
    product_rows, source_rows, answer_rows, ai_cache, meta_cache, content_index,
    selected_question=ALL_QUESTIONS,
    global_source_brands=None,
):
    """Build a run-aligned daily fact table for trend/correlation charts.

    Answers define whether a run occurred and whether product review finished.
    Product rows supply canonical brand outcomes. Source rows are joined by
    run_no. Correlation metrics therefore use only reviewed runs that also have
    at least one archived source row; missing source capture is never filled as
    a zero.
    """
    selected_question = (selected_question or ALL_QUESTIONS).strip()
    configured_brand_groups = {
        canonical_brand_name(item["name"]): item["group"]
        for item in brand_settings.vocabulary()
        if canonical_brand_name(item.get("name"))
    }
    content_entries = (
        content_index.get("entries", {})
        if isinstance(content_index, dict) and isinstance(content_index.get("entries"), dict)
        else {}
    )

    def content_entry_for(source):
        href = str(source.get("href") or "").strip()
        if not href:
            return {}
        entry = content_entries.get(href)
        if isinstance(entry, dict):
            return entry
        canonical = canonical_source_url(href)
        entry = content_entries.get(canonical)
        return entry if isinstance(entry, dict) else {}
    if selected_question == ALL_QUESTIONS:
        return {
            "status": "select_question",
            "question": selected_question,
            "timezone": "+08:00",
            "days": [],
            "brands": [],
            "warning": "请选择单个产品问题；跨品类汇总会造成相关性混杂。",
        }

    authoritative_answers = {}
    for row in answer_rows or []:
        question = question_for(row)
        if question != selected_question or not is_recommendation_question(question):
            continue
        if is_excluded_product_stat_row(row, question):
            continue
        run_no = safe_int(row.get("run_no"))
        if not run_no:
            continue
        marker = (
            str(row.get("reviewed_at") or ""),
            str(row.get("extracted_at") or ""),
            str(row.get("answer_hash") or ""),
        )
        previous = authoritative_answers.get(run_no)
        if previous is None or marker >= previous[0]:
            authoritative_answers[run_no] = (marker, row)

    answers_by_run = {run_no: value[1] for run_no, value in authoritative_answers.items()}
    if not answers_by_run:
        return {
            "status": "no_answers",
            "question": selected_question,
            "timezone": "+08:00",
            "days": [],
            "brands": [],
            "warning": "该问题暂无可归档答案，不能计算品牌提及率。",
        }

    reviewed_runs = {
        run_no for run_no, row in answers_by_run.items() if is_terminal_answer_review(row)
    }
    question_products = [
        row for row in (product_rows or [])
        if product_question_for(row) == selected_question
        and not is_excluded_product_stat_row(row, selected_question)
    ]
    # This view is always scoped to one category.  Deduplicating the entire
    # product history here made a first visit to a small category scan tens of
    # thousands of unrelated rows before it could draw the chart.
    deduped_products = dedupe_product_rows_for_stats(question_products)
    source_brand_candidates = set(KNOWN_BRANDS)
    source_brand_candidates.update(global_source_brands or ())
    # Explicit model-confirmed brand names from other categories are cheap to
    # reuse as title-detection vocabulary.  This preserves discovery of a
    # brand that appears only in today's source titles without invoking the
    # slower product-name inference across the whole history.
    if global_source_brands is None:
        for candidate_row in product_rows or []:
            explicit_brand = str(candidate_row.get("brand_name") or "").strip()
            if explicit_brand and not is_invalid_brand_candidate(explicit_brand):
                source_brand_candidates.add(canonical_brand_name(explicit_brand))
    for candidate_row in deduped_products:
        candidate_product = normalize_product_for_stats(candidate_row.get("product_name") or "")
        if not candidate_product or is_noisy_product_name(candidate_product):
            continue
        candidate_brand = brand_for_row(candidate_row, candidate_product)
        if candidate_brand:
            source_brand_candidates.add(candidate_brand)

    brand_by_run = defaultdict(set)
    rank_by_run_brand = {}
    for row in deduped_products:
        question = product_question_for(row)
        run_no = safe_int(row.get("run_no"))
        answer = answers_by_run.get(run_no)
        if not answer or run_no not in reviewed_runs:
            continue
        answer_hash = str(answer.get("answer_hash") or "").strip()
        product_hash = str(row.get("answer_hash") or "").strip()
        if answer_hash and product_hash and answer_hash != product_hash:
            continue
        review_status = str(row.get("review_status") or "").strip().casefold()
        if review_status and not (
            review_status.startswith("ai_verified")
            or review_status == "rule_unverified"
        ):
            continue
        product = normalize_product_for_stats(row.get("product_name") or "")
        if not product or is_noisy_product_name(product):
            continue
        brand = brand_for_row(row, product)
        if not brand:
            continue
        brand_by_run[run_no].add(brand)
        rank = safe_int(row.get("product_index"))
        key = (run_no, brand)
        if rank and (key not in rank_by_run_brand or rank < rank_by_run_brand[key]):
            rank_by_run_brand[key] = rank

    sources_by_run = defaultdict(list)
    sources_by_day = defaultdict(list)
    for row in source_rows or []:
        question = question_for(row)
        run_no = safe_int(row.get("run_no"))
        if question != selected_question or not run_no or run_no not in answers_by_run:
            continue
        if is_excluded_product_stat_row(row, question) or is_quarantined_source_row(row):
            continue
        sources_by_run[run_no].append(row)
        source_day = date_for(row)
        if source_day and source_day != "未知日期":
            sources_by_day[source_day].append((run_no, row))

    # Most categories mention only a small fraction of the confirmed global
    # brand vocabulary.  A cheap C-level substring prefilter keeps the exact
    # per-title boundary matching below from becoming O(all titles × every
    # historical brand), which was slow for categories with ~10k references.
    title_blob = "\0".join(
        str(source.get("title") or "").casefold()
        for day_sources in sources_by_day.values()
        for _run_no, source in day_sources
    )
    compact_title_blob = re.sub(r"[\s\-_—·&＆'’]+", "", title_blob)
    possible_source_brands = set()
    for brand in source_brand_candidates:
        for alias in aliases_for_brand(brand):
            alias_folded = str(alias or "").casefold()
            if not alias_folded:
                continue
            if re.search(r"[\u3400-\u9fff]", alias_folded):
                possible = re.sub(r"[\s\-_—·&＆'’]+", "", alias_folded) in compact_title_blob
            else:
                possible = alias_folded in title_blob
            if possible:
                possible_source_brands.add(brand)
                break
    # The body crawler stores canonical brand hits in a lightweight index.
    # Include those hits in the candidate set so a brand that is absent from
    # every title can still be discovered from article/video content.
    for day_sources in sources_by_day.values():
        for _run_no, source in day_sources:
            entry = content_entry_for(source)
            if entry.get("status") != "ok":
                continue
            if entry.get("extraction_quality") not in ("high", "medium"):
                continue
            for brand in entry.get("brand_mentions") or []:
                canonical = canonical_brand_name(brand)
                if canonical:
                    possible_source_brands.add(canonical)
    source_brand_candidates = possible_source_brands

    title_brand_matches = {}
    content_brand_matches = {}
    source_detected_brands = set()
    for day_sources in sources_by_day.values():
        for _run_no, source in day_sources:
            title = str(source.get("title") or "")
            matches = {
                brand for brand in source_brand_candidates
                if title_mentions_brand(title, brand)
            }
            title_brand_matches[id(source)] = matches
            entry = content_entry_for(source)
            content_matches = set()
            if (
                entry.get("status") == "ok"
                and entry.get("extraction_quality") in ("high", "medium")
            ):
                content_matches = {
                    canonical_brand_name(brand)
                    for brand in (entry.get("brand_mentions") or [])
                    if canonical_brand_name(brand)
                }
            content_brand_matches[id(source)] = content_matches
            source_detected_brands.update(matches | content_matches)

    buckets = {}

    def bucket_for(day):
        return buckets.setdefault(day, {
            "observed_runs": set(),
            "reviewed_runs": set(),
            "source_observed_runs": set(),
            "aligned_runs": set(),
            "brand_mentions": Counter(),
            "aligned_brand_mentions": Counter(),
            "brand_ranks": defaultdict(list),
            "source_rows": [],
            "source_types": Counter(),
            "title_available_refs": 0,
            "content_available_refs": 0,
            "content_pending_refs": 0,
            "content_failed_refs": 0,
            "urls": set(),
        })

    for run_no, answer in answers_by_run.items():
        day = date_for(answer)
        if not day or day == "未知日期":
            continue
        bucket = bucket_for(day)
        bucket["observed_runs"].add(run_no)
        if run_no not in reviewed_runs:
            continue
        bucket["reviewed_runs"].add(run_no)
        for brand in brand_by_run.get(run_no, set()):
            bucket["brand_mentions"][brand] += 1
            rank = rank_by_run_brand.get((run_no, brand))
            if rank:
                bucket["brand_ranks"][brand].append(rank)
        run_sources = sources_by_run.get(run_no, [])
        if not run_sources:
            continue
        bucket["aligned_runs"].add(run_no)
        for brand in brand_by_run.get(run_no, set()):
            bucket["aligned_brand_mentions"][brand] += 1

    source_kind_by_id = {}
    for day, day_sources in sources_by_day.items():
        bucket = bucket_for(day)
        for run_no, source in day_sources:
            bucket["source_observed_runs"].add(run_no)
            bucket["source_rows"].append((run_no, source))
            source_type, _media, _host, _note = source_for(source, ai_cache, meta_cache)
            if "视频" in source_type:
                type_key = "video"
            elif "文章" in source_type:
                type_key = "article"
            elif "商品" in source_type:
                type_key = "product"
            else:
                type_key = "other"
            source_kind_by_id[id(source)] = type_key
            bucket["source_types"][type_key] += 1
            if str(source.get("title") or "").strip():
                bucket["title_available_refs"] += 1
            entry = content_entry_for(source)
            if (
                entry.get("status") == "ok"
                and entry.get("extraction_quality") in ("high", "medium")
            ):
                bucket["content_available_refs"] += 1
            elif entry.get("status") in ("blocked", "error", "empty", "unsupported"):
                bucket["content_failed_refs"] += 1
            else:
                bucket["content_pending_refs"] += 1
            canonical_url = canonical_source_url(source.get("href"))
            if canonical_url:
                bucket["urls"].add(canonical_url)

    # A relationship chart requires a product-answer denominator.  Historical
    # source-only days remain preserved in the raw source dashboard, but are
    # not injected here as empty product-rate dates.
    dates = sorted(
        day for day, bucket in buckets.items()
        if bucket["observed_runs"]
    )
    all_brands = set()
    for bucket in buckets.values():
        all_brands.update(bucket["brand_mentions"])
    all_brands.update(source_detected_brands)

    # Calculate ranks independently within each day. Equal counts share rank.
    ranks_by_day = {}
    for day in dates:
        ordered = sorted(
            buckets[day]["brand_mentions"].items(),
            key=lambda item: (-item[1], item[0]),
        )
        rank_map = {}
        previous_count = None
        current_rank = 0
        for position, (brand, count) in enumerate(ordered, 1):
            if count != previous_count:
                current_rank = position
                previous_count = count
            rank_map[brand] = current_rank
        ranks_by_day[day] = rank_map

    today_cst = datetime.now(CST).strftime("%Y-%m-%d")
    seen_urls = set()
    day_rows = []
    brand_source_facts = defaultdict(dict)
    source_examples_by_brand = defaultdict(dict)
    for index, day in enumerate(dates):
        bucket = buckets[day]
        observed = len(bucket["observed_runs"])
        reviewed = len(bucket["reviewed_runs"])
        source_observed = len(bucket["source_observed_runs"])
        source_answer_runs = len(bucket["source_observed_runs"] & bucket["observed_runs"])
        source_orphan_runs = len(bucket["source_observed_runs"] - bucket["observed_runs"])
        aligned = len(bucket["aligned_runs"])
        refs = len(bucket["source_rows"])
        urls = set(bucket["urls"])
        if index == 0:
            new_url_count = None
            new_url_share = None
        else:
            new_url_count = len(urls - seen_urls)
            new_url_share = (new_url_count / len(urls)) if urls else None
        seen_urls.update(urls)
        review_coverage = (reviewed / observed) if observed else None
        source_coverage = (source_answer_runs / observed) if observed else None
        if day == today_cst:
            status = "partial"
        elif not reviewed:
            status = "data_unavailable"
        elif review_coverage is not None and review_coverage < 0.999:
            status = "incomplete"
        elif source_coverage is None or source_coverage < 0.95:
            status = "incomplete"
        else:
            status = "closed"
        type_total = sum(bucket["source_types"].values())
        day_rows.append({
            "date": day,
            "status": status,
            "correlation_eligible": status == "closed" and aligned > 0,
            "observed_runs": observed,
            "reviewed_runs": reviewed,
            "review_coverage": review_coverage,
            "source_observed_runs": source_observed,
            "source_answer_runs": source_answer_runs,
            "source_orphan_runs": source_orphan_runs,
            "source_coverage": source_coverage,
            "aligned_runs": aligned,
            "refs": refs,
            "title_available_refs": bucket["title_available_refs"],
            "title_available_coverage": (
                bucket["title_available_refs"] / refs
            ) if refs else None,
            "content_available_refs": bucket["content_available_refs"],
            "content_fetch_coverage": (
                bucket["content_available_refs"] / refs
            ) if refs else None,
            "content_pending_refs": bucket["content_pending_refs"],
            "content_failed_refs": bucket["content_failed_refs"],
            "unique_urls": len(urls),
            "refs_per_run": (refs / source_observed) if source_observed else None,
            "unique_urls_per_run": (len(urls) / source_observed) if source_observed else None,
            "new_url_count": new_url_count,
            "new_url_share": new_url_share,
            "video_refs": bucket["source_types"]["video"],
            "article_refs": bucket["source_types"]["article"],
            "product_refs": bucket["source_types"]["product"],
            "other_refs": bucket["source_types"]["other"],
            "video_share": (bucket["source_types"]["video"] / type_total) if type_total else None,
            "article_share": (bucket["source_types"]["article"] / type_total) if type_total else None,
            "product_share": (bucket["source_types"]["product"] / type_total) if type_total else None,
            "other_share": (bucket["source_types"]["other"] / type_total) if type_total else None,
        })

        for brand in all_brands:
            title_matching_rows = [
                (run_no, source)
                for run_no, source in bucket["source_rows"]
                if brand in title_brand_matches.get(id(source), set())
            ]
            content_matching_rows = [
                (run_no, source)
                for run_no, source in bucket["source_rows"]
                if brand in content_brand_matches.get(id(source), set())
            ]
            content_only_rows = [
                (run_no, source)
                for run_no, source in content_matching_rows
                if brand not in title_brand_matches.get(id(source), set())
            ]
            matching_rows = [
                (run_no, source)
                for run_no, source in bucket["source_rows"]
                if (
                    brand in title_brand_matches.get(id(source), set())
                    or brand in content_brand_matches.get(id(source), set())
                )
            ]
            article_matching_rows = [
                item for item in title_matching_rows
                if source_kind_by_id.get(id(item[1])) == "article"
            ]
            video_matching_rows = [
                item for item in title_matching_rows
                if source_kind_by_id.get(id(item[1])) == "video"
            ]
            av_matching_rows = article_matching_rows + video_matching_rows
            source_article_rows = [
                item for item in matching_rows
                if source_kind_by_id.get(id(item[1])) == "article"
            ]
            source_video_rows = [
                item for item in matching_rows
                if source_kind_by_id.get(id(item[1])) == "video"
            ]
            source_av_rows = source_article_rows + source_video_rows
            matching_runs = {run_no for run_no, _source in matching_rows}
            matching_urls = {
                canonical_source_url(source.get("href"))
                for _run_no, source in matching_rows
                if canonical_source_url(source.get("href"))
            }
            for run_no, source in matching_rows:
                href = str(source.get("href") or "").strip()
                canonical_url = canonical_source_url(href) or href
                if not canonical_url:
                    continue
                example = source_examples_by_brand[brand].setdefault(
                    canonical_url,
                    {
                        "href": href,
                        "title": str(source.get("title") or "").strip(),
                        "source_type": source_kind_by_id.get(id(source), "other"),
                        "refs": 0,
                        "runs": set(),
                        "dates": set(),
                        "title_hit": False,
                        "content_hit": False,
                    },
                )
                example["refs"] += 1
                if run_no:
                    example["runs"].add(run_no)
                example["dates"].add(day)
                example["title_hit"] = (
                    example["title_hit"]
                    or brand in title_brand_matches.get(id(source), set())
                )
                example["content_hit"] = (
                    example["content_hit"]
                    or brand in content_brand_matches.get(id(source), set())
                )
                if not example["title"] and str(source.get("title") or "").strip():
                    example["title"] = str(source.get("title") or "").strip()
            brand_source_facts[brand][day] = {
                "source_title_runs": len(matching_runs),
                "source_title_coverage": (
                    len(matching_runs) / source_observed
                ) if source_observed else None,
                "source_title_refs": len(matching_rows),
                "source_title_unique_urls": len(matching_urls),
                "title_article_refs": len(article_matching_rows),
                "title_video_refs": len(video_matching_rows),
                "title_av_refs": len(av_matching_rows),
                "title_article_ref_share": (
                    len(article_matching_rows) / refs
                ) if refs else None,
                "title_video_ref_share": (
                    len(video_matching_rows) / refs
                ) if refs else None,
                "title_av_ref_share": (
                    len(av_matching_rows) / refs
                ) if refs else None,
                "content_refs": len(content_matching_rows),
                "content_only_refs": len(content_only_rows),
                "content_ref_share": (
                    len(content_matching_rows) / refs
                ) if refs else None,
                "content_only_ref_share": (
                    len(content_only_rows) / refs
                ) if refs else None,
                "source_article_refs": len(source_article_rows),
                "source_video_refs": len(source_video_rows),
                "source_av_refs": len(source_av_rows),
                "source_article_ref_share": (
                    len(source_article_rows) / refs
                ) if refs else None,
                "source_video_ref_share": (
                    len(source_video_rows) / refs
                ) if refs else None,
                "source_av_ref_share": (
                    len(source_av_rows) / refs
                ) if refs else None,
                "title_article_within_type_share": (
                    len(article_matching_rows) / bucket["source_types"]["article"]
                ) if bucket["source_types"]["article"] else None,
                "title_video_within_type_share": (
                    len(video_matching_rows) / bucket["source_types"]["video"]
                ) if bucket["source_types"]["video"] else None,
            }

    brands = []
    for brand in all_brands:
        points = []
        total_mentions = 0
        for day, day_info in zip(dates, day_rows):
            bucket = buckets[day]
            mentioned = bucket["brand_mentions"].get(brand, 0)
            aligned_mentioned = bucket["aligned_brand_mentions"].get(brand, 0)
            reviewed = day_info["reviewed_runs"]
            aligned = day_info["aligned_runs"]
            total_mentions += mentioned
            source_fact = brand_source_facts[brand].get(day, {})
            points.append({
                "date": day,
                "mentioned_runs": mentioned,
                "denominator_runs": reviewed,
                "rate": (mentioned / reviewed) if reviewed else None,
                "aligned_mentioned_runs": aligned_mentioned,
                "aligned_runs": aligned,
                "aligned_rate": (aligned_mentioned / aligned) if aligned else None,
                "rank": ranks_by_day.get(day, {}).get(brand),
                "avg_rank": (
                    sum(bucket["brand_ranks"].get(brand, [])) /
                    len(bucket["brand_ranks"].get(brand, []))
                ) if bucket["brand_ranks"].get(brand) else None,
                **source_fact,
            })
        source_examples = []
        for example in source_examples_by_brand.get(brand, {}).values():
            if example["title_hit"] and example["content_hit"]:
                scope = "标题+正文"
            elif example["content_hit"]:
                scope = "正文"
            else:
                scope = "标题"
            source_examples.append({
                "href": example["href"],
                "title": example["title"] or example["href"],
                "source_type": example["source_type"],
                "refs": example["refs"],
                "runs": len(example["runs"]),
                "first_date": min(example["dates"]) if example["dates"] else "",
                "latest_date": max(example["dates"]) if example["dates"] else "",
                "scope": scope,
            })
        source_examples.sort(
            key=lambda item: (
                -item["refs"],
                -item["runs"],
                item["title"].casefold(),
            )
        )
        brands.append({
            "name": brand,
            "group": configured_brand_groups.get(brand, "other"),
            "total_mentioned_runs": total_mentions,
            "points": points,
            "source_examples": source_examples[:80],
        })
    brands.sort(key=lambda item: (-item["total_mentioned_runs"], item["name"]))

    closed_days = sum(1 for day in day_rows if day["status"] == "closed")
    return {
        "status": "ok",
        "question": selected_question,
        "timezone": "+08:00",
        "generated_at": beijing_now(),
        "date_min": dates[0] if dates else "",
        "date_max": dates[-1] if dates else "",
        "closed_days": closed_days,
        "days": day_rows,
        "brands": brands,
        "brand_settings": brand_settings.load_settings(),
        "definitions": {
            "source_av_ref_share": "当日文章或视频的标题、正文、页面描述任一处命中所选品牌的信源行数 ÷ 当日该品类全部信源行数；同一链接跨轮重复出现仍逐行计数。正文尚未成功归档的链接不当作未命中。",
            "content_only_ref_share": "标题未命中、但正文或视频页面描述命中所选品牌的信源行数 ÷ 当日该品类全部信源行数。",
            "content_fetch_coverage": "正文或视频页面描述已成功归档且质量达到中/高的信源行数 ÷ 当日全部信源行数。正文相关性分析仅纳入归档覆盖率达到95%的完整日。",
            "mention_rate": "结构化产品品牌出现轮次÷当日已完成AI商品审核的答案轮次；仅供旧版产品审核分析，新版回答品牌率直接按全部有效回答正文计算。",
            "title_av_ref_share": "当日文章或视频标题命中所选品牌的信源链接行数÷当日该品类全部信源链接行数；同一URL跨轮再次出现会再次计数。",
            "source_title_coverage": "标题中可识别该品牌的信源出现轮次÷当日有信源的运行轮次；这是标题可见下限，不等于正文或视频内容命中。",
            "correlation": "比较当日品牌产品提及率与当日标题命中信源份额；属于跨日聚合相关，不证明标题命中导致品牌被推荐。",
            "title_visibility": "空标题保留在全部信源分母中但不能命中；页面同时展示标题可用覆盖率。",
        },
        "warning": (
            "完整日少于7天时，相关系数只作描述，不生成策略结论。"
            if closed_days < 7 else ""
        ),
    }


def _owned_title_tokens(title):
    text = str(title or "").strip().casefold()
    if not text:
        return []
    tokens = []
    for phrase in OWN_TITLE_KEYWORD_PHRASES:
        folded = phrase.casefold()
        if folded in text and folded not in OWN_TITLE_KEYWORD_STOPWORDS:
            tokens.append(phrase)
    own_brands = ("梵玢", "科熙本", "姿生怡", "道和", "焕颜计", "茗媛萃")
    for token in re.findall(r"[#＃]([^#＃\s，。！？、；：,.;:!?]{2,20})", text):
        token = token.strip()
        if (
            token
            and token not in OWN_TITLE_KEYWORD_STOPWORDS
            and not any(brand.casefold() in token for brand in own_brands)
        ):
            tokens.append(token)
    for token in re.findall(r"\b[a-z][a-z0-9+.-]{1,18}\b", text):
        if token not in OWN_TITLE_KEYWORD_STOPWORDS and token not in {"http", "https", "www"}:
            tokens.append(token.upper() if len(token) <= 5 else token)
    return tokens


def _owned_title_themes(title):
    text = str(title or "").strip().casefold()
    return [
        theme
        for theme, patterns in OWN_TITLE_THEME_PATTERNS.items()
        if any(str(pattern).casefold() in text for pattern in patterns)
    ]


def _title_keyword_days(records, vocabulary_limit=240):
    """Return range-composable title keyword counters grouped by date."""
    daily = {}
    global_document_frequency = Counter()
    global_term_frequency = Counter()
    global_normalized_tf = Counter()
    for record in records:
        day = str(record.get("date") or "")
        bucket = daily.setdefault(day, {
            "date": day,
            "source_refs": 0,
            "title_count": 0,
            "document_frequency": Counter(),
            "term_frequency": Counter(),
            "normalized_tf": Counter(),
        })
        bucket["source_refs"] += 1
        if not record.get("title"):
            continue
        bucket["title_count"] += 1
        counts = Counter(record.get("tokens") or [])
        token_total = sum(counts.values()) or 1
        bucket["document_frequency"].update(counts.keys())
        bucket["term_frequency"].update(counts)
        global_document_frequency.update(counts.keys())
        global_term_frequency.update(counts)
        for token, count in counts.items():
            contribution = count / token_total
            bucket["normalized_tf"][token] += contribution
            global_normalized_tf[token] += contribution

    vocabulary = {
        token
        for token, _count in sorted(
            global_document_frequency.items(),
            key=lambda item: (
                item[1],
                global_normalized_tf[item[0]],
                global_term_frequency[item[0]],
                item[0],
            ),
            reverse=True,
        )[:vocabulary_limit]
    }
    result = []
    for day in sorted(daily):
        bucket = daily[day]
        result.append({
            "date": day,
            "source_refs": bucket["source_refs"],
            "title_count": bucket["title_count"],
            "document_frequency": {
                token: count
                for token, count in bucket["document_frequency"].items()
                if token in vocabulary
            },
            "term_frequency": {
                token: count
                for token, count in bucket["term_frequency"].items()
                if token in vocabulary
            },
            "normalized_tf": {
                token: round(value, 7)
                for token, value in bucket["normalized_tf"].items()
                if token in vocabulary
            },
        })
    return result


def _two_proportion_test(left_hits, left_total, right_hits, right_total):
    if not left_total or not right_total:
        return {"z": 0.0, "p": 1.0, "log_odds": 0.0}
    left_rate = left_hits / left_total
    right_rate = right_hits / right_total
    pooled = (left_hits + right_hits) / (left_total + right_total)
    variance = pooled * (1 - pooled) * (1 / left_total + 1 / right_total)
    z_value = (left_rate - right_rate) / math.sqrt(variance) if variance > 0 else 0.0
    p_value = 2 * (1 - 0.5 * (1 + math.erf(abs(z_value) / math.sqrt(2))))
    log_odds = math.log((left_hits + 0.5) / (left_total - left_hits + 0.5))
    log_odds -= math.log((right_hits + 0.5) / (right_total - right_hits + 0.5))
    return {
        "z": round(z_value, 5),
        "p": round(max(0.0, min(1.0, p_value)), 8),
        "log_odds": round(log_odds, 5),
    }


def _owned_source_kind(row, ai_cache, meta_cache):
    source_type, _media, _host, _note = source_for(row, ai_cache, meta_cache)
    href = str(row.get("href") or "").casefold()
    if "视频" in source_type or any(
        token in href
        for token in ("douyin", "iesdouyin", "tiktok", "bilibili", "kuaishou", "youtube", "xigua")
    ):
        return "video"
    if "文章" in source_type:
        return "article"
    return "other"


def owned_product_source_analytics(
    source_rows, ai_cache, meta_cache, content_index, selected_question=ALL_QUESTIONS
):
    """Analyze article/video mix for every user-owned product in one question.

    The primary denominator preserves repeated source rows.  A secondary daily
    denominator deduplicates normalized URLs so a single recurring link cannot
    silently dominate the strategy signal.
    """
    selected_question = (selected_question or ALL_QUESTIONS).strip()
    if selected_question == ALL_QUESTIONS:
        return {
            "status": "select_question",
            "question": selected_question,
            "products": [],
            "warning": "请选择单个产品问题后查看自有产品信源结构。",
        }

    content_entries = (
        content_index.get("entries", {})
        if isinstance(content_index, dict) and isinstance(content_index.get("entries"), dict)
        else {}
    )
    match_cache = {}
    source_kind_cache = {}
    records_by_product = defaultdict(list)
    category_records = []
    all_video_records = []
    observation_dates = set()
    total_rows = 0
    archived_rows = 0
    unverified_rows = 0

    for row in source_rows or []:
        if question_for(row) != selected_question or is_quarantined_source_row(row):
            continue
        total_rows += 1
        href = str(row.get("href") or "").strip()
        title = str(row.get("title") or "").strip()
        entry = content_entries.get(href) or content_entries.get(canonical_source_url(href)) or {}
        content_ready = bool(
            isinstance(entry, dict)
            and entry.get("status") == "ok"
            and entry.get("extraction_quality") in ("high", "medium")
        )
        if content_ready:
            archived_rows += 1

        kind_key = (href, title)
        kind = source_kind_cache.get(kind_key)
        if kind is None:
            kind = _owned_source_kind(row, ai_cache, meta_cache)
            source_kind_cache[kind_key] = kind
        day = date_for(row)
        if day:
            observation_dates.add(day)
        tokens = _owned_title_tokens(title)
        themes = _owned_title_themes(title)
        canonical = canonical_source_url(href) or href
        category_records.append({
            "date": day,
            "run_no": safe_int(row.get("run_no")),
            "source_index": safe_int(row.get("index")),
            "kind": kind,
            "href": href,
            "canonical_url": canonical,
            "title": title,
            "scope": "品类全量",
            "body_only": False,
            "content_ready": content_ready,
            "themes": themes,
            "tokens": tokens,
        })
        if kind == "video":
            all_video_records.append({
                "date": day,
                "title": title,
                "tokens": tokens,
            })

        match_key = (href, title)
        matched = match_cache.get(match_key)
        if matched is None:
            matched = owned_source_products(href, title, content_index)
            match_cache[match_key] = matched
        own_products, scope = matched
        if not own_products and not content_ready:
            unverified_rows += 1
        if not own_products:
            continue

        for product in own_products:
            records_by_product[product].append({
                "date": day,
                "run_no": safe_int(row.get("run_no")),
                "source_index": safe_int(row.get("index")),
                "kind": kind,
                "href": href,
                "canonical_url": canonical,
                "title": title,
                "scope": scope,
                "body_only": scope == "正文",
                "content_ready": content_ready,
                "themes": themes,
                "tokens": tokens,
            })

    has_owned_products = bool(records_by_product)
    if not has_owned_products and category_records:
        records_by_product[CATEGORY_BASELINE_NAME] = category_records

    products = []
    for product, records in records_by_product.items():
        records.sort(key=lambda item: (item["date"], item["run_no"], item["source_index"]))
        daily_buckets = {}
        unique_seen = set()
        for record in records:
            day = record["date"]
            bucket = daily_buckets.setdefault(day, {
                "date": day,
                "refs": 0,
                "article_refs": 0,
                "video_refs": 0,
                "other_refs": 0,
                "unique_urls": 0,
                "unique_article_urls": 0,
                "unique_video_urls": 0,
                "body_only_refs": 0,
                "title_refs": 0,
            })
            bucket["refs"] += 1
            bucket[f"{record['kind']}_refs"] += 1
            if record["body_only"]:
                bucket["body_only_refs"] += 1
            else:
                bucket["title_refs"] += 1
            unique_key = (day, record["canonical_url"])
            if unique_key not in unique_seen:
                unique_seen.add(unique_key)
                bucket["unique_urls"] += 1
                if record["kind"] == "article":
                    bucket["unique_article_urls"] += 1
                elif record["kind"] == "video":
                    bucket["unique_video_urls"] += 1

        days = []
        for day in sorted(daily_buckets):
            bucket = daily_buckets[day]
            refs = bucket["refs"]
            unique_urls = bucket["unique_urls"]
            days.append({
                **bucket,
                "article_share": bucket["article_refs"] / refs if refs else None,
                "video_share": bucket["video_refs"] / refs if refs else None,
                "other_share": bucket["other_refs"] / refs if refs else None,
                "unique_article_share": (
                    bucket["unique_article_urls"] / unique_urls if unique_urls else None
                ),
                "unique_video_share": (
                    bucket["unique_video_urls"] / unique_urls if unique_urls else None
                ),
                "body_only_share": bucket["body_only_refs"] / refs if refs else None,
            })

        medium_records = {
            "article": [record for record in records if record["kind"] == "article"],
            "video": [record for record in records if record["kind"] == "video"],
        }
        keyword_days = {
            kind: _title_keyword_days(medium_records[kind])
            for kind in ("article", "video")
        }
        themes = []
        for theme in OWN_TITLE_THEME_PATTERNS:
            article_total = len(medium_records["article"])
            video_total = len(medium_records["video"])
            article_hits = sum(theme in record["themes"] for record in medium_records["article"])
            video_hits = sum(theme in record["themes"] for record in medium_records["video"])
            stats = _two_proportion_test(
                video_hits, video_total, article_hits, article_total
            )
            article_rate = article_hits / article_total if article_total else 0
            video_rate = video_hits / video_total if video_total else 0
            themes.append({
                "name": theme,
                "article_hits": article_hits,
                "article_total": article_total,
                "article_rate": article_rate,
                "video_hits": video_hits,
                "video_total": video_total,
                "video_rate": video_rate,
                "video_minus_article": video_rate - article_rate,
                "significant": stats["p"] < 0.05,
                **stats,
            })

        keyword_groups = {}
        for kind in ("article", "video"):
            documents = [record["tokens"] for record in medium_records[kind] if record["title"]]
            document_count = len(documents)
            document_frequency = Counter()
            term_frequency = Counter()
            normalized_tf = Counter()
            for tokens in documents:
                counts = Counter(tokens)
                token_total = sum(counts.values()) or 1
                document_frequency.update(counts.keys())
                term_frequency.update(counts)
                for token, count in counts.items():
                    normalized_tf[token] += count / token_total
            rows = []
            for token, df_count in document_frequency.items():
                if df_count < 2:
                    continue
                idf = math.log((document_count + 1) / (df_count + 1)) + 1
                score = normalized_tf[token] * idf / document_count if document_count else 0
                rows.append({
                    "keyword": token,
                    "score": round(score, 6),
                    "document_count": df_count,
                    "coverage": df_count / document_count if document_count else 0,
                    "term_count": term_frequency[token],
                })
            rows.sort(
                key=lambda item: (item["score"], item["document_count"], item["term_count"]),
                reverse=True,
            )
            keyword_groups[kind] = rows[:12]

        total_refs = len(records)
        article_refs = len(medium_records["article"])
        video_refs = len(medium_records["video"])
        body_only_refs = sum(record["body_only"] for record in records)
        all_unique_urls = {record["canonical_url"] for record in records if record["canonical_url"]}
        significant_themes = [theme for theme in themes if theme["significant"]]
        video_theme = max(
            significant_themes,
            key=lambda item: item["video_minus_article"],
            default=None,
        )
        article_theme = min(
            significant_themes,
            key=lambda item: item["video_minus_article"],
            default=None,
        )
        products.append({
            "name": product,
            "is_category_baseline": product == CATEGORY_BASELINE_NAME,
            "brand": canonical_brand_name(next(
                (
                    rule["brand"]
                    for rule in OWN_PRODUCT_RULES
                    if rule["name"] == product
                ),
                "",
            )),
            "total_refs": total_refs,
            "article_refs": article_refs,
            "video_refs": video_refs,
            "other_refs": total_refs - article_refs - video_refs,
            "article_share": article_refs / total_refs if total_refs else None,
            "video_share": video_refs / total_refs if total_refs else None,
            "unique_urls": len(all_unique_urls),
            "body_only_refs": body_only_refs,
            "body_only_share": body_only_refs / total_refs if total_refs else None,
            "days": days,
            "themes": themes,
            "keywords": keyword_groups,
            "keyword_days": keyword_days,
            "strategy": {
                "video_theme": video_theme["name"] if video_theme else "",
                "video_theme_delta": (
                    video_theme["video_minus_article"] if video_theme else 0
                ),
                "article_theme": article_theme["name"] if article_theme else "",
                "article_theme_delta": (
                    -article_theme["video_minus_article"] if article_theme else 0
                ),
            },
        })

    products.sort(key=lambda item: (-item["total_refs"], item["name"]))
    return {
        "status": "ok" if products else "no_source_data",
        "baseline_only": bool(products) and not has_owned_products,
        "question": selected_question,
        "generated_at": beijing_now(),
        "observation_dates": sorted(observation_dates),
        "products": products,
        "all_video_keyword_days": _title_keyword_days(all_video_records),
        "all_video_refs": len(all_video_records),
        "all_video_titled_refs": sum(bool(record["title"]) for record in all_video_records),
        "quality": {
            "question_source_rows": total_rows,
            "content_archived_rows": archived_rows,
            "content_archive_coverage": archived_rows / total_rows if total_rows else None,
            "unverified_rows": unverified_rows,
        },
        "definitions": {
            "primary_denominator": "每日标题或可靠归档正文确认命中该自有产品的信源行；同一链接跨轮重复出现继续计数。",
            "unique_denominator": "同一产品、同一日期按标准化URL去重后的确认命中链接数。",
            "body_only": "标题没有自有产品，但可靠归档的文章正文或视频页面描述命中。",
            "tfidf": "标题关键词按产品×文章/视频分组计算平均TF-IDF；至少出现于2个标题。",
            "all_video_keywords": "当前问题全部被抓视频信源的标题关键词；保留同一链接跨轮重复出现，并与所选自有产品视频标题使用相同日期范围和TF-IDF口径。",
            "category_baseline": "未确认命中自有产品时，使用当前问题全部信源计算文章/视频结构、标题主题和关键词趋势；不把品类全量误写成自有产品表现。",
        },
        "warning": (
            "当前问题尚未确认命中自有产品，已自动展示品类全量基准。"
            if products and not has_owned_products
            else ("" if products else "当前问题暂无可分析的信源数据。")
        ),
    }


def owned_video_source_share_by_category(source_rows, ai_cache, meta_cache, content_index):
    """Return unique owned-brand video links as a share of each category's links."""
    buckets = {}
    match_cache = {}
    kind_cache = {}
    observation_dates = set()
    product_brands = {
        str(rule.get("name") or "").strip(): canonical_brand_name(rule.get("brand") or "")
        for rule in OWN_PRODUCT_RULES
        if str(rule.get("name") or "").strip()
    }
    for row in source_rows or []:
        if is_quarantined_source_row(row):
            continue
        question = question_for(row)
        href = str(row.get("href") or "").strip()
        canonical = canonical_source_url(href) or href
        if not question or question == ALL_QUESTIONS or not canonical:
            continue
        bucket = buckets.setdefault(question, {
            "question": question,
            "all_refs": 0,
            "video_refs": 0,
            "owned_video_refs": 0,
            "all_urls": set(),
            "video_urls": set(),
            "owned_video_urls": set(),
            "owned_brands": set(),
            "dates": set(),
        })
        day = date_for(row)
        if day and day != "未知日期":
            bucket["dates"].add(day)
            observation_dates.add(day)
        bucket["all_refs"] += 1
        bucket["all_urls"].add(canonical)
        title = str(row.get("title") or "").strip()
        source_key = (href, title)
        kind = kind_cache.get(source_key)
        if kind is None:
            kind = _owned_source_kind(row, ai_cache, meta_cache)
            kind_cache[source_key] = kind
        if kind != "video":
            continue
        bucket["video_refs"] += 1
        bucket["video_urls"].add(canonical)
        matched = match_cache.get(source_key)
        if matched is None:
            products, _product_scope = owned_source_products(href, title, content_index)
            brands, _brand_scope = owned_source_brands(href, title, content_index)
            matched = (products, brands)
            match_cache[source_key] = matched
        products, brands = matched
        resolved_brands = {
            canonical_brand_name(brand)
            for brand in brands
            if canonical_brand_name(brand)
        }
        resolved_brands.update(
            product_brands.get(product, "")
            for product in products
            if product_brands.get(product, "")
        )
        if not products and not resolved_brands:
            continue
        bucket["owned_video_refs"] += 1
        bucket["owned_video_urls"].add(canonical)
        bucket["owned_brands"].update(resolved_brands)

    rows = []
    for question, bucket in buckets.items():
        all_urls = len(bucket["all_urls"])
        video_urls = len(bucket["video_urls"])
        owned_video_urls = len(bucket["owned_video_urls"])
        rows.append({
            "question": question,
            "all_refs": bucket["all_refs"],
            "video_refs": bucket["video_refs"],
            "owned_video_refs": bucket["owned_video_refs"],
            "all_unique_urls": all_urls,
            "video_unique_urls": video_urls,
            "owned_video_unique_urls": owned_video_urls,
            "owned_video_link_share": owned_video_urls / all_urls if all_urls else None,
            "owned_within_video_link_share": owned_video_urls / video_urls if video_urls else None,
            "owned_brands": sorted(bucket["owned_brands"]),
            "first_date": min(bucket["dates"]) if bucket["dates"] else "",
            "last_date": max(bucket["dates"]) if bucket["dates"] else "",
        })
    rows.sort(key=lambda item: item["question"])
    return {
        "rows": rows,
        "first_date": min(observation_dates) if observation_dates else "",
        "last_date": max(observation_dates) if observation_dates else "",
        "definitions": {
            "primary": "命中自有品牌或自有产品的视频唯一链接数 ÷ 该品类全部唯一信源链接数。",
            "within_video": "命中自有品牌或自有产品的视频唯一链接数 ÷ 该品类全部视频唯一链接数。",
            "deduplication": "链接按标准化URL去重；同一链接跨轮重复抓取只计一个链接。",
        },
    }


def product_review_coverage(source_rows, product_rows, answer_rows):
    """Explain every source run that is absent from product statistics."""
    source_by_run = {}
    for row in source_rows:
        run_no = safe_int(row.get("run_no"))
        if run_no:
            source_by_run.setdefault(run_no, row)
    product_runs = {safe_int(row.get("run_no")) for row in product_rows if safe_int(row.get("run_no"))}
    answers_by_run = {}
    for row in answer_rows:
        run_no = safe_int(row.get("run_no"))
        if run_no in source_by_run:
            answers_by_run[run_no] = row

    archived_run_numbers = [safe_int(row.get("run_no")) for row in answer_rows if safe_int(row.get("run_no"))]
    archive_start_run = min(archived_run_numbers) if archived_run_numbers else 0
    result = {"source_runs": len(source_by_run), "with_products": 0, "verified_no_products": [], "ai_pending": [], "capture_mismatch": [], "answer_not_archived": [], "legacy_not_archived": []}
    for run_no, source in sorted(source_by_run.items()):
        if run_no in product_runs:
            result["with_products"] += 1
            continue
        answer = answers_by_run.get(run_no)
        item = {"run_no": run_no, "run_time": source.get("run_time", ""), "chat_id": source.get("chat_id", "")}
        if not answer:
            bucket = "legacy_not_archived" if archive_start_run and run_no < archive_start_run else "answer_not_archived"
            result[bucket].append(item)
        elif str(answer.get("review_status") or "") == "ai_pending":
            result["ai_pending"].append(item)
        elif str(answer.get("review_status") or "") == "capture_mismatch":
            result["capture_mismatch"].append(item)
        else:
            result["verified_no_products"].append(item)
    return result


_STATS_CACHE = {}
_STATS_LOCK = threading.Lock()
_DATA_SNAPSHOT = {"signature": None, "data": None}


def _raw_data_signature():
    paths = (
        CSV_PATH, PRODUCT_CSV_PATH, ANSWER_CSV_PATH, CAPTURE_SKIP_CSV_PATH,
        AI_CACHE_PATH, META_CACHE_PATH, QUESTION_ALIASES_PATH,
        BRAND_SETTINGS_PATH,
    )
    return tuple(
        (os.path.getmtime(path), os.path.getsize(path)) if os.path.exists(path) else (0, 0)
        for path in paths
    )


def _load_data_snapshot():
    """Parse and index the large source/product files once per real file change."""
    signature = _raw_data_signature()
    if _DATA_SNAPSHOT["signature"] == signature and _DATA_SNAPSHOT["data"] is not None:
        return _DATA_SNAPSHOT["data"]

    # Reload aliases so edits to the question mapping take effect without
    # restarting the dashboard process.
    try:
        importlib.reload(qa)
    except Exception:
        pass
    canonical_question_name.cache_clear()
    canonical_brand_name.cache_clear()
    aliases_for_brand.cache_clear()
    title_mentions_brand.cache_clear()

    rows = [row for row in read_csv_rows() if not is_quarantined_source_row(row)]
    product_rows = read_product_rows()
    answer_rows = read_answer_rows()
    capture_skip_rows = read_capture_skip_rows()
    ai_cache = read_json(AI_CACHE_PATH)
    meta_cache = read_json(META_CACHE_PATH)

    rows_by_question = defaultdict(list)
    for row in rows:
        rows_by_question[question_for(row)].append(row)
    products_by_question = defaultdict(list)
    for row in product_rows:
        products_by_question[product_question_for(row)].append(row)
    answers_by_question = defaultdict(list)
    for row in answer_rows:
        answers_by_question[question_for(row)].append(row)

    global_source_brands = set()
    global_source_brands.update(
        item["name"] for item in brand_settings.vocabulary()
    )
    for row in product_rows:
        explicit_brand = str(row.get("brand_name") or "").strip()
        if explicit_brand and not is_invalid_brand_candidate(explicit_brand):
            global_source_brands.add(canonical_brand_name(explicit_brand))

    latest_all_no, latest_all_rows = latest_run_rows(rows)
    data = {
        "rows": rows,
        "product_rows": product_rows,
        "answer_rows": answer_rows,
        "capture_skip_rows": capture_skip_rows,
        "ai_cache": ai_cache,
        "meta_cache": meta_cache,
        "rows_by_question": dict(rows_by_question),
        "products_by_question": dict(products_by_question),
        "answers_by_question": dict(answers_by_question),
        "questions": question_summaries(rows, answer_rows),
        "latest_all_no": latest_all_no,
        "latest_all_rows": latest_all_rows,
        "global_source_brands": global_source_brands,
    }
    _DATA_SNAPSHOT.update({"signature": signature, "data": data})
    return data
_VIEW_CACHE = {}
_VIEW_CACHE_LOCK = threading.Lock()
_VIEW_REFRESHING = set()
_VIEW_CACHE_TTL_SECONDS = 30.0
_VIEW_CACHE_SCHEMA_VERSION = 2


def _load_persisted_view_cache():
    """Restore last-rendered views so a restart never makes selectors cold."""
    payload = read_json(VIEW_CACHE_PATH)
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != _VIEW_CACHE_SCHEMA_VERSION
        or not isinstance(payload.get("views"), list)
    ):
        return
    restored = {}
    current_version = _signature_token(_stats_signature())
    now = time.monotonic()
    for item in payload["views"]:
        if not isinstance(item, dict) or not isinstance(item.get("result"), dict):
            continue
        key = _view_key(item.get("question"), item.get("device"))
        restored[key] = {
            # Reuse an unchanged snapshot as hot.  A changed data signature is
            # returned once as stale and refreshed in the background.
            "built_at": (
                now
                if str(item["result"].get("data_version") or "") == current_version
                else 0.0
            ),
            "data_version": str(item["result"].get("data_version") or ""),
            "result": item["result"],
        }
    with _VIEW_CACHE_LOCK:
        _VIEW_CACHE.update(restored)


def _persist_view_cache():
    """Atomically retain selector results for instant reuse after restart."""
    with _VIEW_CACHE_LOCK:
        views = [
            {
                "question": key[0],
                "device": key[1],
                "result": item["result"],
            }
            for key, item in _VIEW_CACHE.items()
            if isinstance(item.get("result"), dict)
        ]
    payload = {
        "schema_version": _VIEW_CACHE_SCHEMA_VERSION,
        "saved_at": beijing_now(),
        "views": views,
    }
    os.makedirs(os.path.dirname(VIEW_CACHE_PATH), exist_ok=True)
    temp_path = VIEW_CACHE_PATH + ".tmp"
    try:
        with open(temp_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
        os.replace(temp_path, VIEW_CACHE_PATH)
    except OSError:
        try:
            os.remove(temp_path)
        except OSError:
            pass


def _stats_signature():
    paths = (
        CSV_PATH, PRODUCT_CSV_PATH, ANSWER_CSV_PATH, CAPTURE_SKIP_CSV_PATH,
        AI_CACHE_PATH, META_CACHE_PATH, BRAND_AI_CACHE_PATH, CONTENT_INDEX_PATH,
        QUESTION_ALIASES_PATH, os.path.abspath(__file__),
        BRAND_SETTINGS_PATH,
    )
    return (
        *tuple(os.path.getmtime(path) if os.path.exists(path) else 0 for path in paths),
        float(NORMALIZATION_SCHEMA_VERSION),
    )


def _signature_token(signature):
    return "|".join(f"{value:.6f}" for value in signature)


def _compute_stats(selected_question=None, selected_device=None):
    selected_question = normalize_selected_question(selected_question)
    selected_device = normalize_selected_device(selected_device)
    signature = _stats_signature()
    cache_key = (
        *signature,
        selected_question or "__default__",
        selected_device,
    )
    cached = _STATS_CACHE.get(cache_key)
    if cached:
        return cached

    with _STATS_LOCK:
        cached = _STATS_CACHE.get(cache_key)
        if cached:
            return cached

        snapshot = _load_data_snapshot()
        all_rows = snapshot["rows"]
        all_product_rows = snapshot["product_rows"]
        all_answer_rows = snapshot["answer_rows"]
        capture_skip_rows = snapshot["capture_skip_rows"]
        ai_cache = snapshot["ai_cache"]
        meta_cache = snapshot["meta_cache"]
        content_index = read_json(CONTENT_INDEX_PATH)
        device_catalog = {}
        for row in all_answer_rows + all_rows:
            instance = row_device(row)
            if not instance:
                continue
            item = device_catalog.setdefault(
                instance,
                {
                    "instance": instance,
                    "uid_masked": "",
                    "nickname": "",
                    "runs": set(),
                    "refs": 0,
                    "latest_at": "",
                },
            )
            run_no = safe_int(row.get("run_no"))
            if run_no:
                item["runs"].add(run_no)
            timestamp = str(row.get("captured_at") or row.get("run_time") or "")
            if timestamp >= item["latest_at"]:
                item["latest_at"] = timestamp
                item["uid_masked"] = str(
                    row.get("account_uid_masked") or item["uid_masked"]
                )
                item["nickname"] = str(
                    row.get("account_nickname") or item["nickname"]
                )
        for row in all_rows:
            instance = row_device(row)
            if instance in device_catalog:
                device_catalog[instance]["refs"] += 1
        device_options = [
            {
                "instance": item["instance"],
                "uid_masked": item["uid_masked"] or "未知 UID",
                "nickname": item["nickname"] or f"历史采集实例 {item['instance']}",
                "run_count": len(item["runs"]),
                "reference_count": item["refs"],
                "latest_at": item["latest_at"],
            }
            for item in device_catalog.values()
        ]
        device_options.sort(key=lambda item: safe_int(item["instance"]))
        if (
            selected_device != ALL_DEVICES
            and selected_device not in device_catalog
        ):
            selected_device = ALL_DEVICES

        if selected_device == ALL_DEVICES:
            rows = all_rows
            product_rows = all_product_rows
            answer_rows = all_answer_rows
        else:
            rows = [
                row for row in all_rows
                if row_device(row) == selected_device
            ]
            product_rows = [
                row for row in all_product_rows
                if row_device(row) == selected_device
            ]
            answer_rows = [
                row for row in all_answer_rows
                if row_device(row) == selected_device
            ]
        questions = (
            snapshot["questions"]
            if selected_device == ALL_DEVICES
            else question_summaries(rows, answer_rows)
        )
        latest_all_no, latest_all_rows = latest_run_rows(rows)
        latest_question = question_for(latest_all_rows[0]) if latest_all_rows else ""
        # Default to the complete dataset. A question-specific view is used only
        # when the client explicitly selects a question.
        selected_question = normalize_selected_question(selected_question)

        if selected_question and selected_question != ALL_QUESTIONS:
            if selected_device == ALL_DEVICES:
                # The snapshot already maintains per-question indexes.  Reusing
                # them avoids rescanning every historic reference/answer when a
                # user switches the dashboard selector.
                rows_for_stats = snapshot["rows_by_question"].get(
                    selected_question, []
                )
                answer_rows_for_stats = snapshot["answers_by_question"].get(
                    selected_question, []
                )
            else:
                rows_for_stats = [
                    row for row in rows
                    if question_for(row) == selected_question
                ]
                answer_rows_for_stats = [
                    row for row in answer_rows
                    if question_for(row) == selected_question
                ]
            if not rows_for_stats and not answer_rows_for_stats:
                selected_question = latest_question or ALL_QUESTIONS
                rows_for_stats = (
                    [
                        row for row in rows
                        if question_for(row) == selected_question
                    ]
                    if selected_question != ALL_QUESTIONS else rows
                )
                answer_rows_for_stats = (
                    [
                        row for row in answer_rows
                        if question_for(row) == selected_question
                    ]
                    if selected_question != ALL_QUESTIONS else answer_rows
                )
        else:
            selected_question = ALL_QUESTIONS
            rows_for_stats = rows
            answer_rows_for_stats = answer_rows
        if selected_question == ALL_QUESTIONS:
            product_rows_for_stats = product_rows
        elif selected_device == ALL_DEVICES:
            product_rows_for_stats = snapshot["products_by_question"].get(
                selected_question, []
            )
        else:
            product_rows_for_stats = [
                row for row in product_rows
                if product_question_for(row) == selected_question
            ]

        # Heavy breakdowns should follow the active question. Previously these were
        # built for every question on every click, which made question switching slow.
        per_question_sources = question_source_breakdown(rows_for_stats, ai_cache, meta_cache)
        daily_question_sources = daily_question_source_breakdown(
            rows_for_stats,
            ai_cache,
            meta_cache,
            content_index,
            answer_rows=answer_rows_for_stats,
        )

        latest_no, latest_rows = latest_run_rows(rows_for_stats)
        unique_links = {row.get("href", "") for row in rows_for_stats if row.get("href")}
        latest_unique_links = {row.get("href", "") for row in latest_rows if row.get("href")}

        by_type = Counter()
        by_media = Counter()
        by_domain = Counter()
        for row in rows_for_stats:
            source_type, media, host, _note = source_for(row, ai_cache, meta_cache)
            by_type[source_type] += 1
            by_media[media] += 1
            if host:
                by_domain[host] += 1

        latest_items = []
        for row in sorted(latest_rows, key=lambda r: safe_int(r.get("index"))):
            source_type, media, host, note = source_for(row, ai_cache, meta_cache)
            own_products, own_match_scope = owned_source_products(
                row.get("href"), row.get("title"), content_index
            )
            own_brands, own_brand_match_scope = owned_source_brands(
                row.get("href"), row.get("title"), content_index
            )
            latest_items.append({
                "run_no": row.get("run_no", ""),
                "question": question_for(row),
                "index": row.get("index", ""),
                "title": row.get("title", ""),
                "href": row.get("href", ""),
                "source_type": source_type,
                "media": media,
                "domain": host,
                "note": note,
                "own_products": own_products,
                "own_match_scope": own_match_scope,
                "own_brands": own_brands,
                "own_brand_match_scope": own_brand_match_scope,
            })

        latest_row = latest_rows[0] if latest_rows else {}
        latest_answer_row = (
            max(
                answer_rows_for_stats,
                key=lambda row: safe_int(row.get("run_no")),
            )
            if answer_rows_for_stats
            else {}
        )
        latest_metadata_row = latest_answer_row or latest_row
        # An archived answer is the authoritative evidence that a round ran.
        # Some valid Doubao answers contain no reference section, so counting
        # source rows alone makes (for example) 33 completed rounds display as
        # 31.  Keep source coverage separate, but use the answer union for the
        # user-facing run count and denominator.
        run_numbers = {
            safe_int(row.get("run_no"))
            for row in (rows_for_stats + answer_rows_for_stats)
            if safe_int(row.get("run_no"))
        }
        run_dates = {}
        for row in rows_for_stats + answer_rows_for_stats:
            run_no = safe_int(row.get("run_no"))
            day = date_for(row)
            if run_no and day:
                run_dates[run_no] = day
        latest_run_day = max(run_dates.values()) if run_dates else ""
        today_runs = sum(1 for day in run_dates.values() if day == latest_run_day)
        account_map = {}
        for row in answer_rows_for_stats + rows_for_stats:
            uid = str(
                row.get("account_uid")
                or row.get("account_uid_masked")
                or row.get("account_nickname")
                or ""
            ).strip()
            instance = str(row.get("mumu_instance") or "").strip()
            key = uid or (f"instance:{instance}" if instance else "unknown")
            item = account_map.setdefault(
                key,
                {
                    "uid_masked": str(row.get("account_uid_masked") or ""),
                    "nickname": str(row.get("account_nickname") or ""),
                    "instances": set(),
                    "runs": set(),
                    "questions": set(),
                    "refs": 0,
                    "first_at": "",
                    "latest_at": "",
                },
            )
            if instance:
                item["instances"].add(instance)
            run_no = safe_int(row.get("run_no"))
            if run_no:
                item["runs"].add(run_no)
            question = question_for(row)
            if question:
                item["questions"].add(question)
            timestamp = str(
                row.get("captured_at")
                or row.get("run_time")
                or row.get("answer_completed_at")
                or ""
            )
            if timestamp:
                if not item["first_at"] or timestamp < item["first_at"]:
                    item["first_at"] = timestamp
                if timestamp > item["latest_at"]:
                    item["latest_at"] = timestamp
        for row in rows_for_stats:
            uid = str(
                row.get("account_uid")
                or row.get("account_uid_masked")
                or row.get("account_nickname")
                or ""
            ).strip()
            instance = str(row.get("mumu_instance") or "").strip()
            key = uid or (f"instance:{instance}" if instance else "unknown")
            if key in account_map:
                account_map[key]["refs"] += 1
        account_summaries = []
        for item in account_map.values():
            account_summaries.append(
                {
                    "uid_masked": item["uid_masked"] or "未知 UID",
                    "nickname": item["nickname"] or "未设置昵称",
                    "instances": sorted(item["instances"], key=safe_int),
                    "run_count": len(item["runs"]),
                    "question_count": len(item["questions"]),
                    "reference_count": item["refs"],
                    "first_at": item["first_at"],
                    "latest_at": item["latest_at"],
                }
            )
        account_summaries.sort(
            key=lambda item: (
                -item["run_count"],
                item["uid_masked"],
                item["nickname"],
            )
        )
        products = product_stats(product_rows_for_stats, selected_question, rows_for_stats)
        product_coverage = product_review_coverage(
            rows_for_stats, product_rows_for_stats, answer_rows_for_stats
        )
        active_capture_skips = [row for row in capture_skip_rows if row.get("status") == "skipped"]
        pending_capture_saves = [
            row for row in active_capture_skips
            if str(row.get("reason") or "").startswith("Save deferred for background retry:")
        ]
        blank_capture_skips = [
            row for row in active_capture_skips
            if row not in pending_capture_saves
        ]
        resolved_capture_skips = [row for row in capture_skip_rows if row.get("status") == "resolved"]
        daily_question_products = daily_question_product_breakdown(
            product_rows_for_stats,
            rows_for_stats,
            selected_question,
            answer_rows=answer_rows_for_stats,
        )
        owned_video_category_share = owned_video_source_share_by_category(
            rows,
            ai_cache,
            meta_cache,
            content_index,
        )
        owned_product_source_analytics_data = owned_product_source_analytics(
            rows_for_stats,
            ai_cache,
            meta_cache,
            content_index,
            selected_question,
        )
        brand_source_daily_analytics_data = brand_source_daily_analytics(
            product_rows_for_stats,
            rows_for_stats,
            answer_rows_for_stats,
            ai_cache,
            meta_cache,
            content_index,
            selected_question,
        )
        scope_rows_all_devices = (
            all_rows
            if selected_question == ALL_QUESTIONS
            else [
                row for row in all_rows
                if question_for(row) == selected_question
            ]
        )
        scope_answers_all_devices = (
            all_answer_rows
            if selected_question == ALL_QUESTIONS
            else [
                row for row in all_answer_rows
                if question_for(row) == selected_question
            ]
        )
        scope_products_all_devices = (
            all_product_rows
            if selected_question == ALL_QUESTIONS
            else [
                row for row in all_product_rows
                if product_question_for(row) == selected_question
            ]
        )
        scope_device_metrics = {
            item["instance"]: {
                "runs": set(),
                "refs": 0,
                "product_mentions": 0,
                "product_runs": set(),
            }
            for item in device_options
        }
        unassigned_runs = set()
        unassigned_refs = 0
        for row in scope_answers_all_devices + scope_rows_all_devices:
            instance = row_device(row)
            run_no = safe_int(row.get("run_no"))
            if instance in scope_device_metrics:
                if run_no:
                    scope_device_metrics[instance]["runs"].add(run_no)
            elif run_no:
                unassigned_runs.add(run_no)
        for row in scope_rows_all_devices:
            instance = row_device(row)
            if instance in scope_device_metrics:
                scope_device_metrics[instance]["refs"] += 1
            else:
                unassigned_refs += 1
        for row in scope_products_all_devices:
            instance = row_device(row)
            if instance not in scope_device_metrics:
                continue
            scope_device_metrics[instance]["product_mentions"] += 1
            run_no = safe_int(row.get("run_no"))
            if run_no:
                scope_device_metrics[instance]["product_runs"].add(run_no)
        scoped_device_options = []
        for item in device_options:
            metrics = scope_device_metrics[item["instance"]]
            scoped_device_options.append({
                **item,
                "scope_run_count": len(metrics["runs"]),
                "scope_reference_count": metrics["refs"],
                "scope_product_mentions": metrics["product_mentions"],
                "scope_product_run_count": len(metrics["product_runs"]),
            })
        active_metrics = [
            item for item in scoped_device_options
            if item["scope_run_count"] or item["scope_reference_count"]
        ]
        active_device_count = len(active_metrics)
        identified_runs = sum(item["scope_run_count"] for item in active_metrics)
        identified_refs = sum(
            item["scope_reference_count"] for item in active_metrics
        )
        identified_product_mentions = sum(
            item["scope_product_mentions"] for item in active_metrics
        )
        device_overview = {
            "selected": selected_device,
            "device_count": len(device_options),
            "active_device_count": active_device_count,
            "identified_run_count": identified_runs,
            "identified_reference_count": identified_refs,
            "identified_product_mentions": identified_product_mentions,
            "average_runs_per_device": round(
                identified_runs / active_device_count, 2
            ) if active_device_count else 0,
            "average_references_per_device": round(
                identified_refs / active_device_count, 2
            ) if active_device_count else 0,
            "average_product_mentions_per_device": round(
                identified_product_mentions / active_device_count, 2
            ) if active_device_count else 0,
            "unassigned_run_count": len(unassigned_runs),
            "unassigned_reference_count": unassigned_refs,
        }
        result = {
            "ok": True,
            "generated_at": beijing_now(),
            "data_version": _signature_token(signature),
            "csv": file_info(CSV_PATH),
            "products_csv": file_info(PRODUCT_CSV_PATH),
            "xlsx": file_info(os.path.join(BASE_DIR, "doubao_refs_result.xlsx")),
            "ai_cache": file_info(AI_CACHE_PATH),
            "selected_question": selected_question,
            "selected_device": selected_device,
            "device_options": scoped_device_options,
            "device_overview": device_overview,
            "latest_question": latest_question,
            "questions": questions,
            "per_question_sources": per_question_sources,
            "daily_question_sources": daily_question_sources,
            "daily_question_products": daily_question_products,
            "owned_video_source_share_by_category": owned_video_category_share,
            "owned_product_source_analytics": owned_product_source_analytics_data,
            "brand_source_daily_analytics": brand_source_daily_analytics_data,
            "question_count": len(questions),
            "account_count": len(account_summaries),
            "account_summaries": account_summaries,
            "total_runs": len(run_numbers),
            "today_run_date": latest_run_day,
            "today_runs": today_runs,
            "latest_run_no": max(
                latest_no,
                safe_int(latest_answer_row.get("run_no")),
            ),
            "total_refs": len(rows_for_stats),
            "unique_links": len(unique_links),
            "latest_refs": len(latest_rows),
            "latest_unique_links": len(latest_unique_links),
            "latest_chat_title": latest_metadata_row.get("chat_title", ""),
            "latest_run_time": latest_metadata_row.get("run_time", ""),
            "latest_account_uid_masked": latest_metadata_row.get(
                "account_uid_masked",
                "",
            ),
            "latest_account_nickname": latest_metadata_row.get(
                "account_nickname",
                "",
            ),
            "latest_source_device": latest_metadata_row.get(
                "source_device",
                "",
            ),
            "latest_question_sent_at": latest_metadata_row.get(
                "question_sent_at",
                "",
            ),
            "latest_answer_completed_at": latest_metadata_row.get(
                "answer_completed_at",
                "",
            ),
            "latest_captured_at": latest_metadata_row.get(
                "captured_at",
                "",
            ),
            "latest_complete": latest_row.get("complete", ""),
            "latest_expected_count": latest_row.get("expected_count", ""),
            "latest_count": latest_row.get("count", ""),
            "by_type": counter_items(by_type),
            "by_media": counter_items(by_media),
            "by_domain": counter_items(by_domain),
            "media_total": len(by_media),
            "domain_total": len(by_domain),
            "latest_items": latest_items,
            "products": products,
            "product_coverage": product_coverage,
            "capture_skips": {
                "active_count": len(blank_capture_skips),
                "resolved_count": len(resolved_capture_skips),
                "items": blank_capture_skips[-20:],
                "pending_save_count": len(pending_capture_saves),
                "pending_save_items": pending_capture_saves[-20:],
            },
            "log_tail": log_tail(),
        }
        # 只保留同一视图的最新版本。旧实现按所有文件 mtime 建键但从不
        # 淘汰，监控持续写入时会不断积累大对象，最终拖慢并吃满内存。
        view_key = cache_key[-2:]
        for old_key in [
            key for key in _STATS_CACHE
            if key[-2:] == view_key and key != cache_key
        ]:
            _STATS_CACHE.pop(old_key, None)
        _STATS_CACHE[cache_key] = result
        return result


def _view_key(selected_question, selected_device=None):
    return (
        normalize_selected_question(selected_question),
        normalize_selected_device(selected_device),
    )


def _refresh_view_cache(key, selected_question, selected_device):
    try:
        result = _compute_stats(selected_question, selected_device)
        _store_view_cache(selected_question, selected_device, result)
    finally:
        with _VIEW_CACHE_LOCK:
            _VIEW_REFRESHING.discard(key)


def _store_view_cache(selected_question, selected_device, result):
    key = _view_key(selected_question, selected_device)
    with _VIEW_CACHE_LOCK:
        _VIEW_CACHE[key] = {
            "built_at": time.monotonic(),
            "data_version": result.get("data_version", ""),
            "result": result,
        }
    _persist_view_cache()


def build_stats(selected_question=None, selected_device=None):
    """快速返回最近快照；过期后在后台重算，避免页面请求排队。"""
    key = _view_key(selected_question, selected_device)
    now = time.monotonic()
    with _VIEW_CACHE_LOCK:
        cached = _VIEW_CACHE.get(key)
        if cached and now - cached["built_at"] < _VIEW_CACHE_TTL_SECONDS:
            return cached["result"]
        if cached:
            if key not in _VIEW_REFRESHING:
                _VIEW_REFRESHING.add(key)
                threading.Thread(
                    target=_refresh_view_cache,
                    args=(key, selected_question, selected_device),
                    daemon=True,
                    name="doubao-stats-refresh-" + str(key)[:24],
                ).start()
            return cached["result"]

    # 首次访问没有旧快照时同步计算一次；后续更新全部走后台刷新。
    result = _compute_stats(selected_question, selected_device)
    _store_view_cache(selected_question, selected_device, result)
    return result


def stats_version(selected_question=None, selected_device=None):
    """轻量检查数据变化；需要时触发后台刷新，不传输完整统计。"""
    key = _view_key(selected_question, selected_device)
    current_version = _signature_token(_stats_signature())
    start_refresh = False
    with _VIEW_CACHE_LOCK:
        cached = _VIEW_CACHE.get(key)
        cached_version = cached.get("data_version", "") if cached else ""
        if cached_version != current_version and key not in _VIEW_REFRESHING:
            _VIEW_REFRESHING.add(key)
            start_refresh = True
        refreshing = key in _VIEW_REFRESHING
    if start_refresh:
        threading.Thread(
            target=_refresh_view_cache,
            args=(key, selected_question, selected_device),
            daemon=True,
            name="doubao-version-refresh-" + str(key)[:24],
        ).start()
    return {
        "ok": True,
        "version": current_version,
        "cached_version": cached_version,
        "ready": bool(cached_version) and cached_version == current_version,
        "refreshing": refreshing or start_refresh,
    }


def prewarm_question_views():
    """Build only device overviews; category views are already inexpensive."""
    try:
        snapshot = _load_data_snapshot()
        view_specs = [(ALL_QUESTIONS, ALL_DEVICES)]
        devices = sorted(
            {
                row_device(row)
                for row in (
                    list(snapshot.get("answer_rows") or [])
                    + list(snapshot.get("rows") or [])
                )
                if row_device(row)
            },
            key=safe_int,
        )
        view_specs.extend((ALL_QUESTIONS, device) for device in devices)
        for question, device in view_specs:
            key = _view_key(question, device)
            with _VIEW_CACHE_LOCK:
                if key in _VIEW_CACHE:
                    continue
            try:
                result = _compute_stats(question, device)
            except Exception:
                continue
            _store_view_cache(question, device, result)
            time.sleep(0.1)
    except Exception:
        return


INDEX_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>豆包参考资料实时面板</title>
<style>
:root {
  --bg: #f5f7f6; --card-bg: #fff; --ink: #18201d; --muted: #66716d;
  --border: #dfe5e2; --accent: #087f65; --accent-light: #e5f5ef;
  --orange: #e69f23; --orange-light: #fef7e8; --blue: #1a6fdd; --blue-light: #eaf2fd;
  --danger: #b42318; --danger-light:#fff0ee; --warning:#a15c00; --warning-light:#fff6df;
  --radius: 14px; --shadow: 0 1px 3px rgba(18,32,26,.06),0 8px 28px rgba(18,32,26,.035);
  --font: -apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",sans-serif;
  --sidebar-w: 280px;
}
* { box-sizing:border-box; margin:0; padding:0; }
body { font-family:var(--font); background:var(--bg); color:var(--ink); font-size:14px; line-height:1.5; }
button,input,select { font:inherit; }
button:focus-visible,input:focus-visible,select:focus-visible,a:focus-visible,summary:focus-visible {
  outline:3px solid rgba(8,127,101,.24); outline-offset:2px;
}
.sr-only { position:absolute!important; width:1px!important; height:1px!important; padding:0!important; margin:-1px!important; overflow:hidden!important; clip:rect(0,0,0,0)!important; white-space:nowrap!important; border:0!important; }
a { color:var(--blue); text-decoration:none; } a:hover{text-decoration:underline; }

/* ===== LAYOUT ===== */
.app { display:flex; min-height:100vh; }
.sidebar {
  width:var(--sidebar-w); flex-shrink:0; background:#fff;
  border-right:1px solid var(--border); display:flex; flex-direction:column;
  position:sticky; top:0; height:100vh; overflow-y:auto; z-index:10;
}
.main { flex:1; min-width:0; display:flex; flex-direction:column; container-type:inline-size; container-name:dashboard; }

/* ===== SIDEBAR ===== */
.sidebar-header {
  padding:18px 16px 10px; border-bottom:1px solid var(--border);
}
.sidebar-logo { font-size:17px; font-weight:800; letter-spacing:.02em; display:flex; align-items:center; gap:8px; }
.sidebar-logo i { font-style:normal; font-size:20px; color:var(--accent); }
.sidebar-status { font-size:11px; color:var(--muted); margin-top:4px; display:flex; align-items:center; gap:6px; }
.status-dot { width:7px; height:7px; border-radius:50%; background:var(--accent); box-shadow:0 0 0 4px rgba(8,127,101,.1); }

.sidebar-metrics { padding:12px 14px; display:grid; gap:8px; border-bottom:1px solid var(--border); }
.metric-mini { display:flex; justify-content:space-between; align-items:baseline; padding:6px 8px; border-radius:8px; transition:background .15s; }
.metric-mini:hover { background:var(--bg); }
.metric-mini-label { font-size:11px; color:var(--muted); }
.metric-mini-val { font-size:18px; font-weight:800; font-variant-numeric:tabular-nums; }

.sidebar-nav { padding:10px 14px; border-bottom:1px solid var(--border); }
.sidebar-nav label { font-size:11px; color:var(--muted); font-weight:700; display:block; margin-bottom:6px; }
.sidebar-nav select {
  width:100%; padding:8px 10px; border:1px solid var(--border); border-radius:8px;
  background:#fff; font-size:13px; color:var(--ink); outline:none; cursor:pointer;
}
.sidebar-nav select:focus { border-color:var(--accent); box-shadow:0 0 0 3px rgba(15,139,111,.1); }
.device-filter-label { margin-top:12px; }
.device-switch { display:grid; gap:6px; }
.device-switch-btn {
  width:100%; border:1px solid var(--border); border-radius:9px; background:#f8faf9;
  padding:8px 10px; text-align:left; color:var(--text); cursor:pointer;
  transition:border-color .16s ease,background .16s ease,transform .16s ease;
}
.device-switch-btn:hover { border-color:#8bcaba; background:#f0faf7; transform:translateY(-1px); }
.device-switch-btn.active { border-color:var(--accent); background:#e8f7f2; box-shadow:0 0 0 2px rgba(15,139,111,.09); }
.device-switch-btn strong { display:block; font-size:12px; line-height:1.35; }
.device-switch-btn span { display:block; margin-top:2px; font-size:10px; color:var(--muted); }
.device-overview-grid {
  display:grid; grid-template-columns:repeat(4,minmax(145px,1fr)); gap:10px;
  margin:12px 0 14px;
}
.device-overview-item {
  border:1px solid var(--border); background:#f8fbfa; border-radius:10px; padding:11px 12px;
}
.device-overview-label { font-size:11px; color:var(--muted); font-weight:700; }
.device-overview-value { margin-top:5px; font-size:20px; font-weight:900; font-variant-numeric:tabular-nums; }
.device-overview-note { margin-top:3px; font-size:10px; color:var(--muted); }
.device-row-action {
  border:0; background:transparent; color:var(--accent); font:inherit; font-weight:800;
  padding:0; cursor:pointer; text-align:left;
}
.device-row-action:hover { text-decoration:underline; }
@media (max-width:1100px) {
  .device-overview-grid { grid-template-columns:repeat(2,minmax(140px,1fr)); }
}
@media (max-width:640px) {
  .device-overview-grid { grid-template-columns:1fr; }
}

.sidebar-tabs { padding:8px 14px; display:flex; flex-direction:column; gap:4px; border-bottom:1px solid var(--border); }
.tab-btn {
  border:0; border-radius:8px; background:transparent; color:var(--muted); padding:8px 10px;
  cursor:pointer; font-weight:600; font-size:13px; text-align:left; transition:all .15s;
}
.tab-btn:hover { background:var(--bg); color:var(--ink); }
.tab-btn.active { background:var(--accent-light); color:var(--accent); }

.sidebar-footer { padding:12px 14px; font-size:11px; color:var(--muted); margin-top:auto; }

/* ===== MAIN HEADER ===== */
.main-header {
  padding:14px 22px; border-bottom:1px solid var(--border);
  background:rgba(255,255,255,.8); backdrop-filter:blur(12px);
  position:sticky; top:0; z-index:5; display:flex; align-items:center; gap:12px; flex-wrap:wrap;
}
.page-heading { min-width:220px; flex:1 1 260px; }
.page-kicker { font-size:11px; color:var(--muted); font-weight:700; letter-spacing:.08em; text-transform:uppercase; }
.page-title { font-size:20px; font-weight:900; line-height:1.15; margin-top:2px; }
.page-subtitle { font-size:12px; color:var(--muted); margin-top:3px; }
.search-box {
  flex:1 1 260px; min-width:180px; max-width:420px; padding:7px 12px;
  border:1px solid var(--border); border-radius:20px; background:var(--bg);
  font-size:13px; outline:none;
}
.search-box:focus { border-color:var(--accent); box-shadow:0 0 0 3px rgba(15,139,111,.08); }
.view-tabs { display:flex; gap:2px; background:var(--bg); border-radius:8px; padding:3px; }
.view-tab {
  border:0; border-radius:6px; background:transparent; color:var(--muted);
  padding:6px 14px; cursor:pointer; font-size:12px; font-weight:600; transition:all .15s; white-space:nowrap;
}
.view-tab.active { background:#fff; color:var(--ink); box-shadow:var(--shadow); }

.density-btn {
  border:1px solid var(--border); border-radius:8px; background:#fff;
  padding:6px 12px; cursor:pointer; font-size:12px; color:var(--muted); transition:all .15s;
}
.density-btn.active { background:var(--accent-light); color:var(--accent); border-color:var(--accent); }
.rag-header-link { display:none; align-items:center; min-height:44px; text-decoration:none; }

/* ===== MAIN CONTENT ===== */
.content { width:100%; max-width:1760px; min-width:0; margin:0 auto; padding:18px 22px; display:grid; gap:16px; }
.content > * { min-width:0; }
.content.is-hidden { display:none; }

/* ===== EXECUTIVE SUMMARY ===== */
.hero-grid { display:grid; grid-template-columns:minmax(260px,1.35fr) repeat(3,minmax(170px,1fr)); gap:12px; }
.hero-card {
  background:linear-gradient(145deg,#ffffff,#fbfaf7); border:1px solid var(--border);
  border-radius:16px; padding:16px; box-shadow:var(--shadow); min-height:118px;
  position:relative; overflow:hidden;
}
.hero-card::after {
  content:""; position:absolute; right:-28px; top:-28px; width:92px; height:92px;
  border-radius:50%; background:rgba(15,139,111,.08); pointer-events:none;
}
.hero-card.primary { background:linear-gradient(135deg,#0f8b6f,#2bb493); color:#fff; }
.hero-card.primary::after { background:rgba(255,255,255,.14); }
.hero-label { font-size:12px; color:var(--muted); font-weight:700; }
.hero-card.primary .hero-label { color:rgba(255,255,255,.78); }
.hero-value { font-size:clamp(20px,2vw,28px); font-weight:900; margin-top:8px; font-variant-numeric:tabular-nums; overflow-wrap:anywhere; }
.hero-note { font-size:12px; color:var(--muted); margin-top:8px; position:relative; z-index:1; }
.hero-card.primary .hero-note { color:rgba(255,255,255,.86); }
.hero-chip {
  display:inline-flex; align-items:center; gap:6px; padding:4px 8px; border-radius:99px;
  background:rgba(255,255,255,.18); font-size:12px; font-weight:700; margin-top:10px;
}
.section-grid { display:grid; grid-template-columns:minmax(0,1fr) minmax(320px,.72fr); gap:16px; align-items:start; }
.question-rank-list { display:grid; gap:7px; }
.question-rank-item {
  display:grid; grid-template-columns:28px minmax(0,1fr) auto; gap:10px; align-items:center;
  width:100%; padding:9px 10px; border:1px solid transparent; border-radius:10px; cursor:pointer;
  background:transparent; color:inherit; text-align:left;
  transition:background .15s,border-color .15s,transform .15s;
}
.question-rank-item:hover { background:var(--bg); border-color:var(--border); transform:translateX(2px); }
.question-rank-item.active { background:var(--accent-light); border-color:rgba(15,139,111,.28); }
.rank-no {
  width:24px; height:24px; border-radius:8px; display:grid; place-items:center;
  background:var(--bg); color:var(--muted); font-size:12px; font-weight:900;
}
.question-rank-item.active .rank-no { background:var(--accent); color:#fff; }
.rank-title { font-weight:800; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.rank-meta { color:var(--muted); font-size:11px; margin-top:2px; }
.rank-num { text-align:right; font-variant-numeric:tabular-nums; font-weight:900; }
.rank-num small { display:block; color:var(--muted); font-weight:600; font-size:11px; }
.insight-list { display:grid; gap:8px; }
.insight-row {
  padding:10px 12px; border-radius:10px; background:var(--bg);
  display:flex; justify-content:space-between; gap:12px; align-items:center;
}
.insight-row b { font-size:13px; }
.insight-row span { color:var(--muted); font-size:12px; text-align:right; }

/* ===== CARDS ===== */
.card {
  background:var(--card-bg); border:1px solid var(--border); border-radius:var(--radius);
  min-width:0; padding:16px; box-shadow:var(--shadow);
}
.card-header {
  display:flex; justify-content:space-between; align-items:center; gap:12px;
  margin-bottom:12px; flex-wrap:wrap;
}
.card-title { font-weight:800; font-size:15px; }
.card-hint { font-size:12px; color:var(--muted); }

/* ===== GRIDS ===== */
.grid-2 { display:grid; grid-template-columns:1fr 1fr; gap:16px; }
.grid-3 { display:grid; grid-template-columns:repeat(auto-fill,minmax(280px,1fr)); gap:12px; }
.grid-4 { display:grid; grid-template-columns:repeat(auto-fill,minmax(220px,1fr)); gap:12px; }

/* Secondary overview data stays available without occupying the initial screen. */
.collapsible-summary { margin-top:12px; }
.collapsible-summary > summary {
  list-style:none; cursor:pointer; user-select:none; display:flex; align-items:center;
  justify-content:space-between; padding:11px 14px; border:1px solid var(--border);
  border-radius:var(--radius); background:var(--card-bg); box-shadow:var(--shadow);
  font-weight:800;
}
.collapsible-summary > summary::-webkit-details-marker { display:none; }
.collapsible-summary > summary::after { content:"展开"; color:var(--accent); font-size:12px; font-weight:700; }
.collapsible-summary[open] > summary { border-radius:var(--radius) var(--radius) 0 0; }
.collapsible-summary[open] > summary::after { content:"收起"; }
.collapsible-summary[open] > .section-grid {
  padding:12px; border:1px solid var(--border); border-top:0;
  border-radius:0 0 var(--radius) var(--radius); background:rgba(255,255,255,.55);
}

/* ===== BAR CHARTS ===== */
.bar-item {
  display:grid; grid-template-columns:28px minmax(0,1.6fr) minmax(0,2.5fr) 92px 150px;
  gap:10px; align-items:center; padding:5px 8px; border-radius:6px; transition:background .15s; font-size:13px;
}
.bar-item:hover { background:var(--bg); box-shadow:inset 3px 0 0 var(--accent); }
.bar-rank { color:var(--muted); font-weight:800; font-size:11px; text-align:center; }
.bar-name { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.bar-track { height:8px; border-radius:99px; background:#eee; overflow:hidden; }
.bar-fill { height:100%; border-radius:99px; transition:width .4s ease; }
.bar-fill.type-文章, .bar-fill.type-网站 { background:var(--blue); }
.bar-fill.type-视频 { background:var(--orange); }
.bar-fill.type-商品页 { background:var(--accent); }
.bar-fill.type-其他 { background:#aaa; }
.bar-fill.type-default { background:linear-gradient(90deg,var(--accent),#54c0a4); }
.bar-val { text-align:right; font-size:12px; line-height:1.3; }
.bar-val .pct { font-weight:700; }
.bar-val .abs { color:var(--muted); font-size:11px; display:block; }
.product-bar-item { grid-template-columns:28px minmax(0,1.5fr) minmax(0,2.2fr) 142px 150px; }
.product-ratio .mention-share { color:var(--muted); font-size:10px; display:block; margin-top:2px; }

/* ===== QUESTION CARDS ===== */
.q-card {
  background:var(--card-bg); border:1px solid var(--border); border-radius:var(--radius);
  padding:14px 16px; box-shadow:var(--shadow); transition:box-shadow .2s;
}
.q-card:hover { box-shadow:0 4px 16px rgba(0,0,0,.1); }
.q-card-head {
  display:flex; justify-content:space-between; align-items:baseline;
  gap:10px; margin-bottom:10px; flex-wrap:wrap;
}
.q-card-title { font-weight:800; font-size:15px; }
.q-card-stats { font-size:12px; color:var(--muted); }
.q-card-body { display:grid; grid-template-columns:1fr 1fr; gap:12px; }
.mini-rank-card { border:1px solid var(--border); border-radius:14px; padding:12px; background:#fff; }
.mini-rank-card:hover { box-shadow:0 6px 18px rgba(0,0,0,.08); border-color:rgba(15,139,111,.22); }
.mini-rank-head { display:flex; justify-content:space-between; align-items:center; gap:10px; margin-bottom:8px; }
.mini-rank-title { font-weight:800; color:var(--ink); }

/* ===== DAILY CARDS ===== */
.daily-card {
  background:var(--card-bg); border:1px solid var(--border); border-radius:var(--radius);
  min-width:0; max-width:100%; box-shadow:var(--shadow); overflow:hidden;
}
.daily-card-head {
  padding:14px 16px 10px; display:flex; justify-content:space-between;
  align-items:baseline; gap:10px; flex-wrap:wrap;
}
.daily-table-wrap { width:100%; min-width:0; max-width:100%; overflow-x:auto; padding:0 0 8px; }
.daily-table { width:100%; border-collapse:collapse; font-size:13px; }
.daily-table th,.daily-table td { padding:8px 10px; border-bottom:1px solid var(--border); white-space:nowrap; }
.daily-table th { background:var(--bg); color:var(--muted); font-weight:700; font-size:12px; position:sticky; top:0; z-index:1; }
.daily-table th:first-child,.daily-table td:first-child { text-align:left; position:sticky; left:0; background:#fff; z-index:1; min-width:140px; max-width:280px; overflow:hidden; text-overflow:ellipsis; }
.daily-table th.today-col { background:#eaf9f4; color:var(--accent); }
.daily-table td.today-col { background:#f7fcfa; }
.daily-table td { text-align:right; }
.daily-table .total-row td { font-weight:700; background:var(--bg); border-bottom:2px solid var(--border); }
.daily-table .total-row td:first-child { background:var(--bg); }
.daily-table tbody tr:hover td { background:#fafaf8; }
.daily-table tbody tr:hover td:first-child { background:#fafaf8; }
.daily-pct { font-weight:700; font-size:13px; } .daily-abs { font-size:11px; color:var(--muted); display:block; }
.daily-subtitle { font-weight:800; margin:12px 0 8px; color:var(--ink); }
.delta-up { color:var(--accent); font-weight:700; }
.delta-down { color:var(--danger); font-weight:700; }
.delta-flat { color:var(--muted); }
.delta-pos { color:var(--accent); font-weight:700; }
.delta-neg { color:var(--danger); font-weight:700; }

.trend-bar { display:inline-block; height:4px; min-width:3px; border-radius:2px; vertical-align:middle; margin-right:2px; background:var(--accent); opacity:.6; }

/* ===== TOP LINKS ===== */
.top-links { margin-top:12px; padding:8px 12px; background:var(--bg); border-radius:8px; }
.top-links-title { font-size:12px; font-weight:700; color:var(--muted); margin-bottom:6px; }
.top-links-row { display:flex; flex-wrap:wrap; gap:4px; align-items:center; margin-bottom:4px; }
.top-links-day-label { font-size:11px; color:var(--muted); min-width:60px; flex-shrink:0; }
.top-link-tag {
  display:inline-flex; align-items:center; gap:3px; padding:3px 10px; border-radius:99px;
  font-size:12px; white-space:normal; max-width:100%; word-break:break-all;
  transition:opacity .15s; cursor:pointer; text-decoration:none; color:inherit;
}
.top-link-tag:hover { opacity:.8; }
.top-link-tag.own-source { box-shadow:inset 3px 0 0 var(--accent); }
.own-content-mark {
  display:inline-flex; align-items:center; gap:5px; flex-shrink:0; padding:2px 8px; border-radius:99px;
  background:rgba(15,139,111,.11); color:var(--accent); border:1px solid rgba(15,139,111,.24);
  font-size:11px; font-weight:700;
}
.own-content-mark::before {
  content:""; width:6px; height:6px; border-radius:50%; background:var(--accent); flex:0 0 auto;
}
.own-match-scope { opacity:.72; font-style:normal; font-weight:600; }
.tag-video { background:var(--orange-light); color:#9a6b14; }
.tag-article { background:var(--blue-light); color:var(--blue); }
.tag-count { font-weight:800; font-size:12px; flex-shrink:0; padding:1px 6px; border-radius:99px; background:rgba(255,255,255,.7); }
.type-badge {
  display:inline-block; padding:1px 5px; border-radius:3px; font-size:10px; font-weight:700;
  flex-shrink:0; margin-right:2px;
}
.type-badge-video { background:rgba(230,159,35,.15); color:#b8781a; }
.type-badge-article { background:rgba(9,105,218,.12); color:var(--blue); }

/* ===== TABLE ===== */
.data-table-wrap { max-height:500px; overflow:auto; border-radius:var(--radius); border:1px solid var(--border); }
.data-table { width:100%; border-collapse:collapse; font-size:13px; }
.data-table th,.data-table td { padding:8px 10px; border-bottom:1px solid var(--border); }
.data-table th { background:var(--bg); color:var(--muted); font-weight:700; position:sticky; top:0; z-index:2; }
.data-table td { vertical-align:top; }
.data-table td:nth-child(2) { max-width:500px; overflow:hidden; text-overflow:ellipsis; }
.data-table tbody tr:hover { background:rgba(15,139,111,.04); }
.data-table tbody tr.own-source-row { background:rgba(15,139,111,.08); }
.source-badge {
  display:inline-flex; align-items:center; padding:2px 7px; border-radius:99px;
  background:var(--bg); color:var(--ink); font-size:12px; font-weight:700;
}
.source-badge.video { background:var(--orange-light); color:#9a6b14; }
.source-badge.article { background:var(--blue-light); color:var(--blue); }
.source-badge.product { background:var(--accent-light); color:var(--accent); }
.product-summary { display:grid; grid-template-columns:repeat(4, minmax(0,1fr)); gap:12px; margin-bottom:14px; }
.product-stat { background:var(--bg); border:1px solid var(--border); border-radius:10px; padding:12px; }
.product-stat b { display:block; font-size:22px; line-height:1.1; margin-top:4px; }
.product-pill-list { display:flex; flex-wrap:wrap; gap:8px; }
.product-pill {
  display:inline-flex; align-items:center; gap:6px; padding:7px 10px; border-radius:999px;
  background:var(--accent-light); color:var(--accent); font-weight:700; font-size:13px;
}
.product-pill small { color:var(--muted); font-weight:600; }
.product-pill .rank-meta-inline { color:#5f7770; font-size:11px; font-weight:700; }
.product-evidence { color:var(--muted); font-size:12px; max-width:680px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.latest-product-list { display:grid; gap:10px; }
.latest-product-card {
  display:grid; grid-template-columns:36px minmax(160px,.9fr) minmax(0,1.4fr);
  gap:12px; align-items:start; padding:10px 12px; border:1px solid var(--border);
  border-radius:12px; background:linear-gradient(135deg,#fff,#fbfaf7);
}
.latest-product-card:hover { background:var(--accent-light); border-color:rgba(15,139,111,.28); }
.latest-product-rank {
  width:28px; height:28px; border-radius:9px; display:grid; place-items:center;
  background:var(--accent); color:#fff; font-weight:900; font-size:13px;
}
.latest-product-main { min-width:0; }
.latest-product-name { font-weight:900; line-height:1.35; word-break:break-word; }
.latest-product-brand { color:var(--accent); font-size:12px; font-weight:800; margin-top:3px; }
.latest-product-evidence {
  color:var(--muted); font-size:12px; line-height:1.45; max-height:54px; overflow:auto;
  word-break:break-word;
}
.bar-rankmeta {
  color:var(--muted); font-size:11px; line-height:1.25; min-width:136px; text-align:right;
  font-variant-numeric:tabular-nums;
}

/* ===== DECISION COCKPIT ===== */
.decision-layout { display:grid; grid-template-columns:minmax(0,1.05fr) minmax(360px,.95fr); gap:16px; align-items:stretch; }
.decision-card { min-width:0; }
.decision-state { display:flex; align-items:flex-start; justify-content:space-between; gap:12px; margin-bottom:12px; }
.decision-state-main { min-width:0; }
.decision-state-title { font-size:18px; font-weight:900; line-height:1.3; }
.decision-state-note { color:var(--muted); font-size:12px; margin-top:4px; }
.status-label { display:inline-flex; align-items:center; min-height:28px; padding:4px 9px; border-radius:999px; font-size:12px; font-weight:800; white-space:nowrap; }
.status-label.ok { color:var(--accent); background:var(--accent-light); }
.status-label.warning { color:var(--warning); background:var(--warning-light); }
.status-label.danger { color:var(--danger); background:var(--danger-light); }
.decision-metrics { display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:8px; }
.decision-metric { min-width:0; padding:10px; border:1px solid var(--border); border-radius:11px; background:var(--bg); }
.decision-metric span { display:block; color:var(--muted); font-size:11px; }
.decision-metric b { display:block; margin-top:4px; font-size:18px; line-height:1.15; font-variant-numeric:tabular-nums; overflow-wrap:anywhere; }
.decision-metric small { display:block; color:var(--muted); font-size:11px; margin-top:4px; }
.strategy-list { display:grid; gap:8px; }
.strategy-signal { display:grid; grid-template-columns:34px minmax(0,1fr) auto; gap:10px; align-items:start; padding:9px 0; border-bottom:1px solid var(--border); }
.strategy-signal:last-child { border-bottom:0; padding-bottom:0; }
.signal-mark { width:30px; height:30px; border-radius:9px; display:grid; place-items:center; background:var(--accent-light); color:var(--accent); font-weight:900; }
.strategy-signal.warning .signal-mark { background:var(--warning-light); color:var(--warning); }
.strategy-signal.danger .signal-mark { background:var(--danger-light); color:var(--danger); }
.signal-title { font-weight:850; line-height:1.35; }
.signal-evidence { color:var(--muted); font-size:12px; margin-top:2px; }
.signal-action { border:1px solid var(--border); background:var(--card-bg); color:var(--ink); border-radius:9px; min-height:34px; padding:5px 9px; cursor:pointer; font-size:12px; white-space:nowrap; }
.signal-action:hover { border-color:var(--accent); color:var(--accent); }

.methodology-summary > summary { cursor:pointer; color:var(--muted); font-size:12px; font-weight:750; padding:3px 2px; }
.methodology-grid { display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:8px; margin-top:9px; }
.methodology-item { padding:9px 10px; border-left:3px solid var(--border); background:rgba(255,255,255,.48); }
.methodology-item b { display:block; font-size:12px; }
.methodology-item span { display:block; color:var(--muted); font-size:11px; margin-top:2px; }

/* ===== AUDIT / COVERAGE ===== */
.coverage-panel { display:grid; gap:10px; }
.audit-funnel { display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:8px; }
.audit-step { position:relative; min-width:0; padding:10px 11px; border:1px solid var(--border); border-radius:11px; background:var(--bg); }
.audit-step:not(:last-child)::after { content:"→"; position:absolute; right:-8px; top:50%; transform:translateY(-50%); z-index:1; color:var(--muted); font-weight:900; }
.audit-step span { display:block; color:var(--muted); font-size:11px; }
.audit-step b { display:block; font-size:19px; margin-top:3px; font-variant-numeric:tabular-nums; }
.audit-step small { color:var(--muted); font-size:11px; }
.quality-chips { display:flex; flex-wrap:wrap; gap:6px; }
.quality-chip { display:inline-flex; align-items:center; gap:5px; min-height:28px; padding:4px 8px; border-radius:999px; background:var(--accent-light); color:var(--accent); font-size:11px; font-weight:800; }
.quality-chip.warning { background:var(--warning-light); color:var(--warning); }
.quality-chip.danger { background:var(--danger-light); color:var(--danger); }
.audit-details > summary { cursor:pointer; color:var(--muted); font-size:12px; font-weight:750; }
.audit-groups { display:grid; gap:7px; margin-top:8px; }
.audit-group { padding:8px 10px; background:var(--bg); border-radius:9px; font-size:12px; overflow-wrap:anywhere; }
.audit-group b { margin-right:6px; }

.coverage-label { display:inline-flex; align-items:center; gap:5px; padding:3px 7px; border-radius:999px; background:var(--accent-light); color:var(--accent); font-size:11px; font-weight:800; }
.coverage-label.warning { background:var(--warning-light); color:var(--warning); }
.ci-note { display:block; color:var(--muted); font-size:10px; margin-top:2px; }
.bar-track { position:relative; }
.bar-fill { min-width:0; }
#productBars,#mediaBars,#typeBars,#domainBars { container-type:inline-size; }
.scroll-hint { display:none; color:var(--muted); font-size:11px; margin:0 0 6px; }

/* ===== BRAND TREND + SOURCE RELATION ===== */
.analysis-card { container-type:inline-size; container-name:brand-analysis; }
.analysis-head { display:flex; align-items:flex-start; justify-content:space-between; gap:14px; flex-wrap:wrap; margin-bottom:12px; }
.analysis-title { font-size:18px; font-weight:900; line-height:1.3; }
.analysis-desc { margin-top:3px; color:var(--muted); font-size:12px; max-width:860px; }
.analysis-toolbar { display:flex; align-items:end; flex-wrap:wrap; gap:8px; }
.analysis-field { display:grid; gap:3px; min-width:142px; }
.analysis-field.metric-field { min-width:220px; }
.analysis-field label { color:var(--muted); font-size:11px; font-weight:750; }
.analysis-field select { min-height:38px; max-width:260px; padding:6px 30px 6px 9px; border:1px solid var(--border); border-radius:9px; background:var(--card-bg); color:var(--ink); }
.analysis-check { min-height:38px; display:flex; align-items:center; gap:7px; padding:0 8px; color:var(--muted); font-size:12px; }
.analysis-check input { width:17px; height:17px; accent-color:var(--accent); }
.analysis-kpis { display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:8px; margin-bottom:14px; }
.analysis-kpi { min-width:0; padding:10px 11px; border:1px solid var(--border); border-radius:11px; background:var(--bg); }
.analysis-kpi span { display:block; color:var(--muted); font-size:11px; }
.analysis-kpi b { display:block; margin-top:4px; font-size:19px; line-height:1.15; font-variant-numeric:tabular-nums; overflow-wrap:anywhere; }
.analysis-kpi small { display:block; margin-top:3px; color:var(--muted); font-size:11px; }
.analysis-grid { display:grid; grid-template-columns:minmax(0,1.35fr) minmax(330px,.85fr); gap:16px; align-items:start; }
.chart-panel { min-width:0; }
.chart-panel-head { display:flex; align-items:baseline; justify-content:space-between; gap:10px; flex-wrap:wrap; margin-bottom:6px; }
.chart-panel-title { font-weight:850; }
.chart-panel-meta { color:var(--muted); font-size:11px; }
.chart-frame { position:relative; width:100%; min-width:0; height:390px; border-top:1px solid var(--border); }
.chart-frame.scatter-frame { height:390px; }
.analysis-svg { display:block; width:100%; height:100%; overflow:hidden; touch-action:pan-y; }
.analysis-svg .chart-grid { stroke:var(--border); stroke-width:1; }
.analysis-svg .chart-axis { stroke:var(--muted); stroke-width:1; }
.analysis-svg .chart-tick { fill:var(--muted); font-size:11px; }
.analysis-svg .chart-label { fill:var(--ink); font-size:11px; font-weight:700; }
.analysis-svg .mention-line { fill:none; stroke:var(--accent); stroke-width:2.5; }
.analysis-svg .source-line { fill:none; stroke:var(--blue); stroke-width:2.2; stroke-dasharray:6 4; }
.analysis-svg .mention-dot { fill:var(--accent); stroke:var(--card-bg); stroke-width:2; }
.analysis-svg .source-dot { fill:var(--card-bg); stroke:var(--blue); stroke-width:2; }
.analysis-svg .partial-dot { stroke-dasharray:2 2; opacity:.55; }
.analysis-svg .ci-band { fill:rgba(8,127,101,.10); stroke:none; }
.analysis-svg .scatter-dot { fill:var(--blue); stroke:var(--card-bg); stroke-width:2; }
.analysis-svg .scatter-dot.partial { fill:var(--card-bg); stroke:var(--muted); stroke-dasharray:3 2; }
.analysis-svg .regression-line { stroke:var(--orange); stroke-width:2; stroke-dasharray:7 5; }
.analysis-svg .selected-guide { stroke:var(--muted); stroke-width:1; stroke-dasharray:3 3; }
.chart-tooltip { position:absolute; z-index:3; display:none; pointer-events:none; max-width:260px; padding:8px 10px; border-radius:9px; background:#17211d; color:#fff; font-size:11px; line-height:1.45; box-shadow:var(--shadow); }
.chart-tooltip.is-visible { display:block; }
.chart-legend { display:flex; flex-wrap:wrap; gap:10px; margin-top:7px; color:var(--muted); font-size:11px; }
.legend-key { display:inline-flex; align-items:center; gap:5px; }
.legend-line { width:22px; height:3px; border-radius:3px; background:var(--accent); }
.legend-line.source { height:0; border-top:2px dashed var(--blue); background:transparent; }
.legend-line.owned-article { background:var(--accent); }
.legend-line.owned-video { background:var(--blue); }
.legend-line.unique { height:0; background:transparent; border-top:2px dashed currentColor; }
.legend-line.owned-article.unique { color:var(--accent); }
.legend-line.owned-video.unique { color:var(--blue); }
.analysis-svg .owned-article-line { fill:none; stroke:var(--accent); stroke-width:2.5; }
.analysis-svg .owned-video-line { fill:none; stroke:var(--blue); stroke-width:2.5; }
.analysis-svg .owned-unique-line { stroke-dasharray:6 4; stroke-width:1.8; opacity:.82; }
.analysis-svg .owned-article-dot { fill:var(--card-bg); stroke:var(--accent); stroke-width:2; }
.analysis-svg .owned-video-dot { fill:var(--card-bg); stroke:var(--blue); stroke-width:2; }
.analysis-svg .theme-article { fill:var(--accent); opacity:.82; }
.analysis-svg .theme-video { fill:var(--blue); opacity:.82; }
.keyword-trend-section { margin-top:18px; padding-top:12px; border-top:1px solid var(--border); }
.keyword-trend-howto { margin:4px 0 8px; color:var(--muted); font-size:11px; line-height:1.5; }
.keyword-trend-howto b { color:var(--ink); }
.keyword-trend-picker { display:flex; flex-wrap:wrap; gap:6px; margin:6px 0 8px; }
.keyword-trend-option { min-height:28px; padding:4px 9px; border:1px solid var(--border); border-radius:999px; background:var(--card-bg); color:var(--ink); cursor:pointer; font-size:11px; }
.keyword-trend-option:hover { border-color:var(--accent); }
.keyword-trend-option.active { border-color:var(--accent); background:var(--accent-light); color:var(--accent); font-weight:800; }
.keyword-trend-frame { position:relative; width:100%; min-width:0; height:360px; }
.analysis-svg .keyword-cell-base { fill:var(--bg); stroke:var(--border); stroke-width:1; }
.analysis-svg .keyword-cell-up { fill:var(--accent); }
.analysis-svg .keyword-cell-down { fill:var(--warning); }
.analysis-svg .keyword-cell-empty { fill:var(--bg); stroke:var(--border); stroke-width:1; stroke-dasharray:3 2; }
.analysis-svg .keyword-cell-text { fill:var(--ink); font-size:9.5px; font-weight:700; pointer-events:none; }
.analysis-svg .keyword-cell-subtext { fill:var(--ink); font-size:9px; opacity:.76; pointer-events:none; }
.keyword-trend-legend { display:flex; flex-wrap:wrap; gap:10px; margin-top:5px; color:var(--muted); font-size:11px; }
.keyword-trend-legend .legend-key[hidden] { display:none; }
.keyword-trend-key { display:inline-flex; align-items:center; gap:5px; }
.keyword-trend-swatch { width:13px; height:13px; border-radius:3px; background:var(--bg); border:1px solid var(--border); }
.keyword-trend-swatch.up { background:var(--accent-light); border-color:var(--accent); }
.keyword-trend-swatch.down { background:var(--warning-light); border-color:var(--warning); }
.keyword-trend-summary { margin-top:8px; padding-left:9px; border-left:3px solid var(--accent); color:var(--ink); font-size:11px; line-height:1.55; }
.owned-keyword-groups { display:grid; grid-template-columns:repeat(auto-fit,minmax(145px,1fr)); gap:10px; }
.owned-keyword-groups > div { min-width:0; }
.owned-keyword-groups b { display:block; margin-bottom:5px; font-size:11px; color:var(--muted); }
.owned-keyword-meta { display:block; margin:-2px 0 6px; color:var(--muted); font-size:10px; line-height:1.35; }
.owned-keyword-list { display:flex; flex-wrap:wrap; gap:5px; }
.owned-keyword-list span { display:inline-flex; min-height:24px; align-items:center; padding:3px 7px; border-radius:999px; background:var(--accent-light); color:var(--ink); font-size:11px; }
.correlation-summary { display:grid; gap:9px; margin-top:8px; }
.correlation-stats { display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:7px; }
.correlation-stat { padding:9px; border-radius:10px; background:var(--bg); border:1px solid var(--border); }
.correlation-stat span { display:block; color:var(--muted); font-size:11px; }
.correlation-stat b { display:block; margin-top:3px; font-size:18px; font-variant-numeric:tabular-nums; }
.correlation-message { padding:9px 10px; border-left:3px solid var(--accent); background:var(--accent-light); font-size:12px; }
.correlation-message.warning { border-left-color:var(--warning); background:var(--warning-light); }
.correlation-message.danger { border-left-color:var(--danger); background:var(--danger-light); }
.analysis-note { margin-top:12px; padding-top:10px; border-top:1px solid var(--border); color:var(--muted); font-size:11px; }
.analysis-empty { padding:30px 12px; text-align:center; color:var(--muted); }
.analysis-data { margin-top:12px; }
.analysis-data > summary { cursor:pointer; color:var(--muted); font-size:12px; font-weight:750; }
.analysis-data-table-wrap { max-width:100%; overflow-x:auto; margin-top:8px; border:1px solid var(--border); border-radius:10px; }
.analysis-data-table { width:100%; min-width:940px; border-collapse:collapse; font-size:12px; }
.analysis-data-table th,.analysis-data-table td { padding:7px 9px; border-bottom:1px solid var(--border); text-align:right; white-space:nowrap; }
.analysis-data-table th { background:var(--bg); color:var(--muted); position:sticky; top:0; }
.analysis-data-table th:first-child,.analysis-data-table td:first-child { text-align:left; position:sticky; left:0; background:var(--card-bg); }
.quality-status { display:inline-flex; align-items:center; min-height:24px; padding:2px 7px; border-radius:999px; background:var(--accent-light); color:var(--accent); font-size:11px; font-weight:800; }
.quality-status.partial,.quality-status.incomplete { background:var(--warning-light); color:var(--warning); }
.quality-status.data_unavailable { background:var(--danger-light); color:var(--danger); }

body.compact .content { gap:10px; padding-top:12px; padding-bottom:12px; }
body.compact .card,body.compact .q-card { padding:11px 12px; }
body.compact .hero-card { min-height:94px; padding:12px; }
body.compact .hero-value { margin-top:4px; }
body.compact .bar-item { padding-top:3px; padding-bottom:3px; }
body.compact .latest-product-card { padding:7px 9px; }

/* ===== LOG ===== */
.log-box { max-height:220px; overflow:auto; background:#1a1d1c; color:#b0d6c8; border-radius:var(--radius); padding:12px; font:11px/1.5 Consolas,monospace; white-space:pre-wrap; }

.empty { text-align:center; padding:32px; color:var(--muted); }

.is-muted { display:none !important; }

/* ===== RESPONSIVE ===== */
@media (min-width:1101px) {
  #mainViewTabs { display:none; }
}
@media (max-width:1400px) {
  .sidebar { width:240px; }
  :root { --sidebar-w:240px; }
}
@container dashboard (max-width:1120px) {
  .grid-2,.q-card-body,.section-grid,.decision-layout,.analysis-grid { grid-template-columns:1fr; }
  .hero-grid { grid-template-columns:1fr 1fr; }
  .latest-product-card { grid-template-columns:32px 1fr; }
  .latest-product-evidence { grid-column:2 / -1; }
}
@container dashboard (max-width:720px) {
  .bar-item,.product-bar-item {
    grid-template-columns:24px minmax(0,1fr) auto;
    grid-template-areas:"rank name value" ". track track" ". meta meta";
    gap:5px 8px; padding:8px 4px;
  }
  .bar-rank { grid-area:rank; }
  .bar-name { grid-area:name; font-weight:750; }
  .bar-track { grid-area:track; display:block; }
  .bar-val { grid-area:value; }
  .bar-rankmeta { grid-area:meta; min-width:0; text-align:left; }
}
@container dashboard (max-width:620px) {
  .hero-grid { grid-template-columns:1fr; }
  .decision-metrics,.audit-funnel { grid-template-columns:1fr 1fr; }
  .methodology-grid { grid-template-columns:1fr; }
  .strategy-signal { grid-template-columns:32px minmax(0,1fr); }
  .signal-action { grid-column:2; justify-self:start; }
  .audit-step:nth-child(2)::after { display:none; }
  .analysis-toolbar { display:grid; grid-template-columns:1fr 1fr; width:100%; }
  .analysis-field,.analysis-field.metric-field { min-width:0; }
  .analysis-field select { width:100%; max-width:none; min-height:44px; font-size:16px; }
  .analysis-check { min-height:44px; }
  .analysis-kpis { grid-template-columns:1fr 1fr; }
  .chart-frame { height:410px; }
  .chart-frame.scatter-frame { height:340px; }
}
@media (max-width:1100px) {
  .app { display:block; min-height:100dvh; }
  .sidebar {
    width:100%; height:auto; position:relative; border-right:0; border-bottom:1px solid var(--border);
    display:grid; grid-template-columns:auto minmax(240px,1fr); align-items:end; overflow:visible; padding:0 14px;
  }
  .sidebar-header { padding:11px 0; border:0; }
  .sidebar-nav { padding:9px 0 10px 16px; border:0; }
  .sidebar-nav select { min-height:44px; font-size:16px; }
  .sidebar-metrics,.sidebar-tabs,.sidebar-footer { display:none; }
  .main-header { top:0; padding:9px 14px; }
  .page-heading { display:none; }
  .search-box { min-height:44px; font-size:16px; }
  #mainViewTabs { display:flex; flex:1 1 100%; width:100%; max-width:100%; overflow-x:auto; scroll-snap-type:x proximity; }
  #mainViewTabs { scrollbar-width:none; }
  #mainViewTabs::-webkit-scrollbar { display:none; }
  .view-tab { min-height:42px; flex:1 0 auto; scroll-snap-align:start; }
  .density-btn { min-height:44px; }
  .rag-header-link { display:inline-flex; }
  .content { padding:14px; }
  .grid-3,.grid-4 { grid-template-columns:repeat(2,minmax(0,1fr)); }
  .product-summary { grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); }
}
@media (max-width:680px) {
  .sidebar { grid-template-columns:1fr; padding:0 12px; }
  .sidebar-header { padding-bottom:5px; }
  .sidebar-nav { padding:4px 0 10px; }
  .sidebar-status { margin-top:2px; }
  .main-header { position:relative; padding:9px 12px; }
  .page-heading { display:none; }
  .search-box { flex-basis:100%; max-width:none; }
  .view-tabs { display:flex!important; overflow-x:auto!important; scroll-snap-type:x proximity; }
  .view-tab { flex:0 0 auto; min-width:104px; padding:7px 10px; white-space:nowrap; }
  #densityToggle { display:none; }
  .rag-header-link { margin-left:auto; }
  .content { padding:10px; gap:12px; }
  .card,.q-card { padding:13px; }
  .hero-card { min-height:0; padding:14px; }
  .grid-3,.grid-4 { grid-template-columns:1fr; }
  .scroll-hint { display:block; }
  .daily-table-wrap { border:1px solid var(--border); border-radius:10px; overscroll-behavior-inline:contain; }
  .daily-table { width:max-content; min-width:100%; }
  .daily-table th:first-child,.daily-table td:first-child { min-width:118px; max-width:150px; }
  .daily-table th:last-child,.daily-table td:last-child { position:sticky; right:0; z-index:1; background:var(--card-bg); box-shadow:-7px 0 10px rgba(18,32,26,.06); }
  .daily-table th:last-child { z-index:2; background:var(--bg); }
  .data-table-wrap { max-height:none; overflow:visible; border:0; }
  .data-table,.data-table tbody,.data-table tr,.data-table td { display:block; width:100%; }
  .data-table thead { display:none; }
  .data-table tr { margin-bottom:9px; padding:9px 10px; border:1px solid var(--border); border-radius:11px; background:var(--card-bg); }
  .data-table td { display:grid; grid-template-columns:74px minmax(0,1fr); gap:8px; padding:4px 0; border:0; max-width:none!important; overflow:visible!important; }
  .data-table td::before { content:attr(data-label); color:var(--muted); font-size:11px; font-weight:700; }
  .latest-product-card { grid-template-columns:30px minmax(0,1fr); padding:9px; }
  .latest-product-evidence { grid-column:1 / -1; }
  .product-evidence { white-space:normal; }
}
@media (max-width:380px) {
  .audit-step::after { display:none!important; }
  .product-summary { grid-template-columns:1fr 1fr; gap:7px; }
  .product-stat { padding:9px; }
  .product-stat b { font-size:18px; }
  .analysis-toolbar { grid-template-columns:1fr; }
  .analysis-kpi { padding:8px; }
  .analysis-kpi b { font-size:17px; }
  .correlation-stats { grid-template-columns:1fr 1fr; }
}
@media (prefers-reduced-motion:reduce) {
  *,*::before,*::after { scroll-behavior:auto!important; transition-duration:.01ms!important; animation-duration:.01ms!important; animation-iteration-count:1!important; }
}
</style>
</head>
<body>

<div class="app">
  <!-- ===== SIDEBAR ===== -->
  <aside class="sidebar" aria-label="面板导航与问题范围">
    <div class="sidebar-header">
      <div class="sidebar-logo"><i>&#9670;</i> 豆包实时面板</div>
      <div class="sidebar-status"><span class="status-dot" aria-hidden="true"></span><span id="status" aria-live="polite">连接中...</span></div>
    </div>
    <div class="sidebar-metrics">
      <div class="metric-mini"><span class="metric-mini-label">总轮次</span><span class="metric-mini-val" id="totalRuns">-</span></div>
      <div class="metric-mini"><span class="metric-mini-label">总链接</span><span class="metric-mini-val" id="totalRefs">-</span></div>
      <div class="metric-mini"><span class="metric-mini-label">去重链接</span><span class="metric-mini-val" id="uniqueLinks">-</span></div>
      <div class="metric-mini"><span class="metric-mini-label">最新轮次</span><span class="metric-mini-val" id="latestRun">-</span></div>
      <div class="metric-mini"><span class="metric-mini-label">最新抓取</span><span class="metric-mini-val" id="latestRefs">-</span></div>
      <div class="metric-mini"><span class="metric-mini-label">问题数</span><span class="metric-mini-val" id="complete">-</span></div>
    </div>
    <div class="sidebar-nav">
      <label for="questionSelect">当前问题</label>
      <select id="questionSelect"></select>
      <label class="device-filter-label">设备视角</label>
      <div class="device-switch" id="deviceSwitch" role="group" aria-label="选择全部或单个历史采集实例">
        <button type="button" class="device-switch-btn active" data-device="all"><strong>全部设备</strong><span>合并总览与每设备平均值</span></button>
      </div>
    </div>
    <div class="sidebar-tabs" id="sidebarViewTabs" role="tablist" aria-label="主要分析视图">
      <button type="button" class="tab-btn active" role="tab" aria-selected="true" data-view="overview">&#9776; 策略总览</button>
      <button type="button" class="tab-btn" role="tab" aria-selected="false" data-view="question">&#9679; 信源策略</button>
      <button type="button" class="tab-btn" role="tab" aria-selected="false" data-view="product">&#9733; 品牌竞争</button>
      <button type="button" class="tab-btn" role="tab" aria-selected="false" data-view="daily">&#8644; 趋势与异常</button>
      <button type="button" class="tab-btn" role="tab" aria-selected="false" data-view="latest">&#9776; 证据明细</button>
      <button type="button" class="tab-btn" role="tab" aria-selected="false" data-view="support">&#9881; 数据审计</button>
      <a class="tab-btn" href="/rag-lab" style="text-decoration:none">&#9670; RAG 机器学习诊断</a>
    </div>
    <div class="sidebar-footer" id="fileInfo">等待加载...</div>
  </aside>

    <!-- ===== MAIN ===== -->
  <div class="main">
    <div class="main-header">
      <div class="page-heading">
        <div class="page-kicker">Source Intelligence</div>
        <div class="page-title" id="pageTitle">信源监控</div>
        <div class="page-subtitle" id="pageSubtitle">正在读取最新数据...</div>
      </div>
      <label class="sr-only" for="filterInput">搜索当前视图</label>
      <input class="search-box" id="filterInput" placeholder="搜索品牌、媒体、域名或标题...">
      <div class="view-tabs" id="mainViewTabs" role="tablist" aria-label="主要分析视图">
        <button type="button" class="view-tab active" role="tab" aria-selected="true" data-view="overview">策略总览</button>
        <button type="button" class="view-tab" role="tab" aria-selected="false" data-view="question">信源策略</button>
        <button type="button" class="view-tab" role="tab" aria-selected="false" data-view="product">品牌竞争</button>
        <button type="button" class="view-tab" role="tab" aria-selected="false" data-view="daily">趋势异常</button>
        <button type="button" class="view-tab" role="tab" aria-selected="false" data-view="latest">证据明细</button>
        <button type="button" class="view-tab" role="tab" aria-selected="false" data-view="support">数据审计</button>
      </div>
      <button type="button" class="density-btn" id="densityToggle" aria-pressed="false">紧凑</button>
      <a class="density-btn rag-header-link" href="/rag-lab">RAG诊断</a>
    </div>

    <!-- OVERVIEW -->
    <section class="content" data-view-group="overview">
      <div class="hero-grid">
        <div class="hero-card primary">
          <div class="hero-label">当前观察问题</div>
          <div class="hero-value" id="heroQuestion">-</div>
          <div class="hero-note" id="heroQuestionNote">选择左侧问题后，可查看单个问题的信源结构。</div>
          <div class="hero-chip" id="heroStatus">实时刷新中</div>
        </div>
        <div class="hero-card">
          <div class="hero-label">采集样本轮次</div>
          <div class="hero-value" id="heroRefs">-</div>
          <div class="hero-note" id="heroRefsNote">信源轮次：-</div>
        </div>
        <div class="hero-card">
          <div class="hero-label">Top1 信源集中度</div>
          <div class="hero-value" id="heroTopMedia">-</div>
          <div class="hero-note" id="heroTopMediaNote">引用行占比分母：总引用条数</div>
        </div>
        <div class="hero-card">
          <div class="hero-label">最新轮次完整度</div>
          <div class="hero-value" id="heroLatest">-</div>
          <div class="hero-note" id="heroLatestNote">等待抓取数据</div>
        </div>
      </div>

      <div class="card">
        <div class="section-head">
          <div>
            <h2>多账号采集汇总</h2>
            <p id="accountSummaryHint">网页采集数据默认合并分析；历史记录仍保留原采集实例来源。</p>
          </div>
        </div>
        <div class="device-overview-grid" id="deviceOverview">
          <div class="device-overview-item"><div class="device-overview-label">设备范围</div><div class="device-overview-value">-</div></div>
        </div>
        <div class="data-table-wrap">
          <table class="data-table">
            <thead><tr><th>账号</th><th>历史实例</th><th>采集轮次</th><th>问题数</th><th>信源引用</th><th>最近采集（北京时间）</th></tr></thead>
            <tbody id="accountSummaryRows"><tr><td colspan="6" class="empty">等待账号数据</td></tr></tbody>
          </table>
        </div>
      </div>

      <div class="decision-layout">
        <section class="card decision-card" aria-labelledby="decisionTitle">
          <div class="decision-state">
            <div class="decision-state-main">
              <div class="decision-state-title" id="decisionTitle">数据可用性</div>
              <div class="decision-state-note" id="decisionStateNote">正在核对信源、正文与AI商品审核覆盖。</div>
            </div>
            <span class="status-label warning" id="decisionStatus">核对中</span>
          </div>
          <div class="decision-metrics" id="decisionMetrics"></div>
        </section>
        <section class="card decision-card" aria-labelledby="strategyTitle">
          <div class="card-header"><span class="card-title" id="strategyTitle">策略信号</span><span class="card-hint">变化均按比例与样本量判断</span></div>
          <div class="strategy-list" id="strategySignals"></div>
        </section>
      </div>

      <section class="card" aria-labelledby="ownedVideoCategoryTitle">
        <div class="card-header">
          <span class="card-title" id="ownedVideoCategoryTitle">各产品品类 · 自有品牌视频信源链接占比</span>
          <span class="card-hint" id="ownedVideoCategoryHint">按唯一链接统计，跟随设备筛选</span>
        </div>
        <div class="analysis-note">主占比＝命中自有品牌或自有产品的视频唯一链接 ÷ 该品类全部唯一信源链接；同一链接跨轮重复出现只计一次。</div>
        <div class="data-table-wrap" data-scroll-key="owned-video-category-share" tabindex="0" role="region" aria-label="各产品品类自有品牌视频信源链接占比">
          <table class="data-table">
            <thead><tr><th>产品品类</th><th>自有品牌</th><th>全部唯一链接</th><th>全部视频链接</th><th>自有品牌视频链接</th><th>占品类全部链接</th><th>占品类视频链接</th></tr></thead>
            <tbody id="ownedVideoCategoryRows"><tr><td colspan="7" class="empty">等待统计数据</td></tr></tbody>
          </table>
        </div>
      </section>

      <details class="methodology-summary" data-detail-key="overview-methodology">
        <summary>查看统计口径与使用边界</summary>
        <div class="methodology-grid">
          <div class="methodology-item"><b>总轮次</b><span>答案与信源 run_no 的并集，不等于每轮都完成商品审核。</span></div>
          <div class="methodology-item"><b>品牌出现率</b><span>聚合榜为品牌出现轮次 ÷ 有效产品轮次；同一品牌每轮最多计一次。</span></div>
          <div class="methodology-item"><b>每日出现率</b><span>品牌出现轮次 ÷ 当日归档答案轮次；不同样本量需结合95%区间判断。</span></div>
          <div class="methodology-item"><b>引用占比</b><span>引用行数 ÷ 总引用行数；同一链接跨轮重复出现会重复计数。</span></div>
          <div class="methodology-item"><b>变化</b><span>最新观测日减前一观测日，以百分点为主，绝对次数仅作补充。</span></div>
          <div class="methodology-item"><b>证据边界</b><span>高频引用不等于权威；同轮信源也不代表直接支持某个品牌。</span></div>
        </div>
      </details>

      <details class="collapsible-summary" data-detail-key="overview-source-summary">
        <summary>&#20449;&#28304;&#27010;&#35272;&#26126;&#32454;</summary>
        <div class="section-grid">
        <div class="grid-2">
          <div class="card">
            <div class="card-header"><span class="card-title">信源类型结构</span><span class="card-hint" id="typeHint"></span></div>
            <div id="typeBars"></div>
          </div>
          <div class="card">
            <div class="card-header"><span class="card-title">媒体 / 平台占比</span><span class="card-hint" id="mediaHint"></span></div>
            <div id="mediaBars"></div>
          </div>
        </div>
        <div class="card">
          <div class="card-header"><span class="card-title">问题排行</span><span class="card-hint" id="questionRankHint"></span></div>
          <div class="question-rank-list" id="questionRankList"></div>
        </div>
        </div>
      </details>

      <details class="collapsible-summary" data-detail-key="overview-stat-summary">
        <summary>展开完整统计摘要</summary>
        <div class="card" style="border-top:0;border-radius:0 0 var(--radius) var(--radius)">
          <div class="insight-list" id="insightList"></div>
        </div>
      </details>
    </section>

    <!-- QUESTION -->
    <section class="content" data-view-group="question">
      <div class="card">
        <div class="card-header"><span class="card-title">按问题信源拆分</span><span class="card-hint" id="questionSourceHint"></span></div>
        <div id="questionSourceCards"></div>
      </div>
    </section>

    <!-- PRODUCT -->
    <section class="content" data-view-group="product">
      <div class="card">
        <div class="card-header"><span class="card-title">产品推荐统计</span><span class="card-hint" id="productHint"></span></div>
        <div class="product-summary">
          <div class="product-stat"><span>推荐条目数（每轮可多个）</span><b id="productMentions">-</b></div>
          <div class="product-stat"><span>有效品牌数</span><b id="productUnique">-</b></div>
          <div class="product-stat"><span>最新一轮产品数</span><b id="productLatestCount">-</b></div>
          <div class="product-stat"><span id="productRunLabel">当日运行轮次</span><b id="productRunCount">-</b></div>
        </div>
        <div id="productCoverage" class="coverage-panel" style="margin:0 0 14px"></div>
        <div class="grid-2">
          <div>
            <div class="card-hint" style="margin-bottom:8px">品牌出现率排行 · 分母为有效产品轮次 · 每品牌每轮去重</div>
            <div id="productBars"></div>
          </div>
          <div>
            <div class="card-hint" style="margin-bottom:8px">最新一轮推荐产品</div>
            <div id="latestProducts"></div>
          </div>
        </div>
      </div>
      <div class="card">
        <div class="card-header"><span class="card-title">按问题拆分品牌 / 产品</span><span class="card-hint">每个问题推荐了哪些品牌和产品</span></div>
        <div id="questionProductCards"></div>
      </div>
    </section>

    <!-- DAILY -->
    <section class="content" data-view-group="daily">
      <section class="card analysis-card" aria-labelledby="brandAnalysisTitle">
        <div class="analysis-head">
          <div>
            <h2 class="analysis-title" id="brandAnalysisTitle">自有品牌 / 竞品信源命中</h2>
            <p class="analysis-desc" id="brandAnalysisDesc">选择品牌后查看产品提及趋势、标题与正文命中率以及具体信源链接。</p>
          </div>
          <div class="analysis-toolbar" aria-label="品牌信源筛选">
            <div class="analysis-field"><label for="analysisBrand">品牌</label><select id="analysisBrand"><option value="">等待数据</option></select></div>
            <div class="analysis-field metric-field"><label for="analysisMetric">信源指标</label><select id="analysisMetric">
              <option value="source_av_ref_share">标题或正文命中占比</option>
              <option value="content_only_ref_share">正文新增命中占比</option>
              <option value="title_av_ref_share">标题命中占比</option>
              <option value="source_title_unique_urls">命中唯一信源数</option>
            </select></div>
            <div class="analysis-field"><label for="analysisRange">日期范围</label><select id="analysisRange"><option value="7">最近7个观测日</option><option value="14">最近14个观测日</option><option value="30">最近30个观测日</option><option value="all">全部</option></select></div>
            <div class="analysis-field"><label for="analysisMode">比较方式</label><select id="analysisMode"><option value="level">每日水平</option><option value="delta">相邻日变化</option></select></div>
            <label class="analysis-check"><input type="checkbox" id="analysisIncludePartial">包含未收盘日</label>
          </div>
        </div>
        <div id="analysisEmpty" class="analysis-empty">请选择单个产品问题后查看。</div>
        <div id="analysisBody" hidden>
          <div class="analysis-kpis">
            <div class="analysis-kpi"><span>最新品牌提及率</span><b id="analysisLatestRate">-</b><small id="analysisLatestRateMeta">-</small></div>
            <div class="analysis-kpi"><span id="analysisLatestSourceLabel">信源命中率</span><b id="analysisLatestSource">-</b><small id="analysisLatestSourceMeta">-</small></div>
            <div class="analysis-kpi"><span>有效观测</span><b id="analysisSample">-</b><small id="analysisSampleMeta">-</small></div>
            <div class="analysis-kpi"><span>数据质量</span><b id="analysisQuality">-</b><small id="analysisQualityMeta">-</small></div>
          </div>
          <div class="card" style="margin-top:12px">
            <div class="card-header"><span class="card-title">该品牌命中的具体信源</span><span class="card-hint">自有品牌和竞品使用相同口径；列出标题或可靠正文命中的链接</span></div>
            <div class="data-table-wrap" data-scroll-key="brand-source-examples">
              <table class="data-table"><thead><tr><th>标题</th><th>命中位置</th><th>类型</th><th>引用次数</th><th>最近日期</th><th>链接</th></tr></thead><tbody id="analysisSourceExamples"></tbody></table>
            </div>
          </div>
          <details class="analysis-data" data-detail-key="brand-source-analysis-table">
            <summary>查看逐日数据与分子 / 分母</summary>
            <div class="analysis-data-table-wrap" data-scroll-key="brand-source-analysis-table"><table class="analysis-data-table"><thead><tr><th>日期</th><th>状态</th><th>品牌提及</th><th>提及率</th><th>全部信源</th><th>标题命中</th><th>正文新增</th><th>标题或正文</th><th>正文归档</th><th>覆盖</th></tr></thead><tbody id="analysisTableRows"></tbody></table></div>
          </details>
          <div class="analysis-note" id="analysisFootnote"></div>
        </div>
      </section>
      <section class="card analysis-card" aria-labelledby="ownedAnalysisTitle">
        <div class="analysis-head">
          <div>
            <h2 class="analysis-title" id="ownedAnalysisTitle">自有产品信源结构与标题策略</h2>
            <p class="analysis-desc" id="ownedAnalysisDesc">查看自有产品被豆包提取的文章/视频占比、重复链接影响、正文补充命中与标题主题差异。</p>
          </div>
          <div class="analysis-toolbar" aria-label="自有产品信源筛选">
            <div class="analysis-field">
              <label for="ownedAnalysisProduct" id="ownedAnalysisProductLabel">我的产品</label>
              <select id="ownedAnalysisProduct"><option value="">等待数据</option></select>
            </div>
            <div class="analysis-field">
              <label for="ownedAnalysisRange">日期范围</label>
              <select id="ownedAnalysisRange">
                <option value="7">最近7个观测日</option>
                <option value="14">最近14个观测日</option>
                <option value="30">最近30个观测日</option>
                <option value="all">全部</option>
              </select>
            </div>
          </div>
        </div>

        <div id="ownedAnalysisEmpty" class="analysis-empty">请选择单个产品问题后查看。</div>
        <div id="ownedAnalysisBody" hidden>
          <div class="analysis-kpis">
            <div class="analysis-kpi"><span id="ownedTotalRefsLabel">确认命中信源</span><b id="ownedTotalRefs">-</b><small id="ownedTotalRefsMeta">标题或可靠正文命中</small></div>
            <div class="analysis-kpi"><span>最新视频占比</span><b id="ownedLatestVideo">-</b><small id="ownedLatestVideoMeta">出现次数 ÷ 当日确认命中信源</small></div>
            <div class="analysis-kpi"><span>唯一信源链接</span><b id="ownedUniqueUrls">-</b><small id="ownedUniqueUrlsMeta">全部观测期去重</small></div>
            <div class="analysis-kpi"><span id="ownedBodyOnlyLabel">正文补充贡献</span><b id="ownedBodyOnly">-</b><small id="ownedBodyOnlyMeta">标题未写产品、正文命中</small></div>
          </div>

          <div class="analysis-grid">
            <div class="chart-panel">
              <div class="chart-panel-head">
                <div class="chart-panel-title" id="ownedMainTrendTitle">每日品牌提及率 × 信源命中率</div>
                <div class="chart-panel-meta" id="ownedMixChartMeta">上层为产品推荐提及率，下层为标题或可靠正文命中的信源占比</div>
              </div>
              <div class="chart-frame" id="ownedMixChartFrame">
                <svg class="analysis-svg" id="ownedMixChart" role="img" aria-labelledby="ownedAnalysisTitle ownedMixChartMeta"></svg>
                <div class="chart-tooltip" id="ownedMixTooltip" role="status" aria-live="polite"></div>
              </div>
              <div class="chart-legend">
                <span class="legend-key"><span class="legend-line"></span><span id="ownedMentionLegend">品牌提及率</span></span>
                <span class="legend-key"><span class="legend-line source"></span><span id="ownedSourceLegend">标题或正文命中信源占比</span></span>
              </div>
              <div class="keyword-trend-section">
                <div class="chart-panel-head">
                  <div class="chart-panel-title">每日视频标题关键词对比</div>
                  <div class="chart-panel-meta" id="ownedKeywordTrendMeta">覆盖率＝含该词的视频标题数 ÷ 当天视频标题总数</div>
                </div>
                <div class="keyword-trend-howto" id="ownedKeywordHowto"><b>怎么看：</b>先选择一个关键词，再比较两条线每天的覆盖率变化。覆盖率＝标题含该词的信源条数 ÷ 当天对应视频标题总数。</div>
                <div class="keyword-trend-picker" id="ownedKeywordTrendPicker" aria-label="选择需要对比的关键词"></div>
                <div class="keyword-trend-frame" id="ownedKeywordTrendFrame">
                  <svg class="analysis-svg" id="ownedKeywordTrendChart" role="img" aria-labelledby="ownedAnalysisTitle ownedKeywordTrendMeta"></svg>
                  <div class="chart-tooltip" id="ownedKeywordTrendTooltip" role="status" aria-live="polite"></div>
                </div>
                <div class="keyword-trend-legend">
                  <span class="legend-key" id="ownedKeywordOwnLegend"><span class="legend-line"></span>我的产品视频标题</span>
                  <span class="legend-key" id="ownedKeywordAllLegend"><span class="legend-line source"></span>品类全部视频标题</span>
                </div>
                <div class="keyword-trend-summary" id="ownedKeywordTrendSummary">正在总结最新一天的关键词优势与缺口。</div>
              </div>
            </div>

            <div class="chart-panel">
              <div class="chart-panel-head">
                <div class="chart-panel-title">文章 / 视频标题主题差异</div>
                <div class="chart-panel-meta">标题命中率；★表示双比例检验 p&lt;0.05</div>
              </div>
              <div class="chart-frame scatter-frame" id="ownedThemeChartFrame">
                <svg class="analysis-svg" id="ownedThemeChart" role="img" aria-labelledby="ownedAnalysisTitle ownedStrategyMessage"></svg>
                <div class="chart-tooltip" id="ownedThemeTooltip" role="status" aria-live="polite"></div>
              </div>
              <div class="correlation-summary">
                <div class="owned-keyword-groups">
                  <div>
                    <b id="ownedArticleKeywordsLabel">文章关键词（自有命中）</b>
                    <small class="owned-keyword-meta" id="ownedArticleKeywordsMeta">按当前日期范围</small>
                    <div id="ownedArticleKeywords" class="owned-keyword-list"></div>
                  </div>
                  <div>
                    <b id="ownedVideoKeywordsLabel">视频关键词（自有产品）</b>
                    <small class="owned-keyword-meta" id="ownedVideoKeywordsMeta">按当前日期范围</small>
                    <div id="ownedVideoKeywords" class="owned-keyword-list"></div>
                  </div>
                  <div id="allVideoKeywordsGroup">
                    <b id="allVideoKeywordsLabel">全量视频关键词（品类对照）</b>
                    <small class="owned-keyword-meta" id="allVideoKeywordsMeta">当前品类全部被抓视频标题</small>
                    <div id="allVideoKeywords" class="owned-keyword-list"></div>
                  </div>
                </div>
                <div class="correlation-message" id="ownedStrategyMessage">正在分析标题策略。</div>
              </div>
            </div>
          </div>

          <details class="analysis-data" data-detail-key="owned-source-analysis-table">
            <summary>查看逐日数据与分子 / 分母</summary>
            <div class="analysis-data-table-wrap" data-scroll-key="owned-source-analysis-table">
              <table class="analysis-data-table">
                <caption class="sr-only">自有产品文章视频占比逐日数据</caption>
                <thead><tr><th scope="col">日期</th><th scope="col">确认命中信源</th><th scope="col">文章</th><th scope="col">视频</th><th scope="col">文章占比</th><th scope="col">视频占比</th><th scope="col">唯一链接</th><th scope="col">唯一文章占比</th><th scope="col">唯一视频占比</th><th scope="col">正文补充命中</th></tr></thead>
                <tbody id="ownedAnalysisTableRows"></tbody>
              </table>
            </div>
          </details>
          <div class="analysis-note" id="ownedAnalysisFootnote">正文未可靠归档的链接不能当作未提及；这里展示的是已确认命中的保守下界。</div>
        </div>
      </section>
      <div class="card">
        <div class="card-header"><span class="card-title">每日信源变化</span><span class="card-hint" id="dailySourceHint"></span></div>
        <div id="dailySourceCards"></div>
      </div>
      <div class="card">
        <div class="card-header"><span class="card-title">每日产品推荐变化</span><span class="card-hint" id="dailyProductHint"></span></div>
        <div id="dailyProductCards"></div>
      </div>
    </section>

    <!-- LATEST -->
    <section class="content" data-view-group="latest">
      <div class="card">
        <div class="card-header"><span class="card-title">最新一轮明细</span><span class="card-hint" id="latestInfo"></span></div>
        <div class="data-table-wrap" data-scroll-key="latest-evidence" tabindex="0" role="region" aria-label="最新一轮信源明细">
          <table class="data-table">
            <caption class="sr-only">最新一轮抓取的信源标题、类型、媒体、域名与链接</caption>
            <thead><tr><th style="width:52px">序号</th><th>标题</th><th style="width:70px">类型</th><th style="width:140px">媒体</th><th style="width:180px">域名</th><th style="width:60px">链接</th></tr></thead>
            <tbody id="latestRows"></tbody>
          </table>
        </div>
      </div>
    </section>

    <!-- SUPPORT -->
    <section class="content" data-view-group="support">
      <div class="card">
        <div class="card-header"><span class="card-title">数据审核漏斗</span><span class="card-hint">缺失显示为未知，不按0处理</span></div>
        <div id="auditSummary" class="coverage-panel"></div>
      </div>
      <div class="grid-2">
        <div class="card">
          <div class="card-header"><span class="card-title">全部域名</span><span class="card-hint" id="domainHint"></span></div>
          <div id="domainBars"></div>
        </div>
        <div class="card">
          <div class="card-header"><span class="card-title">运行日志</span></div>
          <div class="log-box" id="logTail">暂无日志</div>
        </div>
      </div>
    </section>
  </div>
</div>

<script>
const $=id=>document.getElementById(id);
const esc=s=>String(s??"").replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const _urlState=new URLSearchParams(location.search);
let selectedQuestion=_urlState.get("question")||sessionStorage.getItem("doubaoSelectedQuestion")||"全部问题";
let selectedDevice=_urlState.get("device")||sessionStorage.getItem("doubaoSelectedDevice")||"all";
let activeView=_urlState.get("view")||sessionStorage.getItem("doubaoActiveView")||"overview";
if(!["overview","question","product","daily","latest","support"].includes(activeView))activeView="overview";
let _lastSignature="",_refreshSeq=0;
let _refreshBusy=false,_refreshController=null;
let _versionBusy=false,_appliedDataVersion="";
let _pollTimer=null,_refreshFailures=0,_lastSuccessAt="";
const _statsCache=new Map();
const _openDetailKeys=new Set((()=>{try{return JSON.parse(sessionStorage.getItem("doubaoOpenDetails")||"[]")}catch(_){return[]}})());
let _brandAnalysisData=null;
let _analysisBrand=sessionStorage.getItem("doubaoAnalysisBrand")||"";
// V2 resets the old ambiguous "article share" selection to the exact title-hit
// denominator requested for this analysis.
let _analysisMetric=sessionStorage.getItem("doubaoAnalysisMetricV3")||"source_av_ref_share";
let _analysisRange=sessionStorage.getItem("doubaoAnalysisRange")||"7";
let _analysisMode=sessionStorage.getItem("doubaoAnalysisMode")||"level";
let _analysisIncludePartial=sessionStorage.getItem("doubaoAnalysisIncludePartial")==="1";
let _analysisRenderQueued=false,_analysisResizeObserver=null;
let _ownedAnalysisData=null;
let _ownedProduct=sessionStorage.getItem("doubaoOwnedAnalysisProduct")||"";
let _ownedRange=sessionStorage.getItem("doubaoOwnedAnalysisRange")||"7";
let _ownedKeyword=sessionStorage.getItem("doubaoOwnedKeywordTrend")||"";
let _ownedRenderQueued=false,_ownedResizeObserver=null;
if(localStorage.getItem("doubaoCompact")==="1")document.body.classList.add("compact");

function restoreDynamicDetails(root){
  if(!root)return;
  root.querySelectorAll("details[data-detail-key]").forEach(node=>{
    const key=node.getAttribute("data-detail-key")||"";
    node.open=_openDetailKeys.has(key);
    if(node.dataset.detailBound==="1")return;
    node.dataset.detailBound="1";
    node.addEventListener("toggle",()=>{
      if(node.open)_openDetailKeys.add(key);else _openDetailKeys.delete(key);
      try{sessionStorage.setItem("doubaoOpenDetails",JSON.stringify(Array.from(_openDetailKeys)))}catch(_){}
    });
  });
}

function hasDataChanged(d){
  const sig=(d.data_version||"")+"|"+(d.selected_question||"")+"|"+(d.selected_device||"all")+"|"+[d.total_refs||0,d.latest_run_no||0,d.csv?.mtime||"",d.products_csv?.mtime||"",d.ai_cache?.mtime||"",d.answer_csv?.mtime||"",d.capture_skips?.mtime||""].join("|");
  if(sig!==_lastSignature){_lastSignature=sig;return true}
  return false
}
function pct(c,t){c=+c||0;t=+t||0;return t?(c*100/t).toFixed(2)+"%":"0.00%"}
function pctOrDash(c,t,digits=1){c=+c||0;t=+t||0;return t?(c*100/t).toFixed(digits)+"%":"—"}
function wilsonInterval(count,total){
  count=+count||0;total=+total||0;if(!total)return null;
  const z=1.96,p=count/total,z2=z*z,den=1+z2/total;
  const center=(p+z2/(2*total))/den;
  const margin=z*Math.sqrt((p*(1-p)+z2/(4*total))/total)/den;
  return [Math.max(0,center-margin),Math.min(1,center+margin)];
}
function ciText(count,total){const ci=wilsonInterval(count,total);return ci?`${(ci[0]*100).toFixed(1)}–${(ci[1]*100).toFixed(1)}%`:"样本不足"}
function intervalsOverlap(a,b){return !!(a&&b&&Math.max(a[0],b[0])<=Math.min(a[1],b[1]))}
function truthy(v){return v===true||v==="True"||v==="true"||v===1}
function ratioHtml(c,t,cls){c=+c||0;t=+t||0;return`<span class="${cls||"bar-val"}"><span class="pct">${esc(pct(c,t))}</span><span class="abs">${esc(c)}/${esc(t)}</span></span>`}
function productRatioHtml(r,totalMentions,totalRuns){
  const runCount=+r.run_count||0;
  return`<span class="bar-val product-ratio" aria-label="品牌出现率 ${esc(pctOrDash(runCount,totalRuns,2))}，${esc(runCount)}/${esc(totalRuns)}轮，95%置信区间${esc(ciText(runCount,totalRuns))}">
    <span class="pct">出现率 ${esc(pctOrDash(runCount,totalRuns,2))}</span>
    <span class="abs">出现 ${esc(runCount)}/${esc(totalRuns)} 轮</span>
    <span class="ci-note">95%CI ${esc(ciText(runCount,totalRuns))}</span>
  </span>`
}
function rankText(r){
  if(!r||!r.avg_rank)return"";
  const parts=[`均第${esc(r.avg_rank)}名`];
  if(r.best_rank)parts.push(`最高第${esc(r.best_rank)}`);
  const rankCounts=r.rank_counts||{};
  const rankEntries=Object.entries(rankCounts)
    .map(([rank,count])=>[+rank||0,+count||0])
    .filter(([rank,count])=>rank>0&&count>0)
    .sort((a,b)=>a[0]-b[0]);
  if(rankEntries.length){
    rankEntries.forEach(([rank,count])=>parts.push(`第${esc(rank)}名${esc(count)}次`));
  }else{
    if(r.top1)parts.push(`第1名${esc(r.top1)}次`);
    if(r.top2)parts.push(`第2名${esc(r.top2)}次`);
    if(r.top3)parts.push(`第3名${esc(r.top3)}次`);
  }
  if(+r.unranked_count>0)parts.push(`未标明排名${esc(r.unranked_count)}次`);
  return parts.join(" · ");
}
function rankMetaHtml(r){const t=rankText(r);return t?`<div class="bar-rankmeta">${t}</div>`:""}
function productPillHtml(x){const hasRuns=x.run_count!==undefined&&x.run_count!==null,n=hasRuns?x.run_count:x.count;return`<span class="product-pill">${esc(x.name)} <small>${esc(n)}${hasRuns?"轮":"次"}</small>${rankText(x)?`<span class="rank-meta-inline">${rankText(x)}</span>`:""}</span>`}
function deltaHtml(d){d=+d||0;if(d>0)return`<span class="delta-up">+${d}</span>`;if(d<0)return`<span class="delta-down">${d}</span>`;return'<span class="delta-flat">0</span>'}
function deltaPctHtml(counts,totals){const n=Math.min((counts||[]).length,(totals||[]).length);if(n<2)return'<span class="delta-flat">0</span>';const a=+counts[n-1]||0,b=+counts[n-2]||0,ta=+totals[n-1]||1,tb=+totals[n-2]||1;const dp=+(a*100/ta-b*100/tb).toFixed(1);const d=a-b;const cls=dp>0?"delta-up":(dp<0?"delta-down":"delta-flat");return`<span class="${cls}">${dp>0?"+":""}${dp.toFixed(1)}pct</span>`}

function applyView(){
  document.querySelectorAll("[data-view-group]").forEach(s=>{const show=s.getAttribute("data-view-group")===activeView;s.classList.toggle("is-hidden",!show);s.hidden=!show});
  document.querySelectorAll(".view-tab[data-view],.tab-btn[data-view]").forEach(b=>{const on=b.getAttribute("data-view")===activeView;b.classList.toggle("active",on);b.setAttribute("aria-selected",on?"true":"false")});
}
function applyFilter(){
  const kw=($("filterInput")?.value||"").trim().toLowerCase();
  document.querySelectorAll("[data-filter-text]").forEach(el=>el.classList.toggle("is-muted",!!kw&&!(el.getAttribute("data-filter-text")||"").toLowerCase().includes(kw)));
}
function captureUiState(){
  const scrolls={};
  document.querySelectorAll("[data-scroll-key]").forEach(el=>{scrolls[el.getAttribute("data-scroll-key")]=[el.scrollLeft,el.scrollTop]});
  const active=document.activeElement;
  return{scrollY:window.scrollY,scrolls,activeId:active&&active.id?active.id:""};
}
function restoreUiState(state){
  if(!state)return;
  requestAnimationFrame(()=>{
    document.querySelectorAll("[data-scroll-key]").forEach(el=>{const v=state.scrolls[el.getAttribute("data-scroll-key")];if(v){el.scrollLeft=v[0];el.scrollTop=v[1]}});
    if(Math.abs(window.scrollY-state.scrollY)>2)window.scrollTo(0,state.scrollY);
    if(state.activeId){const el=$(state.activeId);if(el&&document.activeElement!==el)el.focus({preventScroll:true})}
  });
}

function barsHtml(rows,total,typeMap){
  if(!rows.length)return'<div class="empty">暂无</div>';
  const den=+total||rows.reduce((s,r)=>s+(+r.count||0),0);
  return rows.map((r,i)=>{
    const w=Math.max(0,Math.min(100,(+r.count||0)*100/Math.max(1,den)));
    const cls=typeMap?.(r.name)||"default";
    return`<div class="bar-item" data-filter-text="${esc(r.name)} ${esc(r.count)}">
      <div class="bar-rank">#${i+1}</div>
      <div class="bar-name" title="${esc(r.name)}">${esc(r.name)}</div>
      <div class="bar-track" aria-hidden="true"><div class="bar-fill type-${cls}" style="width:${w.toFixed(2)}%"></div></div>
      ${ratioHtml(r.count,den)}
      ${rankMetaHtml(r)}
    </div>`;
  }).join("");
}

function productBarsHtml(rows,totalMentions,totalRuns){
  if(!rows.length)return'<div class="empty">暂无</div>';
  return rows.map((r,i)=>{
    const w=Math.max(0,Math.min(100,(+r.run_count||0)*100/Math.max(1,+totalRuns||0)));
    return`<div class="bar-item product-bar-item" data-filter-text="${esc(r.name)} ${esc(r.count)} ${esc(r.run_count)}">
      <div class="bar-rank">#${i+1}</div>
      <div class="bar-name" title="${esc(r.name)}">${esc(r.name)}</div>
      <div class="bar-track" aria-hidden="true"><div class="bar-fill type-default" style="width:${w.toFixed(2)}%"></div></div>
      ${productRatioHtml(r,totalMentions,totalRuns)}
      ${rankMetaHtml(r)}
    </div>`;
  }).join("");
}

function renderBars(id,rows,total,typeMap){$(id).innerHTML=barsHtml(rows,total,typeMap)}

function setText(id,value){const el=$(id);if(el)el.textContent=value}

const ANALYSIS_METRICS={
  source_av_ref_share:{label:"文章+视频标题或正文命中占全部信源",shortLabel:"标题或正文命中",percent:true,requiresContent:true,value:(day,point)=>point?.source_av_ref_share},
  content_only_ref_share:{label:"正文新增命中占全部信源",shortLabel:"正文新增命中",percent:true,requiresContent:true,value:(day,point)=>point?.content_only_ref_share},
  content_fetch_coverage:{label:"正文成功归档占全部信源",shortLabel:"正文归档覆盖",percent:true,requiresContent:true,value:day=>day?.content_fetch_coverage},
  title_av_ref_share:{label:"文章+视频标题命中占全部信源",shortLabel:"标题命中份额",percent:true,value:(day,point)=>point?.title_av_ref_share},
  title_article_ref_share:{label:"文章标题命中占全部信源",shortLabel:"文章标题份额",percent:true,value:(day,point)=>point?.title_article_ref_share},
  title_video_ref_share:{label:"视频标题命中占全部信源",shortLabel:"视频标题份额",percent:true,value:(day,point)=>point?.title_video_ref_share},
  source_title_coverage:{label:"品牌标题信源覆盖率（下限）",shortLabel:"标题信源覆盖",percent:true,value:(day,point)=>point?.source_title_coverage},
  source_title_unique_urls:{label:"品牌标题唯一信源数",shortLabel:"标题唯一信源数",value:(day,point)=>point?.source_title_unique_urls},
  refs_per_run:{label:"品类信源引用强度（条 / 运行轮）",shortLabel:"每轮信源条数",value:day=>day?.refs_per_run},
  unique_urls_per_run:{label:"唯一信源数 / 信源运行轮",shortLabel:"每轮唯一信源数",value:day=>day?.unique_urls_per_run},
  new_url_share:{label:"新信源占比",shortLabel:"新信源占比",percent:true,value:day=>day?.new_url_share},
  video_share:{label:"视频类型信源占全部信源",shortLabel:"视频类型占比",percent:true,value:day=>day?.video_share},
  article_share:{label:"文章类型信源占全部信源",shortLabel:"文章类型占比",percent:true,value:day=>day?.article_share},
};
const ANALYSIS_STATUS_LABELS={closed:"完整日",partial:"未收盘",incomplete:"覆盖不足",data_unavailable:"数据不可用"};
function finiteNumber(v){return typeof v==="number"&&Number.isFinite(v)}
function formatRate(v,digits=1){return finiteNumber(v)?`${(v*100).toFixed(digits)}%`:"—"}
function formatMetricValue(v,metricKey=_analysisMetric,digits=1){
  if(!finiteNumber(v))return"—";
  return ANALYSIS_METRICS[metricKey]?.percent?`${(v*100).toFixed(digits)}%`:Number(v).toFixed(digits);
}
function shortDate(value){const parts=String(value||"").split("-");return parts.length===3?`${+parts[1]}/${+parts[2]}`:String(value||"")}
function mean(values){return values.reduce((sum,value)=>sum+value,0)/Math.max(1,values.length)}
function pearson(xs,ys){
  if(xs.length<2||xs.length!==ys.length)return null;
  const mx=mean(xs),my=mean(ys);let num=0,dx=0,dy=0;
  for(let i=0;i<xs.length;i++){const ax=xs[i]-mx,ay=ys[i]-my;num+=ax*ay;dx+=ax*ax;dy+=ay*ay}
  return dx>0&&dy>0?num/Math.sqrt(dx*dy):null;
}
function averageRanks(values){
  const order=values.map((value,index)=>({value,index})).sort((a,b)=>a.value-b.value);
  const ranks=Array(values.length);let i=0;
  while(i<order.length){let j=i+1;while(j<order.length&&order[j].value===order[i].value)j++;const rank=(i+1+j)/2;for(let k=i;k<j;k++)ranks[order[k].index]=rank;i=j}
  return ranks;
}
function spearman(xs,ys){return xs.length<2?null:pearson(averageRanks(xs),averageRanks(ys))}
function analysisBrandObject(){return(_brandAnalysisData?.brands||[]).find(item=>item.name===_analysisBrand)||null}
function analysisRows(){
  const brand=analysisBrandObject();if(!brand)return[];
  const pointByDate=new Map((brand.points||[]).map(point=>[point.date,point]));
  let rows=(_brandAnalysisData.days||[]).map(day=>{const point=pointByDate.get(day.date)||{};return{...day,...point,metricValue:ANALYSIS_METRICS[_analysisMetric]?.value(day,point)}});
  const count=_analysisRange==="all"?rows.length:Math.max(1,+_analysisRange||7);
  return rows.slice(Math.max(0,rows.length-count));
}
function correlationRows(rows){
  const metric=ANALYSIS_METRICS[_analysisMetric]||ANALYSIS_METRICS.source_av_ref_share;
  const eligible=rows.filter(row=>{
    const allowed=row.status==="closed"||(_analysisIncludePartial&&row.status==="partial"&&(+row.source_coverage||0)>=.95&&(+row.review_coverage||0)>=.999);
    const contentReady=!metric.requiresContent||(+row.content_fetch_coverage||0)>=.95;
    return allowed&&contentReady&&finiteNumber(row.metricValue)&&finiteNumber(row.rate);
  }).map(row=>({...row,x:row.metricValue,y:row.rate}));
  if(_analysisMode!=="delta")return eligible;
  const deltas=[];
  for(let i=1;i<eligible.length;i++)deltas.push({...eligible[i],x:eligible[i].x-eligible[i-1].x,y:eligible[i].y-eligible[i-1].y,previousDate:eligible[i-1].date});
  return deltas;
}
function correlationSummary(pairs){
  const xs=pairs.map(row=>row.x),ys=pairs.map(row=>row.y),r=pearson(xs,ys),rho=spearman(xs,ys);
  let stable=true;
  if(pairs.length>=4&&finiteNumber(r)&&Math.abs(r)>.05){
    const sign=Math.sign(r);
    for(let i=0;i<pairs.length;i++){
      const subset=pairs.filter((_row,index)=>index!==i),leave=pearson(subset.map(row=>row.x),subset.map(row=>row.y));
      if(!finiteNumber(leave)||Math.sign(leave)!==sign){stable=false;break}
    }
  }
  return{n:pairs.length,r,rho,stable};
}
function persistAnalysisControls(){
  sessionStorage.setItem("doubaoAnalysisBrand",_analysisBrand);
  sessionStorage.setItem("doubaoAnalysisMetricV3",_analysisMetric);
  sessionStorage.setItem("doubaoAnalysisRange",_analysisRange);
  sessionStorage.setItem("doubaoAnalysisMode",_analysisMode);
  sessionStorage.setItem("doubaoAnalysisIncludePartial",_analysisIncludePartial?"1":"0");
}

function renderBrandSourceAnalysis(analytics){
  _brandAnalysisData=analytics||null;
  const empty=$("analysisEmpty"),body=$("analysisBody"),brandSelect=$("analysisBrand");
  if(!analytics||analytics.status!=="ok"||!(analytics.brands||[]).length){
    body.hidden=true;empty.hidden=false;
    empty.textContent=analytics?.warning||"请选择单个产品问题后查看品牌趋势与信源关系。";
    brandSelect.innerHTML='<option value="">暂无品牌</option>';
    return;
  }
  const priority={owned:0,competitor:1,other:2};
  const brands=[...(analytics.brands||[])].sort((a,b)=>
    (priority[a.group]??2)-(priority[b.group]??2)
    ||(+b.total_mentioned_runs||0)-(+a.total_mentioned_runs||0)
    ||String(a.name).localeCompare(String(b.name),"zh-CN")
  );
  analytics.brands=brands;
  if(!brands.some(item=>item.name===_analysisBrand)){
    _analysisBrand=(brands.find(item=>item.group==="owned")||brands[0])?.name||"";
  }
  if(!ANALYSIS_METRICS[_analysisMetric])_analysisMetric="source_av_ref_share";
  const groupLabel={owned:"自有",competitor:"竞品",other:"其他"};
  brandSelect.innerHTML=brands.map(item=>`<option value="${esc(item.name)}">【${esc(groupLabel[item.group]||"其他")}】${esc(item.name)}${item.total_mentioned_runs?` · ${esc(item.total_mentioned_runs)}轮`:" · 仅信源命中"}</option>`).join("");
  brandSelect.value=_analysisBrand;
  $("analysisMetric").value=_analysisMetric;
  $("analysisRange").value=_analysisRange;
  $("analysisMode").value=_analysisMode;
  $("analysisIncludePartial").checked=_analysisIncludePartial;
  empty.hidden=true;body.hidden=false;
  const selected=brands.find(item=>item.name===_analysisBrand),selectedGroup=groupLabel[selected?.group]||"其他";
  setText("brandAnalysisDesc",`${analytics.question} · 当前为${selectedGroup}品牌 · 北京时间 · 产品提及率按轮去重；品牌信源命中检查标题、文章正文及视频页面描述，相同链接跨轮重复出现继续计数。`);
  setText("analysisFootnote",`${analytics.definitions?.source_av_ref_share||""} ${analytics.definitions?.content_fetch_coverage||""} ${analytics.definitions?.correlation||"日级相关只表示共同变化，不证明因果。"}`);
  restoreDynamicDetails(body);
  persistAnalysisControls();
  scheduleAnalysisRender();
}

function renderAnalysisSummary(rows,pairs,summary){
  const latest=rows[rows.length-1]||{};
  const metric=ANALYSIS_METRICS[_analysisMetric]||ANALYSIS_METRICS.source_av_ref_share;
  setText("analysisLatestRate",formatRate(latest.rate,2));
  setText("analysisLatestRateMeta",finiteNumber(latest.rate)?`${latest.mentioned_runs||0}/${latest.denominator_runs||0}轮 · #${latest.rank||"-"} · 95%CI ${ciText(latest.mentioned_runs||0,latest.denominator_runs||0)}`:"该日商品结果不可用");
  setText("analysisLatestSourceLabel",metric.label);
  setText("analysisLatestSource",formatMetricValue(latest.metricValue,_analysisMetric,2));
  setText("analysisLatestSourceMeta",finiteNumber(latest.metricValue)?`${metricEvidence(latest)} · 正文归档${latest.content_available_refs||0}/${latest.refs||0} · 重复保留`:"该日没有可计算的信源链接");
  setText("analysisSample",`${summary.n}日`);
  setText("analysisSampleMeta",`${_analysisMode==="delta"?"相邻日变化":"每日水平"} · ${rows.reduce((sum,row)=>sum+(row.reviewed_runs||0),0)}个审核轮 · ${rows.reduce((sum,row)=>sum+(row.refs||0),0)}条信源`);
  const closed=rows.filter(row=>row.status==="closed").length,partial=rows.filter(row=>row.status==="partial").length,incomplete=rows.filter(row=>row.status==="incomplete").length;
  setText("analysisQuality",`${closed}/${rows.length}日完整`);
  setText("analysisQualityMeta",`${partial?`未收盘${partial}日 · `:""}${incomplete?`覆盖不足${incomplete}日 · `:""}缺失不补0`);
  setText("pearsonValue",finiteNumber(summary.r)?summary.r.toFixed(2):"—");
  setText("spearmanValue",finiteNumber(summary.rho)?summary.rho.toFixed(2):"—");
  setText("correlationN",String(summary.n));
  const message=$("correlationMessage");
  if(!message)return;
  message.className="correlation-message";
  if(summary.n<3||!finiteNumber(summary.r)||!finiteNumber(summary.rho)){
    message.classList.add("warning");message.textContent="满足质量条件的完整日不足3个或序列无变化，暂时无法计算相关性。";
  }else if(summary.n<7){
    message.classList.add("warning");message.textContent=`当前只有${summary.n}个有效观测日。系数仅描述样本内共同变化，不生成策略结论；至少积累7个完整日。`;
  }else if(Math.sign(summary.r)!==Math.sign(summary.rho)){
    message.classList.add("warning");message.textContent="Pearson 与 Spearman 方向不一致，可能存在异常日或非线性；先查看散点和逐日证据。";
  }else if(!summary.stable){
    message.classList.add("warning");message.textContent="去掉任一观测日后相关方向可能改变，结果由少数日期驱动，暂不用于策略。";
  }else{
    const strength=Math.max(Math.abs(summary.r),Math.abs(summary.rho));
    const level=strength>=.7?"较强":(strength>=.4?"中等":"较弱");
    const direction=(summary.r+summary.rho)>=0?"同向":"反向";
    message.textContent=`样本内呈${level}${direction}共同变化。该结果仍不是因果证据，建议固定问题集做内容A/B实验。`;
  }
}

function clampValue(value,min,max){return Math.max(min,Math.min(max,value))}
function nicePositiveCeiling(values,{minimum=.05,maximum=null,headroom=.12}={}){
  const numeric=(values||[]).filter(finiteNumber).map(value=>Math.max(0,value));
  const observed=numeric.length?Math.max(...numeric):0;
  if(observed<=0)return minimum;
  if(maximum!==null&&observed>=maximum)return maximum;
  const target=Math.max(minimum,observed*(1+headroom));
  const power=10**Math.floor(Math.log10(target));
  const scaled=target/power;
  const factor=[1,1.2,1.5,2,2.5,3,4,5,6,8,10].find(value=>value>=scaled)||10;
  const ceiling=factor*power;
  return Math.max(minimum,maximum===null?ceiling:Math.min(maximum,ceiling));
}
function adaptiveAxisDigits(domain,percent=true){
  const span=Math.abs((domain?.[1]||0)-(domain?.[0]||0));
  if(!percent)return span<10?1:0;
  return span<=.05?1:0;
}
function linePath(rows,valueFn,xFn,yFn){
  let path="",open=false;
  rows.forEach((row,index)=>{const value=valueFn(row,index);if(!finiteNumber(value)){open=false;return}const x=xFn(index),y=yFn(value);path+=`${open?"L":"M"}${x.toFixed(2)},${y.toFixed(2)} `;open=true});
  return path.trim();
}
function tickIndexes(length,maxTicks){
  if(length<=maxTicks)return Array.from({length},(_v,index)=>index);
  const result=new Set([0,length-1]);for(let i=1;i<maxTicks-1;i++)result.add(Math.round(i*(length-1)/(maxTicks-1)));return[...result].sort((a,b)=>a-b);
}
function metricEvidence(row,metricKey=_analysisMetric){
  const total=row.refs||0;
  if(metricKey==="source_av_ref_share")return`${row.source_av_refs||0}/${total}条（标题${row.title_av_refs||0} + 正文新增${row.content_only_refs||0}）`;
  if(metricKey==="content_only_ref_share")return`${row.content_only_refs||0}/${total}条正文新增`;
  if(metricKey==="content_fetch_coverage")return`${row.content_available_refs||0}/${total}条已归档`;
  if(metricKey==="title_av_ref_share")return`${row.title_av_refs||0}/${total}条（文章${row.title_article_refs||0} + 视频${row.title_video_refs||0}）`;
  if(metricKey==="title_article_ref_share")return`${row.title_article_refs||0}/${total}条`;
  if(metricKey==="title_video_ref_share")return`${row.title_video_refs||0}/${total}条`;
  if(metricKey==="source_title_coverage")return`${row.source_title_runs||0}/${row.source_observed_runs||0}个信源轮`;
  if(metricKey==="source_title_unique_urls")return`${row.source_title_unique_urls||0}个去重链接`;
  if(metricKey==="refs_per_run")return`${total}/${row.source_observed_runs||0}个信源轮`;
  return total?`当日${total}条信源`:"当日无信源";
}
function placeChartTooltip(tooltip,frame,event){
  const box=frame.getBoundingClientRect();
  tooltip.classList.add("is-visible");
  const width=tooltip.offsetWidth||260,height=tooltip.offsetHeight||105;
  const preferredLeft=event.clientX-box.left+10,preferredTop=event.clientY-box.top-height/2;
  tooltip.style.left=`${clampValue(preferredLeft,6,Math.max(6,box.width-width-6))}px`;
  tooltip.style.top=`${clampValue(preferredTop,6,Math.max(6,box.height-height-6))}px`;
}
function renderTrendChart(rows){
  const frame=$("trendChartFrame"),svg=$("trendChart"),tooltip=$("trendTooltip");
  if(!frame||!svg||frame.clientWidth<120||!rows.length)return;
  tooltip.classList.remove("is-visible");tooltip.innerHTML="";
  const W=Math.max(260,frame.clientWidth),H=Math.max(300,frame.clientHeight),m={l:48,r:54,t:18,b:34},gap=42;
  const laneH=(H-m.t-m.b-gap)/2,topY=m.t,bottomY=m.t+laneH+gap,plotW=W-m.l-m.r;
  const x=index=>rows.length===1?m.l+plotW/2:m.l+index*plotW/(rows.length-1);
  const mentionScaleValues=[];
  rows.forEach(row=>{
    if(finiteNumber(row.rate))mentionScaleValues.push(row.rate);
    const ci=wilsonInterval(row.mentioned_runs||0,row.denominator_runs||0);
    if(ci)mentionScaleValues.push(ci[1]);
  });
  const mentionMax=nicePositiveCeiling(mentionScaleValues,{minimum:.1,maximum:1});
  const mentionY=value=>topY+laneH*(1-clampValue(value/mentionMax,0,1));
  const metric=ANALYSIS_METRICS[_analysisMetric]||ANALYSIS_METRICS.source_av_ref_share;
  const sourceValues=rows.map(row=>row.metricValue).filter(finiteNumber);
  const sourceMax=nicePositiveCeiling(sourceValues,{minimum:metric.percent?.05:1,maximum:metric.percent?1:null});
  const sourceY=value=>bottomY+laneH*(1-clampValue(value/sourceMax,0,1));
  let html=`<title>${esc(_analysisBrand)}每日提及率与${esc(metric.label)}趋势</title><desc>上层为品牌提及率及95%置信区间，下层为所选信源指标，共用北京时间日期轴。</desc>`;
  for(const tick of [0,0.5,1]){
    const raw=tick*mentionMax,y=mentionY(raw);html+=`<line class="chart-grid" x1="${m.l}" y1="${y}" x2="${W-m.r}" y2="${y}"></line><text class="chart-tick" x="${m.l-7}" y="${y+4}" text-anchor="end">${esc(formatRate(raw,adaptiveAxisDigits([0,mentionMax],true)))}</text>`;
  }
  for(const tick of [0,0.5,1]){
    const raw=tick*sourceMax,y=sourceY(raw);html+=`<line class="chart-grid" x1="${m.l}" y1="${y}" x2="${W-m.r}" y2="${y}"></line><text class="chart-tick" x="${W-m.r+7}" y="${y+4}">${esc(formatMetricValue(raw,_analysisMetric,adaptiveAxisDigits([0,sourceMax],metric.percent)))}</text>`;
  }
  html+=`<text class="chart-label" x="${m.l}" y="${topY-6}">品牌提及率</text><text class="chart-label" x="${m.l}" y="${bottomY-6}">${esc(metric.label)}</text>`;
  const ciUpper=[],ciLower=[];
  rows.forEach((row,index)=>{const ci=wilsonInterval(row.mentioned_runs||0,row.denominator_runs||0);if(ci){ciUpper.push([x(index),mentionY(ci[1])]);ciLower.push([x(index),mentionY(ci[0])])}});
  if(ciUpper.length>1){const band=[...ciUpper,...ciLower.reverse()];html+=`<path class="ci-band" d="${band.map((point,index)=>`${index?"L":"M"}${point[0].toFixed(2)},${point[1].toFixed(2)}`).join(" ")} Z"></path>`}
  html+=`<path class="mention-line" d="${linePath(rows,row=>row.rate,x,mentionY)}"></path><path class="source-line" d="${linePath(rows,row=>row.metricValue,x,sourceY)}"></path>`;
  rows.forEach((row,index)=>{
    const partial=row.status!=="closed"?" partial-dot":"";
    if(finiteNumber(row.rate))html+=`<circle class="mention-dot${partial}" cx="${x(index)}" cy="${mentionY(row.rate)}" r="4.5"></circle>`;
    if(finiteNumber(row.metricValue))html+=`<circle class="source-dot${partial}" cx="${x(index)}" cy="${sourceY(row.metricValue)}" r="4"></circle>`;
  });
  const maxTicks=W<420?3:(W<760?5:7);
  tickIndexes(rows.length,maxTicks).forEach(index=>{html+=`<text class="chart-tick" x="${x(index)}" y="${H-9}" text-anchor="middle">${esc(shortDate(rows[index].date))}</text>`});
  html+=`<line class="selected-guide" id="trendGuide" x1="0" y1="${topY}" x2="0" y2="${bottomY+laneH}" style="display:none"></line>`;
  svg.setAttribute("viewBox",`0 0 ${W} ${H}`);svg.innerHTML=html;
  setText("mentionLegend",`${_analysisBrand} 提及率（出现轮次/审核轮次）`);setText("sourceLegend",metric.label);
  const show=(event,index)=>{
    const row=rows[index],guide=svg.querySelector("#trendGuide");
    guide.style.display="";guide.setAttribute("x1",x(index));guide.setAttribute("x2",x(index));
    tooltip.innerHTML=`<b>${esc(row.date)} · ${esc(ANALYSIS_STATUS_LABELS[row.status]||row.status)}</b><br>${esc(_analysisBrand)} 产品提及率：${esc(formatRate(row.rate,2))}（${esc(row.mentioned_runs||0)}/${esc(row.denominator_runs||0)}轮）<br>${esc(metric.label)}：${esc(formatMetricValue(row.metricValue,_analysisMetric,2))}（${esc(metricEvidence(row))}）<br>全信源：${esc(row.refs||0)}条 · 正文已归档${esc(row.content_available_refs||0)}条（${esc(formatRate(row.content_fetch_coverage,1))}）`;
    placeChartTooltip(tooltip,frame,event);
  };
  svg.onpointermove=event=>{const rect=svg.getBoundingClientRect(),local=(event.clientX-rect.left)*W/Math.max(1,rect.width),index=rows.length===1?0:Math.round((local-m.l)/plotW*(rows.length-1));show(event,clampValue(index,0,rows.length-1))};
  svg.onpointerdown=svg.onpointermove;
  svg.onpointerleave=()=>{tooltip.classList.remove("is-visible");const guide=svg.querySelector("#trendGuide");if(guide)guide.style.display="none"};
}

function paddedDomain(values,includeZero=false){
  if(!values.length)return[0,1];let min=Math.min(...values),max=Math.max(...values);if(includeZero){min=Math.min(0,min);max=Math.max(0,max)}
  if(min===max){const pad=Math.abs(min||1)*.15;return[min-pad,max+pad]}
  const pad=(max-min)*.12;return[min-pad,max+pad];
}
function renderCorrelationChart(pairs,summary){
  const frame=$("correlationChartFrame"),svg=$("correlationChart"),tooltip=$("correlationTooltip"),metric=ANALYSIS_METRICS[_analysisMetric]||ANALYSIS_METRICS.source_av_ref_share;
  if(!frame||!svg||frame.clientWidth<120)return;
  tooltip.classList.remove("is-visible");tooltip.innerHTML="";
  const W=Math.max(260,frame.clientWidth),H=Math.max(280,frame.clientHeight),m={l:48,r:18,t:20,b:48},plotW=W-m.l-m.r,plotH=H-m.t-m.b;
  let xDomain,yDomain;
  const xValues=pairs.map(row=>row.x),yValues=pairs.map(row=>row.y);
  if(_analysisMode==="level")xDomain=[0,nicePositiveCeiling(xValues,{minimum:metric.percent?.05:1,maximum:metric.percent?1:null})];else xDomain=paddedDomain(xValues,true);
  if(_analysisMode==="level")yDomain=[0,nicePositiveCeiling(yValues,{minimum:.1,maximum:1})];else yDomain=paddedDomain(yValues,true);
  const sx=value=>m.l+(value-xDomain[0])/(xDomain[1]-xDomain[0]||1)*plotW,sy=value=>m.t+(1-(value-yDomain[0])/(yDomain[1]-yDomain[0]||1))*plotH;
  const clipId="correlationPlotClip";
  let html=`<title>${esc(_analysisBrand)}产品提及率与${esc(metric.label)}的日级相关散点</title><desc>每个点代表一个观测日；标题命中分子与全部信源分母均保留跨轮重复链接。虚线仅为样本内拟合，不代表因果。</desc><defs><clipPath id="${clipId}"><rect x="${m.l}" y="${m.t}" width="${plotW}" height="${plotH}"></rect></clipPath></defs>`;
  const tickSteps=W<360?2:(W<600?3:4);
  const xDigits=adaptiveAxisDigits(xDomain,metric.percent),yDigits=adaptiveAxisDigits(yDomain,true);
  for(let i=0;i<=tickSteps;i++){
    const fraction=i/tickSteps,xv=xDomain[0]+fraction*(xDomain[1]-xDomain[0]),yv=yDomain[0]+fraction*(yDomain[1]-yDomain[0]),x=sx(xv),y=sy(yv);
    html+=`<line class="chart-grid" x1="${x}" y1="${m.t}" x2="${x}" y2="${H-m.b}"></line><text class="chart-tick" x="${x}" y="${H-m.b+17}" text-anchor="middle">${esc(formatMetricValue(xv,_analysisMetric,xDigits))}</text>`;
    html+=`<line class="chart-grid" x1="${m.l}" y1="${y}" x2="${W-m.r}" y2="${y}"></line><text class="chart-tick" x="${m.l-7}" y="${y+4}" text-anchor="end">${esc(formatRate(yv,yDigits))}</text>`;
  }
  if(pairs.length>=3){
    const xs=pairs.map(row=>row.x),ys=pairs.map(row=>row.y),mx=mean(xs),my=mean(ys),den=xs.reduce((sum,value)=>sum+(value-mx)**2,0);
    const fitMin=Math.min(...xs),fitMax=Math.max(...xs);
    if(den>1e-12&&fitMax>fitMin){
      const slope=xs.reduce((sum,value,index)=>sum+(value-mx)*(ys[index]-my),0)/den,intercept=my-slope*mx;
      const y1=intercept+slope*fitMin,y2=intercept+slope*fitMax;
      if([slope,intercept,y1,y2].every(Number.isFinite))html+=`<line class="regression-line" clip-path="url(#${clipId})" x1="${sx(fitMin)}" y1="${sy(y1)}" x2="${sx(fitMax)}" y2="${sy(y2)}"></line>`;
    }
  }
  const points=[];
  pairs.forEach(row=>{
    const plotX=sx(row.x),plotY=sy(row.y),r=clampValue(4+Math.sqrt(Math.max(1,row.reviewed_runs||1))*.45,5,11),partial=row.status!=="closed"?" partial":"";
    points.push({...row,plotX,plotY});
    html+=`<circle class="scatter-dot${partial}" cx="${plotX}" cy="${plotY}" r="${r}"></circle>`;
    if(pairs.length<=8){const onRight=plotX>W-m.r-70,labelX=onRight?plotX-7:plotX+7,labelY=clampValue(plotY-7,m.t+11,H-m.b-4);html+=`<text class="chart-tick" x="${labelX}" y="${labelY}" text-anchor="${onRight?"end":"start"}">${esc(shortDate(row.date))}</text>`}
  });
  const axisMetric=W<420?(metric.shortLabel||"标题命中份额"):metric.label;
  html+=`<text class="chart-label" x="${m.l+plotW/2}" y="${H-5}" text-anchor="middle">${esc(axisMetric)}${_analysisMode==="delta"?"日变化 Δ":""}</text><text class="chart-label" transform="translate(13 ${m.t+plotH/2}) rotate(-90)" text-anchor="middle">品牌产品提及率${_analysisMode==="delta"?"日变化 Δ":""}</text>`;
  svg.setAttribute("viewBox",`0 0 ${W} ${H}`);svg.innerHTML=html;
  if(!points.length){html+=`<text class="chart-label" x="${W/2}" y="${H/2}" text-anchor="middle">暂无满足质量条件的观测日</text>`;svg.innerHTML=html;return}
  const show=(event,point)=>{tooltip.innerHTML=`<b>${esc(point.date)}${point.previousDate?`（较${esc(point.previousDate)}）`:""}</b><br>${esc(metric.label)}：${esc(formatMetricValue(point.x,_analysisMetric,2))}${_analysisMode==="delta"?" Δ":""}（${esc(metricEvidence(point))}）<br>品牌产品提及率：${esc(formatRate(point.y,2))}${_analysisMode==="delta"?" Δ":""}（${esc(point.mentioned_runs||0)}/${esc(point.denominator_runs||0)}轮）<br>全信源：${esc(point.refs||0)}条 · 重复保留`;placeChartTooltip(tooltip,frame,event)};
  svg.onpointermove=event=>{const rect=svg.getBoundingClientRect(),px=(event.clientX-rect.left)*W/Math.max(1,rect.width),py=(event.clientY-rect.top)*H/Math.max(1,rect.height);let nearest=points[0],distance=Infinity;points.forEach(point=>{const d=(point.plotX-px)**2+(point.plotY-py)**2;if(d<distance){distance=d;nearest=point}});if(distance<=900)show(event,nearest);else tooltip.classList.remove("is-visible")};
  svg.onpointerdown=svg.onpointermove;svg.onpointerleave=()=>tooltip.classList.remove("is-visible");
}

function renderAnalysisTable(rows){
  const body=$("analysisTableRows");if(!body)return;
  body.innerHTML=rows.map(row=>`<tr>
    <td>${esc(row.date)}</td><td><span class="quality-status ${esc(row.status)}">${esc(ANALYSIS_STATUS_LABELS[row.status]||row.status)}</span></td>
    <td>${esc(row.mentioned_runs||0)}/${esc(row.denominator_runs||0)}轮</td><td>${esc(formatRate(row.rate,2))}</td><td>${esc(row.refs||0)}条</td>
    <td>${esc(row.title_av_refs||0)}/${esc(row.refs||0)} · ${esc(formatRate(row.title_av_ref_share,2))}</td>
    <td>${esc(row.content_only_refs||0)}/${esc(row.refs||0)} · ${esc(formatRate(row.content_only_ref_share,2))}</td>
    <td>${esc(row.source_av_refs||0)}/${esc(row.refs||0)} · ${esc(formatRate(row.source_av_ref_share,2))}</td>
    <td>${esc(row.content_available_refs||0)}/${esc(row.refs||0)} · ${esc(formatRate(row.content_fetch_coverage,1))}</td>
    <td>${esc(row.reviewed_runs||0)}/${esc(row.observed_runs||0)}审核 · ${esc(row.source_observed_runs||0)}信源轮</td>
  </tr>`).join("")||'<tr><td colspan="10" class="empty">暂无逐日数据</td></tr>';
}

function renderAnalysisSourceExamples(){
  const body=$("analysisSourceExamples");if(!body)return;
  const brand=analysisBrandObject(),rows=brand?.source_examples||[];
  body.innerHTML=rows.map(row=>`<tr data-filter-text="${esc(`${brand?.name||""} ${row.title||""} ${row.source_type||""} ${row.scope||""}`)}">
    <td data-label="标题">${esc(row.title||"（无标题）")}</td>
    <td data-label="命中位置">${esc(row.scope||"—")}</td>
    <td data-label="类型">${esc(row.source_type||"其他")}</td>
    <td data-label="引用次数">${esc(row.refs||0)}次 / ${esc(row.runs||0)}轮</td>
    <td data-label="最近日期">${esc(row.latest_date||row.first_date||"—")}</td>
    <td data-label="链接">${row.href?`<a href="${esc(row.href)}" target="_blank" rel="noopener noreferrer">打开信源</a>`:"—"}</td>
  </tr>`).join("")||'<tr><td colspan="6" class="empty">当前品牌暂无线索可回溯的信源链接</td></tr>';
}

function ownedProductObject(){
  return(_ownedAnalysisData?.products||[]).find(item=>item.name===_ownedProduct)||null;
}
function ownedAnalysisDateSet(){
  const dates=_ownedAnalysisData?.observation_dates||[];
  const count=_ownedRange==="all"?dates.length:Math.max(1,+_ownedRange||7);
  return new Set(dates.slice(Math.max(0,dates.length-count)));
}
function ownedAnalysisRows(product=ownedProductObject()){
  const rows=product?.days||[];
  const selectedDates=ownedAnalysisDateSet();
  return selectedDates.size?rows.filter(row=>selectedDates.has(row.date)):rows;
}
function persistOwnedAnalysisControls(){
  sessionStorage.setItem("doubaoOwnedAnalysisProduct",_ownedProduct);
  sessionStorage.setItem("doubaoOwnedAnalysisRange",_ownedRange);
}
function aggregateOwnedKeywordDays(days,selectedDates){
  let titleCount=0,sourceRefs=0;
  const documentFrequency={},termFrequency={},normalizedTf={};
  (days||[]).forEach(day=>{
    if(selectedDates&&selectedDates.size&&!selectedDates.has(day.date))return;
    titleCount+=+day.title_count||0;sourceRefs+=+day.source_refs||0;
    Object.entries(day.document_frequency||{}).forEach(([token,value])=>documentFrequency[token]=(documentFrequency[token]||0)+(+value||0));
    Object.entries(day.term_frequency||{}).forEach(([token,value])=>termFrequency[token]=(termFrequency[token]||0)+(+value||0));
    Object.entries(day.normalized_tf||{}).forEach(([token,value])=>normalizedTf[token]=(normalizedTf[token]||0)+(+value||0));
  });
  const rows=Object.keys(documentFrequency).filter(token=>documentFrequency[token]>=2).map(token=>{
    const df=documentFrequency[token],idf=Math.log((titleCount+1)/(df+1))+1;
    return{keyword:token,document_count:df,term_count:termFrequency[token]||0,coverage:titleCount?df/titleCount:0,score:titleCount?(normalizedTf[token]||0)*idf/titleCount:0};
  }).sort((a,b)=>b.score-a.score||b.document_count-a.document_count||b.term_count-a.term_count||String(a.keyword).localeCompare(String(b.keyword),"zh-CN"));
  return{titleCount,sourceRefs,rows};
}
function renderOwnedProductSourceAnalysis(analytics){
  _ownedAnalysisData=analytics||null;
  const empty=$("ownedAnalysisEmpty"),body=$("ownedAnalysisBody"),select=$("ownedAnalysisProduct");
  const products=analytics?.products||[];
  if(!analytics||analytics.status!=="ok"||!products.length){
    body.hidden=true;empty.hidden=false;
    empty.textContent=analytics?.warning||"请选择单个产品问题后查看自有产品信源结构。";
    select.innerHTML='<option value="">暂无自有产品命中</option>';
    return;
  }
  if(!products.some(item=>item.name===_ownedProduct))_ownedProduct=products[0]?.name||"";
  const baselineOnly=!!analytics.baseline_only;
  select.innerHTML=products.map(item=>`<option value="${esc(item.name)}">${esc(item.name)} · ${esc(item.total_refs)}条</option>`).join("");
  select.value=_ownedProduct;
  $("ownedAnalysisRange").value=_ownedRange;
  empty.hidden=true;body.hidden=false;
  setText("ownedAnalysisProductLabel",baselineOnly?"分析对象":"我的产品");
  setText("ownedAnalysisDesc",baselineOnly
    ?`${analytics.question} · 尚未确认命中自有产品，当前自动展示豆包在该品类全部信源中的文章/视频结构、标题主题和关键词偏好。`
    :`${analytics.question} · 分母是标题或可靠正文确认命中自有产品的信源行；重复引用与唯一链接同时展示。`);
  const quality=analytics.quality||{};
  setText("ownedAnalysisFootnote",baselineOnly
    ?`${analytics.definitions?.category_baseline||""} 当前问题正文可靠归档 ${formatRate(quality.content_archive_coverage,1)}（${quality.content_archived_rows||0}/${quality.question_source_rows||0}条）。`
    :`${analytics.definitions?.primary_denominator||""} ${analytics.definitions?.unique_denominator||""} 当前问题正文可靠归档 ${formatRate(quality.content_archive_coverage,1)}（${quality.content_archived_rows||0}/${quality.question_source_rows||0}条）；未归档链接不按未提及处理。`);
  restoreDynamicDetails(body);persistOwnedAnalysisControls();scheduleAnalysisRender();
}
function renderOwnedSummary(product,rows){
  const latest=rows[rows.length-1]||{};
  const baseline=!!product.is_category_baseline,quality=_ownedAnalysisData?.quality||{};
  setText("ownedTotalRefsLabel",baseline?"品类全部信源":"确认命中信源");
  setText("ownedBodyOnlyLabel",baseline?"正文归档覆盖":"正文补充贡献");
  setText("ownedTotalRefs",`${product.total_refs||0}条`);
  setText("ownedTotalRefsMeta",`${baseline?"品类全量 · ":""}文章${product.article_refs||0} · 视频${product.video_refs||0} · 重复引用保留`);
  setText("ownedLatestVideo",formatRate(latest.video_share,1));
  setText("ownedLatestVideoMeta",latest.date?`${latest.date} · ${latest.video_refs||0}/${latest.refs||0}条`:"暂无逐日数据");
  setText("ownedUniqueUrls",`${product.unique_urls||0}个`);
  setText("ownedUniqueUrlsMeta",`全部观测期标准化URL去重`);
  setText("ownedBodyOnly",baseline?formatRate(quality.content_archive_coverage,1):formatRate(product.body_only_share,1));
  setText("ownedBodyOnlyMeta",baseline?`${quality.content_archived_rows||0}/${quality.question_source_rows||0}条正文可靠归档`:`${product.body_only_refs||0}/${product.total_refs||0}条由正文补充识别`);
  const selectedDates=ownedAnalysisDateSet();
  const articleKeywords=aggregateOwnedKeywordDays(product.keyword_days?.article||[],selectedDates);
  const videoKeywords=aggregateOwnedKeywordDays(product.keyword_days?.video||[],selectedDates);
  const allVideoKeywords=aggregateOwnedKeywordDays(_ownedAnalysisData?.all_video_keyword_days||[],selectedDates);
  const allVideoByName=new Map(allVideoKeywords.rows.map(item=>[item.keyword,item]));
  const keywordHtml=(items,comparison=null)=>items.slice(0,7).map(item=>{
    const peer=comparison?.get(item.keyword),peerText=peer?`（全量${formatRate(peer.coverage,0)}）`:"";
    return`<span>${esc(item.keyword)} · ${esc(formatRate(item.coverage,0))}${esc(peerText)}</span>`;
  }).join("")||'<span>暂无稳定关键词</span>';
  $("ownedArticleKeywords").innerHTML=keywordHtml(articleKeywords.rows);
  $("ownedVideoKeywords").innerHTML=keywordHtml(videoKeywords.rows,baseline?null:allVideoByName);
  $("allVideoKeywords").innerHTML=keywordHtml(allVideoKeywords.rows);
  setText("ownedArticleKeywordsLabel",baseline?"全量文章关键词":"文章关键词（自有命中）");
  setText("ownedVideoKeywordsLabel",baseline?"全量视频关键词":"视频关键词（自有产品）");
  setText("ownedArticleKeywordsMeta",`${articleKeywords.titleCount}/${articleKeywords.sourceRefs}条${baseline?"品类":""}文章信源有标题`);
  setText("ownedVideoKeywordsMeta",`${videoKeywords.titleCount}/${videoKeywords.sourceRefs}条${baseline?"品类":"自有产品"}视频信源有标题`);
  setText("allVideoKeywordsMeta",`${allVideoKeywords.titleCount}/${allVideoKeywords.sourceRefs}条品类视频信源有标题`);
  $("allVideoKeywordsGroup").hidden=baseline;
  const strategy=product.strategy||{},message=$("ownedStrategyMessage");
  const latestGap=finiteNumber(latest.video_share)&&finiteNumber(latest.unique_video_share)?Math.abs(latest.video_share-latest.unique_video_share):0;
  message.className="correlation-message";
  let text=`${baseline?"品类全量":"当前自有产品"}视频标题更偏“${strategy.video_theme||"暂无显著主题"}”，文章标题更偏“${strategy.article_theme||"暂无显著主题"}”。`;
  const leadingVideo=videoKeywords.rows[0],leadingPeer=leadingVideo?allVideoByName.get(leadingVideo.keyword):null;
  if(leadingVideo&&!baseline){
    const peerCoverage=leadingPeer?.coverage||0,diff=leadingVideo.coverage-peerCoverage;
    text+=` 自有产品视频最强词“${leadingVideo.keyword}”覆盖${formatRate(leadingVideo.coverage,0)}，品类全量${formatRate(peerCoverage,0)}，${diff>=0?"高":"低"}${Math.abs(diff*100).toFixed(0)}个百分点。`;
  }
  if(baseline&&leadingVideo){text+=` 当前品类视频最稳定的标题词是“${leadingVideo.keyword}”，覆盖${formatRate(leadingVideo.coverage,0)}。`}
  if(latestGap>=.2){message.classList.add("warning");text+=` 最新日重复口径与唯一链接口径相差${formatRate(latestGap,1)}，变化主要受少数高频链接推动。`}
  else{text+=baseline?" 这些结果反映豆包当前对该品类信源标题的整体偏好。":" 两种口径方向接近，可把重复引用理解为豆包对这些内容的持续偏好。"}
  message.textContent=text;
}
function ownedMentionTrendRows(product){
  const analytics=_brandAnalysisData||{},brands=analytics.brands||[];
  const brandName=product?.brand||"";
  const brand=brands.find(item=>item.name===brandName)||brands.find(item=>brandName&&(item.name.includes(brandName)||brandName.includes(item.name)));
  if(!brand)return{brand:null,rows:[]};
  const pointByDate=new Map((brand.points||[]).map(point=>[point.date,point]));
  const selectedDates=ownedAnalysisDateSet();
  const rows=(analytics.days||[]).filter(day=>!selectedDates.size||selectedDates.has(day.date)).map(day=>{
    const point=pointByDate.get(day.date)||{};
    return{...day,...point,metricValue:point.source_av_ref_share};
  });
  return{brand,rows};
}
function renderCategoryBaselineMixChart(product){
  const frame=$("ownedMixChartFrame"),svg=$("ownedMixChart"),tooltip=$("ownedMixTooltip"),rows=ownedAnalysisRows(product);
  if(!frame||!svg||frame.clientWidth<120)return;
  tooltip.classList.remove("is-visible");tooltip.innerHTML="";
  const W=Math.max(280,frame.clientWidth),H=Math.max(300,frame.clientHeight),m={l:45,r:18,t:18,b:34},plotW=W-m.l-m.r,plotH=H-m.t-m.b;
  const x=index=>rows.length===1?m.l+plotW/2:m.l+index*plotW/Math.max(1,rows.length-1);
  const y=value=>m.t+(1-clampValue(value,0,1))*plotH;
  let html=`<title>每日品类文章与视频信源占比</title><desc>绿色实线为文章信源占当日品类全部信源的比例，蓝色虚线为视频信源占比。</desc>`;
  for(const tick of [0,.25,.5,.75,1]){
    const yy=y(tick);html+=`<line class="chart-grid" x1="${m.l}" y1="${yy}" x2="${W-m.r}" y2="${yy}"></line><text class="chart-tick" x="${m.l-7}" y="${yy+4}" text-anchor="end">${esc(formatRate(tick,0))}</text>`;
  }
  html+=`<text class="chart-label" x="${m.l}" y="${m.t-7}">占当日品类全部信源</text>`;
  html+=`<path class="mention-line" d="${linePath(rows,row=>row.article_share,x,y)}"></path>`;
  html+=`<path class="source-line" d="${linePath(rows,row=>row.video_share,x,y)}"></path>`;
  rows.forEach((row,index)=>{
    if(finiteNumber(row.article_share))html+=`<circle class="mention-dot" cx="${x(index)}" cy="${y(row.article_share)}" r="4.5"></circle>`;
    if(finiteNumber(row.video_share))html+=`<circle class="source-dot" cx="${x(index)}" cy="${y(row.video_share)}" r="4"></circle>`;
  });
  tickIndexes(rows.length,W<420?3:(W<760?5:7)).forEach(index=>{html+=`<text class="chart-tick" x="${x(index)}" y="${H-9}" text-anchor="middle">${esc(shortDate(rows[index].date))}</text>`});
  svg.setAttribute("viewBox",`0 0 ${W} ${H}`);svg.innerHTML=html;
  setText("ownedMainTrendTitle","每日品类文章 / 视频信源占比");
  setText("ownedMentionLegend","文章信源占比");
  setText("ownedSourceLegend","视频信源占比");
  if(!rows.length){svg.innerHTML=html+`<text class="chart-label" x="${W/2}" y="${H/2}" text-anchor="middle">暂无逐日信源数据</text>`;return}
  const show=(event,index)=>{
    const row=rows[index];
    tooltip.innerHTML=`<b>${esc(row.date)}</b><br>文章信源：${esc(formatRate(row.article_share,1))}（${esc(row.article_refs||0)}/${esc(row.refs||0)}条）<br>视频信源：${esc(formatRate(row.video_share,1))}（${esc(row.video_refs||0)}/${esc(row.refs||0)}条）<br>唯一链接：${esc(row.unique_urls||0)}个`;
    placeChartTooltip(tooltip,frame,event);
  };
  svg.onpointermove=event=>{const rect=svg.getBoundingClientRect(),local=(event.clientX-rect.left)*W/Math.max(1,rect.width),index=rows.length===1?0:Math.round((local-m.l)/plotW*(rows.length-1));show(event,clampValue(index,0,rows.length-1))};
  svg.onpointerdown=svg.onpointermove;svg.onpointerleave=()=>tooltip.classList.remove("is-visible");
}
function renderOwnedMixChart(product){
  const frame=$("ownedMixChartFrame"),svg=$("ownedMixChart"),tooltip=$("ownedMixTooltip");
  if(!frame||!svg||frame.clientWidth<120)return;
  if(product?.is_category_baseline){renderCategoryBaselineMixChart(product);return}
  tooltip.classList.remove("is-visible");tooltip.innerHTML="";
  const trend=ownedMentionTrendRows(product),rows=trend.rows,brand=trend.brand;
  setText("ownedMainTrendTitle","每日品牌提及率 × 信源命中率");
  const W=Math.max(280,frame.clientWidth),H=Math.max(300,frame.clientHeight),m={l:48,r:54,t:18,b:34},gap=42;
  const laneH=(H-m.t-m.b-gap)/2,topY=m.t,bottomY=m.t+laneH+gap,plotW=W-m.l-m.r;
  const x=index=>rows.length===1?m.l+plotW/2:m.l+index*plotW/Math.max(1,rows.length-1);
  const mentionScaleValues=[];
  rows.forEach(row=>{
    if(finiteNumber(row.rate))mentionScaleValues.push(row.rate);
    const ci=wilsonInterval(row.mentioned_runs||0,row.denominator_runs||0);
    if(ci)mentionScaleValues.push(ci[1]);
  });
  const mentionMax=nicePositiveCeiling(mentionScaleValues,{minimum:.1,maximum:1});
  const sourceValues=rows.map(row=>row.metricValue).filter(finiteNumber);
  const sourceMax=nicePositiveCeiling(sourceValues,{minimum:.05,maximum:1});
  const mentionY=value=>topY+laneH*(1-clampValue(value/mentionMax,0,1));
  const sourceY=value=>bottomY+laneH*(1-clampValue(value/sourceMax,0,1));
  let html=`<title>${esc(brand?.name||_ownedProduct)}每日品牌提及率与信源命中率</title><desc>上层为品牌在豆包产品推荐中的提及率及95%置信区间，下层为标题或可靠正文命中该品牌的信源占全部信源的比例。</desc>`;
  for(const tick of [0,.5,1]){
    const mentionRaw=tick*mentionMax,mentionTickY=mentionY(mentionRaw);
    const sourceRaw=tick*sourceMax,sourceTickY=sourceY(sourceRaw);
    html+=`<line class="chart-grid" x1="${m.l}" y1="${mentionTickY}" x2="${W-m.r}" y2="${mentionTickY}"></line><text class="chart-tick" x="${m.l-7}" y="${mentionTickY+4}" text-anchor="end">${esc(formatRate(mentionRaw,adaptiveAxisDigits([0,mentionMax],true)))}</text>`;
    html+=`<line class="chart-grid" x1="${m.l}" y1="${sourceTickY}" x2="${W-m.r}" y2="${sourceTickY}"></line><text class="chart-tick" x="${W-m.r+7}" y="${sourceTickY+4}">${esc(formatRate(sourceRaw,adaptiveAxisDigits([0,sourceMax],true)))}</text>`;
  }
  html+=`<text class="chart-label" x="${m.l}" y="${topY-6}">品牌提及率</text><text class="chart-label" x="${m.l}" y="${bottomY-6}">标题或正文命中信源占比</text>`;
  const ciUpper=[],ciLower=[];
  rows.forEach((row,index)=>{
    const ci=wilsonInterval(row.mentioned_runs||0,row.denominator_runs||0);
    if(ci){ciUpper.push([x(index),mentionY(ci[1])]);ciLower.push([x(index),mentionY(ci[0])])}
  });
  if(ciUpper.length>1){const band=[...ciUpper,...ciLower.reverse()];html+=`<path class="ci-band" d="${band.map((point,index)=>`${index?"L":"M"}${point[0].toFixed(2)},${point[1].toFixed(2)}`).join(" ")} Z"></path>`}
  html+=`<path class="mention-line" d="${linePath(rows,row=>row.rate,x,mentionY)}"></path><path class="source-line" d="${linePath(rows,row=>row.metricValue,x,sourceY)}"></path>`;
  rows.forEach((row,index)=>{
    const partial=row.status!=="closed"?" partial-dot":"";
    if(finiteNumber(row.rate))html+=`<circle class="mention-dot${partial}" cx="${x(index)}" cy="${mentionY(row.rate)}" r="4.5"></circle>`;
    if(finiteNumber(row.metricValue))html+=`<circle class="source-dot${partial}" cx="${x(index)}" cy="${sourceY(row.metricValue)}" r="4"></circle>`;
  });
  tickIndexes(rows.length,W<420?3:(W<760?5:7)).forEach(index=>{html+=`<text class="chart-tick" x="${x(index)}" y="${H-9}" text-anchor="middle">${esc(shortDate(rows[index].date))}</text>`});
  html+=`<line class="selected-guide" id="ownedTrendGuide" x1="0" y1="${topY}" x2="0" y2="${bottomY+laneH}" style="display:none"></line>`;
  svg.setAttribute("viewBox",`0 0 ${W} ${H}`);svg.innerHTML=html;
  setText("ownedMentionLegend",`${brand?.name||_ownedProduct} 品牌提及率`);
  setText("ownedSourceLegend","标题或正文命中信源占比");
  if(!rows.length){svg.innerHTML=html+`<text class="chart-label" x="${W/2}" y="${H/2}" text-anchor="middle">暂无品牌逐日数据</text>`;return}
  const show=(event,index)=>{
    const row=rows[index],guide=svg.querySelector("#ownedTrendGuide");
    if(guide){guide.style.display="";guide.setAttribute("x1",x(index));guide.setAttribute("x2",x(index))}
    tooltip.innerHTML=`<b>${esc(row.date)} · ${esc(ANALYSIS_STATUS_LABELS[row.status]||row.status||"观测日")}</b><br>${esc(brand?.name||_ownedProduct)} 品牌提及率：${esc(formatRate(row.rate,2))}（${esc(row.mentioned_runs||0)}/${esc(row.denominator_runs||0)}轮）<br>标题或正文命中信源占比：${esc(formatRate(row.metricValue,2))}（${esc(row.source_av_refs||0)}/${esc(row.refs||0)}条）<br>其中标题命中${esc(row.title_av_refs||0)}条 · 正文新增命中${esc(row.content_only_refs||0)}条`;
    placeChartTooltip(tooltip,frame,event);
  };
  svg.onpointermove=event=>{const rect=svg.getBoundingClientRect(),local=(event.clientX-rect.left)*W/Math.max(1,rect.width),index=rows.length===1?0:Math.round((local-m.l)/plotW*(rows.length-1));show(event,clampValue(index,0,rows.length-1))};
  svg.onpointerdown=svg.onpointermove;
  svg.onpointerleave=()=>{tooltip.classList.remove("is-visible");const guide=svg.querySelector("#ownedTrendGuide");if(guide)guide.style.display="none"};
}
function ownedKeywordDayPoint(days,date,keyword){
  const day=(days||[]).find(item=>item.date===date);
  if(!day)return{sourceRefs:0,titleCount:0,documentCount:0,coverage:null};
  const documentCount=+(day.document_frequency?.[keyword]||0),titleCount=+day.title_count||0;
  return{sourceRefs:+day.source_refs||0,titleCount,documentCount,coverage:titleCount?documentCount/titleCount:null};
}
function renderOwnedKeywordTrend(product){
  const frame=$("ownedKeywordTrendFrame"),svg=$("ownedKeywordTrendChart"),tooltip=$("ownedKeywordTrendTooltip"),picker=$("ownedKeywordTrendPicker");
  if(!frame||!svg||!picker||frame.clientWidth<120)return;
  tooltip.classList.remove("is-visible");tooltip.innerHTML="";
  const baseline=!!product.is_category_baseline,dates=[...ownedAnalysisDateSet()],dateSet=new Set(dates);
  $("ownedKeywordHowto").innerHTML=baseline
    ?"<b>怎么看：</b>选择关键词后，蓝色折线显示该词在品类全部视频标题中的每日覆盖率，反映豆包当前抓取这一品类视频时偏好的标题表达。"
    :"<b>怎么看：</b>先选择一个关键词，再比较两条线每天的覆盖率变化。覆盖率＝标题含该词的信源条数 ÷ 当天对应视频标题总数。";
  const ownDays=product.keyword_days?.video||[],allDays=_ownedAnalysisData?.all_video_keyword_days||[];
  const ownAggregate=aggregateOwnedKeywordDays(ownDays,dateSet),allAggregate=aggregateOwnedKeywordDays(allDays,dateSet);
  const keywords=[];
  const addKeyword=item=>{if(item?.keyword&&!keywords.includes(item.keyword))keywords.push(item.keyword)};
  if(!baseline)ownAggregate.rows.slice(0,7).forEach(addKeyword);
  allAggregate.rows.slice(0,7).forEach(addKeyword);
  const visibleKeywords=keywords.slice(0,10);
  if(!visibleKeywords.includes(_ownedKeyword))_ownedKeyword=visibleKeywords[0]||"";
  picker.innerHTML=visibleKeywords.map(keyword=>`<button type="button" class="keyword-trend-option${keyword===_ownedKeyword?" active":""}" data-keyword="${esc(keyword)}" aria-pressed="${keyword===_ownedKeyword?"true":"false"}">${esc(keyword)}</button>`).join("")||'<span class="chart-panel-meta">暂无稳定关键词</span>';
  picker.querySelectorAll("[data-keyword]").forEach(button=>button.addEventListener("click",()=>{
    _ownedKeyword=button.dataset.keyword||"";
    sessionStorage.setItem("doubaoOwnedKeywordTrend",_ownedKeyword);
    scheduleAnalysisRender();
  }));
  const W=Math.max(270,frame.clientWidth),H=W<420?270:310,m={l:45,r:18,t:20,b:36},plotW=W-m.l-m.r,plotH=H-m.t-m.b;
  frame.style.height=`${H}px`;
  const x=index=>dates.length===1?m.l+plotW/2:m.l+index*plotW/Math.max(1,dates.length-1);
  const y=value=>m.t+(1-clampValue(value,0,1))*plotH;
  const points=dates.map(date=>{
    const own=ownedKeywordDayPoint(ownDays,date,_ownedKeyword),all=ownedKeywordDayPoint(allDays,date,_ownedKeyword);
    return{date,own,all,delta:finiteNumber(own.coverage)&&finiteNumber(all.coverage)?own.coverage-all.coverage:null};
  });
  let html=`<title>${baseline?"品类全量":esc(_ownedProduct)}“${esc(_ownedKeyword)}”视频标题覆盖率趋势</title><desc>${baseline?"蓝色折线显示当前品类全部视频信源标题覆盖率。":"绿色实线为自有产品视频信源标题覆盖率，蓝色虚线为当前品类全部视频信源标题覆盖率。"}纵轴固定为百分之零到百分之一百。</desc>`;
  for(const tick of [0,.25,.5,.75,1]){
    const yy=y(tick);html+=`<line class="chart-grid" x1="${m.l}" y1="${yy}" x2="${W-m.r}" y2="${yy}"></line><text class="chart-tick" x="${m.l-7}" y="${yy+4}" text-anchor="end">${esc(formatRate(tick,0))}</text>`;
  }
  html+=`<text class="chart-label" x="${m.l}" y="${m.t-7}">“${esc(_ownedKeyword)}”标题覆盖率</text>`;
  if(!baseline)html+=`<path class="mention-line" d="${linePath(points,point=>point.own.coverage,x,y)}"></path>`;
  html+=`<path class="source-line" d="${linePath(points,point=>point.all.coverage,x,y)}"></path>`;
  points.forEach((point,index)=>{
    if(!baseline&&finiteNumber(point.own.coverage))html+=`<circle class="mention-dot" cx="${x(index)}" cy="${y(point.own.coverage)}" r="4.5"></circle>`;
    if(finiteNumber(point.all.coverage))html+=`<circle class="source-dot" cx="${x(index)}" cy="${y(point.all.coverage)}" r="4"></circle>`;
  });
  tickIndexes(dates.length,W<420?3:(W<620?5:7)).forEach(index=>{html+=`<text class="chart-tick" x="${x(index)}" y="${H-10}" text-anchor="middle">${esc(shortDate(dates[index]))}</text>`});
  html+=`<line class="selected-guide" id="ownedKeywordGuide" x1="0" y1="${m.t}" x2="0" y2="${H-m.b}" style="display:none"></line>`;
  svg.setAttribute("viewBox",`0 0 ${W} ${H}`);svg.innerHTML=html;
  $("ownedKeywordOwnLegend").hidden=baseline;
  setText("ownedKeywordTrendMeta",`${dates.length}个观测日 · 当前关键词“${_ownedKeyword}” · ${baseline?"品类全量":"我的产品与品类全量"}标题覆盖率`);
  if(!dates.length||!_ownedKeyword){
    setText("ownedKeywordTrendSummary","暂无可比较的视频标题关键词。");
    svg.innerHTML=html+`<text class="chart-label" x="${W/2}" y="${H/2}" text-anchor="middle">暂无可比较的视频标题关键词</text>`;return
  }
  const latest=points[points.length-1],comparable=points.filter(point=>finiteNumber(point.delta)),advantageDays=comparable.filter(point=>point.delta>.05).length,gapDays=comparable.filter(point=>point.delta<-.05).length;
  if(baseline&&latest&&finiteNumber(latest.all.coverage)){
    setText("ownedKeywordTrendSummary",`${shortDate(latest.date)}“${_ownedKeyword}”：在品类全量视频标题中覆盖${formatRate(latest.all.coverage,1)}（${latest.all.documentCount}/${latest.all.titleCount}个标题）。这条线代表豆包当前抓取该品类视频时的标题偏好。`);
  }else if(!latest||!finiteNumber(latest.delta)){
    setText("ownedKeywordTrendSummary",`${shortDate(latest?.date||"")}“${_ownedKeyword}”：当天缺少可比较的视频标题数据。`);
  }else{
    const direction=Math.abs(latest.delta)<=.05?"与全量接近":(latest.delta>0?`高于全量${(latest.delta*100).toFixed(1)}个百分点`:`低于全量${Math.abs(latest.delta*100).toFixed(1)}个百分点，建议补强`);
    setText("ownedKeywordTrendSummary",`${shortDate(latest.date)}“${_ownedKeyword}”：我的${formatRate(latest.own.coverage,1)}（${latest.own.documentCount}/${latest.own.titleCount}个标题），全量${formatRate(latest.all.coverage,1)}（${latest.all.documentCount}/${latest.all.titleCount}个标题），${direction}；${advantageDays}日为优势、${gapDays}日为缺口。`);
  }
  const show=(event,index)=>{
    const point=points[index],guide=svg.querySelector("#ownedKeywordGuide");
    if(guide){guide.style.display="";guide.setAttribute("x1",x(index));guide.setAttribute("x2",x(index))}
    const ownEvidence=finiteNumber(point.own.coverage)?`${point.own.documentCount}/${point.own.titleCount}个标题`:"当日没有自有产品视频标题";
    const allEvidence=finiteNumber(point.all.coverage)?`${point.all.documentCount}/${point.all.titleCount}个标题`:"当日没有品类视频标题";
    const difference=finiteNumber(point.delta)?`${point.delta>=0?"+":""}${(point.delta*100).toFixed(1)}个百分点`:"不可比较";
    tooltip.innerHTML=baseline
      ?`<b>${esc(point.date)} · ${esc(_ownedKeyword)}</b><br>品类全量：${esc(formatRate(point.all.coverage,1))}（${esc(allEvidence)}）`
      :`<b>${esc(point.date)} · ${esc(_ownedKeyword)}</b><br>我的产品：${esc(formatRate(point.own.coverage,1))}（${esc(ownEvidence)}）<br>品类全量：${esc(formatRate(point.all.coverage,1))}（${esc(allEvidence)}）<br>我的－全量：${esc(difference)}`;
    placeChartTooltip(tooltip,frame,event);
  };
  svg.onpointermove=event=>{const rect=svg.getBoundingClientRect(),local=(event.clientX-rect.left)*W/Math.max(1,rect.width),index=dates.length===1?0:Math.round((local-m.l)/plotW*(dates.length-1));show(event,clampValue(index,0,dates.length-1))};
  svg.onpointerdown=svg.onpointermove;
  svg.onpointerleave=()=>{tooltip.classList.remove("is-visible");const guide=svg.querySelector("#ownedKeywordGuide");if(guide)guide.style.display="none"};
}
function renderOwnedThemeChart(product){
  const frame=$("ownedThemeChartFrame"),svg=$("ownedThemeChart"),tooltip=$("ownedThemeTooltip"),rows=product?.themes||[];
  if(!frame||!svg||frame.clientWidth<120)return;
  tooltip.classList.remove("is-visible");tooltip.innerHTML="";
  const W=Math.max(280,frame.clientWidth),H=Math.max(300,frame.clientHeight),m={l:92,r:18,t:16,b:28},plotW=W-m.l-m.r,plotH=H-m.t-m.b,band=plotH/Math.max(1,rows.length);
  const x=value=>m.l+clampValue(value,0,1)*plotW;
  let html=`<title>${esc(_ownedProduct)}文章视频标题主题差异</title><desc>每个主题显示文章和视频标题覆盖率，星号表示双比例检验显著。</desc>`;
  for(const tick of [0,.25,.5,.75,1]){const xx=x(tick);html+=`<line class="chart-grid" x1="${xx}" y1="${m.t}" x2="${xx}" y2="${H-m.b}"></line><text class="chart-tick" x="${xx}" y="${H-8}" text-anchor="middle">${esc(formatRate(tick,0))}</text>`}
  rows.forEach((row,index)=>{
    const yy=m.t+index*band,labelY=yy+band*.52;
    html+=`<text class="chart-tick" x="${m.l-7}" y="${labelY+3}" text-anchor="end">${esc(row.name)}${row.significant?" ★":""}</text>`;
    html+=`<rect class="theme-article" x="${m.l}" y="${yy+band*.18}" width="${Math.max(0,x(row.article_rate)-m.l)}" height="${Math.max(4,band*.24)}" rx="2"></rect>`;
    html+=`<rect class="theme-video" x="${m.l}" y="${yy+band*.54}" width="${Math.max(0,x(row.video_rate)-m.l)}" height="${Math.max(4,band*.24)}" rx="2"></rect>`;
  });
  svg.setAttribute("viewBox",`0 0 ${W} ${H}`);svg.innerHTML=html;
  if(!rows.length){svg.innerHTML=html+`<text class="chart-label" x="${W/2}" y="${H/2}" text-anchor="middle">暂无标题主题样本</text>`;return}
  const show=(event,index)=>{const row=rows[index];tooltip.innerHTML=`<b>${esc(row.name)}${row.significant?" · 差异显著":""}</b><br>文章标题：${esc(formatRate(row.article_rate,1))}（${row.article_hits||0}/${row.article_total||0}）<br>视频标题：${esc(formatRate(row.video_rate,1))}（${row.video_hits||0}/${row.video_total||0}）<br>视频－文章：${esc(formatRate(row.video_minus_article,1))} · p=${esc(Number(row.p||0).toPrecision(2))}`;placeChartTooltip(tooltip,frame,event)};
  svg.onpointermove=event=>{const rect=svg.getBoundingClientRect(),localY=(event.clientY-rect.top)*H/Math.max(1,rect.height),index=Math.floor((localY-m.t)/Math.max(1,band));if(index>=0&&index<rows.length)show(event,index);else tooltip.classList.remove("is-visible")};
  svg.onpointerdown=svg.onpointermove;svg.onpointerleave=()=>tooltip.classList.remove("is-visible");
}
function renderOwnedAnalysisTable(rows){
  const body=$("ownedAnalysisTableRows");if(!body)return;
  body.innerHTML=rows.map(row=>`<tr>
    <td>${esc(row.date)}</td><td>${esc(row.refs||0)}条</td>
    <td>${esc(row.article_refs||0)}条</td><td>${esc(row.video_refs||0)}条</td>
    <td>${esc(formatRate(row.article_share,1))}</td><td>${esc(formatRate(row.video_share,1))}</td>
    <td>${esc(row.unique_urls||0)}个</td><td>${esc(formatRate(row.unique_article_share,1))}</td>
    <td>${esc(formatRate(row.unique_video_share,1))}</td><td>${esc(row.body_only_refs||0)}条 · ${esc(formatRate(row.body_only_share,1))}</td>
  </tr>`).join("")||'<tr><td colspan="10" class="empty">暂无逐日数据</td></tr>';
}
function renderAnalysisCharts(){
  _analysisRenderQueued=false;
  _ownedRenderQueued=false;
  if(activeView!=="daily")return;
  if(_brandAnalysisData&&!$("analysisBody")?.hidden){
    const rows=analysisRows(),pairs=correlationRows(rows),summary=correlationSummary(pairs);
    renderAnalysisSummary(rows,pairs,summary);
    renderAnalysisTable(rows);
    renderAnalysisSourceExamples();
    applyFilter();
  }
  if(_ownedAnalysisData&&!$("ownedAnalysisBody")?.hidden){
    const product=ownedProductObject(),rows=ownedAnalysisRows(product);
    if(!product)return;
    renderOwnedSummary(product,rows);renderOwnedMixChart(product);renderOwnedKeywordTrend(product);renderOwnedThemeChart(product);renderOwnedAnalysisTable(rows);
    if(product.is_category_baseline){
      setText("ownedMixChartMeta",`${rows.length}个观测日 · 文章/视频信源条数 ÷ 当天该品类全部信源条数`);
    }else{
      const trend=ownedMentionTrendRows(product);
      setText("ownedMixChartMeta",`${trend.rows.length}个观测日 · 品牌提及率=出现轮次/审核轮次 · 信源命中率=标题或可靠正文命中/全部信源`);
    }
  }
}
function scheduleAnalysisRender(){
  if(_analysisRenderQueued||_ownedRenderQueued)return;
  _analysisRenderQueued=true;_ownedRenderQueued=true;
  requestAnimationFrame(()=>requestAnimationFrame(renderAnalysisCharts));
  if(!_analysisResizeObserver&&window.ResizeObserver){_analysisResizeObserver=new ResizeObserver(()=>{if(activeView==="daily")scheduleAnalysisRender()});["trendChartFrame","correlationChartFrame"].forEach(id=>{const node=$(id);if(node)_analysisResizeObserver.observe(node)})}
  if(!_ownedResizeObserver&&window.ResizeObserver){_ownedResizeObserver=new ResizeObserver(()=>{if(activeView==="daily")scheduleAnalysisRender()});["ownedMixChartFrame","ownedKeywordTrendFrame","ownedThemeChartFrame"].forEach(id=>{const node=$(id);if(node)_ownedResizeObserver.observe(node)})}
}

function renderOwnedVideoCategoryShare(payload){
  const body=$("ownedVideoCategoryRows"),rows=[...(payload?.rows||[])];
  if(!body)return;
  rows.sort((a,b)=>(b.owned_video_link_share||0)-(a.owned_video_link_share||0)||String(a.question||"").localeCompare(String(b.question||""),"zh-CN"));
  const ownedLinks=rows.reduce((sum,row)=>sum+(+row.owned_video_unique_urls||0),0);
  const period=payload?.first_date&&payload?.last_date?`${payload.first_date} 至 ${payload.last_date}`:"全部观测期";
  setText("ownedVideoCategoryHint",`${period} · ${rows.length}个品类 · 共${ownedLinks}个品类内自有品牌视频链接 · 按唯一URL统计`);
  body.innerHTML=rows.map(row=>{
    const brands=(row.owned_brands||[]).join("、")||"—";
    return`<tr data-filter-text="${esc(`${row.question||""} ${brands}`)}">
      <td><b>${esc(row.question||"未知品类")}</b></td>
      <td>${esc(brands)}</td>
      <td>${esc(row.all_unique_urls||0)}个</td>
      <td>${esc(row.video_unique_urls||0)}个</td>
      <td><b>${esc(row.owned_video_unique_urls||0)}个</b><span class="daily-abs">${esc(row.owned_video_refs||0)}次引用</span></td>
      <td><span class="daily-pct">${esc(formatRate(row.owned_video_link_share,2))}</span><span class="daily-abs">${esc(row.owned_video_unique_urls||0)}/${esc(row.all_unique_urls||0)}</span></td>
      <td><span class="daily-pct">${esc(formatRate(row.owned_within_video_link_share,2))}</span><span class="daily-abs">${esc(row.owned_video_unique_urls||0)}/${esc(row.video_unique_urls||0)}</span></td>
    </tr>`;
  }).join("")||'<tr><td colspan="7" class="empty">当前设备范围暂无产品品类信源</td></tr>';
}

function renderHero(data){
  const question=data.selected_question||"全部问题";
  const device=(data.device_options||[]).find(item=>String(item.instance)===String(data.selected_device));
  const scope=data.selected_device==="all"?"全部设备":`实例 ${data.selected_device}${device?` · ${device.nickname||device.uid_masked}`:""}`;
  const topMedia=(data.by_media||[])[0]||{};
  const coverage=data.product_coverage||{};
  const daily=(data.daily_question_products||[])[0]||(data.daily_question_sources||[])[0]||{};
  const dates=daily.dates||[];
  const period=dates.length?`${dates[0]} 至 ${dates[dates.length-1]}`:"暂无按日数据";
  const latestExpected=+data.latest_expected_count||0,latestRefs=+data.latest_refs||0;
  setText("pageTitle",question==="全部问题"?"全部问题信源监控":question);
  setText("pageSubtitle",`${scope} · ${period} · ${data.total_runs||0} 总轮次 · ${coverage.source_runs||0} 信源轮次 · ${data.products?.total_product_runs||0} 有效产品轮次`);
  setText("heroQuestion",question);
  setText("heroQuestionNote",question==="全部问题"?`${scope} · 共 ${data.question_count||0} 个问题；策略结论应先切换到单个品类。`:`${scope} · 观测期 ${period}`);
  setText("heroStatus",`更新于 ${data.generated_at||"-"}`);
  setText("heroRefs",data.total_runs||0);
  setText("heroRefsNote",`信源 ${coverage.source_runs||0} 轮 · 商品审核有效样本 ${data.products?.total_product_runs||0} 轮`);
  setText("heroTopMedia",topMedia.name?pctOrDash(topMedia.count,data.total_refs,1):"-");
  setText("heroTopMediaNote",topMedia.name?`${topMedia.name} · ${topMedia.count}/${data.total_refs} 条引用`:"暂无主导信源");
  setText("heroLatest",latestExpected?pctOrDash(latestRefs,latestExpected,0):"-");
  const latestAccount=data.latest_account_uid_masked||data.latest_account_nickname||"-";
  setText("heroLatestNote",`第 ${data.latest_run_no||"-"} 轮 · ${latestRefs}/${latestExpected||"?"} 条 · 账号 ${latestAccount} · ${data.latest_captured_at||data.latest_run_time||"-"}`);
}

function renderDeviceSwitch(data){
  const host=$("deviceSwitch"),options=data.device_options||[],overview=data.device_overview||{};
  if(!host)return;
  const allActive=data.selected_device==="all";
  const buttons=[`<button type="button" class="device-switch-btn ${allActive?"active":""}" data-device="all">
    <strong>全部设备</strong>
    <span>${overview.active_device_count||0} 台有数据 · 合并总览与每设备平均值</span>
  </button>`];
  options.forEach(item=>{
    const active=String(data.selected_device)===String(item.instance);
    buttons.push(`<button type="button" class="device-switch-btn ${active?"active":""}" data-device="${esc(item.instance)}">
      <strong>实例 ${esc(item.instance)} · ${esc(item.nickname||item.uid_masked||"未命名账号")}</strong>
      <span>${esc(item.uid_masked||"未知 UID")} · 当前范围 ${item.scope_run_count||0} 轮 / ${item.scope_reference_count||0} 引用</span>
    </button>`);
  });
  host.innerHTML=buttons.join("");
}

function renderDeviceOverview(data){
  const host=$("deviceOverview"),d=data.device_overview||{},all=data.selected_device==="all";
  if(!host)return;
  if(all){
    host.innerHTML=`
      <div class="device-overview-item"><div class="device-overview-label">当前有数据设备</div><div class="device-overview-value">${esc(d.active_device_count||0)} 台</div><div class="device-overview-note">可点击左侧设备查看明细</div></div>
      <div class="device-overview-item"><div class="device-overview-label">平均回答轮次/设备</div><div class="device-overview-value">${esc(d.average_runs_per_device||0)}</div><div class="device-overview-note">仅统计带实例标识的数据</div></div>
      <div class="device-overview-item"><div class="device-overview-label">平均引用/设备</div><div class="device-overview-value">${esc(d.average_references_per_device||0)}</div><div class="device-overview-note">设备引用总量的算术平均</div></div>
      <div class="device-overview-item"><div class="device-overview-label">平均产品提及/设备</div><div class="device-overview-value">${esc(d.average_product_mentions_per_device||0)}</div><div class="device-overview-note">${d.unassigned_run_count?`另有 ${esc(d.unassigned_run_count)} 轮历史数据无实例标识`:"全部新数据均有设备标识"}</div></div>`;
  }else{
    const item=(data.device_options||[]).find(x=>String(x.instance)===String(data.selected_device))||{};
    host.innerHTML=`
      <div class="device-overview-item"><div class="device-overview-label">当前设备</div><div class="device-overview-value">实例 ${esc(data.selected_device)}</div><div class="device-overview-note">${esc(item.nickname||item.uid_masked||"")}</div></div>
      <div class="device-overview-item"><div class="device-overview-label">回答轮次</div><div class="device-overview-value">${esc(data.total_runs||0)}</div><div class="device-overview-note">当前问题与设备范围</div></div>
      <div class="device-overview-item"><div class="device-overview-label">引用总数</div><div class="device-overview-value">${esc(data.total_refs||0)}</div><div class="device-overview-note">${esc(data.unique_links||0)} 条去重链接</div></div>
      <div class="device-overview-item"><div class="device-overview-label">产品提及</div><div class="device-overview-value">${esc(data.products?.total_mentions||0)}</div><div class="device-overview-note">${esc(data.products?.total_product_runs||0)} 个有效产品轮次</div></div>`;
  }
}

function renderAccountSummaries(data){
  const rows=data.account_summaries||[],body=$("accountSummaryRows");
  if(!body)return;
  const all=data.selected_device==="all";
  setText("accountSummaryHint",all?`${rows.length} 个账号 · 当前为全部设备合并统计；点击实例可切换到单设备。`:`当前仅显示实例 ${data.selected_device}；点击左侧“全部设备”返回合并与平均视图。`);
  body.innerHTML=rows.map(item=>`<tr data-filter-text="${esc(`${item.nickname||""} ${item.uid_masked||""} ${(item.instances||[]).join(" ")}`)}">
    <td data-label="账号"><b>${esc(item.nickname||"未设置昵称")}</b><br><span class="daily-abs">${esc(item.uid_masked||"未知 UID")}</span></td>
    <td data-label="历史实例">${(item.instances||[]).length?(item.instances||[]).map(value=>`<button type="button" class="device-row-action" data-device-action="${esc(value)}">实例 ${esc(value)}</button>`).join("、"):"网页直采"}</td>
    <td data-label="采集轮次">${esc(item.run_count||0)}</td>
    <td data-label="问题数">${esc(item.question_count||0)}</td>
    <td data-label="信源引用">${esc(item.reference_count||0)}</td>
    <td data-label="最近采集">${esc(item.latest_at||"—")}</td>
  </tr>`).join("")||'<tr><td colspan="6" class="empty">当前设备与问题组合暂无数据</td></tr>';
}

function coverageStats(data){
  const c=data.product_coverage||{},skips=data.capture_skips||{};
  const count=name=>(c[name]||[]).length;
  const sourceRuns=+c.source_runs||0,withProducts=+c.with_products||0,verified=count("verified_no_products");
  const reviewed=withProducts+verified;
  return {
    sourceRuns,withProducts,verified,reviewed,
    pending:count("ai_pending"),mismatch:count("capture_mismatch"),missing:count("answer_not_archived"),legacy:count("legacy_not_archived"),
    pendingSave:+skips.pending_save_count||0,blank:+skips.active_count||0
  };
}
function signalHtml(tone,mark,title,evidence,view,action){
  return`<div class="strategy-signal ${tone||""}"><span class="signal-mark" aria-hidden="true">${esc(mark)}</span><div><div class="signal-title">${esc(title)}</div><div class="signal-evidence">${esc(evidence)}</div></div>${view?`<button type="button" class="signal-action" data-go-view="${esc(view)}">${esc(action||"查看")}</button>`:""}</div>`
}
function renderDecisionDashboard(data){
  const q=coverageStats(data),latestExpected=+data.latest_expected_count||0,latestRefs=+data.latest_refs||0;
  const blocking=q.pending+q.mismatch+q.missing+q.pendingSave;
  const unknown=Math.max(q.legacy,Math.max(0,q.sourceRuns-q.reviewed));
  let stateClass="ok",stateText="可用于趋势判断",stateNote="当前无待复核、正文错配或正文缺失；仍需结合样本量和95%区间判断波动。";
  if(blocking){stateClass="danger";stateText="先修复数据缺口";stateNote=`存在 ${blocking} 项待处理数据；缺口解决前，排名和趋势均应视为暂定。`}
  else if(unknown){stateClass="warning";stateText="存在历史未知";stateNote=`${unknown} 轮未形成可核验商品结果，显示为未知，不计作0次推荐。`}
  else if(latestExpected&&!truthy(data.latest_complete)&&latestRefs<latestExpected){stateClass="warning";stateText="最新轮次未完成";stateNote=`最新轮仅归档 ${latestRefs}/${latestExpected} 条信源，暂不用于稳定趋势结论。`}
  const status=$("decisionStatus");if(status){status.className=`status-label ${stateClass}`;status.textContent=stateText}
  setText("decisionStateNote",stateNote);
  const metrics=$("decisionMetrics");if(metrics)metrics.innerHTML=`
    <div class="decision-metric"><span>信源轮次</span><b>${esc(q.sourceRuns)}</b><small>至少归档1条引用</small></div>
    <div class="decision-metric"><span>AI审核覆盖</span><b>${esc(pctOrDash(q.reviewed,q.sourceRuns,1))}</b><small>${esc(q.reviewed)}/${esc(q.sourceRuns)}轮</small></div>
    <div class="decision-metric"><span>有效产品样本</span><b>${esc(data.products?.total_product_runs||0)}</b><small>品牌榜统一分母</small></div>
    <div class="decision-metric"><span>历史未知</span><b>${esc(q.legacy)}</b><small>不按0次处理</small></div>`;

  const signals=[];
  if(blocking||unknown){
    signals.push(signalHtml(blocking?"danger":"warning","!",blocking?"数据缺口会影响排名":"历史结果不可核验",blocking?`待复核${q.pending}轮、错配${q.mismatch}轮、正文缺失${q.missing}轮、待补写${q.pendingSave}次。`:`历史正文未归档${q.legacy}轮；应与有效产品样本分开看。`,"support","查看审计"));
  }else{
    signals.push(signalHtml("","✓","当前审核链路完整",`AI审核 ${q.reviewed}/${q.sourceRuns} 轮；未发现待复核、错配和正文缺失。`,"support","查看口径"));
  }

  const dailyProduct=(data.daily_question_products||[]).find(x=>x.question===data.selected_question)||(data.selected_question!=="全部问题"?(data.daily_question_products||[])[0]:null);
  if(dailyProduct&&(dailyProduct.dates||[]).length>=2){
    const dates=dailyProduct.dates||[],runs=dailyProduct.runs_by_date||[],last=dates.length-1,prev=last-1;
    const candidates=(dailyProduct.brand_rows||[]).filter(r=>(runs[last]||0)&&(runs[prev]||0)&&(r.counts||[]).length>last);
    candidates.sort((a,b)=>Math.abs(+b.pct_delta||0)-Math.abs(+a.pct_delta||0));
    const r=candidates[0];
    if(r){
      const cNow=+r.counts[last]||0,cPrev=+r.counts[prev]||0,nNow=+runs[last]||0,nPrev=+runs[prev]||0;
      const nowRate=cNow/nNow,prevRate=cPrev/nPrev,diff=(nowRate-prevRate)*100;
      const overlap=intervalsOverlap(wilsonInterval(cNow,nNow),wilsonInterval(cPrev,nPrev));
      const dir=diff>=0?"上升":"下降",mark=diff>=0?"↑":"↓";
      signals.push(signalHtml(overlap?"warning":"",mark,`${r.name} ${dir} ${Math.abs(diff).toFixed(1)}pct`,`${dates[prev]}：${cPrev}/${nPrev}（95%CI ${ciText(cPrev,nPrev)}）；${dates[last]}：${cNow}/${nNow}（95%CI ${ciText(cNow,nNow)}）。${overlap?"区间重叠，先观察。":"区间未重叠，优先排查信源变化。"}`,"daily","查看趋势"));
    }
  }else if(data.selected_question==="全部问题"){
    signals.push(signalHtml("warning","→","先选择单个品类",`全局汇总包含 ${data.question_count||0} 个问题，品牌率和排名不应跨品类直接比较。`,"product","选择品类"));
  }

  const top=(data.by_media||[])[0];
  if(top)signals.push(signalHtml("","S",`Top1信源占 ${pctOrDash(top.count,data.total_refs,1)}`,`${top.name} 贡献 ${top.count}/${data.total_refs} 条引用；该比例反映引用结构，不代表内容权威性。`,"question","查看信源"));

  const dailySource=(data.daily_question_sources||[]).find(x=>x.question===data.selected_question)||(data.selected_question!=="全部问题"?(data.daily_question_sources||[])[0]:null);
  if(dailySource&&(dailySource.refs_by_date||[]).length>=2){
    const refs=dailySource.refs_by_date,last=refs.length-1,prev=last-1;
    const moved=(dailySource.media_rows||[]).map(r=>{const cs=r.counts||[];return{r,delta:(+cs[last]||0)/Math.max(1,+refs[last]||0)-(+cs[prev]||0)/Math.max(1,+refs[prev]||0)}}).sort((a,b)=>Math.abs(b.delta)-Math.abs(a.delta))[0];
    if(moved&&Math.abs(moved.delta)>=.001)signals.push(signalHtml("",moved.delta>=0?"↗":"↘",`${moved.r.name} 引用份额${moved.delta>=0?"上升":"下降"}`,`前一观测日到最新观测日变化 ${(moved.delta*100).toFixed(1)}pct；应结合热门链接新增/流失定位原因。`,"daily","查看异常日"));
  }
  const root=$("strategySignals");if(root){root.innerHTML=signals.slice(0,4).join("");root.querySelectorAll("[data-go-view]").forEach(btn=>btn.addEventListener("click",()=>{setView(btn.dataset.goView);window.scrollTo(0,0)}))}
}

function renderQuestionRank(data){
  const root=$("questionRankList");if(!root)return;
  const qs=data.questions||[];
  if(!qs.length){root.innerHTML='<div class="empty">暂无问题数据</div>';return}
  root.innerHTML=qs.slice(0,12).map((q,i)=>`
    <button type="button" class="question-rank-item ${q.question===data.selected_question?"active":""}" data-question="${esc(q.question)}" data-filter-text="${esc(q.question)}">
      <div class="rank-no">${i+1}</div>
      <div>
        <div class="rank-title" title="${esc(q.question)}">${esc(q.question)}</div>
        <div class="rank-meta">${esc(q.runs||0)} 轮 · ${esc(q.unique_links||0)} 去重链接</div>
      </div>
      <div class="rank-num">${esc(q.refs||0)}<small>引用</small></div>
    </button>`).join("");
  setText("questionRankHint",`共 ${qs.length} 个问题，点击可切换`);
  root.querySelectorAll(".question-rank-item").forEach(el=>el.addEventListener("click",()=>{
    selectedQuestion=el.getAttribute("data-question")||"全部问题";
    root.querySelectorAll(".question-rank-item").forEach(x=>x.classList.remove("active"));
    el.classList.add("active");
    const sel=$("questionSelect");if(sel)sel.value=selectedQuestion;
    refresh(true);
  }));
}

function renderInsights(data){
  const root=$("insightList");if(!root)return;
  const media=data.by_media||[],types=data.by_type||[];
  const products=data.products||{};
  const topMedia=media[0],secondMedia=media[1],topType=types[0];
  const topProduct=(products.by_brand||products.by_product||[])[0];
  const rows=[];
  if(topMedia)rows.push(["主导媒体 / 平台",`${topMedia.name} · ${pct(topMedia.count,data.total_refs)} (${topMedia.count}/${data.total_refs})`]);
  if(secondMedia)rows.push(["第二信源",`${secondMedia.name} · ${pct(secondMedia.count,data.total_refs)} (${secondMedia.count}/${data.total_refs})`]);
  if(topType)rows.push(["主导信源类型",`${topType.name} · ${pct(topType.count,data.total_refs)} (${topType.count}/${data.total_refs})`]);
  if(topProduct)rows.push(["最高频推荐品牌",`${topProduct.name} · ${topProduct.run_count||0}/${products.total_product_runs||0}轮 · ${pctOrDash(topProduct.run_count,products.total_product_runs,1)}`]);
  rows.push(["品牌 / 产品覆盖",`${products.total_mentions||0} 个推荐条目（每轮可多个），${products.unique_brands||0} 个品牌，${products.unique_products||0} 个产品变体`]);
  rows.push(["最新一轮状态",`${data.latest_refs||0}/${data.latest_expected_count||"?"} 条，${data.latest_complete==="True"||data.latest_complete===true?"完整":"待确认"}`]);
  rows.push(["数据范围",`${data.total_runs||0} 轮，${data.domain_total||0} 个域名，${data.media_total||0} 个媒体/平台`]);
  root.innerHTML=rows.map(([k,v])=>`<div class="insight-row"><b>${esc(k)}</b><span>${esc(v)}</span></div>`).join("");
}

function renderQuestionSources(items){
  const root=$("questionSourceCards");if(!root)return;
  if(!items?.length){root.innerHTML='<div class="empty">暂无数据</div>';return}
  root.innerHTML=items.map(it=>{
    const filter=[it.question,...(it.by_type||[]).map(r=>r.name),...(it.by_media||[]).map(r=>r.name),...(it.by_domain||[]).map(r=>r.name)].join(" ");
    return`<div class="q-card" data-filter-text="${esc(filter)}" style="margin-bottom:12px">
      <div class="q-card-head"><span class="q-card-title">${esc(it.question)}</span><span class="q-card-stats">${esc(it.refs)}条 / ${esc(it.unique_links)}去重 / ${esc(it.runs)}轮 / ${esc(it.media_total)}媒体</span></div>
      <div class="q-card-body">
        <div><div class="card-hint" style="margin-bottom:6px">信源类型</div>${barsHtml(it.by_type||[],it.refs||0)}</div>
        <div><div class="card-hint" style="margin-bottom:6px">媒体 / 平台</div>${barsHtml(it.by_media||[],it.refs||0)}</div>
      </div>
    </div>`;
  }).join("");
  $("questionSourceHint").textContent=`共 ${items.length} 个问题`;
}

function coveragePanelHtml(data,keyPrefix){
  const c=data.product_coverage||{},skips=data.capture_skips||{},q=coverageStats(data);
  const runNos=items=>(items||[]).map(x=>x.run_no).filter(Boolean).join("、")||"无";
  const skipNos=items=>(items||[]).map(x=>`${x.skip_no||"-"}（${x.question||x.chat_title||"未知问题"}）`).join("、")||"无";
  const reviewed=q.reviewed,unresolved=Math.max(0,q.sourceRuns-reviewed);
  const chip=(label,count,tone)=>`<span class="quality-chip ${tone||""}">${esc(label)} ${esc(count)}</span>`;
  return`<div class="audit-funnel" aria-label="信源到商品审核的覆盖漏斗">
    <div class="audit-step"><span>信源已归档</span><b>${esc(q.sourceRuns)}</b><small>至少1条ref</small></div>
    <div class="audit-step"><span>AI已审核</span><b>${esc(reviewed)}</b><small>${esc(reviewed)}/${esc(q.sourceRuns)}轮</small></div>
    <div class="audit-step"><span>产出推荐</span><b>${esc(q.withProducts)}</b><small>进入品牌榜</small></div>
    <div class="audit-step"><span>确认无推荐</span><b>${esc(q.verified)}</b><small>不是漏统计</small></div>
  </div>
  <div class="quality-chips">
    ${chip("AI待复核",q.pending,q.pending?"warning":"")}
    ${chip("正文错配",q.mismatch,q.mismatch?"danger":"")}
    ${chip("正文缺失",q.missing,q.missing?"danger":"")}
    ${chip("历史未知",q.legacy,q.legacy?"warning":"")}
    ${chip("等待补写",q.pendingSave,q.pendingSave?"warning":"")}
    ${chip("页面空白",q.blank,q.blank?"danger":"")}
  </div>
  <div class="card-hint">审核覆盖 ${esc(pctOrDash(reviewed,q.sourceRuns,1))}（${esc(reviewed)}/${esc(q.sourceRuns)}轮）；未形成可核验审核结果 ${esc(unresolved)} 轮。历史未知不会按0次推荐计入。</div>
  <details class="audit-details" data-detail-key="${esc(keyPrefix)}-audit-runs">
    <summary>展开异常轮次与处理状态</summary>
    <div class="audit-groups">
      <div class="audit-group"><b>AI待复核</b>${esc(runNos(c.ai_pending))}</div>
      <div class="audit-group"><b>正文错配</b>${esc(runNos(c.capture_mismatch))}</div>
      <div class="audit-group"><b>正文缺失</b>${esc(runNos(c.answer_not_archived))}</div>
      <div class="audit-group"><b>历史正文未归档</b>${esc(runNos(c.legacy_not_archived))}</div>
      <div class="audit-group"><b>内容已抓取等待补写</b>${esc(skipNos(skips.pending_save_items))}</div>
      <div class="audit-group"><b>页面确实空白已跳过</b>${esc(skipNos(skips.items))}</div>
    </div>
  </details>`;
}

function renderProducts(data){
  const p=data.products||{};
  const coverage=data.product_coverage||{};
  const captureSkips=data.capture_skips||{};
  setText("productMentions",p.total_mentions||0);
  setText("productUnique",p.unique_brands||p.unique_products||0);
  setText("productLatestCount",(p.latest_products||[]).length);
  setText("productHint",`产品表：${data.products_csv?.exists?data.products_csv.mtime:"暂未生成"}`);
  setText("productRunCount",data.today_runs||0);
  setText("productRunLabel",`当日运行轮次${data.today_run_date?"（"+data.today_run_date+"）":""}`);
  const coverageRoot=$("productCoverage");
  if(coverageRoot){
    coverageRoot.innerHTML=coveragePanelHtml(data,"product");
    restoreDynamicDetails(coverageRoot);
  }
  const auditRoot=$("auditSummary");
  if(auditRoot){auditRoot.innerHTML=coveragePanelHtml(data,"support");restoreDynamicDetails(auditRoot)}
  const productBars=$("productBars");
  if(productBars){
    const isAll=(data.selected_question||"全部问题")==="全部问题";
    const byQuestion=p.by_question||[];
    if(isAll && byQuestion.length){
      productBars.innerHTML=byQuestion.map(q=>`
        <div class="mini-rank-card" data-filter-text="${esc(q.question)} ${(q.top_brands||[]).map(x=>esc(x.name)).join(" ")}" style="margin-bottom:14px">
          <div class="mini-rank-head">
            <span class="mini-rank-title">${esc(q.question)}</span>
            <span class="card-hint">当日采集 ${esc(q.today_runs||0)} 轮${q.today_date?"（"+esc(q.today_date)+"）":""} · 聚合分母 ${esc(q.runs)} 个有效产品轮 · ${esc(q.mentions)}个推荐条目</span>
          </div>
          ${productBarsHtml(q.top_brands||[],q.mentions||0,q.runs||0)}
        </div>`).join("");
    }else{
      productBars.innerHTML=productBarsHtml(
        p.by_brand||p.by_product||[], p.total_mentions||0, p.total_product_runs||0
      );
    }
  }

  const latest=$("latestProducts");
  const latestRows=p.latest_products||[];
  if(latest){
    latest.innerHTML=latestRows.length?`<div class="latest-product-list">${latestRows.map(item=>`
      <div class="latest-product-card" data-filter-text="${esc(item.product_name)} ${esc(item.brand_name)} ${esc(item.evidence)}">
        <div class="latest-product-rank">${esc(item.product_index||"-")}</div>
        <div class="latest-product-main">
          <div class="latest-product-name">${esc(item.product_name)}</div>
          <div class="latest-product-brand">${esc(item.brand_name||"品牌缺失 / 待确认")} · ${["brand_lexicon_match","historical_brand_match"].includes(item.extraction_method)?"词库匹配":(item.review_status==="rule_unverified"?"规则分析":"AI 已审核")}</div>
        </div>
        <div class="latest-product-evidence" title="${esc(item.evidence)}">${esc(item.evidence||"")}</div>
      </div>`).join("")}</div>`:'<div class="empty">暂无产品数据。新跑一轮后会自动生成。</div>';
  }

  const root=$("questionProductCards");
  if(!root)return;
  const byQuestion=p.by_question||[];
  if(!byQuestion.length){root.innerHTML='<div class="empty">暂无产品数据</div>';return}
  root.innerHTML=byQuestion.map(q=>`
    <div class="q-card" data-filter-text="${esc(q.question)} ${(q.top_brands||[]).map(x=>esc(x.name)).join(" ")} ${(q.top_products||[]).map(x=>esc(x.name)).join(" ")}" style="margin-bottom:12px">
      <div class="q-card-head">
        <span class="q-card-title">${esc(q.question)}</span>
        <span class="q-card-stats">当日采集 ${esc(q.today_runs||0)} 轮${q.today_date?"（"+esc(q.today_date)+"）":""} / 聚合分母 ${esc(q.runs)}个有效产品轮 / ${esc(q.mentions)}个推荐条目 / ${esc((q.top_brands||[]).length)}个品牌 / ${esc(q.unique_products)}个产品</span>
      </div>
      <div class="product-pill-list">
        ${(q.top_brands||q.top_products||[]).map(productPillHtml).join("")}
      </div>
      <details data-detail-key="product-variants:${esc(q.question)}" style="margin-top:10px">
        <summary class="card-hint">查看产品变体 / 规格写法</summary>
        <div class="product-pill-list" style="margin-top:8px">
          ${(q.top_products||[]).map(productPillHtml).join("")}
        </div>
      </details>
    </div>`).join("");
  restoreDynamicDetails(root);
}

function renderDaily(items){
  const root=$("dailySourceCards");if(!root)return;
  if(!items?.length){root.innerHTML='<div class="empty">暂无每日数据</div>';return}
  root.innerHTML=items.map(it=>{
    const dates=it.dates||[],n=dates.length;
    const dateHeaders=dates.map((d,i)=>`<th scope="col"${i===n-1?' class="today-col"':''}>${esc(d)}</th>`).join("");
    const refsBy=it.refs_by_date||[],runsBy=it.runs_by_date||[];
    const totalRow=`
      <tr class="total-row"><td>合计条数</td>${refsBy.map(c=>`<td>${esc(c)}</td>`).join("")}<td>${deltaHtml((refsBy[n-1]||0)-(refsBy[n-2]||0))}</td></tr>
      <tr class="total-row"><td>运行轮次</td>${runsBy.map(c=>`<td>${esc(c)}</td>`).join("")}<td>${deltaHtml((runsBy[n-1]||0)-(runsBy[n-2]||0))}</td></tr>`;
    const mediaRows=(it.media_rows||[]).map(r=>{
      const counts=r.counts||[];
      const trend=counts.map(c=>{const p=(+c||0)/Math.max(1,refsBy[counts.indexOf(c)]||1)*100;return Math.round(p*2)});
      const maxT=Math.max(1,...trend);
      return`<tr data-filter-text="${esc(it.question)} ${esc(r.name)}">
        <td title="${esc(r.name)}">${esc(r.name)}</td>
        ${counts.map((c,i)=>`<td${i===n-1?' class="today-col"':''}><span class="daily-pct">${esc(pct(c,refsBy[i]||0))}</span><span class="daily-abs">${esc(c)}/${esc(refsBy[i]||0)}</span></td>`).join("")}
        <td>${deltaPctHtml(counts,refsBy)}<span style="font-size:10px;color:var(--muted);display:block">${deltaHtml(r.delta||0)}</span></td>
      </tr>`;
    }).join("");

    const topLinks=it.top_links_by_date||{};
    const topHtml=dates.filter(d=>topLinks[d]?.length).map(d=>{
      const links=topLinks[d]||[],vid=links.filter(l=>l.type==="视频"),art=links.filter(l=>l.type==="文章");
      function tagList(list,cls){return list.map(l=>{
        const products=l.own_products||[],brands=l.own_brands||[];
        const owned=products.length>0||brands.length>0;
        const labels=products.length?products:brands;
        const scope=products.length?(l.own_match_scope||"标题/正文"):(l.own_brand_match_scope||"标题/正文");
        const ownTitle=owned?` · 自有品牌：${labels.join("、")} · 命中位置：${scope}`:"";
        return`<a class="top-link-tag ${cls}${owned?" own-source":""}" href="${esc(l.href)}" target="_blank" title="${esc((l.title||l.href)+ownTitle)}"><span class="tag-count">${esc(l.count)}次</span>${owned?`<span class="own-content-mark">自有品牌 · ${esc(labels.join("、"))}<em class="own-match-scope">${esc(scope)}</em></span>`:""} ${esc(l.title||l.href)}</a>`;
      }).join("")}
      return`<div class="top-links-row"><span class="top-links-day-label">${esc(d)}</span>${vid.length?`<span class="type-badge type-badge-video">视频</span>`:""}${tagList(vid,"tag-video")}${art.length?`<span class="type-badge type-badge-article">文章</span>`:""}${tagList(art,"tag-article")}</div>`;
    }).join("");

    const filter=[it.question,...(it.media_rows||[]).map(r=>r.name)].join(" ");
    return`<div class="daily-card" data-filter-text="${esc(filter)}" style="margin-bottom:14px">
      <div class="daily-card-head"><span class="q-card-title">${esc(it.question)}</span><span class="card-hint">变化=最新观测日−前一观测日；默认比较引用份额</span></div>
      <div class="scroll-hint">表格可左右滑动；首列与变化列保持可见</div>
      <div class="daily-table-wrap" data-scroll-key="daily-source:${esc(it.question)}" tabindex="0" role="region" aria-label="${esc(it.question)}每日信源变化表">
        <table class="daily-table">
          <caption class="sr-only">${esc(it.question)}每日信源引用份额与变化</caption>
          <thead><tr><th scope="col">媒体 / 平台</th>${dateHeaders}<th scope="col">变化</th></tr></thead>
          <tbody>${totalRow}${mediaRows}</tbody>
        </table>
      </div>
      ${topHtml?`<div class="top-links"><div class="top-links-title">热门链接 Top 10</div>${topHtml}</div>`:""}
    </div>`;
  }).join("");
  $("dailySourceHint").textContent=`共 ${items.length} 个问题，按天对比`;
}

function renderDailyProducts(items){
  const root=$("dailyProductCards");if(!root)return;
  if(!items?.length){root.innerHTML='<div class="empty">暂无每日产品数据</div>';setText("dailyProductHint","暂无");return}
  function tableHtml(title, rows, dates, totalsBy, limitLabel,scrollKey){
    const n=dates.length;
    const dateHeaders=dates.map((d,i)=>`<th scope="col"${i===n-1?' class="today-col"':''}>${esc(d)}</th>`).join("");
    const rankMovement=r=>{
      const latest=r.latest_rank,previous=r.previous_rank;
      if(!latest)return "未上榜";
      if(!previous)return `新上榜 #${latest}`;
      const delta=previous-latest;
      if(delta>0)return `↑${delta}（#${previous}→#${latest}）`;
      if(delta<0)return `↓${Math.abs(delta)}（#${previous}→#${latest}）`;
      return `—（#${latest}）`;
    };
    const body=(rows||[]).map(r=>{
      const counts=r.counts||[];
      const ranks=r.ranks||[];
      return`<tr data-filter-text="${esc(r.name)}">
        <td title="${esc(r.name)}">${esc(r.name)}</td>
        ${counts.map((c,i)=>`<td${i===n-1?' class="today-col"':''}><span class="daily-pct">${esc(pctOrDash(c,totalsBy[i]||0,2))}</span><span class="daily-abs">#${esc(ranks[i]||"-")} · ${esc(c)}/${esc(totalsBy[i]||0)}</span></td>`).join("")}
        <td><span class="${(r.pct_delta||0)>=0?'delta-pos':'delta-neg'}">${(r.pct_delta||0)>=0?'+':''}${esc(r.pct_delta||0)}pct</span><span style="font-size:10px;color:var(--muted);display:block">${esc(rankMovement(r))}</span><span style="font-size:10px;color:var(--muted);display:block">${deltaHtml(r.delta||0)}</span></td>
      </tr>`;
    }).join("");
    return`<div class="daily-subtitle">${esc(title)} <span class="card-hint">${esc(limitLabel||"")}</span></div>
      <div class="scroll-hint">表格可左右滑动；比较时注意每日样本量不同</div>
      <div class="daily-table-wrap" data-scroll-key="${esc(scrollKey||title)}" tabindex="0" role="region" aria-label="${esc(title)}">
        <table class="daily-table">
          <caption class="sr-only">${esc(title)}，出现率、排名和前一观测日变化</caption>
          <thead><tr><th scope="col">品牌 / 产品</th>${dateHeaders}<th scope="col">变化</th></tr></thead>
          <tbody>${body||'<tr><td colspan="'+(dates.length+2)+'" class="empty">暂无</td></tr>'}</tbody>
        </table>
      </div>`;
  }
  root.innerHTML=items.map(it=>{
    const dates=it.dates||[],n=dates.length;
    const totals=it.mentions_by_date||[],runs=it.runs_by_date||[];
    const dateHeaders=dates.map((d,i)=>`<th scope="col"${i===n-1?' class="today-col"':''}>${esc(d)}</th>`).join("");
    const totalRows=`
      <tr class="total-row"><td>推荐条目数（可大于轮次）</td>${totals.map(c=>`<td>${esc(c)}</td>`).join("")}<td>${deltaHtml((totals[n-1]||0)-(totals[n-2]||0))}</td></tr>
      <tr class="total-row"><td>运行轮次</td>${runs.map(c=>`<td>${esc(c)}</td>`).join("")}<td>${deltaHtml((runs[n-1]||0)-(runs[n-2]||0))}</td></tr>`;
    const filter=[it.question,...(it.brand_rows||[]).map(r=>r.name),...(it.product_rows||[]).map(r=>r.name)].join(" ");
    return`<div class="daily-card" data-filter-text="${esc(filter)}" style="margin-bottom:14px">
      <div class="daily-card-head"><span class="q-card-title">${esc(it.question)}</span><span class="card-hint">品牌率分母=当日归档答案轮次；变化=前一观测日对比</span></div>
      <div class="scroll-hint">表格可左右滑动；运行轮次是每日比例的样本量</div>
      <div class="daily-table-wrap" data-scroll-key="daily-product-summary:${esc(it.question)}" tabindex="0" role="region" aria-label="${esc(it.question)}每日产品汇总">
        <table class="daily-table">
          <caption class="sr-only">${esc(it.question)}每日推荐条目数与运行轮次</caption>
          <thead><tr><th scope="col">汇总</th>${dateHeaders}<th scope="col">变化</th></tr></thead>
          <tbody>${totalRows}</tbody>
        </table>
      </div>
      ${tableHtml("品牌每日变化",it.brand_rows||[],dates,runs,"按出现轮次排序；出现率=出现轮次/当日归档答案轮次",`daily-brands:${it.question}`)}
      <details data-detail-key="daily-products:${esc(it.question)}" style="margin-top:10px">
        <summary class="card-hint">展开具体产品 / 规格每日变化</summary>
        ${tableHtml("产品每日变化",it.product_rows||[],dates,runs,"前80个；出现率=出现轮次/当日归档答案轮次",`daily-products:${it.question}`)}
      </details>
    </div>`;
  }).join("");
  restoreDynamicDetails(root);
  setText("dailyProductHint",`共 ${items.length} 个问题，按天对比产品推荐`);
}

function renderLatest(items){
  if(!items.length){$("latestRows").innerHTML='<tr><td colspan="6" class="empty">暂无</td></tr>';return}
  $("latestRows").innerHTML=items.map(it=>{
    const t=String(it.source_type||"");
    const cls=t.includes("视频")?"video":(t.includes("商品")?"product":(t.includes("文章")?"article":""));
    const products=it.own_products||[],brands=it.own_brands||[];
    const owned=products.length>0||brands.length>0;
    const labels=products.length?products:brands;
    const scope=products.length?(it.own_match_scope||"标题/正文"):(it.own_brand_match_scope||"标题/正文");
    return`<tr class="${owned?"own-source-row":""}" data-filter-text="${esc(it.title)} ${esc(it.source_type)} ${esc(it.media)} ${esc(it.domain)} ${esc(it.href)} ${esc(labels.join(" "))}">
      <td data-label="序号">${esc(it.index)}</td><td data-label="标题" title="${esc(it.title)}">${owned?`<span class="own-content-mark">自有品牌 · ${esc(labels.join("、"))}</span><br>`:""}${esc(it.title)}${owned?`<span class="daily-abs"> · ${esc(scope)}命中</span>`:""}</td><td data-label="类型"><span class="source-badge ${cls}">${esc(it.source_type)}</span></td><td data-label="媒体">${esc(it.media)}</td><td data-label="域名">${esc(it.domain)}</td><td data-label="链接"><a href="${esc(it.href)}" target="_blank" aria-label="打开信源：${esc(it.title)}">打开</a></td>
    </tr>`;
  }).join("");
}

function renderQuestions(data){
  const sel=$("questionSelect");if(!sel)return;
  const cur=data.selected_question||"全部问题",qs=data.questions||[];
  const allRefs=qs.reduce((s,q)=>s+(q.refs||0),0),allUnique=qs.reduce((s,q)=>s+(q.unique_links||0),0),allRuns=qs.reduce((s,q)=>s+(q.runs||0),0),allSourceRuns=qs.reduce((s,q)=>s+(q.source_runs||0),0);
  const opts=[{question:"全部问题",refs:allRefs||data.total_refs||0,unique_links:allUnique||data.unique_links||0,runs:allRuns||data.total_runs||0,source_runs:allSourceRuns},...qs];
  sel.innerHTML=opts.map(q=>`<option value="${esc(q.question)}">${esc(q.question)} (${q.refs||0}条引用/${q.runs||0}回答轮/${q.source_runs||0}信源轮)</option>`).join("");
  sel.value=selectedQuestion||cur;if(!sel.value)sel.value=cur;
  $("questionHint")&&($("questionHint").textContent=`${data.question_count||0}个问题`);
}

function paintData(d,force){
  const ch=force||hasDataChanged(d);
  const ui=ch?captureUiState():null;
  renderQuestions(d);
  renderDeviceSwitch(d);
  renderHero(d);
  renderDeviceOverview(d);
  renderAccountSummaries(d);
  renderDecisionDashboard(d);
  renderQuestionRank(d);
  renderInsights(d);
  $("status").textContent="在线 · "+d.generated_at;
  $("fileInfo").textContent=d.csv.exists?"CSV: "+d.csv.mtime:"CSV 未生成";
  if(ch){
    $("totalRuns").textContent=d.total_runs;
    $("totalRefs").textContent=d.total_refs;
    $("uniqueLinks").textContent=d.unique_links;
    $("latestRun").textContent=d.latest_run_no||"-";
    $("latestRefs").textContent=d.latest_refs+"/"+(d.latest_expected_count||"?");
    $("complete").textContent=d.question_count||"-";
    $("latestInfo").textContent=`标题:${d.latest_chat_title||"-"} · 账号:${d.latest_account_uid_masked||d.latest_account_nickname||"-"} · 采集:${d.latest_captured_at||d.latest_run_time||"-"}`;
    $("typeHint").textContent=d.csv.exists?"CSV: "+d.csv.mtime:"";
    $("mediaHint").textContent=`${d.media_total||0}个媒体`;
    $("domainHint").textContent=`${d.domain_total||0}个域名`;
    renderBars("typeBars",d.by_type||[],d.total_refs||0);
    renderBars("mediaBars",d.by_media||[],d.total_refs||0);
    renderBars("domainBars",d.by_domain||[],d.total_refs||0);
    renderOwnedVideoCategoryShare(d.owned_video_source_share_by_category||null);
    renderQuestionSources(d.per_question_sources||[]);
    renderProducts(d);
    renderDaily(d.daily_question_sources||[]);
    renderDailyProducts(d.daily_question_products||[]);
    renderBrandSourceAnalysis(d.brand_source_daily_analytics||null);
    renderOwnedProductSourceAnalysis(d.owned_product_source_analytics||null);
    renderLatest(d.latest_items||[]);
    $("logTail").textContent=(d.log_tail||[]).join("\n")||"暂无日志";
    applyView();applyFilter();
    restoreDynamicDetails(document);
    restoreUiState(ui);
  }
}

async function refresh(force=false){
  // 定时刷新不能和上一请求重叠；用户主动切换问题时则取消旧请求。
  if(_refreshBusy&&!force)return;
  if(_refreshBusy&&force&&_refreshController)_refreshController.abort();
  const controller=new AbortController();
  let timedOut=false;
  // A cold category build can briefly queue behind another live category
  // refresh.  Keep the previous data visible, but do not declare failure at
  // 15s while the local server is still completing a valid first calculation.
  const timeoutId=setTimeout(()=>{timedOut=true;controller.abort()},30000);
  _refreshController=controller;
  _refreshBusy=true;
  const cacheKey=`${selectedQuestion||"全部问题"}::${selectedDevice||"all"}`;
  const cached=_statsCache.get(cacheKey);
  let paintedCached=false;
  if(cached&&Date.now()-cached.t<600000&&force){
    paintData(cached.d,true);
    paintedCached=true;
  }
  try{
    const seq=++_refreshSeq;
    const qq=(selectedQuestion?"&question="+encodeURIComponent(selectedQuestion):"")+"&device="+encodeURIComponent(selectedDevice||"all");
    const r=await fetch("/api/stats?_="+Date.now()+qq,{cache:"no-store",signal:controller.signal});
    if(!r.ok)throw new Error(`HTTP ${r.status}`);
    const d=await r.json();
    if(seq!==_refreshSeq)return;
    if(d.selected_question&&d.selected_question!==selectedQuestion){
      selectedQuestion=d.selected_question;
      sessionStorage.setItem("doubaoSelectedQuestion",selectedQuestion);
      syncUrlState();
    }
    if(d.selected_device&&d.selected_device!==selectedDevice){
      selectedDevice=d.selected_device;
      sessionStorage.setItem("doubaoSelectedDevice",selectedDevice);
      syncUrlState();
    }
    _statsCache.set(cacheKey,{t:Date.now(),d});
    const isNewVersion=!!d.data_version&&d.data_version!==_appliedDataVersion;
    paintData(d,(!paintedCached&&force)||isNewVersion||!_appliedDataVersion);
    _appliedDataVersion=d.data_version||"";
    _lastSuccessAt=d.generated_at||new Date().toLocaleTimeString();
    _refreshFailures=0;
  }catch(e){
    if(timedOut)$("status").textContent="刷新超时 · 已保留最近一次成功数据";
    else if(e.name!=="AbortError"){$("status").textContent="连接失败 · 已保留最近一次成功数据";_refreshFailures+=1}
  }finally{
    clearTimeout(timeoutId);
    if(_refreshController===controller){_refreshBusy=false;_refreshController=null;}
  }
}

async function checkForUpdates(){
  if(document.hidden){schedulePoll(15000);return}
  if(_versionBusy||_refreshBusy){schedulePoll(3000);return}
  _versionBusy=true;
  try{
    const qq="?question="+encodeURIComponent(selectedQuestion||"全部问题")+"&device="+encodeURIComponent(selectedDevice||"all");
    const r=await fetch("/api/version"+qq,{cache:"no-store"});
    if(!r.ok)throw new Error(`HTTP ${r.status}`);
    const v=await r.json();
    if(v.refreshing&&!v.ready){
      $("status").textContent="在线 · 后台同步最新数据...";
      return;
    }
    if(v.ready&&v.version&&v.version!==_appliedDataVersion){
      await refresh(false);
    }
    _refreshFailures=0;
  }catch(e){
    _refreshFailures+=1;
    $("status").textContent=`连接波动 · 显示缓存数据${_lastSuccessAt?" · "+_lastSuccessAt:""}`;
  }finally{
    _versionBusy=false;
    schedulePoll(Math.min(30000,5000*Math.max(1,2**Math.min(_refreshFailures,3))));
  }
}
function schedulePoll(delay=5000){if(_pollTimer)clearTimeout(_pollTimer);_pollTimer=setTimeout(checkForUpdates,delay)}

// View switching
function syncUrlState(){
  const u=new URL(location.href);
  if(selectedQuestion&&selectedQuestion!=="全部问题")u.searchParams.set("question",selectedQuestion);else u.searchParams.delete("question");
  if(selectedDevice&&selectedDevice!=="all")u.searchParams.set("device",selectedDevice);else u.searchParams.delete("device");
  if(activeView&&activeView!=="overview")u.searchParams.set("view",activeView);else u.searchParams.delete("view");
  history.replaceState(null,"",u.pathname+(u.search?u.search:""));
}
function setView(v){
  if(!["overview","question","product","daily","latest","support"].includes(v))return;
  activeView=v;sessionStorage.setItem("doubaoActiveView",v);syncUrlState();applyView();
  if(v==="daily")scheduleAnalysisRender();
}
document.querySelectorAll(".view-tab[data-view],.tab-btn[data-view]").forEach(b=>b.addEventListener("click",()=>setView(b.getAttribute("data-view"))));

$("questionSelect").addEventListener("change",e=>{
  selectedQuestion=e.target.value||"全部问题";
  sessionStorage.setItem("doubaoSelectedQuestion",selectedQuestion);
  syncUrlState();
  setText("pageTitle",selectedQuestion==="全部问题"?"全部问题信源监控":selectedQuestion);
  setText("pageSubtitle","正在切换问题数据...");
  refresh(true);
});
function selectDevice(device){
  selectedDevice=String(device||"all");
  sessionStorage.setItem("doubaoSelectedDevice",selectedDevice);
  syncUrlState();
  setText("pageSubtitle",selectedDevice==="all"?"正在切换到全部设备总览...":`正在切换到实例 ${selectedDevice}...`);
  refresh(true);
}
$("deviceSwitch").addEventListener("click",e=>{
  const button=e.target.closest("[data-device]");
  if(button)selectDevice(button.getAttribute("data-device"));
});
$("accountSummaryRows").addEventListener("click",e=>{
  const button=e.target.closest("[data-device-action]");
  if(button)selectDevice(button.getAttribute("data-device-action"));
});
$("filterInput").addEventListener("input",applyFilter);
$("analysisBrand").addEventListener("change",e=>{
  _analysisBrand=e.target.value||"";persistAnalysisControls();renderBrandSourceAnalysis(_brandAnalysisData);
});
$("analysisMetric").addEventListener("change",e=>{
  _analysisMetric=ANALYSIS_METRICS[e.target.value]?e.target.value:"source_av_ref_share";persistAnalysisControls();scheduleAnalysisRender();
});
$("analysisRange").addEventListener("change",e=>{
  _analysisRange=["7","14","30","all"].includes(e.target.value)?e.target.value:"7";persistAnalysisControls();scheduleAnalysisRender();
});
$("analysisMode").addEventListener("change",e=>{
  _analysisMode=e.target.value==="delta"?"delta":"level";persistAnalysisControls();scheduleAnalysisRender();
});
$("analysisIncludePartial").addEventListener("change",e=>{
  _analysisIncludePartial=!!e.target.checked;persistAnalysisControls();scheduleAnalysisRender();
});
$("ownedAnalysisProduct").addEventListener("change",e=>{
  _ownedProduct=e.target.value||"";persistOwnedAnalysisControls();scheduleAnalysisRender();
});
$("ownedAnalysisRange").addEventListener("change",e=>{
  _ownedRange=["7","14","30","all"].includes(e.target.value)?e.target.value:"7";persistOwnedAnalysisControls();scheduleAnalysisRender();
});
$("densityToggle").addEventListener("click",()=>{
  const compact=document.body.classList.toggle("compact");
  $("densityToggle").classList.toggle("active",compact);$("densityToggle").setAttribute("aria-pressed",compact?"true":"false");$("densityToggle").textContent=compact?"舒展":"紧凑";
  localStorage.setItem("doubaoCompact",compact?"1":"0");
});

const initialCompact=document.body.classList.contains("compact");
$("densityToggle").classList.toggle("active",initialCompact);$("densityToggle").setAttribute("aria-pressed",initialCompact?"true":"false");$("densityToggle").textContent=initialCompact?"舒展":"紧凑";
restoreDynamicDetails(document);applyView();syncUrlState();
if(activeView==="daily")scheduleAnalysisRender();
refresh(true).finally(()=>schedulePoll(5000));
document.addEventListener("visibilitychange",()=>{if(document.hidden){if(_pollTimer)clearTimeout(_pollTimer)}else{checkForUpdates()}});
</script>
</body>
</html>
"""


class HighConcurrencyHTTPServer(ThreadingHTTPServer):
    request_queue_size = max(
        128, min(4096, int(os.environ.get("MONITOR_HTTP_BACKLOG", "1024") or 1024))
    )
    daemon_threads = True
    block_on_close = False
    allow_reuse_address = True

    def __init__(self, *args, **kwargs):
        # ThreadingHTTPServer.__init__ closes a partially constructed server
        # when bind fails (for example, an occupied port). Keep close safe so
        # callers see the original bind error instead of a secondary one.
        self._request_executor = None
        super().__init__(*args, **kwargs)
        cpu_count = os.cpu_count() or 4
        self.max_workers = max(
            16,
            min(512, int(os.environ.get("MONITOR_HTTP_WORKERS", str(min(256, cpu_count * 16))) or 64)),
        )
        self.max_pending = max(
            self.max_workers,
            min(8192, int(os.environ.get("MONITOR_HTTP_MAX_PENDING", "2048") or 2048)),
        )
        self._request_slots = threading.BoundedSemaphore(self.max_pending)
        self._request_state_lock = threading.Lock()
        self._active_requests = 0
        self._accepted_requests = 0
        self._rejected_requests = 0
        self._request_executor = ThreadPoolExecutor(
            max_workers=self.max_workers,
            thread_name_prefix="dashboard-http",
        )

    def process_request(self, request, client_address):
        if not self._request_slots.acquire(blocking=False):
            with self._request_state_lock:
                self._rejected_requests += 1
            try:
                body = b'{"ok":false,"error":"busy"}'
                request.sendall(
                    b"HTTP/1.0 503 Service Unavailable\r\n"
                    b"Content-Type: application/json; charset=utf-8\r\n"
                    b"Retry-After: 1\r\nConnection: close\r\nContent-Length: "
                    + str(len(body)).encode("ascii") + b"\r\n\r\n" + body
                )
            except OSError:
                pass
            self.shutdown_request(request)
            return
        with self._request_state_lock:
            self._accepted_requests += 1
        try:
            self._request_executor.submit(self._process_request_in_pool, request, client_address)
        except Exception:
            self._request_slots.release()
            self.shutdown_request(request)
            raise

    def _process_request_in_pool(self, request, client_address):
        with self._request_state_lock:
            self._active_requests += 1
        try:
            super().process_request_thread(request, client_address)
        finally:
            with self._request_state_lock:
                self._active_requests -= 1
            self._request_slots.release()

    def concurrency_stats(self):
        with self._request_state_lock:
            return {
                "active_requests": self._active_requests,
                "accepted_requests": self._accepted_requests,
                "rejected_requests": self._rejected_requests,
            }

    def server_close(self):
        super().server_close()
        executor = getattr(self, "_request_executor", None)
        if executor is not None:
            executor.shutdown(wait=False, cancel_futures=True)

    def handle_error(self, request, client_address):
        # Rapid filter changes intentionally abort an obsolete browser fetch.
        # On Windows this surfaces as 10053/10054 while the completed response
        # is being written; it is not a server or data failure.
        error = sys.exc_info()[1]
        if isinstance(error, (BrokenPipeError, ConnectionAbortedError, ConnectionResetError)):
            return
        super().handle_error(request, client_address)


class DashboardHandler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(
            max(2.0, float(os.environ.get("MONITOR_HTTP_SOCKET_TIMEOUT", "15") or 15))
        )

    def log_message(self, _format, *_args):
        return

    def request_cache_key(self, prefix):
        parsed = urlparse(self.path)
        pairs = sorted(
            (key, value)
            for key, value in parse_qsl(parsed.query, keep_blank_values=True)
            if key != "_"
        )
        query = urlencode(pairs)
        return f"{prefix}:{parsed.path}?{query}" if query else f"{prefix}:{parsed.path}"

    def send_bytes(
        self, content, content_type, status=200, *, cache_control="no-store",
        etag="", content_encoding="", extra_headers=None,
    ):
        use_gzip = (
            not content_encoding
            and
            len(content) >= 1024
            and "gzip" in (self.headers.get("Accept-Encoding") or "").casefold()
            and (
                content_type.startswith("application/json")
                or content_type.startswith("text/")
            )
        )
        if use_gzip:
            content = gzip.compress(content, compresslevel=1)
            content_encoding = "gzip"
        if etag and status == 200 and self.headers.get("If-None-Match", "").strip() == etag:
            status = 304
            content = b""
            content_encoding = ""
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", cache_control)
        self.send_header("Content-Length", str(len(content)))
        if content_encoding:
            self.send_header("Content-Encoding", content_encoding)
        if etag:
            self.send_header("ETag", etag)
        origin = self.headers.get("Origin", "")
        if origin and (
            self.is_allowed_dashboard_origin(origin)
            or self.is_allowed_browser_extension_origin(origin)
        ):
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Expose-Headers", "ETag, X-Monitor-Cache")
        self.send_header("Vary", "Accept-Encoding, Origin")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header(
            "Access-Control-Allow-Headers",
            "Content-Type, Authorization, X-Monitor-Task-Token",
        )
        for key, value in (extra_headers or {}).items():
            self.send_header(str(key), str(value))
        self.end_headers()
        if content:
            self.wfile.write(content)

    def is_allowed_dashboard_origin(self, origin):
        try:
            parsed = urlparse(origin)
            request_host = (self.headers.get("Host") or "").split(":", 1)[0].strip("[]").casefold()
            origin_host = (parsed.hostname or "").casefold()
            return (
                parsed.scheme in ("http", "https")
                and (
                    origin_host == request_host
                    or (
                        parsed.port == 3000
                        and origin_host in {"127.0.0.1", "localhost", "::1"}
                    )
                )
            )
        except ValueError:
            return False

    @staticmethod
    def is_allowed_browser_extension_origin(origin):
        try:
            parsed = urlparse(origin)
            return (
                parsed.scheme == "chrome-extension"
                and bool(re.fullmatch(r"[a-p]{32}", parsed.hostname or ""))
            )
        except ValueError:
            return False

    def send_json(self, payload, status=200, *, extra_headers=None):
        self.send_bytes(
            json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            "application/json; charset=utf-8", status, extra_headers=extra_headers,
        )

    def read_json(self, maximum=65536):
        length = safe_int(self.headers.get("Content-Length"))
        if length < 0 or length > maximum:
            raise ValueError("请求内容过大")
        body = self.rfile.read(length) if length else b"{}"
        value = json.loads(body.decode("utf-8")) if body else {}
        if not isinstance(value, dict):
            raise ValueError("请求正文必须是 JSON 对象")
        return value

    def task_authorized(self):
        provided = self.headers.get("X-Monitor-Task-Token", "")
        return (
            secure_token_matches(provided, configured_token("MONITOR_TASK_API_TOKEN"))
            or secure_token_matches(provided, configured_token("MONITOR_WORKER_TOKEN"))
        )

    def worker_authorized(self):
        authorization = self.headers.get("Authorization", "")
        provided = authorization[7:].strip() if authorization.lower().startswith("bearer ") else ""
        return secure_token_matches(provided, configured_token("MONITOR_WORKER_TOKEN"))

    @staticmethod
    def _admin_password_valid(password):
        encoded = configured_token("MONITOR_ADMIN_PASSWORD_HASH")
        try:
            scheme, iterations, salt_text, digest_text = encoded.split("$", 3)
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
    def _admin_session_token(identity, lifetime=43200):
        secret = configured_token("MONITOR_ADMIN_SESSION_SECRET")
        if not secret:
            return ""
        expires = int(time.time()) + int(lifetime)
        account_expiry = str((identity or {}).get("expires_at") or "").strip()
        if account_expiry:
            try:
                parsed = datetime.fromisoformat(account_expiry)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=CST)
                expires = min(expires, int(parsed.timestamp()))
            except ValueError:
                return ""
        payload = json.dumps(
            {
                "aid": str((identity or {}).get("id") or ""),
                "u": str((identity or {}).get("username") or ""),
                "role": str((identity or {}).get("role") or "manager"),
                "sv": int((identity or {}).get("session_version") or 0),
                "exp": expires,
            },
            separators=(",", ":"), sort_keys=True,
        ).encode("utf-8")
        body = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
        signature = hmac.new(secret.encode("utf-8"), body.encode("ascii"), hashlib.sha256).hexdigest()
        return body + "." + signature

    def admin_identity(self):
        cookie = self.headers.get("Cookie", "")
        match = re.search(r"(?:^|;\s*)geo_admin_session=([^;]+)", cookie)
        secret = configured_token("MONITOR_ADMIN_SESSION_SECRET")
        if not match or not secret:
            return None
        try:
            body, provided = match.group(1).split(".", 1)
            expected = hmac.new(secret.encode("utf-8"), body.encode("ascii"), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(provided, expected):
                return None
            padded = body + "=" * (-len(body) % 4)
            payload = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
            if int(payload.get("exp") or 0) <= int(time.time()):
                return None
            username = str(payload.get("u") or "")
            if (
                str(payload.get("role") or "") in {"", "super_admin"}
                and str(payload.get("aid") or "") in {"", "root"}
                and secure_token_matches(username, configured_token("MONITOR_ADMIN_USERNAME"))
            ):
                return {
                    "id": "root", "username": username, "display_name": "总管理员",
                    "role": "super_admin", "session_version": 0, "expires_at": "",
                }
            return REMOTE_TASK_QUEUE.admin_account_for_session(
                str(payload.get("aid") or ""), username, int(payload.get("sv") or 0)
            )
        except (ValueError, TypeError, json.JSONDecodeError, binascii.Error):
            return None

    def admin_authorized(self):
        return self.admin_identity() is not None

    @staticmethod
    def _admin_cookie(token, *, clear=False):
        secure = os.environ.get("MONITOR_PUBLIC_ORIGIN", "").lower().startswith("https://")
        value = f"geo_admin_session={token}; Path=/; HttpOnly; SameSite=Strict"
        if secure:
            value += "; Secure"
        if clear:
            value += "; Max-Age=0"
        else:
            value += "; Max-Age=43200"
        return value

    def require_admin(self, *, super_admin=False):
        identity = self.admin_identity()
        if identity and (not super_admin or identity.get("role") == "super_admin"):
            return identity
        if identity and super_admin:
            self.send_json({"ok": False, "error": "仅总管理员可执行此操作"}, 403)
            return None
        self.send_json({"ok": False, "error": "请先登录管理员账号"}, 401)
        return None

    @staticmethod
    def admin_can_access_report(identity, report_key):
        return bool(
            identity
            and (
                identity.get("role") == "super_admin"
                or REMOTE_TASK_QUEUE.report_owned_by(report_key, identity.get("id"))
            )
        )

    def require_remote_tasks(self):
        if not REMOTE_TASKS_ENABLED:
            self.send_json({"ok": False, "error": "远程任务队列未启用"}, 503)
            return False
        return True

    def send_cached_json(self, cache_key, builder, *, ttl=3.0, stale_ttl=30.0):
        entry, cache_status = HTTP_JSON_CACHE.get_or_build(
            cache_key, builder, ttl=ttl, stale_ttl=stale_ttl,
        )
        accepts_gzip = "gzip" in (self.headers.get("Accept-Encoding") or "").casefold()
        content = entry.gzip_body if accepts_gzip else entry.body
        self.send_bytes(
            content,
            "application/json; charset=utf-8",
            cache_control=(
                f"private, max-age={max(0, int(ttl))}, "
                f"stale-while-revalidate={max(0, int(stale_ttl - ttl))}"
            ),
            etag=entry.etag,
            content_encoding="gzip" if accepts_gzip else "",
            extra_headers={"X-Monitor-Cache": cache_status},
        )

    def redirect_to_react_dashboard(self):
        if os.environ.get("MONITOR_REACT_DASHBOARD_DISABLED", "").strip() == "1":
            self.send_bytes(INDEX_HTML.encode("utf-8"), "text/html; charset=utf-8")
            return
        host = (self.headers.get("Host") or "127.0.0.1").split(":", 1)[0]
        self.send_response(302)
        self.send_header("Location", f"http://{host}:3000/")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def do_OPTIONS(self):
        origin = self.headers.get("Origin", "")
        path = self.path.split("?", 1)[0]
        extension_worker = (
            self.is_allowed_browser_extension_origin(origin)
            and path.startswith("/api/worker/")
        )
        if origin and not self.is_allowed_dashboard_origin(origin) and not extension_worker:
            self.send_bytes(b"forbidden", "text/plain; charset=utf-8", 403)
            return
        self.send_bytes(b"", "text/plain; charset=utf-8", 204)

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        origin = self.headers.get("Origin", "")
        extension_worker = (
            self.is_allowed_browser_extension_origin(origin)
            and path.startswith("/api/worker/")
        )
        if origin and not self.is_allowed_dashboard_origin(origin) and not extension_worker:
            self.send_json({"ok": False, "error": "forbidden origin"}, 403)
            return
        if path == "/api/admin/login":
            try:
                payload = self.read_json(8192)
                username = str(payload.get("username") or "").strip()
                password = str(payload.get("password") or "")
                client_ip = (self.headers.get("X-Forwarded-For") or "").split(",", 1)[0].strip()
                client_ip = client_ip or str(self.client_address[0] if self.client_address else "unknown")
                now = time.time()
                with _ADMIN_LOGIN_LOCK:
                    attempts = [seen for seen in _ADMIN_LOGIN_ATTEMPTS.get(client_ip, []) if now - seen < 900]
                    if len(attempts) >= 8:
                        self.send_json({"ok": False, "error": "登录尝试过多，请稍后再试"}, 429)
                        return
                identity = None
                if (
                    secure_token_matches(username, configured_token("MONITOR_ADMIN_USERNAME"))
                    and self._admin_password_valid(password)
                ):
                    identity = {
                        "id": "root", "username": username, "display_name": "总管理员",
                        "role": "super_admin", "session_version": 0, "expires_at": "",
                    }
                else:
                    identity = REMOTE_TASK_QUEUE.authenticate_admin_account(username, password)
                if identity is None:
                    with _ADMIN_LOGIN_LOCK:
                        _ADMIN_LOGIN_ATTEMPTS[client_ip] = attempts + [now]
                    self.send_json({"ok": False, "error": "账号或密码错误"}, 401)
                    return
                if not identity.get("can_login", True):
                    message = (
                        "账号有效期已结束，请联系总管理员"
                        if identity.get("expired") else "账号已暂停，请联系总管理员"
                    )
                    self.send_json({"ok": False, "error": message}, 403)
                    return
                with _ADMIN_LOGIN_LOCK:
                    _ADMIN_LOGIN_ATTEMPTS.pop(client_ip, None)
                token = self._admin_session_token(identity)
                if not token:
                    self.send_json({"ok": False, "error": "管理员服务尚未配置"}, 503)
                    return
                self.send_json(
                    {
                        "ok": True, "username": identity.get("username"),
                        "display_name": identity.get("display_name"),
                        "role": identity.get("role"),
                    },
                    extra_headers={"Set-Cookie": self._admin_cookie(token)},
                )
            except (ValueError, TypeError, json.JSONDecodeError) as exc:
                self.send_json({"ok": False, "error": str(exc)}, 400)
            return
        if path == "/api/admin/logout":
            self.send_json(
                {"ok": True},
                extra_headers={"Set-Cookie": self._admin_cookie("", clear=True)},
            )
            return
        if path == "/api/admin/settings":
            if not self.require_admin(super_admin=True):
                return
            try:
                settings = REMOTE_TASK_QUEUE.update_service_settings(self.read_json(8192))
                self.send_json({"ok": True, "settings": settings})
            except (ValueError, TypeError, json.JSONDecodeError) as exc:
                self.send_json({"ok": False, "error": str(exc)}, 400)
            return
        if path == "/api/admin/accounts":
            identity = self.require_admin(super_admin=True)
            if not identity:
                return
            try:
                body = self.read_json(16384)
                if secure_token_matches(
                    str(body.get("username") or "").strip(),
                    configured_token("MONITOR_ADMIN_USERNAME"),
                ):
                    raise ValueError("该账号名称由总管理员使用")
                account = REMOTE_TASK_QUEUE.create_admin_account(
                    body, created_by=str(identity.get("username") or "")
                )
                self.send_json({"ok": True, "account": account}, 201)
            except (ValueError, TypeError, json.JSONDecodeError) as exc:
                self.send_json({"ok": False, "error": str(exc)}, 400)
            return
        account_quota_reset_match = re.fullmatch(
            r"/api/admin/accounts/([a-f0-9]{32})/reset-quota", path, re.I
        )
        if account_quota_reset_match:
            if not self.require_admin(super_admin=True):
                return
            account = REMOTE_TASK_QUEUE.reset_admin_account_quota(
                account_quota_reset_match.group(1).lower()
            )
            if account is None:
                self.send_json({"ok": False, "error": "管理员账号不存在"}, 404)
                return
            self.send_json({"ok": True, "account": account})
            return
        account_update_match = re.fullmatch(
            r"/api/admin/accounts/([a-f0-9]{32})", path, re.I
        )
        if account_update_match:
            if not self.require_admin(super_admin=True):
                return
            try:
                body = self.read_json(16384)
                requested_username = str(body.get("username") or "").strip()
                if requested_username and secure_token_matches(
                    requested_username, configured_token("MONITOR_ADMIN_USERNAME")
                ):
                    raise ValueError("该账号名称由总管理员使用")
                account = REMOTE_TASK_QUEUE.update_admin_account(
                    account_update_match.group(1).lower(), body
                )
                if account is None:
                    self.send_json({"ok": False, "error": "管理员账号不存在"}, 404)
                    return
                self.send_json({"ok": True, "account": account})
            except (ValueError, TypeError, json.JSONDecodeError) as exc:
                self.send_json({"ok": False, "error": str(exc)}, 400)
            return
        if path == "/api/admin/paid-monitors":
            if not self.require_admin(super_admin=True):
                return
            try:
                monitor = REMOTE_TASK_QUEUE.create_paid_monitor(self.read_json(32768))
                self.send_json({"ok": True, "monitor": monitor}, 201)
            except (ValueError, TypeError, json.JSONDecodeError) as exc:
                self.send_json({"ok": False, "error": str(exc)}, 400)
            return
        paid_monitor_action = re.fullmatch(
            r"/api/admin/paid-monitors/([a-f0-9]{32})/(pause|resume|rerun|clear)", path, re.I
        )
        if paid_monitor_action:
            if not self.require_admin(super_admin=True):
                return
            monitor_id, action = paid_monitor_action.groups()
            try:
                if action == "pause":
                    monitor = REMOTE_TASK_QUEUE.pause_paid_monitor(monitor_id.lower())
                elif action == "resume":
                    monitor = REMOTE_TASK_QUEUE.resume_paid_monitor(monitor_id.lower())
                elif action == "rerun":
                    monitor = REMOTE_TASK_QUEUE.rerun_paid_monitor(monitor_id.lower())
                else:
                    body = self.read_json(8192)
                    if str(body.get("confirm_user_id") or "").strip() == "":
                        raise ValueError("请输入付费用户 ID 确认清除")
                    existing = REMOTE_TASK_QUEUE.get_paid_monitor(monitor_id.lower())
                    if existing is None:
                        monitor = None
                    elif not secure_token_matches(body.get("confirm_user_id"), existing.get("paid_user_id")):
                        raise ValueError("付费用户 ID 确认不一致")
                    else:
                        monitor = REMOTE_TASK_QUEUE.clear_paid_monitor_data(
                            monitor_id.lower(), Path(BASE_DIR) / "runtime" / "web_results"
                        )
                if monitor is None:
                    self.send_json({"ok": False, "error": "付费用户不存在"}, 404)
                    return
                self.send_json({"ok": True, "monitor": monitor})
            except (ValueError, TypeError, json.JSONDecodeError) as exc:
                self.send_json({"ok": False, "error": str(exc)}, 400)
            return
        paid_monitor_update = re.fullmatch(
            r"/api/admin/paid-monitors/([a-f0-9]{32})", path, re.I
        )
        if paid_monitor_update:
            if not self.require_admin(super_admin=True):
                return
            try:
                monitor = REMOTE_TASK_QUEUE.update_paid_monitor(
                    paid_monitor_update.group(1).lower(), self.read_json(32768)
                )
                if monitor is None:
                    self.send_json({"ok": False, "error": "付费用户不存在"}, 404)
                    return
                self.send_json({"ok": True, "monitor": monitor})
            except (ValueError, TypeError, json.JSONDecodeError) as exc:
                self.send_json({"ok": False, "error": str(exc)}, 400)
            return
        report_sharing_match = re.fullmatch(
            r"/api/admin/reports/([a-f0-9]{32})/sharing", path, re.I
        )
        if report_sharing_match:
            identity = self.require_admin()
            if not identity:
                return
            report_key = report_sharing_match.group(1).lower()
            if not self.admin_can_access_report(identity, report_key):
                self.send_json({"ok": False, "error": "无权管理这份报告"}, 403)
                return
            try:
                body = self.read_json(8192)
                if not isinstance(body.get("enabled"), bool):
                    raise ValueError("enabled 必须是布尔值")
                sharing = REMOTE_TASK_QUEUE.update_report_sharing(
                    report_key,
                    enabled=body["enabled"],
                    expires_at=str(body.get("expires_at") or ""),
                )
                self.send_json({"ok": True, "sharing": sharing})
            except (ValueError, TypeError, json.JSONDecodeError) as exc:
                self.send_json({"ok": False, "error": str(exc)}, 400)
            return
        report_delete_match = re.fullmatch(
            r"/api/admin/reports/([a-f0-9]{32})/delete", path, re.I
        )
        if report_delete_match:
            if not self.require_admin(super_admin=True):
                return
            try:
                body = self.read_json(8192)
                deleted = REMOTE_TASK_QUEUE.delete_diagnosis_report(
                    report_delete_match.group(1).lower(),
                    confirmation=str(body.get("confirm_brand") or ""),
                    results_root=Path(BASE_DIR) / "runtime" / "web_results",
                )
                if deleted is None:
                    self.send_json({"ok": False, "error": "诊断报告不存在"}, 404)
                    return
                self.send_json({"ok": True, "deleted_report_key": report_delete_match.group(1).lower()})
            except (ValueError, TypeError, json.JSONDecodeError) as exc:
                self.send_json({"ok": False, "error": str(exc)}, 400)
            return
        report_edit_match = re.fullmatch(
            r"/api/admin/reports/([a-f0-9]{32})/edit", path, re.I
        )
        if report_edit_match:
            identity = self.require_admin(super_admin=True)
            if not identity:
                return
            try:
                editor = REMOTE_TASK_QUEUE.update_diagnosis_report(
                    report_edit_match.group(1).lower(),
                    self.read_json(8 * 1024 * 1024),
                    editor_username=str(identity.get("username") or "总管理员"),
                )
                if editor is None:
                    self.send_json({"ok": False, "error": "诊断报告不存在"}, 404)
                    return
                report = REMOTE_TASK_QUEUE.diagnosis_report(
                    report_edit_match.group(1).lower(), include_readiness=False
                ).get("report")
                self.send_json({"ok": True, "editor": editor, "report": report})
            except (ValueError, TypeError, json.JSONDecodeError) as exc:
                self.send_json({"ok": False, "error": str(exc)}, 400)
            return
        report_revert_match = re.fullmatch(
            r"/api/admin/reports/([a-f0-9]{32})/revert", path, re.I
        )
        if report_revert_match:
            identity = self.require_admin(super_admin=True)
            if not identity:
                return
            try:
                editor = REMOTE_TASK_QUEUE.revert_diagnosis_report(
                    report_revert_match.group(1).lower(),
                    editor_username=str(identity.get("username") or "总管理员"),
                )
                if editor is None:
                    self.send_json({"ok": False, "error": "诊断报告不存在"}, 404)
                    return
                report = REMOTE_TASK_QUEUE.diagnosis_report(
                    report_revert_match.group(1).lower(), include_readiness=False
                ).get("report")
                self.send_json({"ok": True, "editor": editor, "report": report})
            except (ValueError, TypeError, json.JSONDecodeError) as exc:
                self.send_json({"ok": False, "error": str(exc)}, 400)
            return
        if path == "/api/diagnosis":
            if not self.require_remote_tasks():
                return
            identity = self.require_admin()
            if not identity:
                return
            try:
                forwarded = (self.headers.get("X-Forwarded-For") or "").split(",", 1)[0].strip()
                client_ip = forwarded or str(self.client_address[0] if self.client_address else "unknown")
                moment = time.monotonic()
                with _DIAGNOSIS_CREATE_LOCK:
                    previous = float(_DIAGNOSIS_LAST_CREATED.get(client_ip) or 0)
                    if moment - previous < 5:
                        raise ValueError("提交过于频繁，请稍后再试")
                    REMOTE_TASK_QUEUE.assert_diagnosis_allowed(identity)
                    report_key, task = REMOTE_TASK_QUEUE.create_public_diagnosis(
                        self.read_json(), account=identity,
                    )
                    _DIAGNOSIS_LAST_CREATED[client_ip] = moment
                public_task = (REMOTE_TASK_QUEUE.diagnosis_report(report_key).get("task") or task)
                self.send_json({
                    "ok": True,
                    "report_key": report_key,
                    "report_path": f"/geo/{report_key}",
                    "task": public_task,
                }, 201)
            except PermissionError as exc:
                self.send_json({"ok": False, "error": str(exc)}, 403)
            except (ValueError, TypeError, RuntimeError, json.JSONDecodeError) as exc:
                self.send_json({"ok": False, "error": str(exc)}, 400)
            return
        login_start_match = re.fullmatch(
            r"/api/logins/(doubao|yuanbao|wenxin|deepseek|kimi)/start", path
        )
        if login_start_match:
            if not self.require_remote_tasks():
                return
            if not self.task_authorized():
                self.send_json({"ok": False, "error": "任务访问密钥无效"}, 401)
                return
            try:
                login = REMOTE_TASK_QUEUE.request_login(login_start_match.group(1))
                self.send_json({"ok": True, "login": login}, 201)
            except (ValueError, TypeError, json.JSONDecodeError) as exc:
                self.send_json({"ok": False, "error": str(exc)}, 400)
            return
        task_action_match = re.fullmatch(
            r"/api/tasks/([a-f0-9]{32})/(cancel|pause|resume|rerun|clear|delete)", path
        )
        result_delete_match = re.fullmatch(
            r"/api/tasks/([a-f0-9]{32})/results/([A-Za-z0-9._:-]{1,160})/delete", path
        )
        if path in {"/api/tasks", "/api/tasks/cleanup"} or task_action_match or result_delete_match:
            if not self.require_remote_tasks():
                return
            if not self.task_authorized():
                self.send_json({"ok": False, "error": "任务访问密钥无效"}, 401)
                return
            try:
                if path == "/api/tasks":
                    task = REMOTE_TASK_QUEUE.create(self.read_json())
                    self.send_json({"ok": True, "task": task}, 201)
                    return
                if path == "/api/tasks/cleanup":
                    deleted = REMOTE_TASK_QUEUE.clear_finished(
                        Path(BASE_DIR) / "runtime" / "web_results"
                    )
                    self.send_json({"ok": True, "deleted": deleted})
                    return
                if result_delete_match:
                    task_id, request_id = result_delete_match.groups()
                    task = REMOTE_TASK_QUEUE.delete_result(
                        task_id, request_id, Path(BASE_DIR) / "runtime" / "web_results"
                    )
                    if task is None:
                        self.send_json({"ok": False, "error": "任务不存在"}, 404)
                        return
                    self.send_json({"ok": True, "task": task})
                    return
                task_id, action = task_action_match.groups()
                if action == "cancel":
                    task = REMOTE_TASK_QUEUE.cancel(task_id)
                elif action == "pause":
                    task = REMOTE_TASK_QUEUE.pause(task_id)
                elif action == "resume":
                    task = REMOTE_TASK_QUEUE.resume(task_id)
                elif action == "rerun":
                    task = REMOTE_TASK_QUEUE.rerun(task_id)
                elif action == "clear":
                    task = REMOTE_TASK_QUEUE.clear_results(
                        task_id, Path(BASE_DIR) / "runtime" / "web_results"
                    )
                else:
                    task = REMOTE_TASK_QUEUE.delete(
                        task_id, Path(BASE_DIR) / "runtime" / "web_results"
                    )
                if task is None:
                    self.send_json({"ok": False, "error": "任务不存在"}, 404)
                    return
                self.send_json({"ok": True, "task": task}, 201 if action == "rerun" else 200)
            except (KeyError, ValueError, TypeError, json.JSONDecodeError) as exc:
                self.send_json({"ok": False, "error": str(exc)}, 400)
            return
        if path == "/api/worker/claim" or re.fullmatch(
            r"/api/worker/(?:tasks/[a-f0-9]{32}/(?:heartbeat|result|finish)|"
            r"logins/[a-f0-9]{32}/(?:heartbeat|finish))", path
        ):
            if not self.require_remote_tasks():
                return
            if not self.worker_authorized():
                self.send_json({"ok": False, "error": "Worker 凭证无效"}, 401)
                return
            try:
                payload = self.read_json(maximum=8 * 1024 * 1024)
                if path == "/api/worker/claim":
                    worker_id = str(payload.get("worker_id") or "local-worker")
                    readiness = (
                        payload.get("readiness")
                        if isinstance(payload.get("readiness"), dict) else {}
                    )
                    login = (
                        None if worker_id.startswith("chrome-")
                        else REMOTE_TASK_QUEUE.claim_login(worker_id, readiness)
                    )
                    task = None if login else REMOTE_TASK_QUEUE.claim(worker_id, readiness)
                    self.send_json({"ok": True, "login": login, "task": task})
                    return
                parts = path.split("/")
                resource, item_id, action = parts[3], parts[4], parts[5]
                lease = str(payload.get("lease_token") or "")
                if resource == "logins":
                    result = (
                        REMOTE_TASK_QUEUE.login_heartbeat(item_id, lease, payload)
                        if action == "heartbeat"
                        else {"ok": True, "login": REMOTE_TASK_QUEUE.finish_login(
                            item_id, lease, payload
                        )}
                    )
                elif action == "heartbeat":
                    result = REMOTE_TASK_QUEUE.heartbeat(item_id, lease, payload)
                elif action == "result":
                    result = REMOTE_TASK_QUEUE.accept_result(
                        item_id, lease, str(payload.get("model_id") or ""),
                        str(payload.get("request_id") or ""), payload.get("record") or {},
                        Path(BASE_DIR) / "runtime" / "web_results",
                    )
                else:
                    result = {"ok": True, "task": REMOTE_TASK_QUEUE.finish(item_id, lease, payload)}
                self.send_json(result)
            except PermissionError as exc:
                self.send_json({"ok": False, "error": str(exc)}, 403)
            except KeyError as exc:
                self.send_json({"ok": False, "error": str(exc)}, 404)
            except (ValueError, TypeError, json.JSONDecodeError) as exc:
                self.send_json({"ok": False, "error": str(exc)}, 400)
            return
        model_route = re.fullmatch(r"/api/models/([a-z0-9_-]+)/(questions|account-check)", path)
        if model_route:
            if REMOTE_TASKS_ENABLED:
                self.send_json({"ok": False, "error": "服务器展示模式禁止直接控制采集器"}, 403)
                return
            model, action = model_route.groups()
            plugin = MODEL_PLUGINS.get(model)
            if plugin is None:
                self.send_json({"ok": False, "error": "未知模型"}, 404)
                return
            try:
                if action == "account-check":
                    self.send_json({"ok": True, "model": model, "account": plugin.account_check()})
                    return
                length = min(safe_int(self.headers.get("Content-Length")), 65536)
                body = self.rfile.read(length) if length else b"{}"
                payload = json.loads(body.decode("utf-8")) if body else {}
                raw_questions = payload.get("questions") if isinstance(payload, dict) else None
                if not isinstance(raw_questions, list):
                    raise ValueError("questions 必须是数组")
                questions = []
                for item in raw_questions[:100]:
                    text = str(item.get("text") if isinstance(item, dict) else item or "").strip()
                    if text and text not in questions:
                        questions.append(text[:500])
                if not questions:
                    raise ValueError("至少保留一个有效问题")
                plugin.save_questions(questions)
                if "question_mode" in payload:
                    plugin.save_question_mode(payload.get("question_mode"))
                self.send_json({"ok": True, "model": model, "questions": plugin.load_questions(),
                                "question_mode": plugin.load_question_mode()})
            except Exception as exc:
                self.send_json({"ok": False, "error": str(exc)}, 400)
            return
        match = re.fullmatch(r"/api/control/([a-z0-9_-]+)/(start|stop)", path)
        if not match:
            self.send_json({"ok": False, "error": "not found"}, 404)
            return
        if REMOTE_TASKS_ENABLED:
            self.send_json({"ok": False, "error": "服务器展示模式禁止直接启动采集器"}, 403)
            return
        try:
            length = min(safe_int(self.headers.get("Content-Length")), 65536)
            body = self.rfile.read(length) if length else b"{}"
            options = json.loads(body.decode("utf-8")) if body else {}
            model, action = match.groups()
            state = (
                _start_controlled_job(model, options)
                if action == "start" else _stop_controlled_job(model)
            )
            self.send_json({"ok": True, "model": model, "state": state})
        except (ValueError, OSError, subprocess.SubprocessError) as exc:
            self.send_json({"ok": False, "error": str(exc)}, 400)

    def do_GET(self):
        if self.path.startswith("/api/health"):
            self.send_json({
                "ok": True,
                "generated_at": beijing_now(),
                "http": {
                    "max_workers": getattr(self.server, "max_workers", 0),
                    "max_pending": getattr(self.server, "max_pending", 0),
                    "backlog": getattr(self.server, "request_queue_size", 0),
                    **(
                        self.server.concurrency_stats()
                        if hasattr(self.server, "concurrency_stats") else {}
                    ),
                    "response_cache": HTTP_JSON_CACHE.stats(),
                },
                "database": {
                    "enabled": monitor_database.enabled(),
                    "pool": monitor_database.pool_stats() if monitor_database.enabled() else {},
                },
            })
            return
        path = self.path.split("?", 1)[0]
        if path == "/api/admin/session":
            identity = self.admin_identity()
            quota = REMOTE_TASK_QUEUE.diagnosis_quota(identity) if identity else None
            self.send_json({
                "ok": True,
                "authenticated": bool(identity),
                "username": str((identity or {}).get("username") or ""),
                "display_name": str((identity or {}).get("display_name") or ""),
                "role": str((identity or {}).get("role") or ""),
                "expires_at": str((identity or {}).get("expires_at") or ""),
                "quota": quota,
            })
            return
        if path == "/api/admin/authorize":
            identity = self.admin_identity()
            original_uri = str(self.headers.get("X-Original-URI") or "").split("?", 1)[0]
            if identity and identity.get("role") == "super_admin":
                self.send_bytes(b"", "text/plain; charset=utf-8", 204)
                return
            if identity:
                allowed_manager_path = original_uri in {
                    "/geo", "/geo/", "/geo/admin", "/geo/admin/",
                }
                owned_match = re.fullmatch(r"/geo/([a-f0-9]{32})/?", original_uri, re.I)
                paid_match = re.fullmatch(r"/geo/(paid-[a-f0-9]{24})/?", original_uri, re.I)
                paid_access = False
                if paid_match and REMOTE_TASKS_ENABLED:
                    payload = REMOTE_TASK_QUEUE.paid_monitor_dashboard(paid_match.group(1).lower())
                    paid_access = bool(payload and payload.get("access_allowed"))
                if allowed_manager_path or (
                    owned_match
                    and self.admin_can_access_report(identity, owned_match.group(1).lower())
                ) or paid_access:
                    self.send_bytes(b"", "text/plain; charset=utf-8", 204)
                    return
                # An authenticated lower-level administrator is forbidden from
                # super-admin pages (and from reports owned by other accounts).
                # Return 403 instead of 401 so Nginx does not send the browser
                # through the login-page `next` redirect loop.
                self.send_bytes(b"", "text/plain; charset=utf-8", 403)
                return
            # Nginx supplies the original browser URI for its auth subrequest.
            # A completed report may be shared by its unpredictable 128-bit key;
            # every other /geo page continues to require an administrator session.
            public_match = re.fullmatch(r"/geo/([a-f0-9]{32})/?", original_uri, re.I)
            if public_match and REMOTE_TASKS_ENABLED:
                try:
                    public_payload = REMOTE_TASK_QUEUE.diagnosis_report(public_match.group(1).lower())
                    public_task = (
                        public_payload.get("task")
                        if isinstance(public_payload.get("task"), dict)
                        else None
                    )
                    if (
                        public_task
                        and str(public_task.get("status") or "") == "completed"
                        and REMOTE_TASK_QUEUE.report_is_public(public_match.group(1).lower())
                    ):
                        self.send_bytes(b"", "text/plain; charset=utf-8", 204)
                        return
                except (ValueError, TypeError, json.JSONDecodeError):
                    pass
            paid_match = re.fullmatch(r"/geo/(paid-[a-f0-9]{24})/?", original_uri, re.I)
            if paid_match and REMOTE_TASKS_ENABLED:
                try:
                    paid_payload = REMOTE_TASK_QUEUE.paid_monitor_dashboard(
                        paid_match.group(1).lower()
                    )
                    if paid_payload and paid_payload.get("access_allowed"):
                        self.send_bytes(b"", "text/plain; charset=utf-8", 204)
                        return
                except (ValueError, TypeError, json.JSONDecodeError):
                    pass
            self.send_bytes(b"", "text/plain; charset=utf-8", 401)
            return
        if path == "/api/admin/settings":
            if not self.require_admin(super_admin=True):
                return
            self.send_json({"ok": True, "settings": REMOTE_TASK_QUEUE.service_settings()})
            return
        if path == "/api/admin/accounts":
            if not self.require_admin(super_admin=True):
                return
            self.send_json({
                "ok": True,
                "accounts": REMOTE_TASK_QUEUE.list_admin_accounts(),
            })
            return
        if path == "/api/admin/paid-monitors":
            if not self.require_admin(super_admin=True):
                return
            self.send_json({"ok": True, "monitors": REMOTE_TASK_QUEUE.list_paid_monitors()})
            return
        report_editor_match = re.fullmatch(
            r"/api/admin/reports/([a-f0-9]{32})/editor", path, re.I
        )
        if report_editor_match:
            if not self.require_admin(super_admin=True):
                return
            try:
                editor = REMOTE_TASK_QUEUE.report_editor_data(
                    report_editor_match.group(1).lower()
                )
                if editor is None:
                    self.send_json({"ok": False, "error": "诊断报告不存在"}, 404)
                    return
                self.send_json({"ok": True, "editor": editor})
            except (ValueError, TypeError, json.JSONDecodeError) as exc:
                self.send_json({"ok": False, "error": str(exc)}, 400)
            return
        if path == "/api/admin/reports":
            identity = self.require_admin()
            if not identity:
                return
            is_super_admin = identity.get("role") == "super_admin"
            self.send_json({
                "ok": True,
                "reports": REMOTE_TASK_QUEUE.completed_diagnosis_reports(
                    1000,
                    owner_account_id=None if is_super_admin else str(identity.get("id") or ""),
                ),
                "settings": REMOTE_TASK_QUEUE.service_settings() if is_super_admin else {},
                "account": {
                    "username": identity.get("username"),
                    "display_name": identity.get("display_name"),
                    "role": identity.get("role"),
                    "expires_at": identity.get("expires_at"),
                    "quota": REMOTE_TASK_QUEUE.diagnosis_quota(identity),
                },
            })
            return
        if path == "/api/diagnosis":
            if not self.require_remote_tasks():
                return
            identity = self.require_admin()
            if not identity:
                return
            self.send_json({
                "ok": True,
                "readiness": REMOTE_TASK_QUEUE.diagnosis_readiness(),
                "settings": REMOTE_TASK_QUEUE.service_settings(),
                "quota": REMOTE_TASK_QUEUE.diagnosis_quota(identity),
            })
            return
        # Public reports are addressed only by server-generated 128-bit keys.
        # Legacy customer slugs must never expose stale, failed or cancelled jobs.
        diagnosis_match = re.fullmatch(r"/api/diagnosis/([a-f0-9]{32})", path)
        if diagnosis_match:
            if not self.require_remote_tasks():
                return
            try:
                payload = REMOTE_TASK_QUEUE.diagnosis_report(diagnosis_match.group(1))
                task = payload.get("task") if isinstance(payload.get("task"), dict) else None
                if task is None:
                    self.send_json({"ok": False, "error": "诊断报告不存在"}, 404)
                    return
                identity = self.admin_identity()
                is_admin = self.admin_can_access_report(
                    identity, diagnosis_match.group(1).lower()
                )
                if (
                    not is_admin
                    and (
                        str(task.get("status") or "") != "completed"
                        or not REMOTE_TASK_QUEUE.report_is_public(diagnosis_match.group(1))
                    )
                ):
                    # Do not reveal whether a random key belongs to a queued,
                    # running, failed or cancelled task.
                    self.send_json({"ok": False, "error": "诊断报告不存在"}, 404)
                    return
                if not is_admin:
                    payload = {
                        "customer_slug": payload.get("customer_slug"),
                        "task": task,
                        "history": [],
                        "readiness": {
                            "ready": False, "online": False, "worker_id": "",
                            "models": {}, "message": "",
                        },
                        "report": payload.get("report"),
                    }
                self.send_json({"ok": True, **payload})
            except (ValueError, TypeError, json.JSONDecodeError) as exc:
                self.send_json({"ok": False, "error": str(exc)}, 400)
            return
        paid_monitor_public_match = re.fullmatch(
            r"/api/paid-monitor/(paid-[a-f0-9]{24})", path, re.I
        )
        if paid_monitor_public_match:
            if not self.require_remote_tasks():
                return
            try:
                payload = REMOTE_TASK_QUEUE.paid_monitor_dashboard(
                    paid_monitor_public_match.group(1).lower()
                )
                if not payload or not payload.get("access_allowed"):
                    self.send_json({"ok": False, "error": "监控面板不存在或已到期"}, 404)
                    return
                self.send_json({"ok": True, **payload})
            except (ValueError, TypeError, json.JSONDecodeError) as exc:
                self.send_json({"ok": False, "error": str(exc)}, 400)
            return
        if path == "/tasks":
            if not self.require_admin(super_admin=True):
                return
            self.send_bytes(task_page_html().encode("utf-8"), "text/html; charset=utf-8")
            return
        if path == "/api/logins":
            if not self.require_remote_tasks():
                return
            if not self.task_authorized():
                self.send_json({"ok": False, "error": "任务访问密钥无效"}, 401)
                return
            self.send_json({"ok": True, "logins": REMOTE_TASK_QUEUE.list_login_requests()})
            return
        if path == "/api/workers":
            if not self.require_remote_tasks():
                return
            if not self.task_authorized():
                self.send_json({"ok": False, "error": "任务访问密钥无效"}, 401)
                return
            self.send_json({"ok": True, "workers": REMOTE_TASK_QUEUE.list_workers()})
            return
        task_results_match = re.fullmatch(r"/api/tasks/([a-f0-9]{32})/results", path)
        if task_results_match:
            if not self.require_remote_tasks():
                return
            if not self.task_authorized():
                self.send_json({"ok": False, "error": "任务访问密钥无效"}, 401)
                return
            results = REMOTE_TASK_QUEUE.results(task_results_match.group(1))
            if results is None:
                self.send_json({"ok": False, "error": "任务不存在"}, 404)
            else:
                self.send_json({"ok": True, "results": results})
            return
        task_match = re.fullmatch(r"/api/tasks(?:/([a-f0-9]{32}))?", path)
        if task_match:
            if not self.require_remote_tasks():
                return
            if not self.task_authorized():
                self.send_json({"ok": False, "error": "任务访问密钥无效"}, 401)
                return
            task_id = task_match.group(1)
            if task_id:
                task = REMOTE_TASK_QUEUE.get(task_id)
                if task is None:
                    self.send_json({"ok": False, "error": "任务不存在"}, 404)
                else:
                    self.send_json({"ok": True, "task": task})
            else:
                params = parse_qs(urlparse(self.path).query)
                limit = max(1, min(safe_int((params.get("limit") or [50])[0]), 100))
                self.send_json({"ok": True, "tasks": REMOTE_TASK_QUEUE.list(limit)})
            return
        if path == "/api/models":
            self.send_json({
                "ok": True,
                "models": list(MODEL_REGISTRY.values()),
                "reserved_slots": RESERVED_MODEL_SLOTS,
            })
            return
        question_route = re.fullmatch(r"/api/models/([a-z0-9_-]+)/questions", self.path.split("?", 1)[0])
        if question_route:
            plugin = MODEL_PLUGINS.get(question_route.group(1))
            if plugin is None:
                self.send_json({"ok": False, "error": "未知模型"}, 404)
            else:
                try:
                    self.send_json({"ok": True, "model": plugin.id, "questions": plugin.load_questions(),
                                    "question_mode": plugin.load_question_mode()})
                except Exception as exc:
                    self.send_json({"ok": False, "error": str(exc)}, 500)
            return
        activity_route = re.fullmatch(r"/api/models/([a-z0-9_-]+)/activity", self.path.split("?", 1)[0])
        if activity_route:
            plugin = MODEL_PLUGINS.get(activity_route.group(1))
            if plugin is None:
                self.send_json({"ok": False, "error": "未知模型"}, 404)
            else:
                try:
                    params = parse_qs(urlparse(self.path).query)
                    limit = max(1, min(safe_int((params.get("limit") or [40])[0]), 100))
                    self.send_json(plugin.activity(limit))
                except Exception as exc:
                    self.send_json({"ok": False, "error": str(exc)}, 500)
            return
        if self.path.startswith("/api/control/status"):
            self.send_json(_control_status())
            return
        if self.path.startswith("/api/analytics/brand-sources"):
            try:
                params = parse_qs(urlparse(self.path).query)
                self.send_cached_json(
                    self.request_cache_key("brand-sources"),
                    lambda: _brand_source_links_payload(params),
                    ttl=5.0,
                    stale_ttl=60.0,
                )
            except Exception as exc:
                self.send_json({"ok": False, "error": str(exc)}, 400)
            return
        if self.path.startswith("/api/analytics/source-intersections"):
            try:
                params = parse_qs(urlparse(self.path).query)
                self.send_cached_json(
                    self.request_cache_key("source-intersections"),
                    lambda: _source_intersection_payload(params),
                    ttl=5.0,
                    stale_ttl=60.0,
                )
            except Exception as exc:
                self.send_json({"ok": False, "error": str(exc)}, 500)
            return
        if self.path.startswith("/api/analytics"):
            try:
                params = parse_qs(urlparse(self.path).query)
                include_runs = (params.get("include_runs") or [""])[0] == "1"
                view = (params.get("view") or [""])[0]
                self.send_cached_json(
                    self.request_cache_key("analytics"),
                    lambda: _analytics_payload_for_client(
                        _unified_analytics(params), include_runs, view,
                    ),
                    ttl=3.0,
                    stale_ttl=30.0,
                )
            except Exception as exc:
                self.send_json({"ok": False, "error": str(exc)}, 500)
            return
        if self.path.startswith("/api/yuanbao/stats"):
            try:
                self.send_json(_yuanbao_stats())
            except (OSError, ValueError, json.JSONDecodeError, subprocess.SubprocessError) as exc:
                self.send_json({"ok": False, "error": str(exc)}, 500)
            return
        model_stats_route = re.fullmatch(r"/api/models/([a-z0-9_-]+)/stats", self.path.split("?", 1)[0])
        if model_stats_route:
            model_id = model_stats_route.group(1)
            plugin = MODEL_PLUGINS.get(model_id)
            if plugin is None:
                self.send_json({"ok": False, "error": "未知模型"}, 404)
                return
            try:
                if model_id == "doubao":
                    parsed = urlparse(self.path)
                    params = parse_qs(parsed.query)
                    self.send_json(build_stats((params.get("question") or [""])[0], (params.get("device") or [ALL_DEVICES])[0]))
                else:
                    self.send_json(plugin.stats())
            except (OSError, ValueError, json.JSONDecodeError, subprocess.SubprocessError) as exc:
                self.send_json({"ok": False, "error": str(exc)}, 500)
            return
        if self.path.startswith("/api/version"):
            parsed = urlparse(self.path)
            params = parse_qs(parsed.query)
            question = (params.get("question") or [""])[0]
            device = (params.get("device") or [ALL_DEVICES])[0]
            content = json.dumps(
                stats_version(question, device),
                ensure_ascii=False,
            ).encode("utf-8")
            self.send_bytes(content, "application/json; charset=utf-8")
            return
        if self.path.startswith("/api/stats"):
            parsed = urlparse(self.path)
            params = parse_qs(parsed.query)
            question = (params.get("question") or [""])[0]
            device = (params.get("device") or [ALL_DEVICES])[0]
            content = json.dumps(
                build_stats(question, device),
                ensure_ascii=False,
            ).encode("utf-8")
            self.send_bytes(content, "application/json; charset=utf-8")
            return
        if self.path.split("?", 1)[0] in ("/", "/index.html"):
            self.redirect_to_react_dashboard()
            return
        if self.path.split("?", 1)[0] in ("/rag-lab", "/rag-lab.html"):
            try:
                with open(RAG_ML_LAB_PATH, "rb") as handle:
                    self.send_bytes(handle.read(), "text/html; charset=utf-8")
            except OSError:
                self.send_bytes(b"RAG ML lab has not been generated", "text/plain; charset=utf-8", 404)
            return
        self.send_bytes(b"not found", "text/plain; charset=utf-8", 404)


def start_content_worker():
    """Keep body extraction alive for as long as the dashboard is alive."""
    if os.environ.get("DOUBAO_CONTENT_WORKER_DISABLED", "").strip() == "1":
        return None
    if not os.path.exists(CONTENT_WORKER_PATH):
        return None
    worker_python = sys.executable
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        return subprocess.Popen(
            [
                worker_python,
                CONTENT_WORKER_PATH,
                "--parent-pid",
                str(os.getpid()),
            ],
            cwd=BASE_DIR,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creationflags,
        )
    except Exception as exc:
        print("Doubao content worker failed to start:", exc)
        return None


def supervise_content_worker():
    """Restart the incremental source-page archiver if it exits."""
    while True:
        process = start_content_worker()
        if process is None:
            time.sleep(30)
            continue
        return_code = process.wait()
        print("Doubao content worker exited (%s); restarting shortly." % return_code)
        time.sleep(5)


def main():
    # Load only cheap persisted state before opening the socket. Full analytics
    # preparation touches the entire source corpus and previously kept 8765
    # unavailable for tens of seconds after every restart.
    _load_persisted_view_cache()
    server = HighConcurrencyHTTPServer((HOST, PORT), DashboardHandler)
    warmups_enabled = os.environ.get("MONITOR_DASHBOARD_WARMUP_DISABLED", "").strip() != "1"
    threading.Thread(
        target=supervise_content_worker,
        name="doubao-content-worker-supervisor",
        daemon=True,
    ).start()
    # Do not preload the all-history snapshot. It materializes every answer and
    # source row, consumes gigabytes on a mature dataset, and competes with the
    # first interactive requests. Date/model scoped analytics load on demand;
    # all-history views can still build the snapshot when explicitly used.
    def warm_legacy_stats():
        with _VIEW_CACHE_LOCK:
            has_initial = _view_key(ALL_QUESTIONS, ALL_DEVICES) in _VIEW_CACHE
        if has_initial:
            return
        try:
            initial = _compute_stats(ALL_QUESTIONS, ALL_DEVICES)
            _store_view_cache(ALL_QUESTIONS, ALL_DEVICES, initial)
        except Exception as exc:
            print("Doubao initial dashboard cache failed:", exc)

    if warmups_enabled:
        threading.Thread(
            target=warm_legacy_stats,
            name="doubao-legacy-stats-initial-warmup",
            daemon=True,
        ).start()

    def keep_dashboard_views_warm():
        """Prebuild the five interactive views once per Beijing day."""
        warmed_day = ""
        # Let an immediately opened browser complete its first interactive
        # request before CPU-heavy background aggregation begins.
        time.sleep(30)
        while True:
            current_day = datetime.now(CST).date().isoformat()
            if current_day != warmed_day:
                for view in ("overview", "brands", "compare", "sources", "runs"):
                    try:
                        _unified_analytics({"date": [current_day], "view": [view]})
                    except Exception as exc:
                        print("Dashboard warmup failed:", view, exc)
                    time.sleep(2)
                # The two most common "全部日期" landing pages are expensive
                # only on their first build. Prepare them once in the
                # background so the first real user never pays that cost.
                for view in ("overview", "brands", "sources"):
                    try:
                        _unified_analytics({"view": [view]})
                    except Exception as exc:
                        print("Dashboard all-date warmup failed:", view, exc)
                    time.sleep(2)
                try:
                    _source_intersection_payload({})
                except Exception as exc:
                    print("Dashboard source-intersection warmup failed:", exc)
                warmed_day = current_day
            time.sleep(60)

    if warmups_enabled:
        threading.Thread(
            target=keep_dashboard_views_warm,
            name="dashboard-current-day-warmup",
            daemon=True,
        ).start()

    def keep_source_filter_caches_warm():
        """Warm every question's all-date source page without delaying startup."""
        time.sleep(90)
        while True:
            try:
                questions = monitor_database.analytics_filter_options().get("questions") or []
            except Exception as exc:
                print("Source filter warmup options failed:", exc)
                time.sleep(300)
                continue
            for question in questions:
                try:
                    _unified_analytics({"question": [question], "view": ["sources"]})
                    _source_intersection_payload({"question": [question]})
                except Exception as exc:
                    print("Source filter warmup failed:", question, exc)
                time.sleep(1)
            time.sleep(1800)

    if warmups_enabled:
        threading.Thread(
            target=keep_source_filter_caches_warm,
            name="dashboard-source-filter-warmup",
            daemon=True,
        ).start()
    print("Doubao dashboard:", "http://%s:%s" % (HOST, PORT))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
