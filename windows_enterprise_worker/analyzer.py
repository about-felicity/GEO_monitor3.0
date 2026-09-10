from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from windows_worker_sdk.contracts import AnalysisResult, CapturedAnswer


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_KEY_FILE = ROOT.parent / "ds_apikey.txt"


def _clean_terms(*values: str) -> list[str]:
    output: list[str] = []
    for raw in values:
        value = re.sub(r"\s+", "", str(raw or "")).casefold()
        if value and value not in output:
            output.append(value)
    return output


def _plausible_commercial_brand(value: str) -> bool:
    name = " ".join(str(value or "").split()).strip()
    compact = re.sub(r"\s+", "", name).casefold()
    if len(compact) < 2 or len(compact) > 100:
        return False
    if re.search(r"(?:经营部|专卖店|旗舰店|店铺|自营店|经销处|销售部)$", compact):
        return False
    if re.fullmatch(r"[a-z0-9²+./_-]{2,18}", compact, re.I):
        return False
    if any(marker in compact for marker in (
        "有限公司", "股份公司", "股份有限公司", "集团", "科技", "环保",
        "水务", "膜业", "环境", "装备", "公司",
    )):
        return True
    if re.search(
        r"(?:工艺|格栅|气浮机|沉淀池|加药系统|氧化池|催化氧化|"
        r"反应器|压滤机|脱水机|蒸发器|发生器|处理设备|处理装置|泵站)$",
        compact,
    ):
        return False
    return compact not in {"芬顿", "板框", "叠螺", "膜生物反应器", "移动床生物膜反应器"}


class DeepSeekAnalyzer:
    """Bounded DeepSeek analysis with a deterministic evidence safety net."""

    def __init__(self) -> None:
        key_file = Path(os.environ.get("GEO_DEEPSEEK_KEY_FILE", DEFAULT_KEY_FILE))
        self.api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
        if not self.api_key and key_file.is_file():
            self.api_key = key_file.read_text(encoding="utf-8-sig").strip()
        if not self.api_key:
            raise RuntimeError("DeepSeek API key is not configured")
        self.endpoint = os.environ.get(
            "GEO_DEEPSEEK_URL", "https://api.deepseek.com/chat/completions"
        ).strip()
        self.model = os.environ.get("GEO_DEEPSEEK_MODEL", "deepseek-v4-flash").strip()
        self.timeout = max(10, min(120, int(os.environ.get("GEO_ANALYSIS_TIMEOUT", "45"))))

    @staticmethod
    def _evidence(body: str, brand_name: str, product_name: str) -> tuple[bool, list[str]]:
        compact = re.sub(r"\s+", "", body).casefold()
        terms = _clean_terms(brand_name, product_name)
        matched = [term for term in terms if term in compact]
        return bool(matched), matched

    def _request(self, payload: dict[str, Any]) -> dict[str, Any]:
        request = urllib.request.Request(
            self.endpoint,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json; charset=utf-8",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            value = json.loads(response.read().decode("utf-8"))
        content = value["choices"][0]["message"]["content"]
        if isinstance(content, str):
            return json.loads(content)
        if isinstance(content, dict):
            return content
        raise ValueError("DeepSeek returned a non-JSON analysis")

    def analyze(
        self,
        model_id: str,
        question: str,
        brand_name: str,
        product_name: str,
        captured: CapturedAnswer,
    ) -> AnalysisResult:
        evidence, literal_matches = self._evidence(captured.body, brand_name, product_name)
        system = (
            "You extract complete GEO recommendation and competitor evidence from a model answer. "
            "Return one JSON object only. Enumerate every explicitly named commercial brand, company, "
            "manufacturer, service provider, and named competing product in the answer, "
            "not only the target brand. Different answers may contain different competitors, so do not "
            "omit a named competitor merely because it appears once. Exclude technologies, process names, "
            "equipment categories, chemicals, standards, model codes, marketplace storefronts, generic "
            "products, locations, and descriptive phrases that are not commercial brand names. "
            "Use one concise canonical brand name for aliases, while preserving the named product/store "
            "in products. Each products entry must describe one explicitly named brand or product and "
            "recommended indicates whether that item is positively recommended in this answer. "
            "Never infer a recommendation without literal evidence in the answer. Schema: "
            '{"recommended":boolean,"rank":integer|null,"matched_terms":[string],'
            '"products":[{"name":string,"brand":string,"recommended":boolean,"rank":integer|null}],'
            '"brands":[string],"sentiment":"positive|neutral|negative|unknown","summary":string}. '
            "brands must contain all unique canonical commercial brand names mentioned in the answer. "
            "Rank is the explicit recommendation/list order only, otherwise null. "
            "The summary must be concise Simplified Chinese."
        )
        user = json.dumps(
            {
                "model": model_id,
                "question": question,
                "target_brand": brand_name,
                "target_product": product_name,
                "literal_target_evidence": evidence,
                "answer": captured.body,
            },
            ensure_ascii=False,
        )
        payload = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "response_format": {"type": "json_object"},
            "thinking": {"type": "disabled"},
            "temperature": 0,
            "max_tokens": 2400,
        }
        parsed: dict[str, Any] = {}
        error = ""
        for attempt in range(3):
            try:
                parsed = self._request(payload)
                break
            except (OSError, ValueError, KeyError, IndexError, json.JSONDecodeError, urllib.error.URLError) as exc:
                error = f"{type(exc).__name__}: {exc}"[:300]
                if attempt < 2:
                    time.sleep(1.5 * (attempt + 1))

        # The model may enrich the record, but literal source text remains the
        # authority for whether this particular target was actually mentioned.
        ai_matched = [str(item).strip() for item in parsed.get("matched_terms") or [] if str(item).strip()]
        matched = list(dict.fromkeys(literal_matches + ai_matched)) if evidence else []
        semantic_recommended = bool(evidence and parsed.get("recommended") is True)
        rank_raw = parsed.get("rank") if semantic_recommended else None
        try:
            rank = int(rank_raw) if rank_raw is not None else None
            if rank is not None and rank < 1:
                rank = None
        except (TypeError, ValueError):
            rank = None
        products = [
            dict(item) for item in parsed.get("products") or []
            if isinstance(item, dict) and _plausible_commercial_brand(
                str(item.get("brand") or item.get("brand_name") or "")
            )
        ]
        brands = [
            str(item).strip() for item in parsed.get("brands") or []
            if str(item).strip() and _plausible_commercial_brand(str(item))
        ]
        details = {
            "provider": "deepseek",
            "model": self.model,
            "sentiment": str(parsed.get("sentiment") or "unknown"),
            "summary": str(parsed.get("summary") or "")[:2000],
            "literal_evidence_enforced": True,
        }
        if error and not parsed:
            details["fallback"] = "literal_evidence"
            details["provider_error"] = error
        return AnalysisResult(
            # Literal occurrence is mandatory, but occurrence alone is not a
            # recommendation. Preserve the analyzer's explicit semantic
            # judgment so neutral mentions never inflate target probability.
            recommended=semantic_recommended,
            rank=rank,
            matched_terms=matched,
            products=products,
            brands=brands,
            details=details,
        )


def create_analyzer() -> DeepSeekAnalyzer:
    return DeepSeekAnalyzer()
