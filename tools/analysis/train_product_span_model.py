"""Fine-tune a compact Chinese encoder on verified product evidence spans."""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
from collections import Counter
from pathlib import Path

import doubao_env_loader  # noqa: F401

from monitor_core.database import connection
from monitor_core.supervised_product_model import (
    encode_run,
    split_bucket,
    token_metrics,
    verified_spans,
)


ROOT = Path(__file__).resolve().parents[2]


def load_rows(max_runs: int = 0) -> list[dict]:
    with connection() as conn:
        rows = conn.execute(
            "SELECT r.model_id,r.run_id,r.question,r.answer,COALESCE((SELECT jsonb_agg(p.payload "
            "ORDER BY p.product_index) FROM monitor_products p WHERE p.model_id=r.model_id "
            "AND p.run_id=r.run_id),'[]'::jsonb) products FROM monitor_runs r "
            "WHERE COALESCE(r.payload->>'product_review_status','')='ai_verified' "
            "ORDER BY md5(r.model_id || '/' || r.run_id)"
        ).fetchall()
    values = [dict(row) for row in rows]
    if max_runs > 0:
        values = values[:max_runs]
    return values


def build_dataset(tokenizer, rows: list[dict], max_length: int, stride: int) -> tuple[dict, dict]:
    datasets = {"train": [], "validation": [], "test": []}
    audit = Counter()
    for index, row in enumerate(rows, 1):
        question = str(row.get("question") or "")
        answer = str(row.get("answer") or "")
        products = [dict(item) for item in row.get("products") or [] if isinstance(item, dict)]
        spans = verified_spans(answer, products)
        audit["runs"] += 1
        audit["products"] += len(products)
        audit["grounded_spans"] += len(spans)
        if products and not spans:
            audit["skipped_ungrounded_runs"] += 1
            continue
        split = split_bucket(question, answer)
        datasets[split].extend(encode_run(
            tokenizer, question, answer, spans, max_length=max_length, stride=stride,
        ))
        audit[split + "_runs"] += 1
        if index % 1000 == 0:
            print(f"encoded {index}/{len(rows)} runs", flush=True)
    audit.update({split + "_windows": len(items) for split, items in datasets.items()})
    return datasets, dict(audit)


def tensor_dataset(items: list[dict]):
    import torch
    from torch.utils.data import TensorDataset

    names = ["input_ids", "attention_mask"]
    if items and "token_type_ids" in items[0]:
        names.append("token_type_ids")
    names.append("labels")
    tensors = [torch.tensor([item[name] for item in items], dtype=torch.long) for name in names]
    return names, TensorDataset(*tensors)


def batch_kwargs(names: list[str], batch, device):
    return {name: value.to(device) for name, value in zip(names, batch)}


def evaluate(model, loader, names, device) -> dict:
    import torch

    model.eval()
    predictions: list[int] = []
    positive_scores: list[float] = []
    labels: list[int] = []
    losses = []
    with torch.inference_mode():
        for batch in loader:
            values = batch_kwargs(names, batch, device)
            output = model(**values)
            losses.append(float(output.loss))
            predictions.extend(output.logits.argmax(-1).cpu().reshape(-1).tolist())
            probabilities = output.logits.softmax(-1)[..., 1:].sum(-1)
            positive_scores.extend(probabilities.cpu().reshape(-1).tolist())
            labels.extend(values["labels"].cpu().reshape(-1).tolist())
    metrics = token_metrics(predictions, labels)
    result = {
        "loss": sum(losses) / max(1, len(losses)),
        "precision": metrics.precision,
        "recall": metrics.recall,
        "f1": metrics.f1,
        "predicted_tokens": metrics.predicted,
        "expected_tokens": metrics.expected,
        "correct_tokens": metrics.correct,
    }
    result["thresholds"] = {}
    for threshold in (0.5, 0.7, 0.8, 0.9, 0.95, 0.98):
        threshold_metrics = token_metrics(
            (1 if score >= threshold else 0 for score in positive_scores), labels,
        )
        result["thresholds"][str(threshold)] = {
            "precision": threshold_metrics.precision,
            "recall": threshold_metrics.recall,
            "f1": threshold_metrics.f1,
            "predicted_tokens": threshold_metrics.predicted,
        }
    return result


