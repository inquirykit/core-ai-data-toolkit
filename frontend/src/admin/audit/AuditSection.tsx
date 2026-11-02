// /admin#/audit: the audit log (GET /admin/audit), newest first: every change
// made through the admin's routes and the setup wizard, and every delete, with
// the key it carried, where it came from and how it went. Filters and paging
// are the server's; helpers in audit.ts.
import { useState, type FormEvent } from "react";
import { History } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import type { ApiClient } from "../../shared/api";
import { EmptyCard, SelectInput, StatusLabel } from "../../shared/components";
import { fmtAgo, fmtClock, fmtMs } from "../../shared/format";
import { DataGrid, type GridColumn } from "../../shared/grid";
import { errorText } from "../../shared/query";
import type { AuditEntry } from "../../shared/types";
import {
  ACTOR_OPTIONS,
  actorLabel,
  AUDIT_PAGE,
  auditQuery,
  NO_FILTERS,
  outcomeOf,
  type AuditActor,
  type AuditFilters,
  type AuditMethod,
  type AuditOutcome,
} from "./audit";
import { LoadedCard, Pager, useCursorPages } from "../layout";

const METHODS: { value: AuditMethod; label: string }[] = (["POST", "PUT", "PATCH", "DELETE"] as const).map((m) => ({ value: m, label: m }));
const OUTCOMES: { value: AuditOutcome; label: string }[] = [
  { value: "ok", label: "Done" },
  { value: "refused", label: "Refused (4xx)" },
  { value: "failed", label: "Failed (5xx)" },
];

const COLUMNS: GridColumn<AuditEntry>[] = [
  {
    key: "ts",
    label: "When",
    className: "whitespace-nowrap tabular-nums",
    title: (e) => fmtClock(e.ts, true),
    render: (e) => fmtAgo(e.ts),
  },
  {
    key: "change",
    label: "Change",
    className: "max-w-[30rem]",
    render: (e) => (
      <>
        <span className="flex min-w-0 items-center gap-2">
          <Badge variant={e.method === "DELETE" ? "destructive" : "secondary"} className="font-mono text-[11px]">
            {e.method}
          </Badge>
          <span className="truncate font-mono text-[13px]" title={e.path}>
            {e.path}
          </span>
        </span>
        <div className="truncate font-mono text-xs text-muted-foreground">{e.route}</div>
      </>
    ),
  },
  { key: "status", label: "Outcome", className: "whitespace-nowrap", render: (e) => <StatusLabel status={outcomeOf(e.status)} /> },
  { key: "actor", label: "Key", className: "whitespace-nowrap", render: (e) => actorLabel(e.actor) },
  {
    key: "from",
    label: "From",
    className: "max-w-56",
    render: (e) => (
      <>
        <div className="truncate font-mono text-xs" title={e.forwarded ? `X-Forwarded-For: ${e.forwarded}` : undefined}>
          {e.forwarded ? `${e.forwarded} via ${e.ip ?? "?"}` : (e.ip ?? "–")}
        </div>
        {e.agent && (
          <div className="truncate text-xs text-muted-foreground" title={e.agent}>
            {e.agent}
          </div>
        )}
      </>
    ),
  },
  { key: "duration", label: "Took", align: "right", className: "whitespace-nowrap tabular-nums text-muted-foreground", render: (e) => fmtMs(e.duration_ms) },
];

export function AuditSection({ client }: { client: ApiClient }) {
  const [filters, setFilters] = useState<AuditFilters>(NO_FILTERS);
  const [route, setRoute] = useState("");
  // Paged by the last id of the page before (undefined: the newest page); new filters start from the newest.
  const pages = useCursorPages<number>(filters);
  const query = client.useQuery("get", "/admin/audit", { params: { query: auditQuery(filters, pages.cursor) } });
  const data = query.data ?? null;
  const error = errorText(query.error);
  const entries = data?.entries ?? null;

  const filter = (patch: Partial<AuditFilters>) => setFilters((f) => ({ ...f, ...patch }));
  const submit = (event: FormEvent) => {
    event.preventDefault();
    filter({ route });
  };

  if (data && !data.on) {
    return (
      <EmptyCard
        icon={History}
        title="The audit log is off"
        body={
          <>
            Set <code>AUDIT_RETENTION_DAYS</code> above 0 (and <code>ARCADEDB_PATH</code>) and restart the server.
          </>
        }
      />
    );
  }

  return (
    <LoadedCard
      title="Audit log"
      description={
        <>
          Every change made through the admin's routes and the setup wizard, and every delete, refused ones too. Kept{" "}
          {data ? `${data.retention_days} days` : "…"}; request bodies are never kept.
        </>
      }
      data={entries}
      error={error}
      onRefresh={() => void query.refetch()}
      skeleton="h-64"
      top={
        <form className="flex flex-wrap items-center gap-2" onSubmit={submit}>
          <SelectInput size="sm" className="w-32" aria-label="Method" value={filters.method} none="Any method" onChange={(v) => filter({ method: v as AuditMethod })} options={METHODS} />
          <SelectInput size="sm" className="w-40" aria-label="Outcome" value={filters.outcome} none="Any outcome" onChange={(v) => filter({ outcome: v as AuditOutcome })} options={OUTCOMES} />
          <SelectInput size="sm" className="w-36" aria-label="Key" value={filters.actor} none="Any key" onChange={(v) => filter({ actor: v as AuditActor })} options={ACTOR_OPTIONS} />
          <Input
            type="search"
            aria-label="Route or path"
            placeholder="Part of the route or path: /db, {table}, enrichers"
            maxLength={200}
            className="h-8 min-w-56 flex-1 font-mono text-xs"
            value={route}
            onChange={(e) => {
              setRoute(e.target.value);
              if (!e.target.value) filter({ route: "" });
            }}
          />
          <Button type="submit" variant="secondary" size="sm">
            Filter
          </Button>
        </form>
      }
      after={
        entries &&
        (pages.hasPrevious || entries.length === AUDIT_PAGE) && (
          <Pager
            start={pages.index * AUDIT_PAGE}
            shown={entries.length}
            count={null}
            hasPrevious={pages.hasPrevious}
            hasNext={entries.length === AUDIT_PAGE}
            loading={query.isFetching}
            onPrevious={pages.previous}
            onNext={() => pages.next(entries[entries.length - 1]?.id)}
          />
        )
      }
    >
      {(list) => (
        <div className="overflow-x-auto rounded-lg border">
          <DataGrid columns={COLUMNS} rows={list} rowKey={(e) => e.id} headerClassName="bg-muted/50" empty={pages.hasPrevious ? "No older entries" : "Nothing changed yet"} />
        </div>
      )}
    </LoadedCard>
  );
}
