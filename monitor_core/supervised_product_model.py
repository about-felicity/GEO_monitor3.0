"""Training-data and inference helpers for grounded product span extraction.

The model never generates text.  It labels character spans that already exist
in an answer, and downstream product normalization remains deterministic.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Iterable


INVISIBLE_RE = re.compile(r"[\u200b-\u200f\u2060\ufeff]")


def split_bucket(question: str, answer: str) -> str:
    """Group equal answers into the same deterministic 80/10/10 split."""
    normalized = re.sub(r"\s+", "", unicodedata.normalize("NFKC", question + "\n" + answer)).casefold()
    value = int(hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:8], 16) % 100
    return "train" if value < 80 else "validation" if value < 90 else "test"


def _fold_with_offsets(text: str) -> tuple[str, list[int]]:
    folded: list[str] = []
    offsets: list[int] = []
    for index, character in enumerate(text):
        value = INVISIBLE_RE.sub("", unicodedata.normalize("NFKC", character)).casefold()
        for item in value:
            if item.isspace():
                continue
            folded.append(item)
            offsets.append(index)
    return "".join(folded), offsets


def grounded_span(text: str, needle: str) -> tuple[int, int] | None:
    """Locate a verified phrase despite harmless width/space differences."""
    needle = str(needle or "").strip()
    if not needle:
        return None
    exact = text.find(needle)
    if exact >= 0:
        return exact, exact + len(needle)
    folded_text, offsets = _fold_with_offsets(text)
    folded_needle, _ = _fold_with_offsets(needle)
    if not folded_needle:
        return None
    start = folded_text.find(folded_needle)
    if start < 0:
        return None
    end = start + len(folded_needle) - 1
    return offsets[start], offsets[end] + 1


def merge_spans(spans: Iterable[tuple[int, int]]) -> list[tuple[int, int]]:
    values = sorted({(max(0, int(start)), max(0, int(end))) for start, end in spans if end > start})
    merged: list[list[int]] = []
    for start, end in values:
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(start, end) for start, end in merged]


def verified_spans(answer: str, products: Iterable[dict[str, Any]]) -> list[tuple[int, int]]:
    """Resolve labels to original answer characters, preferring concise names."""
    spans = []
    for product in products:
        name = str(product.get("product_name") or product.get("product") or "")
        evidence = str(product.get("evidence") or "")
        span = grounded_span(answer, name)
        if span is None:
            span = grounded_span(answer, evidence)
        if span is not None:
            spans.append(span)
    return merge_spans(spans)


def encode_run(tokenizer: Any, question: str, answer: str,
               spans: list[tuple[int, int]], max_length: int = 384,
               stride: int = 96) -> list[dict[str, list[int]]]:
    """Create overlapping BIO-labelled windows for one verified answer."""
    encoded = tokenizer(
        question,
        answer,
        truncation="only_second",
        max_length=max_length,
        stride=stride,
        return_overflowing_tokens=True,
        return_offsets_mapping=True,
        padding="max_length",
    )
    windows = []
    for index in range(len(encoded["input_ids"])):
        labels = [-100] * max_length
        sequence_ids = encoded.sequence_ids(index)
        offsets = encoded["offset_mapping"][index]
        active_span: tuple[int, int] | None = None
        for token_index, (sequence_id, offset) in enumerate(zip(sequence_ids, offsets)):
            if sequence_id != 1 or offset[1] <= offset[0]:
                continue
            labels[token_index] = 0
            match = next(
                ((start, end) for start, end in spans
                 if offset[0] < end and offset[1] > start),
                None,
            )
            if match is not None:
                labels[token_index] = 1 if active_span != match else 2
                active_span = match
            else:
                active_span = None
        item = {
            "input_ids": list(encoded["input_ids"][index]),
            "attention_mask": list(encoded["attention_mask"][index]),
            "labels": labels,
        }
        if "token_type_ids" in encoded:
            item["token_type_ids"] = list(encoded["token_type_ids"][index])
        windows.append(item)
    return windows


@dataclass
class SpanMetrics:
    precision: float
    recall: float
    f1: float
    predicted: int
    expected: int
    correct: int


def token_metrics(predictions: Iterable[int], labels: Iterable[int]) -> SpanMetrics:
    predicted = expected = correct = 0
    for prediction, label in zip(predictions, labels):
        if label < 0:
            continue
        pred_positive = prediction in (1, 2)
        label_positive = label in (1, 2)
        predicted += int(pred_positive)
        expected += int(label_positive)
        correct += int(pred_positive and label_positive)
    precision = correct / predicted if predicted else 0.0
    recall = correct / expected if expected else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return SpanMetrics(precision, recall, f1, predicted, expected, correct)
