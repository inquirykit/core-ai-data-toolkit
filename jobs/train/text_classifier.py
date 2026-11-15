"""
Fine-tune a small encoder (BERT-style) to classify texts, with Hugging Face's Trainer.

The rows are a JSON Lines or CSV file of a text and a label each: an eval
dataset's labeled examples, which POST /admin/datasets/{id}/training-file
writes to DATA_DIR/datasets, or any such file. A share of each label is held
out; the model is scored on it after every epoch and the best epoch is kept.

The model is saved to model/ and served afterwards as model ID
"trained:<job id>" for text-classification (models/families/trained/): in the
service's own process, batched and cached like any Hub classifier.
"""

import csv
import json
import random
import shutil
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from pydantic import Field

from jobs import DataFile, Job, JobContext, JobParams, JobResult, register_job

# What models/families/trained/ serves: the model ID (in the result, which models/trained.py reads), its task and its
# folder. That family has its own copy, as neither side imports the other: tests/test_trained_models.py ties them.
MODEL_ID_PREFIX = "trained:"
MODEL_DIR = "model"
TASK = "text-classification"


class ClassifierParams(JobParams):
    train_file: DataFile
    text_column: str = Field("text", min_length=1, max_length=256, description="The column (or field) holding the text")
    label_column: str = Field("label", min_length=1, max_length=256, description="The column holding its label")
    base_model: str = Field(
        "answerdotai/ModernBERT-base", min_length=1, max_length=256,
        description="The encoder to start from, a Hub ID: jhu-clsp/mmBERT-small for many languages, "
                    "google/electra-small-discriminator for speed",
    )
    epochs: int = Field(3, ge=1, le=50)
    learning_rate: float = Field(5e-5, gt=0, le=0.01)
    batch_size: int = Field(16, ge=1, le=256)
    max_length: int = Field(256, ge=16, le=8192, description="Tokens of a text it reads; the rest is cut")
    validation_share: float = Field(0.15, ge=0.05, le=0.5, description="Of each label's examples, held out to score each epoch")
    seed: int = Field(0, ge=0)


class EpochScores(JobResult):
    epoch: float
    loss: Optional[float] = None
    accuracy: Optional[float] = None
    macro_f1: Optional[float] = None


class ClassifierResult(JobResult):
    model_id: str = Field(..., description="Serve the model with POST /inference under this model ID")
    task: str = Field(..., description="... and this task")
    base_model: str
    labels: Dict[str, int] = Field(..., description="Each label and how many examples it had")
    train_rows: int
    validation_rows: int
    accuracy: Optional[float] = Field(None, description="On the held-out examples, at the best epoch")
    macro_f1: Optional[float] = Field(None, description="The mean of the labels' F1, on the held-out examples")
    epochs: List[EpochScores] = Field(default_factory=list, description="Each epoch's training loss and held-out scores")
    fit_seconds: float


def read_rows(path: str, text_column: str, label_column: str) -> List[Tuple[str, str]]:
    """The (text, label) pairs of a CSV file, or else JSON Lines; rows without both are skipped."""
    with open(path, encoding="utf-8", newline="") as f:
        if Path(path).suffix.lower() == ".csv":
            records: List[Any] = list(csv.DictReader(f))
        else:
            records = [json.loads(line) for line in f if line.strip()]
    pairs = []
    for record in records:
        if not isinstance(record, dict):
            continue
        text, label = record.get(text_column), record.get(label_column)
        if isinstance(text, str) and text.strip() and label is not None and str(label).strip():
            pairs.append((text, str(label).strip()))
    return pairs


def split(pairs: List[Tuple[str, str]], share: float, seed: int) -> Tuple[List[Tuple[str, str]], List[Tuple[str, str]]]:
    """Training and held-out rows: ``share`` of each label's, rounded down, so a label with few examples stays
    whole in training."""
    by_label: Dict[str, List[Tuple[str, str]]] = defaultdict(list)
    for pair in pairs:
        by_label[pair[1]].append(pair)
    shuffle = random.Random(seed).shuffle
    train, held = [], []
    for label in sorted(by_label):
        rows = by_label[label]
        shuffle(rows)
        n = int(len(rows) * share)
        held += rows[:n]
        train += rows[n:]
    shuffle(train)
    return train, held


def scores(predicted: List[int], actual: List[int], n_labels: int) -> Dict[str, float]:
    """Accuracy and macro F1 (the mean of the labels' F1, a label never seen nor predicted left out)."""
    f1s = []
    for label in range(n_labels):
        hits = sum(p == a == label for p, a in zip(predicted, actual))
        said, true = predicted.count(label), actual.count(label)
        if not said and not true:
            continue
        precision, recall = (hits / said if said else 0.0), (hits / true if true else 0.0)
        f1s.append(2 * precision * recall / (precision + recall) if precision + recall else 0.0)
    return {
        "accuracy": sum(p == a for p, a in zip(predicted, actual)) / len(actual) if actual else 0.0,
        "macro_f1": sum(f1s) / len(f1s) if f1s else 0.0,
    }


