// The API's answers, named for the pages. Each is the generated type of its
// response model (api/responses.py, through openapi.json; `npm run gen:api`
// after changing one), so the pages and the API cannot drift apart. Only
// what has no model of its own is typed by hand here.
import type { Schemas } from "./api";

/** GET /models/list: task -> model IDs. */
export type Catalog = Record<string, string[]>;

export type Limits = Schemas["Limits"];
export type SetupResult = Schemas["SetupResult"];

// ------------------------------------------------------------------ /admin
export type TimelinePoint = Schemas["TimelinePoint"];
export type ModelStats = Schemas["ModelStats"];
export type LoadedModel = Schemas["LoadedModel"];
export type ProcessInfo = Schemas["ProcessInfo"];
/** GET /admin/stats */
export type Stats = Schemas["Stats"];
export type StatsWindow = Stats["window"];
/** GET /admin/timeline: the request metrics of GET /admin/stats over any range. */
export type Timeline = Schemas["Timeline"];
export type ModelEvent = Schemas["ModelEvent"];
/** A change made through the API, as the audit log keeps it (GET /admin/audit) */
export type AuditEntry = Schemas["AuditEntry"];
export type RequestRow = Schemas["RequestRow"];
export type LogLine = Schemas["LogLine"];
export type Logs = Schemas["Logs"];
export type AdminConfig = Schemas["AdminConfig"];

// ------------------------------------------------------------ model servers
/** GET /admin/model-servers, and the reply of every change to it. */
export type ModelServers = Schemas["ModelServers"];
export type ModelServerInfo = Schemas["ModelServerInfo"];
export type ProviderInfo = Schemas["ProviderInfo"];
/** One backend as given: what it leaves out is its provider's setting, or the default. */
export type Backend = Schemas["Backend"];
/** PUT /admin/model-servers/models/{id} body. */
export type ModelEntry = Schemas["ModelEntry"];
export type ModelTask = ModelEntry["task"];
/** PUT /admin/model-servers/providers/{name} body. */
export type Provider = Schemas["Provider"];
/** POST /admin/model-servers/probe */
export type ModelServerProbe = Schemas["ModelServerProbe"];
export type BackendProbe = Schemas["BackendProbe"];

// ---------------------------------------------------------------- enrichers
export type EntityType = Schemas["EntityType"];
/** Every type, in order: keyed by the API's, so tsc fails on one missing. */
const ENTITY_TYPE_ORDER: Record<EntityType, null> = {
  ipv4: null, ipv6: null, cidr: null, asn: null, domain: null, url: null, email: null, phone: null, md5: null, sha1: null,
  sha256: null, cve: null, crypto_wallet: null, generic: null,
};
export const ENTITY_TYPES = Object.keys(ENTITY_TYPE_ORDER) as EntityType[];
/** An enricher's request, as the API's answers carry it: every field, defaults included. */
type EnricherRequest = Schemas["RequestSpec-Output"];
/** PUT /enrichers/{name} body; what the file holds. */
export type EnricherSpec = Omit<Schemas["EnricherInfo"], "builtin" | "overridden" | "secret_set" | "allowed" | "usable" | "state" | "request"> & {
  request: EnricherRequest;
};
export type EnricherTerms = EnricherSpec["terms"];
export type ValueFilter = EnricherSpec["value_filters"][number];
/** One entry of GET /enrichers, and the reply of GET/PUT /enrichers/{name}. */
export type EnricherInfo = Schemas["EnricherInfo"];
export type EnricherList = Schemas["EnricherList"];
export type EnrichResult = Schemas["EnrichResult"];
export type EnrichStatus = EnrichResult["status"];
export type EnrichResponse = Schemas["EnrichResponse"];
/** The bodies of POST /enrich and POST /enrichers/{name}/test */
export type EnrichBody = Schemas["EnrichRequest"];
export type EnricherTestBody = Schemas["TestRequest"];
export type EnricherTest = Schemas["EnricherTest"];

