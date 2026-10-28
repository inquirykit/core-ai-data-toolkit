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
export const ENTITY_TYPES: EntityType[] = [
  "ipv4", "ipv6", "cidr", "asn", "domain", "url", "email", "phone", "md5", "sha1", "sha256", "cve", "crypto_wallet", "generic",
];
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
export type TraceSummary = Schemas["TraceSummary"];
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

// -------------------------------------------------------------------- hub
export type HubSearchResult = Schemas["HubSearchResult"];
export type HubSearch = Schemas["HubSearch"];
export type HubRepo = Schemas["HubRepo"];
export type HubDownload = Schemas["HubDownload"];
export type HubDownloadStatus = HubDownload["status"];
export type HubLocalRepo = Schemas["HubLocalRepo"];
export type HubLocal = Schemas["HubLocal"];

// ------------------------------------------------------------------- jobs
export type Job = Schemas["Job"];
export type JobStatus = Job["status"];
/** How urgent a job, a schedule's jobs or an eval run is: queued work starts the most urgent first */
export type Priority = Job["priority"];
export type JobType = Schemas["JobType"];
/** The body of POST /jobs */
export type SubmitJobBody = Schemas["SubmitJob"];
export type Schedule = Schemas["Schedule"];
/** The body of POST /jobs/schedules and PUT /jobs/schedules/{id}: one of ``cron`` and ``interval_s`` */
export type ScheduleBody = Schemas["ScheduleBody"];
export type Webhooks = Schemas["Webhooks"];
export type WebhookTest = Schemas["WebhookTestResult"];

// --------------------------------------------------------------- evidence
export type EvidenceWorkspace = Schemas["EvidenceWorkspace"];
export type EvidenceCase = Schemas["EvidenceCase"];
export type CaptureSummary = Schemas["CaptureSummary"];
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

/** A status in words with the colour of its dot: what the tables of statuses (jobs, runs, downloads…) hold. */
export interface Status {
  label: string;
  color: string;
}
