// The Jobs section's schedules: the table of them (run now, enable, edit,
// delete) and the dialog that creates or edits one.

import { useRef, useState, type FormEvent } from "react";
import { Pencil, Play, Plus, RotateCw } from "lucide-react";
import { toast } from "sonner";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardAction, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Skeleton } from "@/components/ui/skeleton";
import { Switch } from "@/components/ui/switch";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { useAction } from "../shared/action";
import type { ApiClient } from "../shared/api";
import { failed } from "../shared/errors";
import { ConfirmButton, Field, Notice, Segmented, SelectField, StatusDot, SubmitButton } from "../shared/components";
import { fmtAgo, fmtClock } from "../shared/format";
import type { SchemaFormHandle } from "../shared/schema-form";
import type { JobType, Priority, Schedule } from "../shared/types";
import {
  describeCadence,
  EMPTY_SCHEDULE_FORM,
  fmtUntil,
  formFromSchedule,
  INTERVAL_UNITS,
  PRIORITY_OPTIONS,
  scheduleBody,
  sortSchedules,
  toggledBody,
  validateCron,
  validateScheduleForm,
  type IntervalUnit,
  type ScheduleForm,
} from "./jobs";
import { JobFields } from "./JobsSubmit";
import { StatusBadge } from "./JobsTable";

const CRON_HINT = "minute hour day-of-month month day-of-week, e.g. 0 3 * * * is daily at 03:00 (server time)";

