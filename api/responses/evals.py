"""/admin/datasets, /admin/runs: datasets, runs, scores, the judge."""

from typing import Any, Dict, List, Literal, Optional


from evals.scoring import METRICS
from .tasks import TaskStatus
from .common import Json, Out


class Dataset(Out):
    id: str
    name: str
    description: str
    created: float
    # The choices a reference is picked from when labeling; empty: typed
    labels: List[str]
    examples: int
    runs: int


class Datasets(Out):
    datasets: List[Dataset]


class DatasetExample(Out):
    id: str
    dataset_id: str
    created: float
    task: Optional[str]
    input: Json
    reference: Any
    preview: str
    tags: List[str]
    source_trace_id: Optional[str]
    source_model_id: Optional[str]
    # What the traced model answered, when the example came from a trace
    source_output: Optional[Json]


class ExampleToLabel(DatasetExample):
    # What the chosen run answered for it, else what the traced model did
    suggestion: Optional[Json]


class ExamplesToLabel(Out):
    examples: List[ExampleToLabel]
    # The examples without a reference
    left: int


class DatasetExamples(Out):
    examples: List[DatasetExample]
    total: int
    next: Optional[str] = None


class RunActivity(Out):
    kind: Literal["queued", "running", "judging"]
    done: Optional[int] = None
    total: Optional[int] = None
    error: Optional[str] = None


class Run(Out):
    id: str
    dataset_id: str
    name: str
    model_id: str
    task: Optional[str]
    params: Json
    # Its newest task's
    status: TaskStatus
    created: float
    started: Optional[float]
    finished: Optional[float]
    total: int
    done: int
    failed: int
    error: Optional[str]
    activity: Optional[RunActivity] = None


class Runs(Out):
    runs: List[Run]
    next: Optional[str] = None


class DatasetDetail(Dataset):
    run_list: List[Run]


class Score(Out):
    id: str
    run_id: str
    item_id: str
    example_id: str
    metric: str
    value: Optional[float]
    label: Optional[str]
    rationale: Optional[str]
    judge: Optional[str]
    # The other run, for a pairwise judgment
    against: Optional[str]


class RunItem(Out):
    id: str
    run_id: str
    example_id: str
    created: float
    output: Optional[Json]
    status: str
    error: Optional[str]
    duration_ms: float
    cold: bool
    input: Json
    reference: Any
    task: Optional[str]
    source_output: Optional[Json]
    source_model_id: Optional[str]
    scores: List[Score]


class MetricSummary(Out):
    count: int
    scored: int
    mean: Optional[float]
    wins: Optional[int] = None
    ties: Optional[int] = None
    losses: Optional[int] = None
    # Set for a metric where less is better (word error rate)
    lower_is_better: Optional[bool] = None


class LabelScores(Out):
    label: str
    precision: float
    recall: float
    f1: float
    # The items whose reference is this label
    support: int


class ClassificationReport(Out):
    """A run whose outputs are labels, over its items with a reference."""
    count: int
    accuracy: float
    macro_f1: float
    labels: List[LabelScores]
    # A row per reference label, a column per predicted one, in labels' order; none past 50 labels
    confusion: Optional[List[List[int]]]


class RunDetail(Run):
    items: List[RunItem]
    summary: Dict[str, MetricSummary]
    classification: Optional[ClassificationReport] = None


class RunSummary(Run):
    summary: Dict[str, MetricSummary]
    classification: Optional[ClassificationReport] = None


class Scored(Out):
    """POST /admin/runs/{id}/score: the run's summary per metric, the new scores included."""
    summary: Dict[str, MetricSummary]


class JudgeStarted(Out):
    started: str
    # The other run, when judging pairwise
    against: Optional[str]
    judge: str


class ExamplesAdded(Out):
    added: int


class TrainingFile(Out):
    """POST /admin/datasets/{id}/training-file: what it wrote."""
    # Inside DATA_DIR/datasets: the job's train_file
    path: str
    rows: int
    # Each label and how many examples it has
    labels: Dict[str, int]


class CompareSide(Out):
    id: str
    output: Optional[Json]
    status: str
    error: Optional[str]
    duration_ms: float
    scores: List[Score]


class CompareRow(Out):
    example_id: str
    input: Json
    reference: Any
    a: CompareSide
    b: Optional[CompareSide]


class Comparison(Out):
    a: RunSummary
    b: RunSummary
    rows: List[CompareRow]


# The metrics POST /admin/runs/{id}/score takes
MetricName = Literal[tuple(METRICS)]  # type: ignore[valid-type]


class MetricInfo(Out):
    name: MetricName
    description: str


class JudgeDefaults(Out):
    model: str
    rubric: str


class MetricsInfo(Out):
    metrics: List[MetricInfo]
    judge: JudgeDefaults
