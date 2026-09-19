"""Run QLoRA invoice extraction training on a CUDA Linux GPU environment."""

import os
from pathlib import Path


def main() -> None:
    try:
        import torch
        from datasets import load_dataset
        from peft import LoraConfig
        from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig
        from trl import SFTConfig, SFTTrainer
    except ImportError as exc:
        raise SystemExit('Install training dependencies: pip install -e ".[finetune]"') from exc

    if not torch.cuda.is_available():
        raise SystemExit("QLoRA training requires a CUDA GPU. Use a Linux GPU machine, Colab, Modal, or RunPod.")

    root = Path(__file__).resolve().parents[1]
    model_id = os.getenv("FINETUNE_BASE_MODEL", "Qwen/Qwen2.5-1.5B-Instruct")
    dataset = load_dataset("json", data_files={
        "train": str(root / "artifacts/fine_tuning/train.jsonl"),
        "validation": str(root / "artifacts/fine_tuning/validation.jsonl"),
    })
    tokenizer = AutoTokenizer.from_pretrained(model_id)
    tokenizer.pad_token = tokenizer.eos_token
    model = AutoModelForCausalLM.from_pretrained(
        model_id,
        quantization_config=BitsAndBytesConfig(
            load_in_4bit=True,
            bnb_4bit_quant_type="nf4",
            bnb_4bit_compute_dtype=torch.float16,
            bnb_4bit_use_double_quant=True,
        ),
        device_map="auto",
    )
    trainer = SFTTrainer(
        model=model,
        processing_class=tokenizer,
        train_dataset=dataset["train"],
        eval_dataset=dataset["validation"],
        peft_config=LoraConfig(r=16, lora_alpha=32, lora_dropout=0.05, task_type="CAUSAL_LM"),
        args=SFTConfig(
            output_dir=str(root / "artifacts/fine_tuning/qwen-invoice-lora"),
            num_train_epochs=3,
            learning_rate=2e-4,
            per_device_train_batch_size=1,
            gradient_accumulation_steps=8,
            max_length=512,
            eval_strategy="epoch",
            logging_steps=5,
            report_to="none",
        ),
    )
    trainer.train()
    trainer.save_model()


if __name__ == "__main__":
    main()
