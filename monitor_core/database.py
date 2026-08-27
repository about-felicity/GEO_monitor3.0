"""PostgreSQL storage, incremental synchronization and analytics caching."""

from __future__ import annotations

from collections import defaultdict
from contextlib import contextmanager
from datetime import date, datetime, timezone
import hashlib
import json
import os
import atexit
import re
import sys
import threading
import unicodedata
from typing import Any, Iterable

try:
    import psycopg
    from psycopg.rows import dict_row
    from psycopg.types.json import Jsonb
    from psycopg_pool import ConnectionPool
except ImportError:  # Database support remains an optional, safe fallback.
    psycopg = None
    dict_row = None
    Jsonb = None
    ConnectionPool = None


SCHEMA_VERSION = 7
_POOL: ConnectionPool | None = None
_POOL_LOCK = threading.Lock()
_SCHEMA_READY = False
_SCHEMA_LOCK = threading.Lock()


def database_url() -> str:
    value = os.getenv("MONITOR_DATABASE_URL", "").strip()
    if value:
        return value
    if os.name == "nt" and "unittest" not in sys.modules:
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
                value = str(winreg.QueryValueEx(key, "MONITOR_DATABASE_URL")[0]).strip()
        except (OSError, ImportError):
            value = ""
    return value


def enabled() -> bool:
    return bool(psycopg is not None and database_url())


def pool() -> ConnectionPool:
    global _POOL
    if not enabled():
        raise RuntimeError("PostgreSQL is not configured")
    with _POOL_LOCK:
        if _POOL is None:
            min_size = max(1, int(os.getenv("MONITOR_DATABASE_POOL_MIN", "2") or 2))
            max_size = max(
                min_size,
                min(256, int(os.getenv("MONITOR_DATABASE_POOL_MAX", "32") or 32)),
            )
            _POOL = ConnectionPool(
                conninfo=database_url(), min_size=min_size, max_size=max_size,
                timeout=max(1, float(os.getenv("MONITOR_DATABASE_POOL_TIMEOUT", "10") or 10)),
                max_idle=300, max_lifetime=3600,
                kwargs={"autocommit": False, "row_factory": dict_row,
                        "application_name": "monitor-dashboard"},
                open=True,
            )
    return _POOL


def pool_stats() -> dict[str, int]:
    """Expose bounded pool occupancy for health checks without opening a query."""
    current = _POOL
    if current is None:
        return {"initialized": 0}
    try:
        return {"initialized": 1, **{str(key): int(value) for key, value in current.get_stats().items()}}
    except Exception:
        return {"initialized": 1}


def close_pool() -> None:
    global _POOL
    with _POOL_LOCK:
        current, _POOL = _POOL, None
    if current is not None:
        current.close()


atexit.register(close_pool)


@contextmanager
def connection():
    with pool().connection() as conn:
        yield conn


DDL = """
CREATE TABLE IF NOT EXISTS monitor_meta (
    key text PRIMARY KEY,
    value text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS monitor_runs (
    model_id text NOT NULL,
    run_id text NOT NULL,
    sequence_no bigint NOT NULL,
    question text NOT NULL,
    finished_at timestamptz,
    day date,
    serial text NOT NULL DEFAULT '',
    answer text NOT NULL DEFAULT '',
    status text NOT NULL DEFAULT 'success',
    record_hash text NOT NULL,
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    product_ai_retry_count integer NOT NULL DEFAULT 0,
    product_ai_next_retry_at timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (model_id, run_id)
);
CREATE TABLE IF NOT EXISTS monitor_sources (
    model_id text NOT NULL,
    run_id text NOT NULL,
    source_index integer NOT NULL,
    canonical_url text NOT NULL DEFAULT '',
    url text NOT NULL DEFAULT '',
    title text NOT NULL DEFAULT '',
    domain text NOT NULL DEFAULT '',
    media text NOT NULL DEFAULT '',
    source_type text NOT NULL DEFAULT '',
    own_brand boolean NOT NULL DEFAULT false,
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (model_id, run_id, source_index),
    FOREIGN KEY (model_id, run_id) REFERENCES monitor_runs(model_id, run_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS monitor_brands (
    model_id text NOT NULL,
    run_id text NOT NULL,
    brand text NOT NULL,
    PRIMARY KEY (model_id, run_id, brand),
    FOREIGN KEY (model_id, run_id) REFERENCES monitor_runs(model_id, run_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS monitor_products (
    model_id text NOT NULL,
    run_id text NOT NULL,
    product_index integer NOT NULL,
    brand text NOT NULL DEFAULT '',
    product text NOT NULL DEFAULT '',
    rank_no integer NOT NULL DEFAULT 0,
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (model_id, run_id, product_index),
    FOREIGN KEY (model_id, run_id) REFERENCES monitor_runs(model_id, run_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS analytics_versions (
    scope_key text PRIMARY KEY,
    version bigint NOT NULL DEFAULT 1,
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS analytics_cache (
    cache_key text PRIMARY KEY,
    scope_key text NOT NULL,
    scope_version bigint NOT NULL,
    content_token text NOT NULL,
    payload jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    hit_count bigint NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS analytics_source_url_refcounts (
    model_id text NOT NULL,
    canonical_url text NOT NULL,
    refs bigint NOT NULL DEFAULT 0,
    PRIMARY KEY (model_id, canonical_url)
);
CREATE TABLE IF NOT EXISTS analytics_source_model_stats (
    model_id text PRIMARY KEY,
    source_refs bigint NOT NULL DEFAULT 0,
    unique_sources bigint NOT NULL DEFAULT 0,
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS monitor_sync_state (
    source_key text PRIMARY KEY,
    source_version text NOT NULL,
    synced_at timestamptz NOT NULL DEFAULT now(),
    rows_synced bigint NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS monitor_ingest_events (
    model_id text NOT NULL,
    request_id text NOT NULL,
    source_device text NOT NULL DEFAULT '',
    received_at timestamptz,
    envelope jsonb NOT NULL DEFAULT '{}'::jsonb,
    record jsonb NOT NULL,
    run_id text NOT NULL DEFAULT '',
    stored_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (model_id, request_id)
);
ALTER TABLE monitor_ingest_events ADD COLUMN IF NOT EXISTS run_id text NOT NULL DEFAULT '';
ALTER TABLE monitor_runs ADD COLUMN IF NOT EXISTS product_ai_retry_count integer NOT NULL DEFAULT 0;
ALTER TABLE monitor_runs ADD COLUMN IF NOT EXISTS product_ai_next_retry_at timestamptz;
ALTER TABLE monitor_sources ADD COLUMN IF NOT EXISTS production_valid boolean NOT NULL DEFAULT true;

CREATE INDEX IF NOT EXISTS idx_runs_day_model_question ON monitor_runs(day DESC, model_id, question);
CREATE INDEX IF NOT EXISTS idx_runs_model_question_day ON monitor_runs(model_id, question, day DESC);
CREATE INDEX IF NOT EXISTS idx_runs_question_day_model ON monitor_runs(question, day DESC, model_id);
CREATE INDEX IF NOT EXISTS idx_runs_finished_desc ON monitor_runs(finished_at DESC) INCLUDE (model_id, question, status);
CREATE INDEX IF NOT EXISTS idx_runs_updated_desc ON monitor_runs(updated_at DESC) INCLUDE (model_id, run_id, day, question);
CREATE INDEX IF NOT EXISTS idx_runs_success_scope ON monitor_runs(model_id, day DESC, question) WHERE status = 'success';
CREATE INDEX IF NOT EXISTS idx_runs_scope_cover ON monitor_runs(model_id, day DESC, status, run_id) INCLUDE (question, finished_at);
CREATE INDEX IF NOT EXISTS idx_sources_run ON monitor_sources(model_id, run_id, source_index);
CREATE INDEX IF NOT EXISTS idx_sources_run_cover ON monitor_sources(model_id, run_id) INCLUDE (source_type, media, canonical_url, own_brand);
CREATE INDEX IF NOT EXISTS idx_sources_type_media ON monitor_sources(source_type, media);
CREATE INDEX IF NOT EXISTS idx_sources_canonical_url ON monitor_sources(canonical_url) WHERE canonical_url <> '';
CREATE UNIQUE INDEX IF NOT EXISTS idx_sources_unique_url_per_run ON monitor_sources(model_id, run_id, canonical_url) WHERE canonical_url <> '';
CREATE INDEX IF NOT EXISTS idx_sources_owned ON monitor_sources(model_id, own_brand) WHERE own_brand;
CREATE INDEX IF NOT EXISTS idx_sources_payload_gin ON monitor_sources USING gin(payload jsonb_path_ops);
CREATE INDEX IF NOT EXISTS idx_runs_payload_gin ON monitor_runs USING gin(payload jsonb_path_ops);
CREATE INDEX IF NOT EXISTS idx_runs_product_ai_retry ON monitor_runs(product_ai_next_retry_at, updated_at)
WHERE COALESCE(payload->>'product_review_status','') = 'ai_pending';
CREATE INDEX IF NOT EXISTS idx_brands_name_model ON monitor_brands(brand, model_id);
CREATE INDEX IF NOT EXISTS idx_products_brand_product ON monitor_products(brand, product, model_id);
CREATE INDEX IF NOT EXISTS idx_cache_scope ON analytics_cache(scope_key, scope_version);
CREATE INDEX IF NOT EXISTS idx_ingest_received ON monitor_ingest_events(model_id, received_at DESC);
CREATE INDEX IF NOT EXISTS idx_ingest_record_gin ON monitor_ingest_events USING gin(record jsonb_path_ops);
CREATE INDEX IF NOT EXISTS idx_ingest_run ON monitor_ingest_events(model_id, run_id) WHERE run_id <> '';

CREATE OR REPLACE FUNCTION monitor_source_stats_apply() RETURNS trigger AS $$
DECLARE
    target_model text;
    target_url text;
    run_is_valid boolean := false;
    remaining_refs bigint;
BEGIN
    -- Serialize source-stat maintenance and one-time bootstrap across the
    -- dashboard and all receiver/worker processes. A batch transaction takes
    -- the advisory lock once and subsequent trigger rows reuse it cheaply.
    PERFORM pg_advisory_xact_lock(72426082501);
    IF TG_OP = 'INSERT' THEN
        target_model := NEW.model_id;
        target_url := COALESCE(NEW.canonical_url, '');
    ELSE
        target_model := OLD.model_id;
        target_url := COALESCE(OLD.canonical_url, '');
    END IF;
    IF TG_OP = 'INSERT' THEN
        SELECT (
            r.status='success'
            AND COALESCE(r.payload->>'body_capture_complete','true') <> 'false'
            AND COALESCE(r.payload->>'source_capture_complete','true') <> 'false'
        ) INTO run_is_valid
        FROM monitor_runs r
        WHERE r.model_id=target_model AND r.run_id=NEW.run_id;
        NEW.production_valid := COALESCE(run_is_valid, false);
    ELSE
        run_is_valid := OLD.production_valid;
    END IF;
    IF NOT COALESCE(run_is_valid, false) THEN
        IF TG_OP = 'INSERT' THEN RETURN NEW; ELSE RETURN OLD; END IF;
    END IF;
    IF TG_OP = 'INSERT' THEN
        INSERT INTO analytics_source_model_stats(model_id,source_refs,unique_sources)
        VALUES(target_model,1,0)
        ON CONFLICT(model_id) DO UPDATE
        SET source_refs=analytics_source_model_stats.source_refs+1,updated_at=now();
        IF target_url <> '' THEN
            UPDATE analytics_source_url_refcounts
            SET refs=refs+1
            WHERE model_id=target_model AND canonical_url=target_url;
            IF NOT FOUND THEN
                INSERT INTO analytics_source_url_refcounts(model_id,canonical_url,refs)
                VALUES(target_model,target_url,1);
                UPDATE analytics_source_model_stats
                SET unique_sources=unique_sources+1,updated_at=now()
                WHERE model_id=target_model;
            END IF;
        END IF;
        RETURN NEW;
    END IF;
    UPDATE analytics_source_model_stats
    SET source_refs=GREATEST(source_refs-1,0),updated_at=now()
    WHERE model_id=target_model;
    IF target_url <> '' THEN
        UPDATE analytics_source_url_refcounts
        SET refs=refs-1
        WHERE model_id=target_model AND canonical_url=target_url
        RETURNING refs INTO remaining_refs;
        IF remaining_refs IS NOT NULL AND remaining_refs <= 0 THEN
            DELETE FROM analytics_source_url_refcounts
            WHERE model_id=target_model AND canonical_url=target_url;
            UPDATE analytics_source_model_stats
            SET unique_sources=GREATEST(unique_sources-1,0),updated_at=now()
            WHERE model_id=target_model;
        END IF;
    END IF;
    RETURN OLD;
END;
$$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS monitor_source_stats_insert ON monitor_sources;
CREATE TRIGGER monitor_source_stats_insert
BEFORE INSERT ON monitor_sources FOR EACH ROW EXECUTE FUNCTION monitor_source_stats_apply();
DROP TRIGGER IF EXISTS monitor_source_stats_delete ON monitor_sources;
CREATE TRIGGER monitor_source_stats_delete
BEFORE DELETE ON monitor_sources FOR EACH ROW EXECUTE FUNCTION monitor_source_stats_apply();
"""


