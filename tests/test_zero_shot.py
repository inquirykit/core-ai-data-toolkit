import math
from types import SimpleNamespace

import pytest

from models import contracts
from models.families.generic.zero_shot_model import ZeroShotModel, nli_classes, zero_shot_result, zero_shot_scores


def request(**fields):
    """A request parsed the way the manager does it before run()."""
    return contracts.get("zero-shot-classification").parse_input(fields)


@pytest.mark.parametrize("id2label, classes", [
    ({0: "contradiction", 1: "neutral", 2: "entailment"}, (2, 0)),  # facebook/bart-large-mnli
    ({0: "entailment", 1: "neutral", 2: "contradiction"}, (0, 2)),  # mDeBERTa xnli
    ({0: "entailment", 1: "not_entailment"}, (0, 1)),  # the zeroshot-v2.0 models
    ({0: "ENTAILMENT", 1: "NEUTRAL"}, (0, 1)),  # two classes, none named against it: the other one
])
def test_nli_classes_find_entailment_and_what_it_is_weighed_against(id2label, classes):
    assert nli_classes(id2label) == classes


@pytest.mark.parametrize("id2label", [
    {0: "NEGATIVE", 1: "POSITIVE"},
    # Three classes and none a contradiction: which to weigh entailment against would be a guess.
    {0: "entailment", 1: "neutral", 2: "other"},
])
def test_a_model_without_entailment_and_contradiction_is_refused(id2label):
    with pytest.raises(ValueError, match="not an NLI model"):
        nli_classes(id2label)


def test_labels_share_the_probability_unless_each_is_scored_alone():
    rows = [(0.0, 2.0), (0.0, 0.0), (5.0, -1.0)]
    shared = zero_shot_scores(rows, multi_label=False)
    assert sum(shared) == pytest.approx(1) and shared[0] > shared[1] > shared[2]
    own = zero_shot_scores(rows, multi_label=True)
    assert own == pytest.approx([1 / (1 + math.exp(-2)), 0.5, 1 / (1 + math.exp(6))])


def test_the_result_ranks_the_labels_and_scores_one_label_alone():
    result = zero_shot_result(["politics", "sports"], [(0.0, -1.0), (0.0, 3.0)], multi_label=False)
    assert result.label == "sports" and list(result.scores) == ["sports", "politics"]
    # Shared among one label it would always be 1.
    alone = zero_shot_result(["sports"], [(1.0, 0.0)], multi_label=False)
    assert alone.confidence == pytest.approx(1 / (1 + math.e))


@pytest.mark.parametrize("fields, problem", [
    ({"labels": []}, "at least 1"),
    ({"labels": ["a", "a"]}, "labels must differ"),
    ({"labels": [" a ", "a"]}, "labels must differ"),  # trimmed first
    ({"labels": ["a", "  "]}, "at least 1 character"),
    ({"labels": [str(i) for i in range(contracts.MAX_LABELS + 1)]}, "at most"),
    ({"labels": ["a"], "hypothesis": "About this"}, "hold {} once"),
])
def test_the_contract_refuses_bad_labels_and_hypotheses(fields, problem):
    with pytest.raises(ValueError, match=problem):
        request(text="t", **fields)


def test_predict_batch_scores_each_requests_labels_across_shared_passes():
    torch = pytest.importorskip("torch")

    class Encoding(dict):
        def to(self, device):
            return self

    class Tokenizer:
        pad_token = "[PAD]"

        def __call__(self, texts, hypotheses, **options):
            # The longer of text and hypothesis is cut: the text, unless the hypothesis alone nears the limit (a
            # 500-character hypothesis failed the request with only_first, on deberta-v3-xsmall-zeroshot-v1.1).
            assert options["truncation"] == "longest_first"
            return Encoding(hypotheses=hypotheses)

    class Model:
        # mDeBERTa's xnli order: entailment, neutral, contradiction. It entails any hypothesis about sports.
        config = SimpleNamespace(pad_token_id=0)
        device = "cpu"

        def __call__(self, hypotheses):
            return SimpleNamespace(logits=torch.tensor([[3.0 if "sports" in h else -3.0, 0.0, 0.0] for h in hypotheses]))

    model = ZeroShotModel("m", "zero-shot-classification")
    model.tokenizer, model.model, model.entail, model.against = Tokenizer(), Model(), 0, 2
    first, second = model.predict_batch([
        request(text="A long report of last night's match, and of the vote after it", labels=["politics", "sports"]),
        request(text="Goal!", labels=["weather", "sports"], multi_label=True, hypothesis="It is {}."),
    ])
    assert first.label == "sports" and first.confidence == pytest.approx(1 / (1 + math.exp(-6)))
    assert second.scores == pytest.approx({"sports": 1 / (1 + math.exp(-3)), "weather": 1 / (1 + math.exp(3))})
