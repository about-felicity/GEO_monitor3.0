from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
from typing import Any


BASE_DIR = Path(__file__).resolve().parent
SETTINGS_PATH = BASE_DIR / "doubao_brand_settings.json"

DEFAULT_OWNED_BRANDS = (
    {"name": "梵玢 FBCY", "aliases": ["梵玢 FBCY", "梵玢", "FBCY"]},
    {"name": "科熙本", "aliases": ["科熙本"]},
    {"name": "姿生怡", "aliases": ["姿生怡"]},
    {"name": "道和", "aliases": ["道和"]},
    {"name": "焕颜计", "aliases": ["焕颜计"]},
    {"name": "茗媛萃", "aliases": ["茗媛萃"]},
)


def _normalize_item(value: Any) -> dict[str, Any] | None:
    if isinstance(value, str):
        parts = [part.strip() for part in value.split("|") if part.strip()]
        if not parts:
            return None
        return {"name": parts[0], "aliases": list(dict.fromkeys(parts))}
    if not isinstance(value, dict):
        return None
    name = str(value.get("name") or "").strip()
    if not name:
        return None
    aliases = [name]
    raw_aliases = value.get("aliases")
    if isinstance(raw_aliases, str):
        raw_aliases = raw_aliases.replace("，", ",").split(",")
    if isinstance(raw_aliases, (list, tuple)):
        aliases.extend(str(item or "").strip() for item in raw_aliases)
    aliases = list(dict.fromkeys(item for item in aliases if item))
    return {"name": name, "aliases": aliases}


def _normalize_group(values: Any) -> list[dict[str, Any]]:
    result = []
    seen = set()
    for value in values if isinstance(values, (list, tuple)) else ():
        item = _normalize_item(value)
        if not item:
            continue
        key = item["name"].casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def default_settings() -> dict[str, Any]:
    return {
        "version": 1,
        "owned_brands": [dict(item) for item in DEFAULT_OWNED_BRANDS],
        "competitor_brands": [],
    }


def normalize_settings(data: Any) -> dict[str, Any]:
    source = data if isinstance(data, dict) else {}
    owned = _normalize_group(source.get("owned_brands"))
    competitors = _normalize_group(source.get("competitor_brands"))
    if not owned and not SETTINGS_PATH.exists():
        owned = _normalize_group(DEFAULT_OWNED_BRANDS)
    owned_names = {item["name"].casefold() for item in owned}
    competitors = [
        item for item in competitors
        if item["name"].casefold() not in owned_names
    ]
    return {
        "version": 1,
        "owned_brands": owned,
        "competitor_brands": competitors,
    }


def load_settings() -> dict[str, Any]:
    if not SETTINGS_PATH.exists():
        return default_settings()
    try:
        return normalize_settings(
            json.loads(SETTINGS_PATH.read_text(encoding="utf-8-sig"))
        )
    except Exception:
        return default_settings()


def save_settings(data: Any) -> dict[str, Any]:
    normalized = normalize_settings(data)
    fd, temporary_name = tempfile.mkstemp(
        prefix="doubao_brand_settings_",
        suffix=".json",
        dir=BASE_DIR,
    )
    os.close(fd)
    try:
        Path(temporary_name).write_text(
            json.dumps(normalized, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(temporary_name, SETTINGS_PATH)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)
    return normalized


def parse_editor_text(text: str) -> list[dict[str, Any]]:
    values = []
    for raw_line in str(text or "").replace("；", "\n").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        values.append(line)
    return _normalize_group(values)


def editor_text(values: Any) -> str:
    lines = []
    for item in _normalize_group(values):
        aliases = [
            alias for alias in item["aliases"]
            if alias.casefold() != item["name"].casefold()
        ]
        lines.append("|".join([item["name"], *aliases]))
    return "\n".join(lines)


def group_map(settings: Any | None = None) -> dict[str, str]:
    data = normalize_settings(settings) if settings is not None else load_settings()
    result = {}
    for group, key in (("owned", "owned_brands"), ("competitor", "competitor_brands")):
        for item in data[key]:
            result[item["name"]] = group
    return result


def vocabulary(settings: Any | None = None) -> list[dict[str, Any]]:
    data = normalize_settings(settings) if settings is not None else load_settings()
    result = []
    for group, key in (("owned", "owned_brands"), ("competitor", "competitor_brands")):
        for item in data[key]:
            result.append({**item, "group": group})
    return result


def aliases_for_brand(name: str, settings: Any | None = None) -> list[str]:
    folded = str(name or "").strip().casefold()
    for item in vocabulary(settings):
        if item["name"].casefold() == folded:
            return list(item["aliases"])
    return [str(name or "").strip()] if str(name or "").strip() else []