def ensure_schema() -> None:
    global _SCHEMA_READY
    if _SCHEMA_READY:
        return
    with _SCHEMA_LOCK:
        if _SCHEMA_READY:
            return
        with connection() as conn:
            conn.execute(DDL)
            conn.execute("SELECT pg_advisory_xact_lock(72426082501)")
            validity_initialized = conn.execute(
                "SELECT value FROM monitor_meta WHERE key='source_validity_initialized'"
            ).fetchone()
            if not validity_initialized:
                conn.execute(
                    "UPDATE monitor_sources s SET production_valid=false "
                    "FROM monitor_runs r WHERE r.model_id=s.model_id AND r.run_id=s.run_id "
                    "AND NOT (r.status='success' "
                    "AND COALESCE(r.payload->>'body_capture_complete','true')<>'false' "
                    "AND COALESCE(r.payload->>'source_capture_complete','true')<>'false')"
                )
                conn.execute(
                    "INSERT INTO monitor_meta(key,value) VALUES('source_validity_initialized','1') "
                    "ON CONFLICT(key) DO UPDATE SET value='1',updated_at=now()"
                )
            initialized = conn.execute(
                "SELECT value FROM monitor_meta WHERE key='source_stats_initialized'"
            ).fetchone()
            if not initialized:
                conn.execute("TRUNCATE analytics_source_url_refcounts,analytics_source_model_stats")
                conn.execute(
                    "INSERT INTO analytics_source_url_refcounts(model_id,canonical_url,refs) "
                    "SELECT s.model_id,s.canonical_url,count(*) FROM monitor_sources s "
                    "JOIN monitor_runs r ON r.model_id=s.model_id AND r.run_id=s.run_id "
                    "WHERE s.canonical_url<>'' AND r.status='success' "
                    "AND COALESCE(r.payload->>'body_capture_complete','true')<>'false' "
                    "AND COALESCE(r.payload->>'source_capture_complete','true')<>'false' "
                    "GROUP BY s.model_id,s.canonical_url"
                )
                conn.execute(
                    "INSERT INTO analytics_source_model_stats(model_id,source_refs,unique_sources) "
                    "SELECT model_id,sum(refs),count(*) FROM analytics_source_url_refcounts "
                    "GROUP BY model_id"
                )
                conn.execute(
                    "INSERT INTO monitor_meta(key,value) VALUES('source_stats_initialized','1') "
                    "ON CONFLICT(key) DO UPDATE SET value='1',updated_at=now()"
                )
            conn.execute(
                "INSERT INTO monitor_meta(key,value) VALUES('schema_version',%s) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=now()",
                (str(SCHEMA_VERSION),),
            )
            conn.commit()
        _SCHEMA_READY = True


def _json_hash(value: dict[str, Any]) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def normalized_answer_fingerprint(value: Any) -> str:
    """Stable normalized fingerprint for diagnostics and audits.

    Independent tasks can legitimately receive the same deterministic Baidu
    card.  This fingerprint must therefore never be used as an ingestion key;
    request ids provide retry idempotency without deleting valid observations.
    """
    text = unicodedata.normalize("NFKC", str(value or ""))
    text = re.sub(r"[\u200b-\u200f\u202a-\u202e\u2060\ufeff\s]+", "", text)
    return hashlib.sha256(text.encode("utf-8")).hexdigest() if text else ""


def capture_quarantine_reason(model_id: str, run: dict[str, Any]) -> str:
    """Return why a live result must remain audit-only instead of production data."""
    normalized_model = str(model_id or "").casefold()
    if str(run.get("status") or "success").casefold() != "success":
        return ""
    if not str(run.get("answer") or "").strip():
        return "empty_answer"
    if run.get("body_capture_complete") is False:
        return f"{normalized_model or 'unknown'}_body_capture_incomplete"
    if normalized_model != "wenxin":
        return ""
    usable_sources = [
        source for source in (run.get("sources") or [])
        if isinstance(source, dict)
        and str(source.get("canonical_url") or source.get("url") or "").strip()
    ]
    if not usable_sources:
        return "wenxin_source_capture_empty"
    if run.get("source_capture_complete") is False:
        return "wenxin_source_capture_incomplete"
    return ""


