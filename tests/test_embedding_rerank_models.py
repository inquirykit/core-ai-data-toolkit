import math

import pytest

from core.config import Config
from models import contracts
from models.families.generic import embedding_model
from models.families.generic.embedding_model import EmbeddingModel
from models.families.generic.rerank_model import RerankModel


def parsed(task, **fields):
    """A request parsed the way the manager does it before run()."""
    return contracts.get(task).parse_input(fields)


def features(torch, texts, value, mask=True):
    """What sentence-transformers' preprocess() gives: one token per word, padded at the end, each token's id
    ``value(text)``; the attention mask marks the real ones."""
    width = max(len(t.split()) for t in texts)
    out = {"input_ids": torch.tensor([[value(t)] * len(t.split()) + [0] * (width - len(t.split())) for t in texts])}
    if mask:
        out["attention_mask"] = torch.tensor([[1] * len(t.split()) + [0] * (width - len(t.split())) for t in texts])
    return out


class Loader:
    """Stands in for SentenceTransformer or CrossEncoder: records what it was built with."""

    made = []

    def __init__(self, model_id, **kwargs):
        self.made.append((model_id, kwargs))
        self.prompts, self.default_prompt_name, self.max_seq_length = {}, None, 512

    def eval(self):
        return self


def test_embedding_requests_arriving_together_share_a_pass_and_get_their_own_vectors_and_tokens():
    torch = pytest.importorskip("torch")
    passes = []

    class Model:
        device = "cpu"

        def preprocess(self, texts, prompt=None):
            passes.append(list(texts))
            return features(torch, texts, len)

        def __call__(self, features):
            # A text's vector is (its length, 1), not yet normalized.
            first = features["input_ids"][:, 0].float()
            return {"sentence_embedding": torch.stack([first, torch.ones_like(first)], -1)}

    model = EmbeddingModel("m", "embedding")
    model.model = Model()
    first, second = model.predict_batch([parsed("embedding", texts=["one two three", "a b"]),
                                         parsed("embedding", texts=["single"])])

    assert len(passes) == 1
    unit = lambda n: [n / math.hypot(n, 1), 1 / math.hypot(n, 1)]  # noqa: E731
    assert first.embeddings == [pytest.approx(unit(13)), pytest.approx(unit(3))] and first.tokens == 5
    assert second.embeddings == [pytest.approx(unit(6))] and second.tokens == 1
    assert model.batches


def test_a_model_without_an_attention_mask_counts_no_tokens():
    torch = pytest.importorskip("torch")

    class Model:
        device = "cpu"

        def preprocess(self, texts, prompt=None):
            return features(torch, texts, len, mask=False)

        def __call__(self, features):
            return {"sentence_embedding": torch.ones(len(features["input_ids"]), 2)}

    model = EmbeddingModel("m", "embedding")
    model.model = Model()
    [out] = model.predict_batch([parsed("embedding", texts=["a b", "c"])])
    assert out.tokens == 0 and len(out.embeddings) == 2


def test_rerank_requests_arriving_together_share_a_pass_and_are_ranked_apart():
    torch = pytest.importorskip("torch")
    passes = []

    class Model:
        device = "cpu"

        def preprocess(self, pairs, prompt=None):
            passes.append((list(pairs), prompt))
            return features(torch, [f"{q} {d}" for q, d in pairs], lambda t: len(t.split()[-1]))

        def __call__(self, features):
            # One logit: the length of the document's last word, less 4.
            return {"scores": features["input_ids"][:, :1].float() - 4}

    model = RerankModel("m", "rerank")
    model.model, model.prompt = Model(), "Judge it"
    first, second = model.predict_batch([
        parsed("rerank", query="q", documents=["short", "a much longer", "mid"]),
        parsed("rerank", query="two words", documents=["xx", "yyyyyy"], top_n=1),
    ])

    assert len(passes) == 1 and passes[0][1] == "Judge it"
    sigmoid = lambda x: 1 / (1 + math.exp(-x))  # noqa: E731
    assert [r.index for r in first.results] == [1, 0, 2]
    assert [r.score for r in first.results] == pytest.approx([sigmoid(2), sigmoid(1), sigmoid(-1)])
    assert first.tokens == 2 + 4 + 2
    assert [(r.index, r.score) for r in second.results] == [(1, pytest.approx(sigmoid(2)))]
    assert second.tokens == 3 + 3
    assert model.batches


