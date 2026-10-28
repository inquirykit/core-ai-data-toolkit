// A request's input and output (its trace), opened from the requests table
// of Activity & logs at #/activity/<id>, from which it becomes an example
// of a dataset. Pane, the JSON block, is shared with the Evals section.

import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { BookmarkPlus } from "lucide-react";
import { toast } from "sonner";
import { Badge } from "@/components/ui/badge";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { cn } from "@/lib/utils";
import { useAction } from "../shared/action";
import { ApiError, type ApiClient } from "../shared/api";
import { errorText } from "../shared/query";
import { Field, SelectField, StatusLabel, SubmitButton } from "../shared/components";
import { fmtMs, statusOf } from "../shared/format";
import type { TraceCapture } from "../shared/types";
import { evalsHref, pretty } from "./evals";
import { STATUS_STYLE } from "./panels";

/** What a missing trace means, given how tracing is set up. */
function missingText(capture: TraceCapture | null): string {
  if (capture && !capture.enabled) return "This request's input and output were not kept: tracing is off (TRACE_RETENTION_DAYS is 0).";
  if (capture) {
    const days = `${capture.retention_days} day${capture.retention_days === 1 ? "" : "s"}`;
    return `This request's input and output are gone: traces are kept ${days}, or it was recorded before tracing was on.`;
  }
  return "This request's input and output were not kept.";
}

export function TraceDialog({ client, id, capture, onClose }: { client: ApiClient; id: string | null; capture: TraceCapture | null; onClose(): void }) {
  const [target, setTarget] = useState("");
  const [newName, setNewName] = useState("");

  // The trace, or { trace: null } when it was not kept (404); nothing while no request is open.
  const loaded = useQuery({
    queryKey: ["admin", "trace", id],
    queryFn: async ({ signal }) => {
      try {
        return { trace: await client.get("/admin/traces/{trace_id}", { params: { path: { trace_id: id! } }, signal }) };
      } catch (e) {
        if (e instanceof ApiError && e.status === 404) return { trace: null };
        throw e;
      }
    },
    enabled: Boolean(id),
    // While a request's trace loads, what was loaded (or failed) for the one before is not shown.
    placeholderData: undefined,
  });
  const shown = loaded.isFetching ? null : loaded.data;
  const trace = shown?.trace ?? null;
  const missing = shown?.trace === null;
  const error = loaded.isFetching ? "" : errorText(loaded.error);
  const datasets = client.useQuery("get", "/admin/datasets", undefined, { select: (d) => d.datasets, enabled: Boolean(id) }).data ?? [];
  const queries = useQueryClient();

  const save = useAction(
    async () => {
      if (!trace) return;
      let datasetId = target;
      if (target === "new") {
        const created = await client.post("/admin/datasets", { body: { name: newName.trim() } });
        datasetId = created.id;
      }
      await client.post("/admin/datasets/{dataset_id}/examples", { params: { path: { dataset_id: datasetId } }, body: { trace_ids: [trace.id] } });
      const name = target === "new" ? newName.trim() : datasets.find((d) => d.id === datasetId)?.name;
      toast.success(`Saved to ${name}`, {
        action: { label: "Open dataset", onClick: () => (location.hash = evalsHref(datasetId)) },
      });
      setTarget("");
      setNewName("");
      // The dataset has one more example, or is new: the lists of datasets (here and in Evals) load again.
      void queries.invalidateQueries({ queryKey: ["get", "/admin/datasets"] });
    },
    { failed: "Could not save the example" },
  );

  const canSave = Boolean(trace) && (target === "new" ? newName.trim().length > 0 : Boolean(target));

  return (
    <Dialog open={Boolean(id)} onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="max-h-[90svh] overflow-y-auto sm:max-w-4xl">
        <DialogHeader>
          <DialogTitle className="font-mono text-base [overflow-wrap:anywhere]">{trace?.model_id ?? "Request"}</DialogTitle>
          <DialogDescription asChild>
            <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-sm">
              {trace ? (
                <>
                  <span>{trace.task}</span>
                  <StatusLabel status={statusOf(STATUS_STYLE, trace.status)} className="gap-1.5" />
                  <span className="tabular-nums">{fmtMs(trace.duration_ms)}</span>
                  {trace.cold && <span>cold start</span>}
                  {trace.backend && <span className="font-mono text-xs">via {trace.backend}</span>}
                  <span className="tabular-nums">{new Date(trace.ts * 1000).toLocaleString()}</span>
                  {trace.session && <span>session {trace.session}</span>}
                  {trace.tags.map((t) => (
                    <Badge key={t} variant="secondary">
                      {t}
                    </Badge>
                  ))}
                </>
              ) : (
                <span>{missing ? "No input or output kept" : error ? "Could not load" : "Loading…"}</span>
              )}
            </div>
          </DialogDescription>
        </DialogHeader>
        {error && <p className="text-sm text-critical-text">{error}</p>}
        {missing && <p className="rounded-md border border-dashed px-3 py-6 text-center text-sm text-muted-foreground">{missingText(capture)}</p>}
        {trace && (
          <>
            {trace.error && <p className="rounded-md bg-critical/10 px-3 py-2 font-mono text-xs text-critical-text [overflow-wrap:anywhere]">{trace.error}</p>}
            <div className="grid gap-4 lg:grid-cols-2">
              <Pane title="Input" value={trace.input} />
              <Pane title="Output" value={trace.output} empty="No output: the request failed" />
            </div>
            <div className="flex flex-wrap items-end gap-2 border-t pt-4">
              <SelectField
                label="Save as an example of"
                className="w-56"
                value={target}
                onChange={setTarget}
                none="Pick a dataset…"
                options={[
                  ...datasets.map((d) => ({
                    value: d.id,
                    label: (
                      <>
                        {d.name} <span className="text-muted-foreground">· {d.examples}</span>
                      </>
                    ),
                  })),
                  { value: "new", label: "New dataset…" },
                ]}
              />
              {target === "new" && (
                <Field label="Name" className="w-48">
                  <Input value={newName} onChange={(e) => setNewName(e.target.value)} placeholder="e.g. support-questions" />
                </Field>
              )}
              <SubmitButton type="button" onClick={save.run} busy={save.busy} icon={<BookmarkPlus aria-hidden="true" />} disabled={!canSave}>
                Save example
              </SubmitButton>
              <span className="text-xs text-muted-foreground">The input is copied; its output becomes the example's baseline.</span>
            </div>
          </>
        )}
      </DialogContent>
    </Dialog>
  );
}

/** A titled, scrolling block of JSON. */
export function Pane({ title, value, empty, className }: { title: string; value: unknown; empty?: string; className?: string }) {
  return (
    <section className={cn("min-w-0", className)}>
      <h3 className="mb-1.5 text-xs font-medium text-muted-foreground">{title}</h3>
      {value == null ? (
        <p className="rounded-md border border-dashed px-3 py-6 text-center text-sm text-muted-foreground">{empty ?? "None"}</p>
      ) : (
        <pre className="max-h-96 overflow-auto rounded-md border bg-muted/30 px-3 py-2 font-mono text-xs leading-5 whitespace-pre-wrap [overflow-wrap:anywhere]">
          {pretty(value)}
        </pre>
      )}
    </section>
  );
}
