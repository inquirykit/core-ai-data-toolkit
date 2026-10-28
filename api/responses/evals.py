"""/admin/traces, /admin/datasets, /admin/runs: traces, datasets, runs, scores, the judge."""

from typing import Any, Dict, List, Literal, Optional


from evals.scoring import METRICS
from .common import Json, Out


class TraceSummary(Out):
    id: str
    ts: float
    model_id: str
    task: str
    status: str
    duration_ms: float
    cold: bool
    error: Optional[str]
    tags: List[str]
    session: Optional[str]
    # Which backend of a model server answered, e.g. "ollama: qwen3.5:4b" (absent for other models)
    backend: Optional[str] = None
    input_chars: int
    output_chars: int
    # The first words of the input and output
    preview: str


class Trace(TraceSummary):
    input: Json
    output: Optional[Json]


class TraceCapture(Out):
    enabled: bool
    retention_days: float
    max_chars: int
    dropped: int


class Traces(Out):
    traces: List[TraceSummary]
    capture: TraceCapture


class Dataset(Out):
    id: str
    name: str
    description: str
    created: float
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


class DatasetExamples(Out):
    examples: List[DatasetExample]
    total: int


RunStatus = Literal["queued", "running", "done", "failed", "cancelled", "interrupted"]


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
    status: RunStatus
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


class RunDetail(Run):
    items: List[RunItem]
    summary: Dict[str, MetricSummary]


class RunSummary(Run):
    summary: Dict[str, MetricSummary]


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
    base_url: str
    has_api_key: bool
    rubric: str


class MetricsInfo(Out):
    metrics: List[MetricInfo]
    judge: JudgeDefaults
