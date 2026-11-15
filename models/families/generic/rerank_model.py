"""
Generic reranking model: a cross-encoder that reads the query together with
each document and scores how well they match. Serves any Hub ID for rerank
that no other model file claims, through sentence-transformers' CrossEncoder:
sequence-classification cross-encoders (BGE, GTE, mxbai v1, MS MARCO) and
causal-LM rerankers that score by a "yes" token's logit (Qwen3-Reranker,
mxbai v2), with the instruction the model's config gives. A model server
entry's in-process backend can give it a revision and a max_length
(models/servers/entries.py); a repo with its own code loads only if
TRUST_REMOTE_CODE_MODELS lists it. The IDs below are only what /models/list
suggests.

Scores are the model's relevance logit through a sigmoid (one-logit models)
or the probability of the "relevant" class (two-class models), whatever
activation the model's config names, so they are comparable across requests,
not only within one.
"""

from typing import Any, List

from models import BaseModel, ModelCard, register
from models.base import length_batches, token_counts
from models.contracts import RerankInput, RerankOutput


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
    ModelCard("Qwen/Qwen3-Reranker-0.6B",
              "Qwen3 Reranker 0.6B: an LLM reading each pair, 100+ languages and code, passages up to 32K tokens "
              "(about 1.2 GB)", ["multilingual", "retrieval", "code"]),
])
class RerankModel(BaseModel):
    """Any model sentence-transformers' CrossEncoder loads"""

    # Requests the Batcher gathers into one predict_batch() (models/batching.py); inside it, (query, document) pairs
    # per forward pass, and characters per pass once padded.
    batch_size = 16
    pass_size = 16
    batch_chars = 64_000
    load_settings = ("revision", "max_length")

    def load(self):
        from sentence_transformers import CrossEncoder

        self.model = self.load_sentence_transformers(CrossEncoder)
        # The instruction a causal-LM reranker's config names as its default (Qwen3-Reranker's); None for most.
        self.prompt = self.model.prompts.get(self.model.default_prompt_name) if self.model.default_prompt_name else None

    def _scores(self, logits: Any) -> List[float]:
        import torch

        # [pairs, 1] from a one-logit head or a causal-LM reranker (its log-odds), or [pairs, classes].
        logits = logits.float()
        if logits.shape[-1] == 1:
            return torch.sigmoid(logits[:, 0]).cpu().tolist()
        # Two or more classes: the last one is "relevant" for the usual cross-encoders.
        return torch.softmax(logits, dim=-1)[:, -1].cpu().tolist()

    def predict(self, request: RerankInput) -> RerankOutput:
        return self.predict_batch([request])[0]

    def predict_batch(self, requests: List[RerankInput]) -> List[RerankOutput]:
        """Every request's (query, document) pairs scored together, in forward passes of similar lengths."""
        import torch
        from sentence_transformers.util import batch_to_device

        pairs = [(r, request.query, document) for r, request in enumerate(requests) for document in request.documents]
        scores: List[Any] = [None] * len(pairs)
        tokens = [0] * len(requests)
        with torch.inference_mode():
            for batch in length_batches([len(q) + len(d) for _, q, d in pairs], self.pass_size, self.batch_chars):
                features = self.model.preprocess([(pairs[i][1], pairs[i][2]) for i in batch], prompt=self.prompt)
                counts = token_counts(features, len(batch))
                # The raw logits: the model's own activation (none for MS MARCO's) would make scores incomparable.
                logits = self.model(batch_to_device(features, self.model.device))["scores"]
                for i, score, n in zip(batch, self._scores(logits), counts):
                    scores[i] = score
                    tokens[pairs[i][0]] += n
        results, start = [], 0
        for r, request in enumerate(requests):
            mine = scores[start:start + len(request.documents)]
            start += len(request.documents)
            order = sorted(range(len(mine)), key=lambda i: -mine[i])[:request.top_n]
            results.append(RerankOutput(results=[{"index": i, "score": mine[i]} for i in order], tokens=tokens[r]))
        return results
