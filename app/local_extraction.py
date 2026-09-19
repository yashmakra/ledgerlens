"""Local QLoRA invoice extractor.

The base model and adapter are loaded lazily so the normal rules pipeline stays
fast and does not require a GPU.  Every generated field is checked against the
stored page text before it is returned to the API.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from app.extraction import ExtractedPage
from app.schemas import (
    EvidenceRead,
    ExtractedFieldRead,
    ExtractedLineItemRead,
    ExtractionOutputRead,
)
from app.training_data import FIELDS, SYSTEM_PROMPT

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOCAL_EXTRACTOR_VERSION = "qwen-invoice-lora-v1"


@dataclass
class LocalExtractionResult:
    output: ExtractionOutputRead
    provider: str
    model: str
    notes: list[str]


class LocalModelConfigurationError(RuntimeError):
    """The local fine-tuned model is not available or configured."""


class LocalModelRequestError(RuntimeError):
    """The local model failed to produce valid structured output."""


def _model_paths() -> tuple[str, Path]:
    model_id = os.getenv("FINETUNE_BASE_MODEL", "Qwen/Qwen2.5-1.5B-Instruct")
    adapter = Path(os.getenv(
        "FINETUNE_ADAPTER_PATH",
        str(PROJECT_ROOT / "artifacts" / "fine_tuning" / "qwen-invoice-lora"),
    ))
    return model_id, adapter


@lru_cache(maxsize=1)
def _load_model():
    """Load the base model and adapter once per worker process."""
    model_id, adapter_path = _model_paths()
    if not adapter_path.exists():
        raise LocalModelConfigurationError(
            f"Fine-tuned adapter not found at {adapter_path}. Run scripts/train_qlora.py first."
        )
    try:
        import torch
        from peft import PeftModel
        from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig
    except ImportError as exc:
        raise LocalModelConfigurationError(
            'Install local-model dependencies with: pip install -e ".[finetune]"'
        ) from exc
    if not torch.cuda.is_available():
        raise LocalModelConfigurationError("The local QLoRA extractor requires a CUDA GPU.")

    offline = os.getenv("LOCAL_MODEL_OFFLINE", "1").strip().casefold() not in {"0", "false", "no"}
    load_options = {"local_files_only": offline}
    tokenizer = AutoTokenizer.from_pretrained(model_id, **load_options)
    model = AutoModelForCausalLM.from_pretrained(
        model_id,
        quantization_config=BitsAndBytesConfig(
            load_in_4bit=True,
            bnb_4bit_quant_type="nf4",
            bnb_4bit_compute_dtype=torch.float16,
            bnb_4bit_use_double_quant=True,
        ),
        device_map="auto",
        **load_options,
    )
    model = PeftModel.from_pretrained(model, adapter_path, **load_options)
    model.eval()
    return tokenizer, model, model_id, str(adapter_path)


def _parse_json(text: str) -> dict:
    start = text.find("{")
    if start < 0:
        raise ValueError("model output did not contain a JSON object")
    parsed, _ = json.JSONDecoder().raw_decode(text[start:])
    if not isinstance(parsed, dict):
        raise TypeError("model output was not a JSON object")
    return parsed


def _normalise(value: object) -> str:
    return " ".join(str(value).casefold().split()).strip()


def _number_tokens(value: object) -> set[str]:
    return {token.replace(",", "") for token in re.findall(r"\d[\d,]*(?:\.\d+)?", str(value))}


def _quote_for_value(value: str, field: str, pages: list[ExtractedPage]) -> tuple[ExtractedPage, str] | None:
    for page in pages:
        match = re.search(re.escape(value), page.text, flags=re.IGNORECASE)
        if match:
            return page, match.group(0)
        if field == "total_amount" and _number_tokens(value):
            for candidate in re.finditer(r"\d[\d,]*(?:\.\d+)?", page.text):
                if candidate.group(0).replace(",", "") in _number_tokens(value):
                    return page, candidate.group(0)
    return None


def _field(value: object, field: str, pages: list[ExtractedPage], notes: list[str]) -> ExtractedFieldRead:
    if isinstance(value, dict):
        value = value.get("value")
    if value is None or _normalise(value) in {"", "n/a", "na", "unknown", "not found", "null"}:
        return ExtractedFieldRead()
    text_value = str(value).strip()
    support = _quote_for_value(text_value, field, pages)
    if support is None:
        notes.append(f"Local field '{field}' omitted because its value was not found in source text.")
        return ExtractedFieldRead()
    page, quote = support
    if field != "total_amount" and _normalise(text_value) not in _normalise(quote):
        notes.append(f"Local field '{field}' omitted because its source evidence did not support the value.")
        return ExtractedFieldRead()
    if field == "total_amount" and not _number_tokens(text_value).issubset(_number_tokens(quote)):
        notes.append(f"Local field '{field}' omitted because its amount was not supported by source evidence.")
        return ExtractedFieldRead()
    return ExtractedFieldRead(
        value=text_value,
        evidence=[EvidenceRead(page=page.page_number, text=quote)],
        confidence=None,
    )


def _line_items(raw: object, pages: list[ExtractedPage], notes: list[str]) -> list[ExtractedLineItemRead]:
    if isinstance(raw, dict) and isinstance(raw.get("items"), list):
        raw = raw["items"]
    if not isinstance(raw, list):
        return []
    result: list[ExtractedLineItemRead] = []
    for index, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            continue
        description = item.get("description")
        if not description:
            continue
        support = _quote_for_value(str(description), "description", pages)
        if support is None:
            notes.append(f"Local line item {index} omitted because its description was not found in source text.")
            continue
        page, quote = support
        values = (item.get("item_code"), item.get("quantity"), item.get("unit_price"), item.get("amount"))
        unsupported = [
            value for value in values
            if value is not None
            and _normalise(value) not in _normalise(quote)
            and not _number_tokens(value).issubset(_number_tokens(quote))
        ]
        if unsupported:
            notes.append(f"Local line item {index} omitted because a value was not supported by source text.")
            continue
        result.append(ExtractedLineItemRead(
            description=str(description).strip(),
            item_code=str(item["item_code"]).strip() if item.get("item_code") is not None else None,
            quantity=str(item["quantity"]).strip() if item.get("quantity") is not None else None,
            unit_price=str(item["unit_price"]).strip() if item.get("unit_price") is not None else None,
            amount=str(item["amount"]).strip() if item.get("amount") is not None else None,
            evidence=[EvidenceRead(page=page.page_number, text=quote)],
        ))
    return result


def extract_invoice_with_local_model(pages: list[ExtractedPage]) -> LocalExtractionResult:
    """Generate invoice JSON with the local adapter and attach source evidence."""
    try:
        import torch

        tokenizer, model, model_id, adapter_path = _load_model()
        source_text = "\n\n".join(f"[Page {page.page_number}]\n{page.text}" for page in pages)
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": source_text},
        ]
        prompt = tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
        inputs = tokenizer(prompt, return_tensors="pt").to(model.device)
        with torch.inference_mode():
            generated = model.generate(**inputs, max_new_tokens=256, do_sample=False)
        output_text = tokenizer.decode(
            generated[0][inputs["input_ids"].shape[1]:], skip_special_tokens=True
        )
        parsed = _parse_json(output_text)
    except LocalModelConfigurationError:
        raise
    except Exception as exc:
        raise LocalModelRequestError(f"Local model extraction failed ({type(exc).__name__}).") from exc

    notes: list[str] = []
    fields = {field: _field(parsed.get(field), field, pages, notes) for field in FIELDS}
    output = ExtractionOutputRead(
        document_type="invoice",
        fields=fields,
        line_items=_line_items(parsed.get("line_items", []), pages, notes),
    )
    return LocalExtractionResult(
        output=output,
        provider="local",
        model=f"{model_id}+{Path(adapter_path).name}",
        notes=notes,
    )