def _run_rows(runs_by_model: dict[str, list[dict[str, Any]]]):
    for model_id, runs in runs_by_model.items():
        for run in runs:
            normalized = dict(run)
            if (
                str(normalized.get("status") or "success").casefold() == "success"
                and not str(normalized.get("answer") or "").strip()
            ):
                normalized.update({
                    "status": "invalid",
                    "quality_reason": "empty_answer",
                })
            sources = list(normalized.pop("sources", []) or [])
            brands = list(normalized.pop("brands", []) or [])
            products = list(normalized.pop("products", []) or [])
            identity = {**normalized, "sources": sources, "brands": brands, "products": products}
            yield model_id, normalized, sources, brands, products, _json_hash(identity)


def _write_children(cur, model_id: str, run_id: str, sources, brands, products) -> None:
    # Normalize at the persistence boundary as well as in analytics. This
    # prevents direct database consumers and future snapshots from reviving
    # known ingredients, product series or shop labels as brands.
    from monitor_core.analytics import (
        _product_fields,
        canonical_brand_name,
        canonical_url as normalize_canonical_url,
    )

    def normalized_brand_list(values) -> list[str]:
        return sorted({
            canonical
            for value in values or []
            if (canonical := canonical_brand_name(str(value or "")))
        })

    deduplicated_sources = []
    seen_source_urls: set[str] = set()
    for raw_item in sources or []:
        item = dict(raw_item)
        for field in (
            "brand_mentions", "title_brand_mentions", "body_brand_mentions",
            "owned_brands",
        ):
            if field in item:
                item[field] = normalized_brand_list(item.get(field))
        canonical_url = str(item.get("canonical_url") or item.get("url") or "").strip()
        canonical_url = canonical_url and normalize_canonical_url(canonical_url)
        item["canonical_url"] = canonical_url
        if canonical_url and canonical_url in seen_source_urls:
            continue
        if canonical_url:
            seen_source_urls.add(canonical_url)
        deduplicated_sources.append(item)
    if deduplicated_sources:
        cur.executemany(
            "INSERT INTO monitor_sources(model_id,run_id,source_index,canonical_url,url,title,domain,media,source_type,own_brand,payload) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
            [(model_id, run_id, index, str(item.get("canonical_url") or item.get("url") or ""),
              str(item.get("url") or ""), str(item.get("title") or ""), str(item.get("domain") or ""),
              str(item.get("media") or ""), str(item.get("type") or ""), bool(item.get("own_brand")), Jsonb(item))
             for index, item in enumerate(deduplicated_sources)],
        )
    unique_brands = normalized_brand_list(brands)
    if unique_brands:
        cur.executemany(
            "INSERT INTO monitor_brands(model_id,run_id,brand) VALUES(%s,%s,%s)",
            [(model_id, run_id, item) for item in unique_brands],
        )
    if products:
        rows = []
        for index, raw_item in enumerate(products):
            item = dict(raw_item)
            brand, product, rank = _product_fields(item)
            if not brand or not product:
                continue
            item["brand"] = brand
            item["brand_name"] = brand
            item["brand_identified"] = bool(brand)
            item["product_name"] = product
            item["name"] = product
            item["rank"] = rank
            rows.append((model_id, run_id, index, brand, product, rank, Jsonb(item)))
        if rows:
            cur.executemany(
                "INSERT INTO monitor_products(model_id,run_id,product_index,brand,product,rank_no,payload) VALUES(%s,%s,%s,%s,%s,%s,%s)",
                rows,
            )


def _scope_keys(model_id: str, day_value: Any) -> tuple[str, ...]:
    day_text = str(day_value or "")
    keys = ["global", f"model:{model_id}"]
    if day_text:
        keys.extend((f"day:{day_text}", f"model_day:{model_id}:{day_text}"))
    return tuple(keys)


def _bump_versions(cur, keys: Iterable[str]) -> None:
    for key in sorted(set(keys)):
        cur.execute(
            "INSERT INTO analytics_versions(scope_key,version) VALUES(%s,1) "
            "ON CONFLICT(scope_key) DO UPDATE SET version=analytics_versions.version+1,updated_at=now()",
            (key,),
        )


def replace_all(runs_by_model: dict[str, list[dict[str, Any]]]) -> dict[str, int]:
    ensure_schema()
    counts = {"runs": 0, "sources": 0, "brands": 0, "products": 0}
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "TRUNCATE monitor_sources,monitor_brands,monitor_products,monitor_runs,"
                "analytics_cache,analytics_versions,analytics_source_url_refcounts,"
                "analytics_source_model_stats"
            )
            all_scopes = {"global"}
            for model_id, run, sources, brands, products, record_hash in _run_rows(runs_by_model):
                run_id = str(run.get("run_id") or f"{model_id}-{counts['runs'] + 1}")
                cur.execute(
                    "INSERT INTO monitor_runs(model_id,run_id,sequence_no,question,finished_at,day,serial,answer,status,record_hash,payload) "
                    "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (model_id, run_id, int(run.get("sequence") or 0), str(run.get("question") or ""),
                     run.get("finished_at") or None, run.get("day") or None, str(run.get("serial") or ""),
                     str(run.get("answer") or ""), str(run.get("status") or "success"), record_hash, Jsonb(run)),
                )
                _write_children(cur, model_id, run_id, sources, brands, products)
                all_scopes.update(_scope_keys(model_id, run.get("day")))
                counts["runs"] += 1; counts["sources"] += len(sources)
                counts["brands"] += len(set(brands)); counts["products"] += len(products)
            cur.executemany(
                "INSERT INTO analytics_versions(scope_key,version) VALUES(%s,1)",
                [(key,) for key in sorted(all_scopes)],
            )
        conn.commit()
    return counts


def sync_incremental(runs_by_model: dict[str, list[dict[str, Any]]]) -> dict[str, int]:
    ensure_schema()
    incoming = list(_run_rows(runs_by_model))
    with connection() as conn:
        existing = {(row["model_id"], row["run_id"]): row["record_hash"]
                    for row in conn.execute("SELECT model_id,run_id,record_hash FROM monitor_runs")}
        changed = []
        for row in incoming:
            model_id, run, _sources, _brands, _products, record_hash = row
            run_id = str(run.get("run_id") or "")
            if not run_id or existing.get((model_id, run_id)) != record_hash:
                changed.append(row)
        if not changed:
            return {"changed": 0, "runs": len(existing)}
        scopes = set()
        with conn.cursor() as cur:
            for model_id, run, sources, brands, products, record_hash in changed:
                run_id = str(run.get("run_id") or f"{model_id}-{run.get('sequence') or 0}")
                cur.execute("DELETE FROM monitor_runs WHERE model_id=%s AND run_id=%s", (model_id, run_id))
                cur.execute(
                    "INSERT INTO monitor_runs(model_id,run_id,sequence_no,question,finished_at,day,serial,answer,status,record_hash,payload) "
                    "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (model_id, run_id, int(run.get("sequence") or 0), str(run.get("question") or ""),
                     run.get("finished_at") or None, run.get("day") or None, str(run.get("serial") or ""),
                     str(run.get("answer") or ""), str(run.get("status") or "success"), record_hash, Jsonb(run)),
                )
                _write_children(cur, model_id, run_id, sources, brands, products)
                scopes.update(_scope_keys(model_id, run.get("day")))
            _bump_versions(cur, scopes)
        conn.commit()
        return {"changed": len(changed), "runs": len(existing) + len(changed)}


