"""A trained model is an agreement between the job that trains it (jobs/) and the family that serves it
(models/families/): the model ID the job writes in its result.json, the task, the folder it saves to, and for
AutoGluon the module its process runs. Neither side may import the other (AGENTS.md), so each keeps its own copy, and
this ties them; the pages' copy of the AutoGluon prefix too.
"""

import importlib.util
import re
from pathlib import Path

import pytest

from jobs.autogluon import _common as autogluon_job
from jobs.train import text_classifier as classifier_job
from models import registry
from models.families.autogluon import predictor
from models.families.trained import classifier

ROOT = Path(__file__).resolve().parent.parent
JOB_ID = "ab12cd34"


@pytest.mark.parametrize("prefix, task, family", [
    (classifier_job.MODEL_ID_PREFIX, classifier_job.TASK, classifier.TrainedClassifier),
    (autogluon_job.MODEL_ID_PREFIX, autogluon_job.TABULAR_TASK, predictor.AutoGluonPredictor),
    (autogluon_job.MODEL_ID_PREFIX, autogluon_job.TIMESERIES_TASK, predictor.AutoGluonPredictor),
])
def test_the_id_a_job_writes_is_served_by_its_family(prefix, task, family):
    assert registry.entry_for(prefix + JOB_ID, task).cls is family


@pytest.mark.parametrize("result", [classifier_job.ClassifierResult, autogluon_job.TrainResult])
def test_a_trainers_result_names_what_models_trained_reads(result):
    """models/trained.py reads a job's result.json for these two."""
    assert {"model_id", "task"} <= set(result.model_fields)


def test_the_folders_agree():
    assert classifier_job.MODEL_DIR == classifier.MODEL_DIR
    assert autogluon_job.PREDICTOR_DIR == predictor.PREDICTOR_DIR


def test_the_predictor_process_runs_a_module_that_is_there():
    assert importlib.util.find_spec(predictor.AutoGluonPredictor.serve_module) is not None


def test_the_pages_know_the_autogluon_prefix():
    source = (ROOT / "frontend/src/shared/model-browser/catalog.ts").read_text()
    found = re.search(r'export const AUTOGLUON_PREFIX = "([^"]*)";', source)
    assert found and found.group(1) == autogluon_job.MODEL_ID_PREFIX
