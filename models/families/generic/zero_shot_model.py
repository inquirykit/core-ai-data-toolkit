"""
Generic zero-shot classifier: serves any Hub ID for zero-shot-classification
that no other model file claims, with a natural-language-inference (NLI)
cross-encoder. Each label is put to the model as a hypothesis ("This text is
about {label}.") read together with the text, and how strongly the text
entails it is the label's score. Every (text, label) pair of the requests
waiting is one row of the shared forward passes. The IDs below are only what
/models/list suggests.
"""

import math
from typing import Any, Dict, List, Sequence, Tuple

from models import BaseModel, ModelCard, register
from models.base import fit_max_length, length_batches
from models.contracts import TextClassificationOutput, ZeroShotClassificationInput


def nli_classes(id2label: Dict[int, str]) -> Tuple[int, int]:
    """The entailment class, and the one it is weighed against: contradiction, or not-entailment in two-class
    models (in those, whatever the other class is called)."""
    names = {i: str(name).lower().replace("_", " ").replace("-", " ") for i, name in id2label.items()}
    entail = next((i for i, name in names.items() if name.startswith("entail")), None)
    against = next((i for i, name in names.items() if name.startswith(("contradict", "not entail"))), None)
    if against is None and entail is not None and len(names) == 2:
        against = next(i for i in names if i != entail)
    if entail is None or against is None:
        raise ValueError(f"no entailment and contradiction classes among {sorted(id2label.values())}: not an NLI model")
    return entail, against


def zero_shot_scores(rows: Sequence[Tuple[float, float]], multi_label: bool) -> List[float]:
    """Each label's probability from its (against, entailment) logits: shared among the labels (a softmax over their
    entailment logits), or with ``multi_label`` each label's own (its entailment against the other class)."""
    if multi_label:
        return [1 / (1 + math.exp(against - entail)) for against, entail in rows]
    top = max(entail for _, entail in rows)
    weights = [math.exp(entail - top) for _, entail in rows]
    total = sum(weights)
    return [w / total for w in weights]


def zero_shot_result(labels: List[str], rows: Sequence[Tuple[float, float]], multi_label: bool) -> TextClassificationOutput:
    """The labels ranked by score. One label is scored on its own, since sharing would always give it 1."""
    scores = zero_shot_scores(rows, multi_label or len(labels) == 1)
    ranked = sorted(zip(labels, scores), key=lambda r: -r[1])
    return TextClassificationOutput(label=ranked[0][0], confidence=ranked[0][1], scores=dict(ranked))


@register(task="zero-shot-classification", fallback=True, ids=[
    ModelCard("MoritzLaurer/deberta-v3-base-zeroshot-v2.0", "DeBERTa-v3 base NLI (184M): English, a good default",
              ["english"]),
    ModelCard("MoritzLaurer/deberta-v3-xsmall-zeroshot-v1.1-all-33",
              "DeBERTa-v3 xsmall NLI (70M): English, the quickest here", ["english", "fast"]),
    ModelCard("MoritzLaurer/ModernBERT-large-zeroshot-v2.0",
              "ModernBERT large NLI (396M): English, texts up to 8K tokens, more accurate and slower",
              ["english"]),
    ModelCard("MoritzLaurer/mDeBERTa-v3-base-xnli-multilingual-nli-2mil7",
              "mDeBERTa-v3 base NLI (279M): labels and texts in 100 languages", ["multilingual"]),
    ModelCard("facebook/bart-large-mnli", "BART large MNLI (407M): the classic English zero-shot model", ["english"]),
])
class ZeroShotModel(BaseModel):
    """Any Transformers NLI sequence classifier"""

    # Requests the Batcher gathers into one predict_batch() (models/batching.py); then, inside it, (text, label)
    # pairs per forward pass, and characters per pass once each is padded.
    batch_size = 32
    pass_size = 32
    batch_chars = 64_000

    def load(self):
        from transformers import AutoModelForSequenceClassification, AutoTokenizer

        self.tokenizer = AutoTokenizer.from_pretrained(self.model_id, trust_remote_code=False)
        self.model = self.load_pretrained(AutoModelForSequenceClassification)
        fit_max_length(self.tokenizer, self.model)
        self.entail, self.against = nli_classes(self.model.config.id2label)

    def predict(self, request: ZeroShotClassificationInput) -> TextClassificationOutput:
        return self.predict_batch([request])[0]

    def predict_batch(self, requests: List[ZeroShotClassificationInput]) -> List[TextClassificationOutput]:
        """Score every request's labels, the pairs of all of them sharing forward passes of similar lengths."""
        import torch

        pairs = [(r, request.text, request.hypothesis.replace("{}", label))
                 for r, request in enumerate(requests) for label in request.labels]
        can_pad = self.tokenizer.pad_token is not None and getattr(self.model.config, "pad_token_id", None) is not None
        rows: List[Any] = [None] * len(pairs)
        with torch.no_grad():
            for batch in length_batches([len(t) + len(h) for _, t, h in pairs], self.pass_size if can_pad else 1,
                                        self.batch_chars):
                # longest_first cuts the longer of text and hypothesis: the text, unless the hypothesis alone nears
                # the model's limit, where only_first would fail the request instead.
                inputs = self.tokenizer([pairs[i][1] for i in batch], [pairs[i][2] for i in batch],
                                        padding=len(batch) > 1, truncation="longest_first", return_tensors="pt")
                logits = self.model(**inputs.to(self.model.device)).logits.float()
                for i, row in zip(batch, logits[:, [self.against, self.entail]].tolist()):
                    rows[i] = row
        results = []
        start = 0
        for request in requests:
            end = start + len(request.labels)
            results.append(zero_shot_result(request.labels, rows[start:end], request.multi_label))
            start = end
        return results