def load_runs_by_model(*, day: str = "", day_from: str = "", day_to: str = "",
                       question: str = "", model: str = "") -> dict[str, list[dict[str, Any]]]:
    """Load normalized runs, optionally using a narrow indexed dashboard scope."""
    ensure_schema()
    output: dict[str, list[dict[str, Any]]] = defaultdict(list)
    keyed: dict[tuple[str, str], dict[str, Any]] = {}
    clauses = []
    params: list[Any] = []
    if day:
        clauses.append("r.day=%s")
        params.append(day)
    else:
        if day_from:
            clauses.append("r.day>=%s")
            params.append(day_from)
        if day_to:
            clauses.append("r.day<=%s")
            params.append(day_to)
    if question:
        clauses.append("r.question=%s")
        params.append(question)
    if model:
        clauses.append("r.model_id=%s")
        params.append(model)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    with connection() as conn:
        for row in conn.execute(
            "SELECT r.model_id,r.run_id,r.payload FROM monitor_runs r" + where +
            " ORDER BY r.model_id,r.sequence_no", params,
        ):
            run = dict(row["payload"] or {})
            run.update({"model_id": row["model_id"], "run_id": row["run_id"],
                        "sources": [], "brands": [], "products": []})
            output[row["model_id"]].append(run)
            keyed[(row["model_id"], row["run_id"])] = run
        child_join = " JOIN monitor_runs r ON r.model_id=c.model_id AND r.run_id=c.run_id"
        for row in conn.execute(
            "SELECT c.model_id,c.run_id,c.payload FROM monitor_sources c" + child_join + where +
            " ORDER BY c.model_id,c.run_id,c.source_index", params,
        ):
            run = keyed.get((row["model_id"], row["run_id"]))
            if run is not None: run["sources"].append(dict(row["payload"] or {}))
        for row in conn.execute(
            "SELECT c.model_id,c.run_id,c.brand FROM monitor_brands c" + child_join + where +
            " ORDER BY c.model_id,c.run_id,c.brand", params,
        ):
            run = keyed.get((row["model_id"], row["run_id"]))
            if run is not None: run["brands"].append(row["brand"])
        for row in conn.execute(
            "SELECT c.model_id,c.run_id,c.payload FROM monitor_products c" + child_join + where +
            " ORDER BY c.model_id,c.run_id,c.product_index", params,
        ):
            run = keyed.get((row["model_id"], row["run_id"]))
            if run is not None: run["products"].append(dict(row["payload"] or {}))
    return dict(output)


def load_owned_product_runs(*, day: str = "", latest_days: int = 7,
                            question: str = "", model: str = "") -> dict[str, list[dict[str, Any]]]:
    """Load only fields needed by the real-time owned-product board.

    The all-date analytics snapshot includes hundreds of thousands of source
    rows and can legitimately lag live ingestion.  This narrow query avoids
    loading sources/brand children, so the board can stay current independently
    of that heavyweight archive aggregation.
    """
    ensure_schema()
    clauses: list[str] = []
    params: list[Any] = []
    if day:
        clauses.append("r.day=%s")
        params.append(day)
    if question:
        clauses.append("r.question=%s")
        params.append(question)
    if model:
        clauses.append("r.model_id=%s")
        params.append(model)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    output: dict[str, list[dict[str, Any]]] = defaultdict(list)
    keyed: dict[tuple[str, str], dict[str, Any]] = {}
    with connection() as conn:
        if not day:
            day_clauses = [part.replace("r.", "") for part in clauses]
            day_where = (" WHERE " + " AND ".join(day_clauses)) if day_clauses else ""
            selected_days = [
                row["day"] for row in conn.execute(
                    "SELECT DISTINCT day FROM monitor_runs" + day_where
                    + (" AND day IS NOT NULL" if day_where else " WHERE day IS NOT NULL")
                    + " ORDER BY day DESC LIMIT %s",
                    [*params, max(1, min(int(latest_days), 31))],
                ) if row["day"]
            ]
            if not selected_days:
                return {}
            clauses.append("r.day=ANY(%s::date[])")
            params.append(selected_days)
            where = " WHERE " + " AND ".join(clauses)
        for row in conn.execute(
            "SELECT r.model_id,r.run_id,r.question,r.answer,r.status,r.day,r.finished_at,"
            "r.serial,r.payload FROM monitor_runs r" + where
            + " ORDER BY r.model_id,r.sequence_no", params,
        ):
            run = dict(row["payload"] or {})
            run.update({
                "model_id": row["model_id"], "run_id": row["run_id"],
                "question": row["question"], "answer": row["answer"],
                "status": row["status"], "day": str(row["day"] or ""),
                "finished_at": row["finished_at"], "serial": row["serial"],
                "sources": [], "brands": [], "products": [],
            })
            output[row["model_id"]].append(run)
            keyed[(row["model_id"], row["run_id"])] = run
        child_join = " JOIN monitor_runs r ON r.model_id=c.model_id AND r.run_id=c.run_id"
        for row in conn.execute(
            "SELECT c.model_id,c.run_id,c.payload FROM monitor_products c" + child_join + where
            + " ORDER BY c.model_id,c.run_id,c.product_index", params,
        ):
            run = keyed.get((row["model_id"], row["run_id"]))
            if run is not None:
                run["products"].append(dict(row["payload"] or {}))
    return dict(output)


def analytics_filter_options(*, model: str = "", question: str = "") -> dict[str, list[str]]:
    """Return dashboard selectors without materializing answers or sources."""
    ensure_schema()
    model_clause = " WHERE model_id=%s" if model else ""
    model_params = (model,) if model else ()
    date_clauses = []
    date_params: list[Any] = []
    if model:
        date_clauses.append("model_id=%s")
        date_params.append(model)
    if question:
        date_clauses.append("question=%s")
        date_params.append(question)
    date_where = (" WHERE " + " AND ".join(date_clauses)) if date_clauses else ""
    with connection() as conn:
        questions = [
            str(row["question"])
            for row in conn.execute(
                "SELECT DISTINCT question FROM monitor_runs" + model_clause +
                " ORDER BY question", model_params,
            )
            if str(row["question"] or "").strip()
        ]
        dates = [
            str(row["day"])
            for row in conn.execute(
                "SELECT DISTINCT day FROM monitor_runs" + date_where +
                " AND day IS NOT NULL" if date_where else
                "SELECT DISTINCT day FROM monitor_runs WHERE day IS NOT NULL ORDER BY day DESC",
                date_params,
            )
            if row["day"]
        ]
    if date_where:
        # The conditional query above cannot append ORDER BY before deciding
        # whether a WHERE clause exists; keep its result deterministic here.
        dates.sort(reverse=True)
    return {"questions": questions, "dates": dates}


def analytics_source_scope_summary(*, question: str = "", model: str = "") -> dict[str, dict[str, int]]:
    """Aggregate all-history source KPIs without materializing source payloads.

    The source-insights page only needs full-history totals for its KPI cards.
    Loading every JSONB source row just to count them made the all-date filter
    consume more than a gigabyte of memory.  Keep the production-completeness
    rules aligned with ``build_analytics`` and let PostgreSQL perform the
    indexed grouping instead.
    """
    ensure_schema()
    clauses = [
        "r.status='success'",
        "COALESCE(r.payload->>'body_capture_complete','true') <> 'false'",
        "COALESCE(r.payload->>'source_capture_complete','true') <> 'false'",
        "NOT (r.model_id='wenxin' "
        "AND COALESCE(r.payload->>'source_capture_complete','')='true' "
        "AND NOT EXISTS (SELECT 1 FROM monitor_sources ws "
        "WHERE ws.model_id=r.model_id AND ws.run_id=r.run_id))",
    ]
    params: list[Any] = []
    if question:
        clauses.append("r.question=%s")
        params.append(question)
    if model:
        clauses.append("r.model_id=%s")
        params.append(model)
    where = " AND ".join(clauses)
    # The maintained model totals are exact for the all-question scope. A
    # selected question is much smaller, so its source aggregate remains a
    # fast indexed live query.
    if not question:
        source_stats_sql = (
            "SELECT model_id,source_refs AS sources,unique_sources "
            "FROM analytics_source_model_stats"
        )
    else:
        source_stats_sql = f"""
            SELECT r.model_id,count(s.run_id)::bigint AS sources,
                   count(DISTINCT NULLIF(s.canonical_url,''))::bigint AS unique_sources
            FROM monitor_runs r
            LEFT JOIN monitor_sources s
              ON s.model_id=r.model_id AND s.run_id=r.run_id
            WHERE {where}
            GROUP BY r.model_id
        """
    query = f"""
        WITH run_stats AS (
            SELECT model_id,
                   count(*)::bigint AS runs,
                   count(DISTINCT question)::bigint AS question_count,
                   count(DISTINCT serial)::bigint AS device_count,
                   count(*) FILTER (
                       WHERE COALESCE(payload->>'brand_analysis_ready','true') <> 'false'
                   )::bigint AS analysis_ready_runs,
                   count(*) FILTER (
                       WHERE COALESCE(payload->>'brand_analysis_ready','true') = 'false'
                   )::bigint AS analysis_pending_runs
            FROM monitor_runs r
            WHERE {where}
            GROUP BY model_id
        ), source_stats AS ({source_stats_sql})
        SELECT r.model_id,r.runs,r.question_count,r.device_count,
               r.analysis_ready_runs,r.analysis_pending_runs,
               COALESCE(s.sources,0)::bigint AS sources,
               COALESCE(s.unique_sources,0)::bigint AS unique_sources
        FROM run_stats r
        LEFT JOIN source_stats s USING(model_id)
    """
    query_params = params if not question else [*params, *params]
    with connection() as conn:
        rows = list(conn.execute(query, query_params))
    numeric_fields = (
        "runs", "question_count", "device_count", "analysis_ready_runs",
        "analysis_pending_runs", "sources", "unique_sources",
    )
    return {
        str(row["model_id"]): {
            field: int(row.get(field) or 0) for field in numeric_fields
        }
        for row in rows
    }