@register_job("text-classifier")
class TrainTextClassifier(Job):
    """Fine-tune a small encoder to classify texts, from labeled examples; served as trained:<job id>"""

    Params = ClassifierParams
    Result = ClassifierResult

    def run(self, ctx: JobContext, p: ClassifierParams) -> ClassifierResult:
        import torch
        from transformers import (AutoModelForSequenceClassification, AutoTokenizer, DataCollatorWithPadding, Trainer,
                                  TrainerCallback, TrainingArguments, set_seed)

        ctx.progress(0.0, "reading the examples")
        pairs = read_rows(p.train_file, p.text_column, p.label_column)
        counts = Counter(label for _, label in pairs)
        if len(counts) < 2:
            raise ValueError(f"Training needs examples of at least two labels; the file has {dict(counts) or 'none'} "
                             f"(columns {p.text_column!r} and {p.label_column!r})")
        labels = sorted(counts)
        index = {label: i for i, label in enumerate(labels)}
        train, held = split(pairs, p.validation_share, p.seed)
        set_seed(p.seed)

        ctx.progress(0.02, f"loading {p.base_model}")
        tokenizer = AutoTokenizer.from_pretrained(p.base_model)
        model = AutoModelForSequenceClassification.from_pretrained(
            p.base_model, num_labels=len(labels), id2label=dict(enumerate(labels)), label2id=index)

        class Rows(torch.utils.data.Dataset):
            def __init__(self, rows: List[Tuple[str, str]]):
                self.encodings = tokenizer([text for text, _ in rows], truncation=True, max_length=p.max_length)
                self.labels = [index[label] for _, label in rows]

            def __len__(self) -> int:
                return len(self.labels)

            def __getitem__(self, i: int) -> Dict[str, Any]:
                return {**{k: v[i] for k, v in self.encodings.items()}, "labels": self.labels[i]}

        def compute_metrics(prediction: Any) -> Dict[str, float]:
            return scores(prediction.predictions.argmax(-1).tolist(), prediction.label_ids.tolist(), len(labels))

        class Progress(TrainerCallback):
            def on_step_end(self, args, state, control, **kwargs):
                if state.max_steps:
                    ctx.progress(0.05 + 0.9 * state.global_step / state.max_steps, f"epoch {state.epoch or 0:.1f} of {p.epochs}")

        checkpoints = ctx.path("checkpoints")
        every_epoch = "epoch" if held else "no"
        args = TrainingArguments(
            output_dir=str(checkpoints), num_train_epochs=p.epochs, learning_rate=p.learning_rate,
            per_device_train_batch_size=p.batch_size, per_device_eval_batch_size=p.batch_size,
            eval_strategy=every_epoch, save_strategy=every_epoch, logging_strategy="epoch",
            load_best_model_at_end=bool(held), metric_for_best_model="macro_f1", save_total_limit=1,
            seed=p.seed, report_to="none", disable_tqdm=True, dataloader_num_workers=0,
            use_cpu=ctx.device == "cpu",
        )
        trainer = Trainer(model=model, args=args, train_dataset=Rows(train), eval_dataset=Rows(held) if held else None,
                          data_collator=DataCollatorWithPadding(tokenizer), compute_metrics=compute_metrics,
                          callbacks=[Progress()])
        started = time.monotonic()
        trainer.train()
        fit_seconds = time.monotonic() - started

        ctx.progress(0.96, "saving")
        trainer.save_model(str(ctx.path(MODEL_DIR)))
        # Served, it reads as far as it was trained to.
        tokenizer.model_max_length = p.max_length
        tokenizer.save_pretrained(str(ctx.path(MODEL_DIR)))
        shutil.rmtree(checkpoints, ignore_errors=True)
        epochs: Dict[float, Dict[str, Any]] = {}
        for entry in trainer.state.log_history:
            if "epoch" not in entry:
                continue
            row = epochs.setdefault(round(entry["epoch"], 2), {"epoch": round(entry["epoch"], 2)})
            if "loss" in entry:
                row["loss"] = entry["loss"]
            for key in ("accuracy", "macro_f1"):
                if f"eval_{key}" in entry:
                    row[key] = entry[f"eval_{key}"]
        # The epoch kept: the first with the best macro F1, as the Trainer chose it.
        scored = [row for row in epochs.values() if row.get("macro_f1") is not None]
        best = max(scored, key=lambda row: row["macro_f1"]) if scored else {}
        result = ClassifierResult(
            model_id=f"{MODEL_ID_PREFIX}{ctx.id}", task=TASK, base_model=p.base_model, labels=dict(sorted(counts.items())),
            train_rows=len(train), validation_rows=len(held), accuracy=best.get("accuracy"),
            macro_f1=best.get("macro_f1"), epochs=[EpochScores(**row) for row in epochs.values()],
            fit_seconds=round(fit_seconds, 1),
        )
        return result
