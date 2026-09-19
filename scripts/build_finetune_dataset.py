"""Build train/validation JSONL files from the labeled invoice benchmark."""

import hashlib
import json
from pathlib import Path

from app.training_data import training_example

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "data" / "evaluation" / "golden_invoices.json"
SYNTHETIC_SOURCE = ROOT / "data" / "training" / "synthetic_invoices.json"
OUTPUT_DIR = ROOT / "artifacts" / "fine_tuning"


def split_name(group: str) -> str:
    """Keep a vendor/layout group wholly in train or validation to avoid leakage."""
    bucket = int(hashlib.sha256(group.encode()).hexdigest(), 16) % 5
    return "validation" if bucket == 0 else "train"


def main() -> None:
    records = json.loads(SOURCE.read_text(encoding="utf-8"))
    if SYNTHETIC_SOURCE.exists():
        records.extend(json.loads(SYNTHETIC_SOURCE.read_text(encoding="utf-8")))
    grouped = {"train": [], "validation": []}
    for record in records:
        grouped[split_name(record.get("split_group", record["case_id"]))].append(training_example(record))
    if not grouped["validation"] and grouped["train"]:
        grouped["validation"].append(grouped["train"].pop())
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    for split, examples in grouped.items():
        path = OUTPUT_DIR / f"{split}.jsonl"
        path.write_text("".join(json.dumps(item) + "\n" for item in examples), encoding="utf-8")
        print(f"{split}: {len(examples)} examples -> {path.relative_to(ROOT)}")
    print("This starter dataset is for pipeline validation only. Add 200+ diverse labeled invoices before training.")


if __name__ == "__main__":
    main()