def source_intersection_catalog(*, question: str = "", day: str = "") -> list[dict[str, Any]]:
    """Return one display row per canonical source under intersection filters."""
    ensure_schema()
    clauses = [
        "s.production_valid",
        "s.model_id=ANY(%s::text[])",
        "s.canonical_url<>''",
    ]
    params: list[Any] = [["doubao", "yuanbao", "wenxin"]]
    if question:
        clauses.append("r.question=%s")
        params.append(question)
    if day:
        clauses.append("r.day=%s")
        params.append(day)
    where = " AND ".join(clauses)
    with connection() as conn:
        return list(conn.execute(
            "SELECT DISTINCT ON (s.canonical_url) s.canonical_url,s.url,s.title,"
            "s.media,s.source_type FROM monitor_sources s JOIN monitor_runs r "
            "ON r.model_id=s.model_id AND r.run_id=s.run_id WHERE " + where +
            " ORDER BY s.canonical_url,r.day DESC,r.finished_at DESC,s.source_index DESC",
            params,
        ))


def source_intersection_product_brands(*, question: str = "", day: str = "") -> set[str]:
    """Structured product brands eligible for competitor intersections."""
    ensure_schema()
    clauses = ["r.status='success'", "btrim(p.brand)<>''"]
    params: list[Any] = []
    if question:
        clauses.append("r.question=%s")
        params.append(question)
    if day:
        clauses.append("r.day=%s")
        params.append(day)
    with connection() as conn:
        rows = conn.execute(
            "SELECT DISTINCT p.brand FROM monitor_products p JOIN monitor_runs r "
            "ON r.model_id=p.model_id AND r.run_id=p.run_id WHERE "
            + " AND ".join(clauses), params,
        )
        return {str(row["brand"]).strip() for row in rows if str(row["brand"] or "").strip()}


def source_intersection_citations(
    canonical_urls: Iterable[str], *, question: str = "", day: str = "",
) -> list[dict[str, Any]]:
    """Aggregate per-model run counts for already classified source URLs."""
    ensure_schema()
    urls = sorted({str(item).strip() for item in canonical_urls if str(item).strip()})
    if not urls:
        return []
    clauses = [
        "s.production_valid",
        "s.model_id=ANY(%s::text[])",
        "s.canonical_url=ANY(%s::text[])",
    ]
    params: list[Any] = [["doubao", "yuanbao", "wenxin"], urls]
    if question:
        clauses.append("r.question=%s")
        params.append(question)
    if day:
        clauses.append("r.day=%s")
        params.append(day)
    with connection() as conn:
        return list(conn.execute(
            "SELECT r.day::text AS date,r.question,s.canonical_url,s.model_id,"
            "count(DISTINCT s.run_id)::bigint AS citation_count,max(s.url) AS url,max(s.title) AS title,"
            "max(s.media) AS media,max(s.source_type) AS source_type "
            "FROM monitor_sources s JOIN monitor_runs r "
            "ON r.model_id=s.model_id AND r.run_id=s.run_id WHERE "
            + " AND ".join(clauses) +
            " GROUP BY r.day,r.question,s.canonical_url,s.model_id",
            params,
        ))


def repair_structural_integrity() -> dict[str, Any]:
    """Quarantine provably incomplete production rows without deleting evidence."""
    ensure_schema()
    counts: dict[str, int] = {"empty_answers_quarantined": 0}
    scopes: set[str] = set()
    with connection() as conn:
        rows = conn.execute(
            "SELECT model_id,run_id,day,payload FROM monitor_runs "
            "WHERE status='success' AND btrim(answer)=''"
        ).fetchall()
        with conn.cursor() as cur:
            for row in rows:
                payload = dict(row["payload"] or {})
                payload.update({"status": "invalid", "quality_reason": "empty_answer"})
                cur.execute(
                    "UPDATE monitor_runs SET status='invalid',payload=%s,updated_at=now() "
                    "WHERE model_id=%s AND run_id=%s",
                    (Jsonb(payload), row["model_id"], row["run_id"]),
                )
                cur.execute(
                    "UPDATE monitor_ingest_events SET record=jsonb_set(jsonb_set(record,"
                    "'{status}','\"invalid\"'::jsonb,true),'{quality_reason}',"
                    "'\"empty_answer\"'::jsonb,true),stored_at=now() "
                    "WHERE model_id=%s AND run_id=%s",
                    (row["model_id"], row["run_id"]),
                )
                scopes.update(_scope_keys(row["model_id"], row["day"]))
            if scopes:
                _bump_versions(cur, scopes)
        conn.commit()
    counts["empty_answers_quarantined"] = len(rows)
    return counts


def repair_brand_entities(days: Iterable[str]) -> dict[str, int]:
    """Normalize stored brand children for selected days and invalidate caches."""
    ensure_schema()
    from monitor_core.analytics import _product_fields, canonical_brand_name

    selected_days = sorted({str(item).strip() for item in days if str(item).strip()})
    if not selected_days:
        return {"brands_removed": 0, "brands_renamed": 0,
                "products_updated": 0, "products_removed": 0,
                "sources_updated": 0}
    placeholders = ",".join(["%s"] * len(selected_days))
    counts = {"brands_removed": 0, "brands_renamed": 0,
              "products_updated": 0, "products_removed": 0,
              "sources_updated": 0}
    scopes: set[str] = {"global"}

    def normalize_list(values) -> list[str]:
        return sorted({
            canonical
            for value in values or []
            if (canonical := canonical_brand_name(str(value or "")))
        })

    with connection() as conn:
        brands = list(conn.execute(
            "SELECT b.model_id,b.run_id,b.brand,r.day FROM monitor_brands b "
            "JOIN monitor_runs r ON r.model_id=b.model_id AND r.run_id=b.run_id "
            f"WHERE r.day IN ({placeholders})",
            selected_days,
        ))
        products = list(conn.execute(
            "SELECT p.model_id,p.run_id,p.product_index,p.brand,p.product,p.rank_no,p.payload,r.day "
            "FROM monitor_products p JOIN monitor_runs r "
            "ON r.model_id=p.model_id AND r.run_id=p.run_id "
            f"WHERE r.day IN ({placeholders})",
            selected_days,
        ))
        sources = list(conn.execute(
            "SELECT s.model_id,s.run_id,s.source_index,s.payload,r.day "
            "FROM monitor_sources s JOIN monitor_runs r "
            "ON r.model_id=s.model_id AND r.run_id=s.run_id "
            f"WHERE r.day IN ({placeholders})",
            selected_days,
        ))
        with conn.cursor() as cur:
            for row in brands:
                scopes.update(_scope_keys(row["model_id"], row["day"]))
                original = str(row["brand"] or "")
                canonical = canonical_brand_name(original)
                if canonical == original:
                    continue
                cur.execute(
                    "DELETE FROM monitor_brands WHERE model_id=%s AND run_id=%s AND brand=%s",
                    (row["model_id"], row["run_id"], original),
                )
                if canonical:
                    cur.execute(
                        "INSERT INTO monitor_brands(model_id,run_id,brand) VALUES(%s,%s,%s) "
                        "ON CONFLICT DO NOTHING",
                        (row["model_id"], row["run_id"], canonical),
                    )
                    counts["brands_renamed"] += 1
                else:
                    counts["brands_removed"] += 1
            for row in products:
                scopes.update(_scope_keys(row["model_id"], row["day"]))
                payload = dict(row["payload"] or {})
                canonical, product, rank = _product_fields({
                    **payload,
                    "brand": payload.get("brand") or payload.get("brand_name") or row["brand"],
                    "product_name": payload.get("product_name") or payload.get("name") or row["product"],
                    "rank": payload.get("rank") or payload.get("product_index") or row["rank_no"],
                })
                if not canonical or not product:
                    cur.execute(
                        "DELETE FROM monitor_products WHERE model_id=%s AND run_id=%s AND product_index=%s",
                        (row["model_id"], row["run_id"], row["product_index"]),
                    )
                    counts["products_removed"] += 1
                    continue
                changed = (
                    canonical != str(row["brand"] or "")
                    or product != str(row["product"] or "")
                    or rank != int(row["rank_no"] or 0)
                    or canonical != str(payload.get("brand") or "")
                    or canonical != str(payload.get("brand_name") or "")
                    or product != str(payload.get("product_name") or payload.get("name") or "")
                    or bool(payload.get("brand_identified")) != bool(canonical)
                )
                if not changed:
                    continue
                payload["brand"] = canonical
                payload["brand_name"] = canonical
                payload["brand_identified"] = bool(canonical)
                payload["product_name"] = product
                payload["name"] = product
                payload["rank"] = rank
                cur.execute(
                    "UPDATE monitor_products SET brand=%s,product=%s,rank_no=%s,payload=%s "
                    "WHERE model_id=%s AND run_id=%s AND product_index=%s",
                    (canonical, product, rank, Jsonb(payload), row["model_id"], row["run_id"], row["product_index"]),
                )
                counts["products_updated"] += 1
            for row in sources:
                payload = dict(row["payload"] or {})
                changed = False
                for field in (
                    "brand_mentions", "title_brand_mentions", "body_brand_mentions",
                    "owned_brands",
                ):
                    if field not in payload:
                        continue
                    normalized = normalize_list(payload.get(field))
                    if normalized != list(payload.get(field) or []):
                        payload[field] = normalized
                        changed = True
                if not changed:
                    continue
                cur.execute(
                    "UPDATE monitor_sources SET payload=%s "
                    "WHERE model_id=%s AND run_id=%s AND source_index=%s",
                    (Jsonb(payload), row["model_id"], row["run_id"], row["source_index"]),
                )
                counts["sources_updated"] += 1
            cur.execute("DELETE FROM analytics_cache")
            _bump_versions(cur, scopes)
        conn.commit()
    return counts


