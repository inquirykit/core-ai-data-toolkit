"""
Generic embedding model: serves any Hub ID for embedding that no other model
file claims. Pooling follows the model's sentence-transformers config
(1_Pooling/config.json: mean, CLS or last token), mean when it has none;
vectors are L2-normalized, as OpenAI's are. The IDs below are only what
/models/list suggests.
"""

import json
from typing import Any, List

from models import BaseModel, ModelCard, register
from models.base import fit_max_length
from models.tasks import EmbeddingInput, EmbeddingOutput


def pooling_mode(model_id: str) -> str:
    """"mean", "cls" or "last", from the model's sentence-transformers pooling config."""
    try:
        from huggingface_hub import hf_hub_download

        with open(hf_hub_download(model_id, "1_Pooling/config.json")) as f:
            config = json.load(f)
    except Exception:
        return "mean"
    if config.get("pooling_mode_cls_token"):
        return "cls"
    if config.get("pooling_mode_lasttoken"):
        return "last"
    return "mean"


@register(task="embedding", fallback=True, ids=[
    ModelCard("ibm-granite/granite-embedding-small-english-r2",
              "Granite R2 small (47M): English search vectors, texts up to 8K tokens (384 dimensions)",
              ["english", "retrieval", "fast"]),
    ModelCard("ibm-granite/granite-embedding-97m-multilingual-r2",
              "Granite R2 (97M): vectors for 200+ languages, texts up to 32K tokens (384 dimensions)",
              ["multilingual", "retrieval", "fast"]),
    ModelCard("Alibaba-NLP/gte-modernbert-base",
              "GTE on ModernBERT (149M): strong English retrieval, texts up to 8K tokens (768 dimensions)",
              ["english", "retrieval"]),
    ModelCard("google/embeddinggemma-2",
              "EmbeddingGemma 2 (270M text): 100+ languages and code, texts up to 8K tokens (768 dimensions; "
              "best with its 'task: ... | query: ' prefixes)", ["multilingual", "retrieval", "code"]),
    ModelCard("microsoft/harrier-oss-v1-270m",
              "Harrier (270M, MIT): top multilingual scores under 400M, 94 languages, texts up to 32K tokens "
              "(640 dimensions; queries want an 'Instruct: ...\nQuery: ' prefix)", ["multilingual", "retrieval"]),
    ModelCard("Qwen/Qwen3-Embedding-0.6B",
              "Qwen3 Embedding 0.6B: best quality here, 100+ languages and code, texts up to 32K tokens "
              "(1024 dimensions; about 1.2 GB)", ["multilingual", "retrieval", "code"]),
    ModelCard("sentence-transformers/all-MiniLM-L6-v2", "Fast English sentence vectors (384 dimensions)",
              ["english", "similarity"]),
])
class EmbeddingModel(BaseModel):
    """Any Transformers encoder AutoModel loads"""

    batch_size = 32

    def load(self):
        from transformers import AutoModel, AutoTokenizer

        self.tokenizer = AutoTokenizer.from_pretrained(self.model_id, trust_remote_code=False)
        self.model = self.load_pretrained(AutoModel)
        fit_max_length(self.tokenizer, self.model)
        self.pooling = pooling_mode(self.model_id)

    def _pool(self, hidden: Any, mask: Any) -> Any:
        import torch

        if self.pooling == "cls":
            return hidden[:, 0]
        if self.pooling == "last":
            # The last real token of each row, wherever the padding is.
            last = mask.shape[1] - 1 - mask.flip(1).argmax(1)
            return hidden[torch.arange(hidden.shape[0], device=hidden.device), last]
        weights = mask.unsqueeze(-1).to(hidden.dtype)
        return (hidden * weights).sum(1) / weights.sum(1).clamp(min=1e-9)

    def predict(self, request: EmbeddingInput) -> EmbeddingOutput:
        import torch
        import torch.nn.functional as F

        vectors: List[List[float]] = []
        tokens = 0
        with torch.no_grad():
            for start in range(0, len(request.texts), self.batch_size):
                batch = request.texts[start:start + self.batch_size]
                encoded = self.tokenizer(batch, padding=True, truncation=True, return_tensors="pt").to(self.model.device)
                hidden = self.model(**encoded).last_hidden_state
                pooled = F.normalize(self._pool(hidden, encoded["attention_mask"]).float(), dim=-1)
                vectors.extend(pooled.cpu().tolist())
                tokens += int(encoded["attention_mask"].sum())
        return EmbeddingOutput(embeddings=vectors, tokens=tokens)
