import subprocess
import sys
from pathlib import Path

import pytest

from models import BaseModel, UnsupportedModel, registry
from models.families.custom.custom_model import CustomModel
from models.families.decision.vega import VegaDecisionModel
from models.families.generic.transformers_model import TransformersModel
from models.families.generic.vision_model import ImageTextToTextModel
from models.families.vision.fastvlm import FastVLMModel
from tests.fakes import registered

ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.parametrize("model_id, task, cls", [
    # Listed catalog entries, all served by the generic Hugging Face class.
    ("Qwen/Qwen2.5-0.5B-Instruct", "text-generation", TransformersModel),
    ("LiquidAI/LFM2.5-230M", "text-generation", TransformersModel),
    ("AnkitAI/Sensible-ModernBERT-Sentiment-Analysis", "text-classification", TransformersModel),
    ("deepset/tinyroberta-squad2", "question-answering", TransformersModel),
    ("apple/FastVLM-1.5B", "vision-language", FastVLMModel),
    ("apple/FastVLM-0.5B", "vision-language", FastVLMModel),
    ("vega-4b", "decision", VegaDecisionModel),
    ("nandakishorm/vega-08b-public-intents", "decision", VegaDecisionModel),
    ("custom:thing", "custom", CustomModel),
    # Nobody claims these IDs, so the generic Hugging Face model serves them.
    ("Qwen/Qwen2-0.5B-Instruct", "text-generation", TransformersModel),
    ("org/classifier", "text-classification", TransformersModel),
    ("deepset/roberta-base-squad2", "question-answering", TransformersModel),
    # Other FastVLM checkpoints are native in Transformers: no Hub code needed.
    ("someone/my-FastVLM-tune", "vision-language", ImageTextToTextModel),
    ("Qwen/Qwen2.5-VL-3B-Instruct", "vision-language", ImageTextToTextModel),
    # The 2026 catalog entries are served by the generic classes too.
    ("Qwen/Qwen3.5-0.8B", "vision-language", ImageTextToTextModel),
    ("LiquidAI/LFM2.5-1.2B-Instruct", "text-generation", TransformersModel),
    ("deepset/deberta-v3-base-squad2", "question-answering", TransformersModel),
])
def test_builtin_routing(model_id, task, cls):
    assert registry.resolve(model_id, task) is cls


@pytest.mark.parametrize("model_id, task", [
    ("vega-0.8b", "text-generation"),  # claimed by vega, which only does decisions
    ("Qwen/Qwen2.5-0.5B-Instruct", "decision"),
    ("Qwen/Qwen2.5-0.5B-Instruct", "text-classification"),
    ("apple/FastVLM-1.5B", "text-generation"),
    ("org/model", "no-such-task"),
])
def test_wrong_task_is_rejected(model_id, task):
    with pytest.raises(UnsupportedModel):
        registry.resolve(model_id, task)


def test_one_decorated_class_is_all_a_new_model_needs():
    class Shouter(BaseModel):
        def load(self):
            pass

        def predict(self, text):
            return text.upper()

    with registered(Shouter, task="shouting", ids=["shout-1"], match="shout-*"):
        assert registry.resolve("shout-1", "shouting") is Shouter
        assert registry.resolve("shout-xl", "shouting") is Shouter
        assert registry.catalog()["shouting"] == ["shout-1"]
        assert Shouter("shout-1").task == "shouting"
    assert "shouting" not in registry.known_tasks()


def test_a_later_file_can_take_over_an_id():
    class MyGPT2(BaseModel):
        def load(self):
            pass

        def predict(self, text):
            return text

    with registered(MyGPT2, task="text-generation", ids=["Qwen/Qwen2.5-0.5B-Instruct"]):
        assert registry.resolve("Qwen/Qwen2.5-0.5B-Instruct", "text-generation") is MyGPT2
    assert registry.resolve("Qwen/Qwen2.5-0.5B-Instruct", "text-generation") is TransformersModel


def test_only_a_class_that_claims_an_id_can_say_the_operator_chose_it():
    """The allow-list asks the class serving an ID, so the API names no family; a fallback serving any ID cannot vouch."""
    class Chosen(BaseModel):
        @classmethod
        def operator_chose(cls, model_id):
            return model_id != "mine:not-this-one"

        def load(self):
            pass

        def predict(self, text):
            return text

    with registered(Chosen, task="choosing", match="mine:*"):
        assert registry.operator_chose("mine:trained")
        assert not registry.operator_chose("mine:not-this-one")
    with registered(Chosen, task="choosing", fallback=True):
        assert not registry.operator_chose("someone/else")
    assert not registry.operator_chose("Qwen/Qwen2.5-0.5B-Instruct")


def test_restored_puts_back_what_a_test_registered_swapped_or_dropped():
    class Mine(BaseModel):
        def load(self):
            pass

        def predict(self, text):
            return text

    before, custom_tasks = registry.describe(), CustomModel.tasks
    with registry.restored():
        registry.register(task="mine", ids=["mine-1"])(Mine)
        registry.swap(TransformersModel, Mine)
        registry.unregister(CustomModel)
        assert registry.resolve("org/classifier", "text-classification") is Mine
        assert "custom" not in registry.known_tasks()
    assert registry.describe() == before
    assert CustomModel.tasks == custom_tasks
    assert registry.resolve("org/classifier", "text-classification") is TransformersModel


def test_model_files_are_found_without_being_imported():
    # A fresh interpreter, so nothing has imported the model files yet.
    code = "from models import registry; print(sorted({e['class'].rsplit('.', 1)[1] for e in registry.describe()}))"
    out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, check=True)
    assert out.stdout.strip() == str(sorted({
        "AutoGluonPredictor", "CustomModel", "FastVLMModel", "EmbeddingModel", "ImageTextToTextModel",
        "RerankModel", "SpeechToTextModel", "TransformersModel", "VegaDecisionModel", "ZeroShotModel",
    }))


def test_cards_describe_listed_ids_and_later_ones_fill_in():
    class Card(BaseModel):
        def load(self):
            pass

        def predict(self, text):
            return text

    from models import ModelCard

    with registered(Card, task="carding", ids=[ModelCard("c-1", "First", ["chat"]), "c-2"], match="c-*"):
        assert registry.catalog()["carding"] == ["c-1", "c-2"]
        cards = registry.cards()["carding"]
        assert cards["c-1"] == ModelCard("c-1", "First", ("chat",))
        assert cards["c-2"] == ModelCard("c-2")
        assert registry.patterns("carding") == ["c-*"] and not registry.accepts_any_id("carding")
        # A later registration (a model server, say) wins, keeping what it leaves out.
        with registered(Card, task="carding", ids=[ModelCard("c-1", tags=["fast"])]):
            assert registry.cards()["carding"]["c-1"] == ModelCard("c-1", "First", ("fast",))
    assert registry.accepts_any_id("text-generation")