def repair_source_identities(days: Iterable[str]) -> dict[str, int]:
    """Normalize HTTP/HTTPS/www aliases in derived source rows.

    Raw ingest events remain untouched for auditability.  Only the query-ready
    child table is deduplicated, then its maintained aggregate tables and cache
    versions are rebuilt in the same transaction.
    """
    ensure_schema()
    from monitor_core.analytics import canonical_url as normalize_canonical_url

    selected_days = sorted({str(item).strip() for item in days if str(item).strip()})
    if not selected_days:
        return {"urls_updated": 0, "duplicates_removed": 0, "runs_updated": 0}
    placeholders = ",".join(["%s"] * len(selected_days))
    scopes: set[str] = {"global"}
    with connection() as conn:
        distinct_urls = [
            str(row["canonical_url"] or "")
            for row in conn.execute(
                "SELECT DISTINCT s.canonical_url FROM monitor_sources s "
                "JOIN monitor_runs r ON r.model_id=s.model_id AND r.run_id=s.run_id "
                f"WHERE r.day IN ({placeholders}) AND s.canonical_url<>''",
                selected_days,
            )
        ]
        mapping = [
            (old, new)
            for old in distinct_urls
            if (new := normalize_canonical_url(old)) and new != old
        ]
        if not mapping:
            return {"urls_updated": 0, "duplicates_removed": 0, "runs_updated": 0}
        with conn.cursor() as cur:
            cur.execute("CREATE TEMP TABLE source_identity_map(old_url text PRIMARY KEY,new_url text NOT NULL) ON COMMIT DROP")
            cur.executemany(
                "INSERT INTO source_identity_map(old_url,new_url) VALUES(%s,%s)", mapping,
            )
            cur.execute(
                "CREATE TEMP TABLE affected_source_runs ON COMMIT DROP AS "
                "SELECT DISTINCT s.model_id,s.run_id,r.day FROM monitor_sources s "
                "JOIN monitor_runs r ON r.model_id=s.model_id AND r.run_id=s.run_id "
                "JOIN source_identity_map m ON m.old_url=s.canonical_url "
                f"WHERE r.day IN ({placeholders})",
                selected_days,
            )
            affected = list(cur.execute("SELECT model_id,run_id,day FROM affected_source_runs"))
            for row in affected:
                scopes.update(_scope_keys(row["model_id"], row["day"]))
            removed = cur.execute(
                "WITH ranked AS ("
                " SELECT s.model_id,s.run_id,s.source_index,"
                " row_number() OVER (PARTITION BY s.model_id,s.run_id,"
                " COALESCE(m.new_url,s.canonical_url) ORDER BY "
                " CASE WHEN btrim(s.title)<>'' AND s.title<>s.url AND s.title<>s.canonical_url THEN 0 ELSE 1 END,"
                " s.source_index) AS row_no"
                " FROM monitor_sources s JOIN affected_source_runs a"
                " ON a.model_id=s.model_id AND a.run_id=s.run_id"
                " LEFT JOIN source_identity_map m ON m.old_url=s.canonical_url"
                ") DELETE FROM monitor_sources s USING ranked r"
                " WHERE s.model_id=r.model_id AND s.run_id=r.run_id"
                " AND s.source_index=r.source_index AND r.row_no>1",
            ).rowcount
            updated = cur.execute(
                "UPDATE monitor_sources s SET canonical_url=m.new_url,"
                "payload=jsonb_set(s.payload,'{canonical_url}',to_jsonb(m.new_url),true) "
                "FROM source_identity_map m,affected_source_runs a "
                "WHERE s.canonical_url=m.old_url AND a.model_id=s.model_id AND a.run_id=s.run_id",
            ).rowcount
            runs_updated = cur.execute(
                "UPDATE monitor_runs r SET payload=jsonb_set(r.payload,'{expected_source_count}',"
                "to_jsonb((SELECT count(*)::int FROM monitor_sources s "
                "WHERE s.model_id=r.model_id AND s.run_id=r.run_id)),true),updated_at=now() "
                "FROM affected_source_runs a WHERE a.model_id=r.model_id AND a.run_id=r.run_id",
            ).rowcount
            # Canonical URL updates do not fire the insert/delete-only source
            # aggregate trigger, so rebuild the small maintained summaries.
            cur.execute("TRUNCATE analytics_source_url_refcounts,analytics_source_model_stats")
            cur.execute(
                "INSERT INTO analytics_source_url_refcounts(model_id,canonical_url,refs) "
                "SELECT s.model_id,s.canonical_url,count(*) FROM monitor_sources s "
                "JOIN monitor_runs r ON r.model_id=s.model_id AND r.run_id=s.run_id "
                "WHERE s.canonical_url<>'' AND r.status='success' "
                "AND COALESCE(r.payload->>'body_capture_complete','true')<>'false' "
                "AND COALESCE(r.payload->>'source_capture_complete','true')<>'false' "
                "GROUP BY s.model_id,s.canonical_url"
            )
            cur.execute(
                "INSERT INTO analytics_source_model_stats(model_id,source_refs,unique_sources) "
                "SELECT model_id,sum(refs),count(*) FROM analytics_source_url_refcounts GROUP BY model_id"
            )
            cur.execute("DELETE FROM analytics_cache")
            _bump_versions(cur, scopes)
        conn.commit()
    return {
        "urls_updated": int(updated or 0),
        "duplicates_removed": int(removed or 0),
        "runs_updated": int(runs_updated or 0),
    }


def global_version() -> int:
    if not enabled(): return 0
    ensure_schema()
    with connection() as conn:
        row = conn.execute("SELECT version FROM analytics_versions WHERE scope_key='global'").fetchone()
        return int(row["version"] if row else 0)


def scope_version(cache_key: tuple[str, str, str, str]) -> tuple[str, int]:
    """Return the narrow analytics version for one dashboard selection."""
    if not enabled():
        return "global", 0
    ensure_schema()
    scope = cache_scope(cache_key)
    with connection() as conn:
        return scope, _scope_version(conn, scope)


def cache_scope(cache_key: tuple[str, str, str, str]) -> str:
    _question, day_value, model_id, _view = cache_key
    if day_value and model_id: return f"model_day:{model_id}:{day_value}"
    if day_value: return f"day:{day_value}"
    if model_id: return f"model:{model_id}"
    return "global"


def _scope_version(conn, scope: str) -> int:
    row = conn.execute("SELECT version FROM analytics_versions WHERE scope_key=%s", (scope,)).fetchone()
    return int(row["version"] if row else 0)


def cache_get(cache_key: tuple[str, str, str, str], content_token: str) -> dict[str, Any] | None:
    if not enabled(): return None
    ensure_schema()
    encoded = json.dumps(cache_key, ensure_ascii=False, separators=(",", ":"))
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    scope = cache_scope(cache_key)
    with connection() as conn:
        version = _scope_version(conn, scope)
        row = conn.execute(
            "SELECT payload FROM analytics_cache WHERE cache_key=%s AND scope_key=%s "
            "AND scope_version=%s AND content_token=%s",
            (digest, scope, version, content_token),
        ).fetchone()
        return dict(row["payload"]) if row else None


