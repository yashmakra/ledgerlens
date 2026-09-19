"""Evaluate the trained QLoRA adapter on the held-out validation JSONL."""

import json
import os
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIELDS = ("supplier_name", "currency", "invoice_number", "invoice_date", "purchase_order_number", "total_amount")


def normalize(value) -> str:
    if value is None:
        return ""
    value = " ".join(str(value).casefold().split()).strip()
    try:
        return str(Decimal(value.replace(",", "")))
    except (InvalidOperation, ValueError):
        return "".join(character for character in value if character.isalnum())


def extract_json(text: str) -> dict:
    start = text.find("{")
    if start < 0:
        raise ValueError("model output did not contain a JSON object")
    return json.JSONDecoder().raw_decode(text[start:])[0]


def validate_against_source(output: dict, source_text: str) -> dict:
    """Remove values that are absent from the invoice text or are placeholders."""
    validated = dict(output)
    source = source_text.casefold()
    for field in FIELDS:
        value = validated.get(field)
        if value is None or str(value).strip().casefold() in {"", "n/a", "na", "unknown", "not found"}:
            validated[field] = None
            continue
        value_text = str(value).strip().casefold()
        if field == "total_amount":
            value_text = value_text.replace(",", "")
            source_numbers = {token.replace(",", "") for token in re.findall(r"\d[\d,]*(?:\.\d+)?", source)}
            if value_text not in source_numbers:
                validated[field] = None
        elif value_text not in source:
            validated[field] = None
    return validated


def main() -> None:
    # Evaluation must work from the already downloaded model without metadata calls.
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    try:
        import torch
        from peft import PeftModel
        from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig
    except ImportError as exc:
        raise SystemExit('Install training dependencies: pip install -e ".[finetune]"') from exc
    if not torch.cuda.is_available():
        raise SystemExit("Evaluation requires a CUDA GPU in this local setup.")

    model_id = os.getenv("FINETUNE_BASE_MODEL", "Qwen/Qwen2.5-1.5B-Instruct")
    adapter_path = ROOT / "artifacts/fine_tuning/qwen-invoice-lora"
    validation_path = ROOT / "artifacts/fine_tuning/validation.jsonl"
    if not adapter_path.exists():
        raise SystemExit(f"Adapter not found: {adapter_path}. Run scripts/train_qlora.py first.")

    tokenizer = AutoTokenizer.from_pretrained(model_id, local_files_only=True)
    model = AutoModelForCausalLM.from_pretrained(
        model_id,
        quantization_config=BitsAndBytesConfig(
            load_in_4bit=True,
            bnb_4bit_quant_type="nf4",
            bnb_4bit_compute_dtype=torch.float16,
            bnb_4bit_use_double_quant=True,
        ),
        device_map="auto",
        local_files_only=True,
    )
    model = PeftModel.from_pretrained(model, adapter_path)
    model.eval()
    records = [json.loads(line) for line in validation_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    totals = {field: {"passed": 0, "total": 0} for field in FIELDS}
    parse_failures = 0
    for record in records:
        prompt = tokenizer.apply_chat_template(record["messages"][:2], tokenize=False, add_generation_prompt=True)
        inputs = tokenizer(prompt, return_tensors="pt").to(model.device)
        with torch.inference_mode():
            generated = model.generate(**inputs, max_new_tokens=256, do_sample=False)
        output_text = tokenizer.decode(generated[0][inputs["input_ids"].shape[1]:], skip_special_tokens=True)
        try:
            actual = validate_against_source(extract_json(output_text), record["messages"][1]["content"])
            expected = json.loads(record["messages"][2]["content"])
        except (ValueError, json.JSONDecodeError):
            parse_failures += 1
            print(f"{record['case_id']} | JSON parse failure")
            continue
        for field in FIELDS:
            totals[field]["total"] += 1
            passed = normalize(actual.get(field)) == normalize(expected.get(field))
            totals[field]["passed"] += int(passed)
            if not passed:
                print(f"{record['case_id']} | {field} | expected={expected.get(field)!r} actual={actual.get(field)!r}")
    total = sum(stats["total"] for stats in totals.values())
    passed = sum(stats["passed"] for stats in totals.values())
    print(json.dumps({
        "adapter": str(adapter_path.relative_to(ROOT)),
        "fields_passed": passed,
        "fields_total": total,
        "field_accuracy": round(passed / total, 4) if total else 0.0,
        "parse_failures": parse_failures,
        "by_field": totals,
    }, indent=2))


if __name__ == "__main__":
    main()
