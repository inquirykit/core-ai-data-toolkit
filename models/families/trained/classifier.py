"""
Classifiers the text-classifier job trained (jobs/train/text_classifier.py),
served as model ID "trained:<job id>" for text-classification (models/trained.py
finds them).

Such a model is an ordinary Transformers sequence classifier in the job's
model/ folder, so it is the generic classifier (TransformersModel), which
loads from its weights_source, that folder: in this process, batched, cached and evicted like any Hub
classifier. Deleting the job (DELETE /jobs/{id}) deletes the model.
"""

from pathlib import Path
from typing import List, Optional

from models import ModelCard, register, trained

from ..generic.transformers_model import TransformersModel

# What the text-classifier job writes: the model ID in its result, its task, and the folder it saves the model to
# (jobs/train/text_classifier.py; tests/test_trained_models.py ties the two).
MODEL_ID_PREFIX = "trained:"
TASK = "text-classification"
MODEL_DIR = "model"


def model_dir(model_id: str) -> Optional[Path]:
    """The folder of the classifier ``trained:<job id>`` names, or None if no job here trained one."""
    found = trained.find(model_id, MODEL_ID_PREFIX)
    return found.workdir / MODEL_DIR if found is not None and found.task == TASK else None


@register(task=TASK, match=f"{MODEL_ID_PREFIX}*")
class TrainedClassifier(TransformersModel):
    """A text classifier fine-tuned by a job here, loaded from the job's folder."""

    @classmethod
    def listed(cls, task: str) -> List[ModelCard]:
        return [ModelCard(t.model_id, t.name) for t in trained.listed(MODEL_ID_PREFIX, task)]

    @classmethod
    def weights_source(cls, model_id: str) -> Optional[str]:
        path = model_dir(model_id)
        return str(path) if path else None

    @classmethod
    def operator_chose(cls, model_id: str) -> bool:
        return model_dir(model_id) is not None  # a job here trained it, and only an admin starts jobs