@pytest.mark.parametrize("logits, scores", [
    # One logit per pair: a one-logit head, or a causal-LM reranker's log-odds (sentence-transformers' LogitScore).
    ([[0.0], [2.0]], [0.5, 1 / (1 + math.exp(-2))]),
    # Two classes: the probability of the last, "relevant".
    ([[0.0, 0.0], [0.0, math.log(3)]], [0.5, 0.75]),
])
def test_rerank_scores_are_a_sigmoid_of_one_logit_or_the_relevant_class(logits, scores):
    torch = pytest.importorskip("torch")
    assert RerankModel("m", "rerank")._scores(torch.tensor(logits)) == pytest.approx(scores)


@pytest.mark.parametrize("cls, loader", [(EmbeddingModel, "SentenceTransformer"), (RerankModel, "CrossEncoder")])
def test_a_model_loads_with_its_backends_settings(monkeypatch, cls, loader):
    sentence_transformers = pytest.importorskip("sentence_transformers")
    monkeypatch.setattr(sentence_transformers, loader, Loader)
    Loader.made.clear()
    settings = {"revision": "v2", "max_length": 128}
    model = cls("org/m", cls.tasks[0], "cpu", {"torch_dtype": "float32", **settings})
    model.load()

    [(model_id, kwargs)] = Loader.made
    assert model_id == "org/m" and kwargs["device"] == "cpu" and kwargs["revision"] == "v2"
    assert str(kwargs["model_kwargs"]["dtype"]) == "torch.float32"
    assert "max_length" not in kwargs and model.model.max_seq_length == 128
    assert set(cls.load_settings) == set(settings)
    # A max_length past the model's own (512 here) cuts nothing: it would let a long text past its positions.
    longer = cls("org/m", cls.tasks[0], "cpu", {"max_length": 4096})
    longer.load()
    assert longer.model.max_seq_length == 512
    # Its repo's own code runs only when TRUST_REMOTE_CODE_MODELS names it.
    assert kwargs["trust_remote_code"] is False
    monkeypatch.setattr(Config, "TRUST_REMOTE_CODE_MODELS", ["org/m"])
    cls("org/m", cls.tasks[0], "cpu").load()
    assert Loader.made[-1][1]["trust_remote_code"] is True and Loader.made[-1][1]["revision"] is None
    monkeypatch.setattr(Config, "TRUST_REMOTE_CODE", False)
    cls("org/m", cls.tasks[0], "cpu").load()
    assert Loader.made[-1][1]["trust_remote_code"] is False


def test_a_model_that_needs_its_own_code_names_the_setting_that_allows_it(monkeypatch):
    sentence_transformers = pytest.importorskip("sentence_transformers")

    class NeedsCode(Loader):
        def __init__(self, model_id, **kwargs):
            if not kwargs["trust_remote_code"]:
                raise ValueError("The repository contains custom code ... pass `trust_remote_code=True`")
            super().__init__(model_id, **kwargs)

    monkeypatch.setattr(sentence_transformers, "SentenceTransformer", NeedsCode)
    with pytest.raises(ValueError, match="add it to TRUST_REMOTE_CODE_MODELS"):
        EmbeddingModel("org/m", "embedding", "cpu").load()
    monkeypatch.setattr(Config, "TRUST_REMOTE_CODE_MODELS", ["org/m"])
    EmbeddingModel("org/m", "embedding", "cpu").load()


def test_a_rerankers_default_prompt_is_its_instruction(monkeypatch):
    sentence_transformers = pytest.importorskip("sentence_transformers")

    class Instructed(Loader):
        def __init__(self, model_id, **kwargs):
            super().__init__(model_id, **kwargs)
            self.prompts, self.default_prompt_name = {"web": "Given a web search query..."}, "web"

    monkeypatch.setattr(sentence_transformers, "CrossEncoder", Instructed)
    model = RerankModel("org/m", "rerank", "cpu")
    model.load()
    assert model.prompt == "Given a web search query..."
    monkeypatch.setattr(sentence_transformers, "CrossEncoder", Loader)
    model.load()
    assert model.prompt is None


def test_a_models_prompts_are_those_of_its_sentence_transformers_config(monkeypatch):
    # As Qwen/Qwen3-Embedding-0.6B has them, with a value that is no text left out.
    configs = {"config_sentence_transformers.json": {"prompts": {"query": "Instruct: x\nQuery:", "document": "", "n": 1}}}
    asked = []

    def st_config(model_id, name, revision=None):
        asked.append(revision)
        return configs.get(name)

    monkeypatch.setattr(embedding_model, "st_config", st_config)
    assert EmbeddingModel.prompts("m") == {"query": "Instruct: x\nQuery:", "document": ""}
    assert EmbeddingModel.prompts("m", "b" * 40) and asked[-1] == "b" * 40
    configs["config_sentence_transformers.json"] = {"prompts": None}
    assert EmbeddingModel.prompts("m") == {}
    configs.clear()
    assert EmbeddingModel.prompts("m") == {}