export function SchedulesCard(props: {
  client: ApiClient;
  schedules: Schedule[] | null;
  error: string;
  types: JobType[] | null;
  typesError: string;
  onChange(): Promise<unknown>;
  onJobStarted(): Promise<unknown>;
  onOpenLog(jobId: string): void;
}) {
  const { client, schedules } = props;
  // The form's starting state and the schedule it edits (null for a new one); closed when undefined.
  const [editing, setEditing] = useState<{ form: ScheduleForm; schedule: Schedule | null } | undefined>();

  const runNow = useAction(
    async (s: Schedule) => {
      const job = await client.post("/jobs/schedules/{id}/run", { params: { path: { id: s.id } } });
      toast.success(`Started ${s.name}`, { description: `Job ${job.id}` });
      await Promise.all([props.onJobStarted(), props.onChange()]);
    },
    { failed: (s) => `Could not run ${s.name}` },
  );
  const running = runNow.running?.[0].id ?? null;

  const toggle = (s: Schedule, enabled: boolean) =>
    client
      .put("/jobs/schedules/{id}", { params: { path: { id: s.id } }, body: toggledBody(s, enabled) })
      .then(() => props.onChange(), failed(enabled ? `Could not enable ${s.name}` : `Could not disable ${s.name}`));

  const remove = (s: Schedule) =>
    client
      .delete("/jobs/schedules/{id}", { params: { path: { id: s.id } } })
      .then(() => {
        toast.success(`Deleted ${s.name}.`);
        return props.onChange();
      }, failed(`Could not delete ${s.name}`));

  const rows = schedules ? sortSchedules(schedules) : [];
  return (
    <Card className="gap-4">
      <CardHeader>
        <CardTitle>Schedules</CardTitle>
        <CardDescription>
          {schedules ? `${schedules.length} · ${schedules.filter((s) => s.enabled).length} enabled` : "Loading…"}
        </CardDescription>
        <CardAction className="flex items-center gap-2">
          <Button type="button" variant="ghost" size="icon" className="size-8" aria-label="Refresh" onClick={props.onChange}>
            <RotateCw aria-hidden="true" />
          </Button>
          <Button type="button" size="sm" onClick={() => setEditing({ form: EMPTY_SCHEDULE_FORM, schedule: null })}>
            <Plus aria-hidden="true" />
            New schedule
          </Button>
        </CardAction>
      </CardHeader>
      <CardContent className="grid gap-3">
        {props.error && <Notice notice={{ text: props.error, kind: "error" }} />}
        {!schedules && !props.error && <Skeleton className="h-16" />}
        {schedules && rows.length === 0 && (
          <p className="text-sm text-muted-foreground">No schedules yet. A schedule runs a job on a cron expression or every so often.</p>
        )}
        {rows.length > 0 && (
          <div className="overflow-x-auto rounded-lg border">
            <Table>
              <TableHeader className="bg-muted/50">
                <TableRow className="hover:bg-transparent">
                  <TableHead className="pl-4">Name</TableHead>
                  <TableHead>Job</TableHead>
                  <TableHead>Cadence</TableHead>
                  <TableHead>Next run</TableHead>
                  <TableHead>Last run</TableHead>
                  <TableHead>Webhook</TableHead>
                  <TableHead className="w-0 pr-4" />
                </TableRow>
              </TableHeader>
              <TableBody>
                {rows.map((s) => (
                  <TableRow key={s.id}>
                    <TableCell className="pl-4 font-medium [overflow-wrap:anywhere]">{s.name}</TableCell>
                    <TableCell>
                      <Badge variant="secondary" className="font-mono font-normal">
                        {s.job}
                      </Badge>
                    </TableCell>
                    <TableCell className="font-mono text-xs whitespace-nowrap">{describeCadence(s)}</TableCell>
                    <TableCell className="whitespace-nowrap">
                      {!s.enabled ? (
                        <span className="text-muted-foreground">paused</span>
                      ) : s.next_run ? (
                        <>
                          <span className="tabular-nums">{fmtClock(s.next_run, true)}</span>
                          <div className="text-xs text-muted-foreground">{fmtUntil(s.next_run)}</div>
                        </>
                      ) : (
                        <span className="text-muted-foreground">–</span>
                      )}
                    </TableCell>
                    <TableCell className="whitespace-nowrap">
                      {s.last_run ? (
                        <span className="flex items-center gap-2">
                          {s.last_job_id ? (
                            <Button
                              type="button"
                              variant="link"
                              size="sm"
                              className="h-auto p-0 text-muted-foreground"
                              title="Open the log"
                              onClick={() => props.onOpenLog(s.last_job_id!)}
                            >
                              {fmtAgo(s.last_run)}
                            </Button>
                          ) : (
                            <span className="text-muted-foreground">{fmtAgo(s.last_run)}</span>
                          )}
                          {s.last_status && <StatusBadge status={s.last_status} />}
                        </span>
                      ) : (
                        <span className="text-muted-foreground">never</span>
                      )}
                    </TableCell>
                    <TableCell>
                      {s.webhook_url ? (
                        <Badge variant="outline" className="gap-1.5" title={s.webhook_url}>
                          <StatusDot color="var(--series-1)" className="shadow-none" />
                          set
                        </Badge>
                      ) : (
                        <span className="text-muted-foreground">–</span>
                      )}
                    </TableCell>
                    <TableCell className="pr-4">
                      <span className="flex items-center justify-end gap-2">
                        <SubmitButton type="button" variant="outline" size="sm" busy={running === s.id} icon={<Play aria-hidden="true" />} onClick={() => runNow.run(s)}>
                          Run now
                        </SubmitButton>
                        <Switch checked={s.enabled} onCheckedChange={(v) => toggle(s, v)} aria-label={`${s.name} enabled`} />
                        <Button
                          type="button"
                          variant="ghost"
                          size="icon"
                          className="size-8"
                          aria-label={`Edit ${s.name}`}
                          onClick={() => setEditing({ form: formFromSchedule(s), schedule: s })}
                        >
                          <Pencil aria-hidden="true" />
                        </Button>
                        <ConfirmButton
                          title={`Delete ${s.name}?`}
                          body="It stops running; the jobs it already ran stay in the list below. This cannot be undone."
                          onConfirm={() => remove(s)}
                        />
                      </span>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>
        )}
      </CardContent>

      {editing && (
        <ScheduleDialog
          client={client}
          types={props.types}
          typesError={props.typesError}
          initial={editing.form}
          schedule={editing.schedule}
          onClose={() => setEditing(undefined)}
          onSaved={props.onChange}
        />
      )}
    </Card>
  );
}

function ScheduleDialog(props: {
  client: ApiClient;
  types: JobType[] | null;
  typesError: string;
  initial: ScheduleForm;
  /** The schedule being edited, or null for a new one */
  schedule: Schedule | null;
  onClose(): void;
  onSaved(): Promise<unknown>;
}) {
  const [form, setForm] = useState(props.initial);
  const set = (patch: Partial<ScheduleForm>) => setForm((f) => ({ ...f, ...patch }));
  const paramsForm = useRef<SchemaFormHandle>(null);
  const cronProblem = form.cadence === "cron" && form.cron.trim() ? validateCron(form.cron) : null;

  // The server's 400/422 detail (an unknown job, params its schema rejects) goes under the form.
  const submit = useAction(
    async (event: FormEvent) => {
      event.preventDefault();
      const errors = validateScheduleForm(form);
      if (!(paramsForm.current?.validate() ?? true)) errors.push("Some params need fixing");
      if (errors.length) throw new Error(errors.join(". "));
      const body = scheduleBody(form);
      if (props.schedule) await props.client.put("/jobs/schedules/{id}", { params: { path: { id: props.schedule.id } }, body });
      else await props.client.post("/jobs/schedules", { body });
      await props.onSaved();
      props.onClose();
      return body;
    },
    { notice: true, success: (body) => (props.schedule ? `Saved ${body.name}.` : `Created ${body.name}.`) },
  );

  return (
    <Dialog open onOpenChange={(open) => !open && props.onClose()}>
      <DialogContent className="max-h-[90svh] overflow-y-auto sm:max-w-xl">
        <form noValidate className="grid gap-4" onSubmit={submit.run}>
          <DialogHeader>
            <DialogTitle>{props.schedule ? `Edit ${props.schedule.name}` : "New schedule"}</DialogTitle>
            <DialogDescription>A job to run, with its params, on a cron expression or every so often.</DialogDescription>
          </DialogHeader>
          <Field label="Name">
            <Input placeholder="nightly training" autoComplete="off" autoFocus value={form.name} onChange={(e) => set({ name: e.target.value })} />
          </Field>
          <JobFields types={props.types} typesError={props.typesError} job={form.job} params={form.params} paramsForm={paramsForm} onChange={set} />
          <div className="grid gap-2">
            <span className="text-sm leading-none font-medium">Cadence</span>
            <div className="flex flex-wrap items-start gap-2">
              <Segmented<"cron" | "every">
                label="Cadence"
                options={[
                  ["cron", "Cron"],
                  ["every", "Every"],
                ]}
                value={form.cadence}
                onChange={(cadence) => set({ cadence })}
              />
              {form.cadence === "cron" ? (
                <Field className="min-w-48 flex-1" label={<span className="sr-only">Cron expression</span>} hint={cronProblem ? <span className="text-critical-text">{cronProblem}</span> : CRON_HINT}>
                  <Input
                    placeholder="0 3 * * *"
                    autoComplete="off"
                    spellCheck={false}
                    className="font-mono text-[13px]"
                    aria-invalid={Boolean(cronProblem) || undefined}
                    value={form.cron}
                    onChange={(e) => set({ cron: e.target.value })}
                  />
                </Field>
              ) : (
                <div className="flex flex-1 items-start gap-2">
                  <Field className="w-24" label={<span className="sr-only">Every</span>}>
                    <Input type="number" min={1} step={1} inputMode="numeric" value={form.every} onChange={(e) => set({ every: e.target.value })} />
                  </Field>
                  <SelectField
                    className="w-32"
                    label={<span className="sr-only">Unit</span>}
                    aria-label="Unit"
                    value={form.unit}
                    onChange={(v) => set({ unit: v as IntervalUnit })}
                    options={INTERVAL_UNITS.map(([value, label]) => ({ value, label }))}
                  />
                </div>
              )}
            </div>
          </div>
          <SelectField
            label="Priority"
            hint="The jobs it submits wait in the queue by it"
            value={form.priority}
            onChange={(v) => set({ priority: v as Priority })}
            options={PRIORITY_OPTIONS}
          />
          <Field label="Webhook URL" hint="Optional: POSTed the finished job, besides the URLs in WEBHOOK_URLS">
            <Input
              type="url"
              placeholder="https://…"
              autoComplete="off"
              spellCheck={false}
              value={form.webhook_url}
              onChange={(e) => set({ webhook_url: e.target.value })}
            />
          </Field>
          <Label className="flex items-start gap-3 font-normal">
            <Switch checked={form.enabled} onCheckedChange={(v) => set({ enabled: v })} />
            <span className="grid gap-1">
              <span className="font-medium">Enabled</span>
              <span className="text-xs text-muted-foreground">Off, it keeps its settings but does not run until turned on.</span>
            </span>
          </Label>
          <Notice notice={submit.notice} />
          <DialogFooter>
            <Button type="button" variant="outline" onClick={props.onClose}>
              Cancel
            </Button>
            <SubmitButton busy={submit.busy}>{props.schedule ? "Save" : "Create"}</SubmitButton>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