def cache_put(cache_key: tuple[str, str, str, str], content_token: str,
              payload: dict[str, Any], expected_scope_version: int | None = None) -> bool:
    if not enabled(): return False
    ensure_schema()
    encoded = json.dumps(cache_key, ensure_ascii=False, separators=(",", ":"))
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    scope = cache_scope(cache_key)
    with connection() as conn:
        version = _scope_version(conn, scope)
        # An analytics build can overlap a live callback. Never label the old
        # snapshot with the newer scope version, otherwise an incomplete result
        # remains a valid cache hit until the next callback.
        if expected_scope_version is not None and version != expected_scope_version:
            return False
        conn.execute(
            "INSERT INTO analytics_cache(cache_key,scope_key,scope_version,content_token,payload) VALUES(%s,%s,%s,%s,%s) "
            "ON CONFLICT(cache_key) DO UPDATE SET scope_key=excluded.scope_key,scope_version=excluded.scope_version,"
            "content_token=excluded.content_token,payload=excluded.payload,created_at=now(),hit_count=0",
            (digest, scope, version, content_token, Jsonb(payload)),
        )
        conn.commit()
    return True


def store_ingest_event(model_id: str, request_id: str, record: dict[str, Any],
                       envelope: dict[str, Any] | None = None) -> None:
    """Persist the complete remote response independently from analytics normalization."""
    store_ingest_events([(model_id, request_id, record, envelope or {})])


def store_ingested_run(model_id: str, request_id: str, record: dict[str, Any],
                       envelope: dict[str, Any], run: dict[str, Any]) -> dict[str, Any]:
    """Atomically persist one callback and its query-ready analytics rows.

    The event key makes remote retries idempotent.  A sequence is allocated only
    for a genuinely new callback, and the remote response may be acknowledged
    only after this transaction commits.
    """
    if not enabled():
        raise RuntimeError("PostgreSQL is not configured")
    ensure_schema()
    request_id = str(request_id or "").strip()
    if not request_id:
        raise ValueError("request_id is required")
    received = record.get("remote_received_at") or envelope.get("received_at")
    if isinstance(received, (int, float)):
        received_value = datetime.fromtimestamp(received, timezone.utc)
    else:
        received_value = received or None
    with connection() as conn:
        with conn.cursor() as cur:
            # Serialize sequence allocation per model without blocking the
            # other collectors.
            cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (f"monitor:{model_id}",))
            old = cur.execute(
                "SELECT run_id FROM monitor_ingest_events WHERE model_id=%s AND request_id=%s",
                (model_id, request_id),
            ).fetchone()
            old_run_id = str(old["run_id"] or "") if old else ""
            normalized = dict(run)
            # Both the Baidu AI search card and its dedicated Wenxin fallback
            # are supported Wenxin collection surfaces.  Classify by capture
            # completeness, not by which of those two surfaces supplied it.
            # Zero-link and partial rounds remain raw audit events only.
            quarantine_reason = capture_quarantine_reason(model_id, normalized)
            if not old_run_id and quarantine_reason:
                cur.execute(
                    "INSERT INTO monitor_ingest_events(model_id,request_id,source_device,received_at,envelope,record,run_id) "
                    "VALUES(%s,%s,%s,%s,%s,%s,'') ON CONFLICT(model_id,request_id) DO UPDATE SET "
                    "source_device=excluded.source_device,received_at=excluded.received_at,envelope=excluded.envelope,"
                    "record=excluded.record,run_id='',stored_at=now()",
                    (model_id, request_id,
                     str(envelope.get("source_device") or record.get("remote_source_device") or ""),
                     received_value, Jsonb(envelope), Jsonb(record)),
                )
                conn.commit()
                return {
                    "model_id": model_id, "run_id": "", "quarantined": True,
                    "quarantine_reason": quarantine_reason,
                    "sources": 0, "products": 0,
                }
            # Idempotency is intentionally scoped to the exact request id above.
            # Different task/round callbacks remain independent observations even
            # when Baidu deterministically returns identical answer text.
            if old_run_id:
                run_id = old_run_id
                previous = cur.execute(
                    "SELECT sequence_no,day FROM monitor_runs WHERE model_id=%s AND run_id=%s",
                    (model_id, run_id),
                ).fetchone()
                sequence = int(previous["sequence_no"]) if previous else int(normalized.get("sequence") or 0)
                old_day = previous["day"] if previous else None
            else:
                run_id = str(normalized.get("run_id") or f"{model_id}-{request_id[:20]}")
                previous = cur.execute(
                    "SELECT sequence_no,day FROM monitor_runs WHERE model_id=%s AND run_id=%s",
                    (model_id, run_id),
                ).fetchone()
                if previous:
                    sequence = int(previous["sequence_no"])
                    old_day = previous["day"]
                else:
                    row = cur.execute(
                        "SELECT COALESCE(max(sequence_no),0)+1 AS next_sequence FROM monitor_runs WHERE model_id=%s",
                        (model_id,),
                    ).fetchone()
                    sequence = int(row["next_sequence"])
                    old_day = None
            normalized.update({
                "model_id": model_id, "run_id": run_id, "sequence": sequence,
                "remote_request_id": request_id,
            })
            sources = list(normalized.pop("sources", []) or [])
            brands = list(normalized.pop("brands", []) or [])
            products = list(normalized.pop("products", []) or [])
            identity = {**normalized, "sources": sources, "brands": brands, "products": products}
            record_hash = _json_hash(identity)
            cur.execute(
                "INSERT INTO monitor_ingest_events(model_id,request_id,source_device,received_at,envelope,record,run_id) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(model_id,request_id) DO UPDATE SET "
                "source_device=excluded.source_device,received_at=excluded.received_at,envelope=excluded.envelope,"
                "record=excluded.record,run_id=excluded.run_id,stored_at=now()",
                (model_id, request_id,
                 str(envelope.get("source_device") or record.get("remote_source_device") or ""),
                 received_value, Jsonb(envelope), Jsonb(record), run_id),
            )
            cur.execute("DELETE FROM monitor_runs WHERE model_id=%s AND run_id=%s", (model_id, run_id))
            cur.execute(
                "INSERT INTO monitor_runs(model_id,run_id,sequence_no,question,finished_at,day,serial,answer,status,record_hash,payload) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (model_id, run_id, sequence, str(normalized.get("question") or ""),
                 normalized.get("finished_at") or None, normalized.get("day") or None,
                 str(normalized.get("serial") or ""), str(normalized.get("answer") or ""),
                 str(normalized.get("status") or "success"), record_hash, Jsonb(normalized)),
            )
            _write_children(cur, model_id, run_id, sources, brands, products)
            scopes = set(_scope_keys(model_id, old_day))
            scopes.update(_scope_keys(model_id, normalized.get("day")))
            _bump_versions(cur, scopes)
        conn.commit()
    return {"model_id": model_id, "run_id": run_id, "sequence": sequence,
            "sources": len(sources), "products": len(products)}


def store_ingest_events(events: Iterable[tuple[str, str, dict[str, Any], dict[str, Any]]]) -> None:
    if not enabled():
        return
    ensure_schema()
    rows = []
    for model_id, request_id, record, envelope in events:
        received = record.get("remote_received_at") or envelope.get("received_at")
        if isinstance(received, (int, float)):
            received_value = datetime.fromtimestamp(received, timezone.utc)
        else:
            received_value = received or None
        rows.append((model_id, request_id,
                     str(envelope.get("source_device") or record.get("remote_source_device") or ""),
                     received_value, Jsonb(envelope), Jsonb(record)))
    if not rows:
        return
    with connection() as conn:
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO monitor_ingest_events(model_id,request_id,source_device,received_at,envelope,record) "
                "VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT(model_id,request_id) DO UPDATE SET "
                "source_device=excluded.source_device,received_at=excluded.received_at,envelope=excluded.envelope,"
                "record=excluded.record,stored_at=now()",
                rows,
            )
        conn.commit()


def pending_product_runs(limit: int = 20, max_retries: int = 3,
                         days: list[str] | None = None) -> list[dict[str, Any]]:
    """Return recommendation answers awaiting model review, oldest first."""
    ensure_schema()
    with connection() as conn:
        rows = conn.execute(
            "WITH ranked AS ("
            "SELECT model_id,run_id,question,answer,payload,day,updated_at,finished_at,sequence_no,"
            "product_ai_retry_count,"
            "CASE WHEN day >= (now() AT TIME ZONE 'Asia/Shanghai')::date - 1 THEN 0 ELSE 1 END AS recent_priority,"
            "row_number() OVER (PARTITION BY model_id,CASE WHEN day >= (now() AT TIME ZONE 'Asia/Shanghai')::date - 1 "
            "THEN day ELSE DATE '1900-01-01' END ORDER BY updated_at,finished_at NULLS LAST,sequence_no) AS model_rank "
            "FROM monitor_runs WHERE COALESCE(payload->>'product_review_status','')='ai_pending' "
            "AND product_ai_retry_count < %s "
            "AND (%s::date[] IS NULL OR day = ANY(%s::date[])) "
            "AND (product_ai_next_retry_at IS NULL OR product_ai_next_retry_at <= now())) "
            "SELECT model_id,run_id,question,answer,payload,product_ai_retry_count FROM ranked "
            "ORDER BY recent_priority,model_rank,day DESC,model_id,updated_at,finished_at NULLS LAST,sequence_no LIMIT %s",
            (max(1, min(int(max_retries), 20)), days or None, days or None,
             max(1, min(int(limit), 100))),
        ).fetchall()
        return [{**dict(row["payload"] or {}), "model_id": row["model_id"],
                 "run_id": row["run_id"], "question": row["question"],
                 "answer": row["answer"],
                 "_product_ai_retry_count": int(row["product_ai_retry_count"] or 0)}
                for row in rows]


