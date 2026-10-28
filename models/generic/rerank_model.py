"""
Generic reranking model: a cross-encoder that reads the query together with
each document and scores how well they match. Serves any Hub ID for rerank
that no other model file claims; the IDs below are only what /models/list
suggests.

Scores are the model's relevance logit through a sigmoid (one-logit models)
or the probability of the "relevant" class (two-class models), so they are
comparable across requests, not only within one.
"""

from typing import Any, List

from models import BaseModel, ModelCard, register
from models.base import fit_max_length
from models.tasks import RerankInput, RerankOutput


@register(task="rerank", fallback=True, ids=[
    ModelCard("BAAI/bge-reranker-v2-m3",
              "BGE reranker v2 (568M): strong multilingual relevance scores, passages up to 8K tokens",
              ["multilingual", "retrieval"]),
    ModelCard("Alibaba-NLP/gte-reranker-modernbert-base",
              "GTE reranker on ModernBERT (149M): fast English reranking, passages up to 8K tokens",
              ["english", "retrieval", "fast"]),
    ModelCard("mixedbread-ai/mxbai-rerank-base-v1",
              "mxbai rerank base (184M): English reranking, Apache-2.0", ["english", "retrieval"]),
    ModelCard("cross-encoder/ms-marco-MiniLM-L-6-v2",
              "MiniLM cross-encoder (22M): the classic fast English reranker (MS MARCO)",
              ["english", "retrieval", "fast"]),
])
class RerankModel(BaseModel):
    """Any sequence-classification cross-encoder AutoModelForSequenceClassification loads"""

    batch_size = 16

    def load(self):
        from transformers import AutoModelForSequenceClassification, AutoTokenizer

        self.tokenizer = AutoTokenizer.from_pretrained(self.model_id, trust_remote_code=False)
        self.model = self.load_pretrained(AutoModelForSequenceClassification)
        fit_max_length(self.tokenizer, self.model)

    def _scores(self, logits: Any) -> List[float]:
        import torch

        logits = logits.float()
        if logits.shape[-1] == 1:
            return torch.sigmoid(logits[:, 0]).cpu().tolist()
        # Two or more classes: the last one is "relevant" for the usual cross-encoders.
        return torch.softmax(logits, dim=-1)[:, -1].cpu().tolist()

    def predict(self, request: RerankInput) -> RerankOutput:
        import torch

        scores: List[float] = []
        tokens = 0
        with torch.no_grad():
            for start in range(0, len(request.documents), self.batch_size):
                batch = request.documents[start:start + self.batch_size]
                encoded = self.tokenizer(
                    [request.query] * len(batch), batch, padding=True, truncation=True, return_tensors="pt"
                ).to(self.model.device)
                scores.extend(self._scores(self.model(**encoded).logits))
                tokens += int(encoded["attention_mask"].sum())
        order = sorted(range(len(scores)), key=lambda i: -scores[i])
        if request.top_n is not None:
            order = order[: request.top_n]
        return RerankOutput(results=[{"index": i, "score": scores[i]} for i in order], tokens=tokens)
