"""
Generic embedding model: serves any Hub ID for embedding that no other model
file claims, through sentence-transformers, which runs the steps the model's
repo declares (its pooling, a Dense layer after it, its normalizing), and
pools by mean a plain encoder that declares none. Vectors are L2-normalized,
as OpenAI's are. A model server entry's in-process backend can give it a
revision and a max_length (models/servers/entries.py); a repo with its own
code loads only if TRUST_REMOTE_CODE_MODELS lists it. The IDs below are only
what /models/list suggests.
"""

import json
from typing import Any, Dict, List, Optional

from models import BaseModel, ModelCard, register
from models.base import length_batches, token_counts
from models.contracts import EmbeddingInput, EmbeddingOutput


def st_config(model_id: str, name: str, revision: Optional[str] = None) -> Optional[Any]:
    """The model's sentence-transformers file ``name`` as JSON, or None when it has none (or it can't be read)."""
    try:
        from huggingface_hub import hf_hub_download

        with open(hf_hub_download(model_id, name, revision=revision)) as f:
            return json.load(f)
    except Exception:
        return None


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
    """Any model sentence-transformers' SentenceTransformer loads"""

    # Requests the Batcher gathers into one predict_batch() (models/batching.py), so a search's one text shares a
    # forward pass with the others arriving then; inside it, texts per pass, and characters per pass once padded.
    batch_size = 32
    pass_size = 32
    batch_chars = 64_000
    load_settings = ("revision", "max_length")

    @classmethod
    def prompts(cls, model_id: str, revision: Optional[str] = None) -> Dict[str, str]:
        """The prompts in the model's config_sentence_transformers.json (Qwen3-Embedding's query instruction,
        EmbeddingGemma's task prefixes), by name."""
        config = st_config(model_id, "config_sentence_transformers.json", revision)
        prompts = config.get("prompts") if isinstance(config, dict) else None
        if not isinstance(prompts, dict):
            return {}
        return {name: text for name, text in prompts.items() if isinstance(name, str) and isinstance(text, str)}

    def load(self):
        from sentence_transformers import SentenceTransformer

        self.model = self.load_sentence_transformers(SentenceTransformer)

    def predict(self, request: EmbeddingInput) -> EmbeddingOutput:
        return self.predict_batch([request])[0]

    def predict_batch(self, requests: List[EmbeddingInput]) -> List[EmbeddingOutput]:
        """Every request's texts embedded together, in forward passes of similar lengths. The texts go in as
        given: a /db vector field has put its prompt before them, so the model's default prompt is not added."""
        import torch
        import torch.nn.functional as F
        from sentence_transformers.util import batch_to_device

        texts = [(r, text) for r, request in enumerate(requests) for text in request.texts]
        vectors: List[Any] = [None] * len(texts)
        tokens = [0] * len(requests)
        with torch.inference_mode():
            for batch in length_batches([len(text) for _, text in texts], self.pass_size, self.batch_chars):
                features = self.model.preprocess([texts[i][1] for i in batch])
                counts = token_counts(features, len(batch))
                out = self.model(batch_to_device(features, self.model.device))
                pooled = F.normalize(out["sentence_embedding"].float(), dim=-1)
                for i, vector, n in zip(batch, pooled.cpu().tolist(), counts):
                    vectors[i] = vector
                    tokens[texts[i][0]] += n
        results, start = [], 0
        for r, request in enumerate(requests):
            results.append(EmbeddingOutput(embeddings=vectors[start:start + len(request.texts)], tokens=tokens[r]))
            start += len(request.texts)
        return results