def verified_product_runs() -> list[dict[str, Any]]:
    """Return only directly paid-and-validated examples safe for exact reuse."""
    ensure_schema()
    with connection() as conn:
        rows = conn.execute(
            "SELECT r.question,r.answer,COALESCE((SELECT jsonb_agg(p.payload ORDER BY p.product_index) "
            "FROM monitor_products p WHERE p.model_id=r.model_id AND p.run_id=r.run_id),'[]'::jsonb) AS products "
            "FROM monitor_runs r WHERE COALESCE(r.payload->>'product_review_status','')='ai_verified' "
            "AND COALESCE(r.payload->>'product_extraction_method','') "
            "IN ('anthropic','llm_batch_audited','llm_batch_grounded')"
        ).fetchall()
    return [
        {"question": row["question"], "answer": row["answer"],
         "products": list(row["products"] or [])}
        for row in rows
    ]


def defer_product_analysis(model_id: str, run_id: str, base_delay_seconds: int = 900) -> int:
    """Persist an exponential retry cooldown and return the new attempt count."""
    ensure_schema()
    with connection() as conn:
        row = conn.execute(
            "UPDATE monitor_runs SET product_ai_retry_count=product_ai_retry_count+1, "
            "product_ai_next_retry_at=now() + make_interval(secs => LEAST(%s * "
            "power(4, product_ai_retry_count)::integer, 86400)), updated_at=now() "
            "WHERE model_id=%s AND run_id=%s "
            "AND COALESCE(payload->>'product_review_status','')='ai_pending' "
            "RETURNING product_ai_retry_count",
            (max(60, min(int(base_delay_seconds), 86400)), model_id, run_id),
        ).fetchone()
        conn.commit()
        return int(row["product_ai_retry_count"]) if row else 0


def update_product_analysis(model_id: str, run_id: str, products: list[dict[str, Any]],
                            review_status: str, analysis_model: str,
                            method: str) -> None:
    """Replace product analysis for one run without touching files."""
    ensure_schema()
    with connection() as conn:
        with conn.cursor() as cur:
            row = cur.execute(
                "SELECT payload,day,question,answer FROM monitor_runs "
                "WHERE model_id=%s AND run_id=%s FOR UPDATE",
                (model_id, run_id),
            ).fetchone()
            if not row:
                raise KeyError(f"missing run {model_id}/{run_id}")
            payload = dict(row["payload"] or {})
            from monitor_core.product_analysis import merge_explicit_owned_products
            products = merge_explicit_owned_products(
                str(row["answer"] or ""),
                str(row["question"] or ""),
                products,
            )
            payload.update({"product_review_status": review_status,
                            "product_analysis_model": analysis_model,
                            "product_extraction_method": method,
                            "products": products})
            normalized_products = [dict(item) for item in products if isinstance(item, dict)]
            brands = sorted({str(item.get("brand") or item.get("brand_name") or "").strip()
                             for item in normalized_products
                             if str(item.get("brand") or item.get("brand_name") or "").strip()})
            record_hash = _json_hash({**payload, "products": normalized_products, "brands": brands})
            cur.execute("DELETE FROM monitor_products WHERE model_id=%s AND run_id=%s", (model_id, run_id))
            cur.execute("DELETE FROM monitor_brands WHERE model_id=%s AND run_id=%s", (model_id, run_id))
            _write_children(cur, model_id, run_id, [], brands, normalized_products)
            cur.execute(
                "UPDATE monitor_runs SET payload=%s,record_hash=%s,product_ai_retry_count=0,"
                "product_ai_next_retry_at=NULL,updated_at=now() "
                "WHERE model_id=%s AND run_id=%s",
                (Jsonb(payload), record_hash, model_id, run_id),
            )
            cur.execute(
                "UPDATE monitor_ingest_events SET record=jsonb_set(jsonb_set(jsonb_set(record,"
                "'{products}',%s::jsonb,true),'{product_review_status}',%s::jsonb,true),"
                "'{product_analysis_model}',%s::jsonb,true),stored_at=now() "
                "WHERE model_id=%s AND run_id=%s",
                (json.dumps(normalized_products, ensure_ascii=False), json.dumps(review_status),
                 json.dumps(analysis_model), model_id, run_id),
            )
            _bump_versions(cur, _scope_keys(model_id, row["day"]))
        conn.commit()


def invalidate_run(model_id: str, run_id: str, reason: str) -> bool:
    """Exclude a proven bad capture while preserving its audit evidence."""
    ensure_schema()
    with connection() as conn:
        with conn.cursor() as cur:
            row = cur.execute(
                "SELECT payload,day FROM monitor_runs WHERE model_id=%s AND run_id=%s FOR UPDATE",
                (model_id, run_id),
            ).fetchone()
            if not row:
                return False
            payload = dict(row["payload"] or {})
            payload.update({"status": "invalid", "quality_reason": str(reason or "invalid capture")})
            cur.execute(
                "UPDATE monitor_runs SET status='invalid',payload=%s,updated_at=now() "
                "WHERE model_id=%s AND run_id=%s",
                (Jsonb(payload), model_id, run_id),
            )
            cur.execute(
                "UPDATE monitor_ingest_events SET record=jsonb_set(jsonb_set(record,"
                "'{status}','\"invalid\"'::jsonb,true),'{quality_reason}',%s::jsonb,true),stored_at=now() "
                "WHERE model_id=%s AND run_id=%s",
                (json.dumps(str(reason or "invalid capture"), ensure_ascii=False), model_id, run_id),
            )
            _bump_versions(cur, _scope_keys(model_id, row["day"]))
        conn.commit()
    return True


def reset_product_analysis(model_id: str, run_id: str, reason: str = "") -> bool:
    """Return a questionable product result to the paid-analysis queue."""
    ensure_schema()
    with connection() as conn:
        with conn.cursor() as cur:
            row = cur.execute(
                "SELECT payload,day FROM monitor_runs WHERE model_id=%s AND run_id=%s FOR UPDATE",
                (model_id, run_id),
            ).fetchone()
            if not row:
                return False
            payload = dict(row["payload"] or {})
            payload.update({
                "product_review_status": "ai_pending",
                "product_analysis_model": "",
                "product_extraction_method": "",
                "product_review_reason": str(reason or "reanalysis requested"),
                "products": [],
            })
            cur.execute("DELETE FROM monitor_products WHERE model_id=%s AND run_id=%s", (model_id, run_id))
            cur.execute("DELETE FROM monitor_brands WHERE model_id=%s AND run_id=%s", (model_id, run_id))
            cur.execute(
                "UPDATE monitor_runs SET payload=%s,product_ai_retry_count=0,"
                "product_ai_next_retry_at=NULL,updated_at=now() WHERE model_id=%s AND run_id=%s",
                (Jsonb(payload), model_id, run_id),
            )
            cur.execute(
                "UPDATE monitor_ingest_events SET record=jsonb_set(jsonb_set(record,"
                "'{products}','[]'::jsonb,true),'{product_review_status}',"
                "'\"ai_pending\"'::jsonb,true),stored_at=now() WHERE model_id=%s AND run_id=%s",
                (model_id, run_id),
            )
            _bump_versions(cur, _scope_keys(model_id, row["day"]))
        conn.commit()
    return True


def stats() -> dict[str, Any]:
    ensure_schema()
    with connection() as conn:
        counts = conn.execute(
            "SELECT (SELECT count(*) FROM monitor_runs) runs,"
            "(SELECT count(*) FROM monitor_sources) sources,"
            "(SELECT count(*) FROM monitor_products) products,"
            "(SELECT count(*) FROM analytics_cache) cache_entries,"
            "(SELECT count(*) FROM monitor_ingest_events) ingest_events"
        ).fetchone()
        size = conn.execute("SELECT pg_database_size(current_database()) size").fetchone()["size"]
        row = conn.execute("SELECT version FROM analytics_versions WHERE scope_key='global'").fetchone()
        return {**counts, "database_bytes": int(size), "version": int(row["version"] if row else 0)}