def freeze_lower_layers(model) -> None:
    base = getattr(model, "bert", None) or getattr(model, "roberta", None)
    if base is None:
        return
    for parameter in base.embeddings.parameters():
        parameter.requires_grad = False
    layers = list(base.encoder.layer)
    for layer in layers[: max(0, len(layers) // 2)]:
        for parameter in layer.parameters():
            parameter.requires_grad = False


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-model", default="uer/chinese_roberta_L-4_H-512")
    parser.add_argument("--output", default=str(ROOT / "runtime" / "models" / "product-span-roberta-small"))
    parser.add_argument("--max-runs", type=int, default=0)
    parser.add_argument("--epochs", type=int, default=2)
    parser.add_argument("--batch-size", type=int, default=12)
    parser.add_argument("--max-length", type=int, default=384)
    parser.add_argument("--stride", type=int, default=96)
    parser.add_argument("--learning-rate", type=float, default=5e-5)
    parser.add_argument("--seed", type=int, default=20260818)
    args = parser.parse_args()

    os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
    os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
    random.seed(args.seed)

    import torch
    from torch.optim import AdamW
    from torch.utils.data import DataLoader
    from transformers import AutoModelForTokenClassification, AutoTokenizer

    torch.manual_seed(args.seed)
    torch.set_num_threads(max(1, min(8, os.cpu_count() or 8)))
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    tokenizer = AutoTokenizer.from_pretrained(args.base_model, use_fast=True)
    if not getattr(tokenizer, "is_fast", False):
        raise RuntimeError("a fast tokenizer is required for grounded offsets")

    rows = load_rows(args.max_runs)
    datasets, audit = build_dataset(tokenizer, rows, args.max_length, args.stride)
    print(json.dumps({"dataset": audit}, ensure_ascii=False), flush=True)
    prepared = {split: tensor_dataset(items) for split, items in datasets.items()}
    loaders = {
        split: DataLoader(dataset, batch_size=args.batch_size, shuffle=split == "train")
        for split, (_names, dataset) in prepared.items()
    }
    names = prepared["train"][0]
    model = AutoModelForTokenClassification.from_pretrained(
        args.base_model,
        num_labels=3,
        id2label={0: "O", 1: "B-PRODUCT", 2: "I-PRODUCT"},
        label2id={"O": 0, "B-PRODUCT": 1, "I-PRODUCT": 2},
    )
    freeze_lower_layers(model)
    device = torch.device("cpu")
    model.to(device)
    optimizer = AdamW(
        [parameter for parameter in model.parameters() if parameter.requires_grad],
        lr=args.learning_rate,
        weight_decay=0.01,
    )
    best_f1 = -1.0
    history = []
    started = time.time()
    for epoch in range(1, max(1, args.epochs) + 1):
        model.train()
        losses = []
        for step, batch in enumerate(loaders["train"], 1):
            optimizer.zero_grad(set_to_none=True)
            output_values = model(**batch_kwargs(names, batch, device))
            output_values.loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            optimizer.step()
            losses.append(float(output_values.loss.detach()))
            if step % 100 == 0:
                print(json.dumps({"epoch": epoch, "step": step,
                                  "steps": len(loaders["train"]),
                                  "loss": sum(losses[-100:]) / len(losses[-100:])}), flush=True)
        validation = evaluate(model, loaders["validation"], names, device)
        record = {"epoch": epoch, "train_loss": sum(losses) / max(1, len(losses)),
                  "validation": validation, "elapsed_seconds": round(time.time() - started, 1)}
        history.append(record)
        print(json.dumps(record, ensure_ascii=False), flush=True)
        if validation["f1"] > best_f1:
            best_f1 = validation["f1"]
            model.save_pretrained(output)
            tokenizer.save_pretrained(output)

    best = AutoModelForTokenClassification.from_pretrained(output).to(device)
    test = evaluate(best, loaders["test"], names, device)
    metrics = {
        "base_model": args.base_model,
        "parameters": sum(parameter.numel() for parameter in best.parameters()),
        "trainable_parameters": sum(parameter.numel() for parameter in model.parameters()
                                    if parameter.requires_grad),
        "dataset": audit,
        "history": history,
        "test": test,
        "elapsed_seconds": round(time.time() - started, 1),
    }
    (output / "training_metrics.json").write_text(
        json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    print(json.dumps(metrics, ensure_ascii=False, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
