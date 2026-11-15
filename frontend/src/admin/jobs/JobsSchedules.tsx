// The Jobs section's schedules: the table of them (run now, enable, edit,
// delete) and the dialog that creates or edits one.

import { useRef, useState, type FormEvent } from "react";
import { Pencil, Play, Plus } from "lucide-react";
import { toast } from "sonner";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { useAction } from "../../shared/action";
import { useApi } from "../../shared/api";
import { ConfirmButton, Field, Notice, Segmented, SelectField, StatusDot, SubmitButton, StatusBadge } from "../../shared/components";
import { fmtAgo, fmtClock } from "../../shared/format";
import { DataGrid, type GridColumn } from "../../shared/grid";
import type { SchemaFormHandle } from "../../shared/schema-form";
import type { JobType, Priority, Schedule } from "../../shared/types";
import {
  EMPTY_SCHEDULE_FORM,
  fmtUntil,
  formFromSchedule,
  scheduleBody,
  sortSchedules,
  toggledBody,
  validateScheduleForm,
  type ScheduleForm,
} from "./jobs";
import { JobFields, RetryFields } from "./JobsSubmit";
import { LoadedCard } from "../layout";
import { describeCadence, INTERVAL_UNITS, PRIORITY_OPTIONS, type IntervalUnit } from "../../shared/tasks";

const CRON_HINT = "minute hour day-of-month month day-of-week, e.g. 0 3 * * * is daily at 03:00 (server time)";

