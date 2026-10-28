"""
Response models: what the routes that build their answers as dicts answer with.

They put those answers in the OpenAPI schema, so the browser pages' types
are generated from it (frontend/src/shared/types.ts aliases them), and check
each answer as it goes out. Every one allows fields beyond those declared,
so an answer is never cut down to its model: a field the pages do not read
yet still reaches other clients.

A field without a default is required in the schema even when it may be
null (``Optional[x]`` with no default), so the generated types say "always
there" where it is.
"""

from .common import OPEN, Deleted, Json, ModelStatus, Out, answering  # noqa: F401
from .admin import (  # noqa: F401
    TimelinePoint, ModelStats, Totals, Timeline, LoadedModel, CacheStatus, Gpu, HostMemory, ProcessInfo,
    MonitoringInfo, Stats, ModelEvent, ModelEvents, RequestRow, RequestRows, LogLine, Logs, AdminConfig,
    LimitsResult,
)
from .models import (  # noqa: F401
    SetupStatus, SetupResult, SetupSkipped, ModelMessage, Catalog, TaskInfo, TaskLimits, Tasks, OpenAIModel,
    OpenAIModels, ModelType, ModelTypes, Limits,
)
from .playground import (  # noqa: F401
    ConversationSummary, Conversations, Preset, Presets, ExampleAdded,
)
from .servers import (  # noqa: F401
    Origin, ProviderInfo, ModelServerInfo, ModelServerProblem, ModelServers, BackendProbe, ModelServerProbe,
)
from .enrichers import (  # noqa: F401
    EnricherState, EnricherInfo, EnricherList, EnrichStatus, EnrichResult, EnrichResponse, SentRequest,
    SecretSaved, EnricherTest,
)
from .evals import (  # noqa: F401
    TraceSummary, Trace, TraceCapture, Traces, Dataset, Datasets, DatasetExample, DatasetExamples, RunStatus,
    RunActivity, Run, Runs, DatasetDetail, Score, RunItem, MetricSummary, RunDetail, RunSummary, Scored,
    JudgeStarted, ExamplesAdded, CompareSide, CompareRow, Comparison, MetricName, MetricInfo, JudgeDefaults,
    MetricsInfo,
)
from .hub import (  # noqa: F401
    HubSearchResult, HubSearch, HubFile, HubRepo, HubDownload, HubDownloads, HubLocalRepo, HubLocal,
)
from .jobs import (  # noqa: F401
    JobStatus, Job, Jobs, JobType, JobLimits, JobTypes, Priority, Schedule, Schedules, Webhooks, WebhookTestResult,
)
from .evidence import (  # noqa: F401
    EvidenceWorkspace, EvidenceWorkspaces, WorkspaceCreated, EvidenceCase, EvidenceCases, CaseCreated,
    CaptureSummary, CaptureSummaries, CaptureTimestamp, TimestampRetry, Capture, AnnotationNote, Annotation,
    Annotations, Todo, CaseTodos, VerifyIssue, VerifyReport,
)
from .tables import (  # noqa: F401
    Row, FieldInfo, TableInfo, EdgeTypeInfo, Namespace, Namespaces, ObservedField, Observed, SchemaInfo,
    TableSchemaOut, RowsPage, RowCount, GraphEdge, GraphNode, GraphMore, GraphView, EdgesPage, Neighbor,
    NeighborsPage, RowList, EdgeJoin, EdgeTypeDetail, EdgeTypes,
)