// ------------------------------------------------------------ traces, evals
export type Trace = Schemas["Trace"];
export type TraceCapture = Schemas["TraceCapture"];
export type Dataset = Schemas["Dataset"];
export type Example = Schemas["DatasetExample"];
export type RunActivity = Schemas["RunActivity"];
export type Run = Schemas["Run"];
export type RunStatus = Run["status"];
export type Score = Schemas["Score"];
export type RunItem = Schemas["RunItem"];
export type MetricSummary = Schemas["MetricSummary"];
export type RunDetail = Schemas["RunDetail"];
export type DatasetDetail = Schemas["DatasetDetail"];
export type CompareSide = Schemas["CompareSide"];
export type CompareRow = Schemas["CompareRow"];
export type Comparison = Schemas["Comparison"];
export type MetricsInfo = Schemas["MetricsInfo"];

// ------------------------------------------------------------ tasks, jobs
/** A task of any kind on the one queue (GET /queue): a job, an eval run, a download, a copy... */
export type Task = Schemas["Task"];
export type TaskStatus = Task["status"];
/** A task of the kind job, as the Jobs section shows it */
export type Job = Task;
/** How urgent a job, a schedule's jobs or an eval run is: queued work starts the most urgent first */
export type Priority = Task["priority"];
export type JobType = Schemas["JobType"];
/** The body of POST /jobs */
export type SubmitJobBody = Schemas["SubmitJob"];
export type Schedule = Schemas["Schedule"];
/** The body of POST /jobs/schedules and PUT /jobs/schedules/{id}: one of ``cron`` and ``interval_s`` */
export type ScheduleBody = Schemas["ScheduleSpec"];
export type Webhooks = Schemas["Webhooks"];
/** One event's delivery to one URL, retried with backoff */
export type WebhookDelivery = Schemas["WebhookDelivery"];
export type WebhookTest = Schemas["WebhookTestResult"];

// -------------------------------------------------------------- workflows
/** A workflow as it is saved and sent: its nodes, edges, page size and trigger */
export type WorkflowSpec = Schemas["WorkflowSpec"];
export type WorkflowNode = Schemas["Node"];
export type WorkflowEdge = Schemas["Edge"];
export type WorkflowTrigger = Schemas["Trigger"];
/** A saved workflow: its spec with its id, schedule and times */
export type WorkflowInfo = Schemas["WorkflowInfo"];
/** A node type of the editor's palette, with its settings' JSON Schema */
export type WorkflowNodeType = Schemas["WorkflowNodeType"];
export type WorkflowRun = Schemas["WorkflowRun"];
export type WorkflowDryRun = Schemas["WorkflowDryRun"];
export type WorkflowNodeCount = Schemas["WorkflowNodeCount"];
export type WorkflowEdgeCount = Schemas["WorkflowEdgeCount"];

// --------------------------------------------------------------- evidence
export type EvidenceCase = Schemas["EvidenceCase"];
export type CaptureSummary = Schemas["CaptureSummary"];
/** A capture an evidence search found (GET /evidence/search), from any case of the workspace */
export type SearchHit = Schemas["SearchHit"];
export type VerifyReport = Schemas["VerifyReport"];
export type VerifyIssue = Schemas["VerifyIssue"];

// The signed record itself is a format of its own (evidence/record.py), which the API serves as it is.
interface CaptureArtifact {
  name: string;
  mediaType: string;
  size: number;
  sha256: string;
}

/** The signed record (schema evidence-capture/record/v3; v2 before workspaces) */
export type CaptureRecord = {
  schema: string;
  seq: number;
  captureId: string;
  /** The workspace whose chain it is in; v2 records have none (they are the default workspace's) */
  workspaceId?: string;
  caseId: string;
  receivedAt: string;
  prevRecordHash: string;
  service: { name: string; version: string; signingKeySha256: string };
  artifacts: CaptureArtifact[];
  /** What the extension sent, verbatim */
  client: Record<string, unknown>;
  enrichment: { skipped: true } | { startedAt: string; finishedAt: string; items: EnrichmentItem[] };
};

export interface EnrichmentItem {
  value: string;
  type: string;
  results?: EnrichResponse;
  error?: string;
}

/** GET /evidence/captures/{id} */
export type Capture = Omit<Schemas["Capture"], "record"> & { record: CaptureRecord };

/** A status in words with the colour of its dot: what the tables of statuses (jobs, runs, tasks…) hold. */
export interface Status {
  label: string;
  color: string;
}
