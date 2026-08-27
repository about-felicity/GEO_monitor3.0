"""Evaluate confidence thresholds for an existing product span checkpoint."""

from __future__ import annotations

import argparse
import json

from .train_product_span_model import build_dataset, evaluate, load_rows, tensor_dataset


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True)
    parser.add_argument("--max-runs", type=int, default=0)
    parser.add_argument("--batch-size", type=int, default=24)
    parser.add_argument("--max-length", type=int, default=384)
    parser.add_argument("--stride", type=int, default=96)
    args = parser.parse_args()

    import torch
    from torch.utils.data import DataLoader
    from transformers import AutoModelForTokenClassification, AutoTokenizer

    torch.set_num_threads(8)
    tokenizer = AutoTokenizer.from_pretrained(args.model, use_fast=True, local_files_only=True)
    rows = load_rows(args.max_runs)
    datasets, audit = build_dataset(tokenizer, rows, args.max_length, args.stride)
    model = AutoModelForTokenClassification.from_pretrained(
        args.model, local_files_only=True,
    ).to(torch.device("cpu"))
    results = {"dataset": audit}
    for split in ("validation", "test"):
        names, dataset = tensor_dataset(datasets[split])
        loader = DataLoader(dataset, batch_size=args.batch_size)
        results[split] = evaluate(model, loader, names, torch.device("cpu"))
    print(json.dumps(results, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