export function SchedulesCard(props: {
  schedules: Schedule[] | null;
  error: string;
  types: JobType[] | null;
  typesError: string;
  onChange(): Promise<unknown>;
  onJobStarted(): Promise<unknown>;
  onOpenLog(jobId: string): void;
}) {
  const client = useApi();
  const { schedules } = props;
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

  const toggle = useAction(
    async (s: Schedule, enabled: boolean) => {
      await client.put("/jobs/schedules/{id}", { params: { path: { id: s.id } }, body: toggledBody(s, enabled) });
      await props.onChange();
    },
    { failed: (s, enabled) => (enabled ? `Could not enable ${s.name}` : `Could not disable ${s.name}`) },
  );

  const remove = useAction(
    async (s: Schedule) => {
      await client.delete("/jobs/schedules/{id}", { params: { path: { id: s.id } } });
      await props.onChange();
    },
    { success: (_, s) => `Deleted ${s.name}.`, failed: (s) => `Could not delete ${s.name}` },
  );

  const rows = schedules ? sortSchedules(schedules) : [];

  const columns: GridColumn<Schedule>[] = [
    { key: "name", label: "Name", headClassName: "pl-4", className: "pl-4 font-medium [overflow-wrap:anywhere]", render: (s) => s.name },
    {
      key: "job",
      label: "Job",
      render: (s) => (
        <Badge variant="secondary" className="font-mono font-normal">
          {s.job}
        </Badge>
      ),
    },
    { key: "cadence", label: "Cadence", className: "font-mono text-xs whitespace-nowrap", render: (s) => describeCadence(s) },
    {
      key: "next",
      label: "Next run",
      className: "whitespace-nowrap",
      render: (s) =>
        !s.enabled ? (
          <span className="text-muted-foreground">paused</span>
        ) : s.next_run ? (
          <>
            <span className="tabular-nums">{fmtClock(s.next_run, true)}</span>
            <div className="text-xs text-muted-foreground">{fmtUntil(s.next_run)}</div>
          </>
        ) : (
          <span className="text-muted-foreground">–</span>
        ),
    },
    {
      key: "last",
      label: "Last run",
      className: "whitespace-nowrap",
      render: (s) =>
        s.last_run ? (
          <span className="flex items-center gap-2">
            {s.last_task_id ? (
              <Button type="button" variant="link" size="sm" className="h-auto p-0 text-muted-foreground" title="Open the log" onClick={() => props.onOpenLog(s.last_task_id!)}>
                {fmtAgo(s.last_run)}
              </Button>
            ) : (
              <span className="text-muted-foreground">{fmtAgo(s.last_run)}</span>
            )}
            {s.last_status && <StatusBadge status={s.last_status} />}
          </span>
        ) : (
          <span className="text-muted-foreground">never</span>
        ),
    },
    {
      key: "webhook",
      label: "Webhook",
      render: (s) =>
        s.webhook_url ? (
          <Badge variant="outline" className="gap-1.5" title={s.webhook_url}>
            <StatusDot color="var(--series-1)" className="shadow-none" />
            set
          </Badge>
        ) : (
          <span className="text-muted-foreground">–</span>
        ),
    },
    {
      key: "actions",
      headClassName: "w-0 pr-4",
      className: "pr-4",
      render: (s) => (
        <span className="flex items-center justify-end gap-2">
          <SubmitButton type="button" variant="outline" size="sm" busy={running === s.id} icon={<Play aria-hidden="true" />} onClick={() => runNow.run(s)}>
            Run now
          </SubmitButton>
          <Switch checked={s.enabled} disabled={toggle.running?.[0].id === s.id} onCheckedChange={(v) => toggle.run(s, v)} aria-label={`${s.name} enabled`} />
          <Button type="button" variant="ghost" size="icon" className="size-8" aria-label={`Edit ${s.name}`} onClick={() => setEditing({ form: formFromSchedule(s), schedule: s })}>
            <Pencil aria-hidden="true" />
          </Button>
          <ConfirmButton
            title={`Delete ${s.name}?`}
            body="It stops running; the jobs it already ran stay in the list below. This cannot be undone."
            busy={remove.running?.[0].id === s.id}
            onConfirm={() => remove.run(s)}
          />
        </span>
      ),
    },
  ];
  return (
    <>
      <LoadedCard
        title="Schedules"
        description={(list) => `${list.length} · ${list.filter((s) => s.enabled).length} enabled`}
        data={schedules}
        error={props.error}
        onRefresh={props.onChange}
        actions={
          <Button type="button" size="sm" onClick={() => setEditing({ form: EMPTY_SCHEDULE_FORM, schedule: null })}>
            <Plus aria-hidden="true" />
            New schedule
          </Button>
        }
        skeleton="h-16"
        isEmpty={() => rows.length === 0}
        empty="No schedules yet. A schedule runs a job on a cron expression or every so often."
      >
        {() => (
          <div className="overflow-x-auto rounded-lg border">
            <DataGrid columns={columns} rows={rows} rowKey={(s) => s.id} headerClassName="bg-muted/50" empty="No schedules yet" />
          </div>
        )}
      </LoadedCard>
      {editing && (
        <ScheduleDialog
          types={props.types}
          typesError={props.typesError}
          initial={editing.form}
          schedule={editing.schedule}
          onClose={() => setEditing(undefined)}
          onSaved={props.onChange}
        />
      )}
    </>
  );
}

function ScheduleDialog(props: {
  types: JobType[] | null;
  typesError: string;
  initial: ScheduleForm;
  /** The schedule being edited, or null for a new one */
  schedule: Schedule | null;
  onClose(): void;
  onSaved(): Promise<unknown>;
}) {
  const client = useApi();
  const [form, setForm] = useState(props.initial);
  const set = (patch: Partial<ScheduleForm>) => setForm((f) => ({ ...f, ...patch }));
  const paramsForm = useRef<SchemaFormHandle>(null);

  // The server's 400/422 detail (an unknown job, params its schema rejects) goes under the form.
  const submit = useAction(
    async (event: FormEvent) => {
      event.preventDefault();
      const errors = validateScheduleForm(form);
      if (!(paramsForm.current?.validate() ?? true)) errors.push("Some params need fixing");
      if (errors.length) throw new Error(errors.join(". "));
      const body = scheduleBody(form);
      if (props.schedule) await client.put("/jobs/schedules/{id}", { params: { path: { id: props.schedule.id } }, body });
      else await client.post("/jobs/schedules", { body });
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
                <Field className="min-w-48 flex-1" label={<span className="sr-only">Cron expression</span>} hint={CRON_HINT}>
                  <Input
                    placeholder="0 3 * * *"
                    autoComplete="off"
                    spellCheck={false}
                    className="font-mono text-[13px]"
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
          <RetryFields value={form} onChange={set} />
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
