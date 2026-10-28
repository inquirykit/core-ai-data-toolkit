// The dialog that submits a one-off job. JobFields, the job to run and its
// params (a form made from the job's params schema), is shared with the
// schedule dialog.

import { useMemo, useRef, useState, type FormEvent, type Ref } from "react";
import { Play } from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { useAction } from "../shared/action";
import type { ApiClient } from "../shared/api";
import { Field, Notice, SelectField, SubmitButton } from "../shared/components";
import { formSchema } from "../shared/form-schema";
import { SchemaForm, type SchemaFormHandle } from "../shared/schema-form";
import type { JobType, Priority, SubmitJobBody } from "../shared/types";
import { PRIORITY_OPTIONS } from "./jobs";

const ENV_HINT = "environment not set up";

export function SubmitDialog(props: { client: ApiClient; types: JobType[] | null; typesError: string; onClose(): void; onSubmitted(): Promise<unknown> }) {
  const [job, setJob] = useState("");
  const [params, setParams] = useState<Record<string, unknown>>({});
  const [webhook, setWebhook] = useState("");
  const [priority, setPriority] = useState<Priority>("normal");
  const paramsForm = useRef<SchemaFormHandle>(null);

  const submit = useAction(
    async (event: FormEvent) => {
      event.preventDefault();
      const paramsOk = paramsForm.current?.validate() ?? true;
      const problem = !job ? "Pick a job" : !paramsOk ? "Some params need fixing" : webhook.trim() && !/^https?:\/\//.test(webhook.trim()) ? "The webhook URL starts with https:// or http://" : null;
      if (problem) throw new Error(problem);
      const body: SubmitJobBody = { job, params, priority };
      if (webhook.trim()) body.webhook_url = webhook.trim();
      const row = await props.client.post("/jobs", { body });
      toast.success(`Submitted ${row.job}`, { description: `Job ${row.id}` });
      await props.onSubmitted();
      props.onClose();
    },
    { notice: true },
  );

  return (
    <Dialog open onOpenChange={(open) => !open && props.onClose()}>
      <DialogContent className="max-h-[90svh] overflow-y-auto sm:max-w-xl">
        <form noValidate className="grid gap-4" onSubmit={submit.run}>
          <DialogHeader>
            <DialogTitle>Submit a job</DialogTitle>
            <DialogDescription>It is queued at once and runs in the background; follow it in the list.</DialogDescription>
          </DialogHeader>
          <JobFields
            types={props.types}
            typesError={props.typesError}
            job={job}
            params={params}
            paramsForm={paramsForm}
            onChange={(patch) => {
              if (patch.job !== undefined) setJob(patch.job);
              if (patch.params !== undefined) setParams(patch.params);
            }}
          />
          <SelectField
            label="Priority"
            hint="Queued jobs start the most urgent first, then the oldest"
            value={priority}
            onChange={(v) => setPriority(v as Priority)}
            options={PRIORITY_OPTIONS}
          />
          <Field label="Webhook URL" hint="Optional: POSTed the finished job, besides the URLs in WEBHOOK_URLS">
            <Input type="url" placeholder="https://…" autoComplete="off" spellCheck={false} value={webhook} onChange={(e) => setWebhook(e.target.value)} />
          </Field>
          <Notice notice={submit.notice} />
          <DialogFooter>
            <Button type="button" variant="outline" onClick={props.onClose}>
              Cancel
            </Button>
            <SubmitButton busy={submit.busy} icon={<Play aria-hidden="true" />}>
              Submit
            </SubmitButton>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

/** The job to run and its params: what a schedule and a one-off submission share. ``paramsForm`` checks the params. */
export function JobFields(props: {
  types: JobType[] | null;
  typesError: string;
  job: string;
  params: Record<string, unknown>;
  paramsForm: Ref<SchemaFormHandle>;
  onChange(patch: { job?: string; params?: Record<string, unknown> }): void;
}) {
  const chosen = props.types?.find((t) => t.name === props.job);
  const schema = useMemo(() => (chosen?.params ? formSchema(chosen.params) : null), [chosen]);
  // Another job's params are not this one's: they start over from its defaults.
  const pick = (name: string) => {
    if (name !== props.job) props.onChange({ job: name, params: {} });
  };
  return (
    <>
      <SelectField
        label="Job"
        hint={props.typesError ? <span className="text-critical-text">{props.typesError}</span> : chosen?.description || "What runs; its params follow"}
        aria-label="Job"
        placeholder={props.types ? (props.types.length ? "Pick a job" : "No jobs are registered") : "Loading…"}
        value={props.job}
        onChange={pick}
        options={(props.types ?? []).map((t) => ({
          value: t.name,
          disabled: !t.env_ready,
          label: (
            <>
              <span className="font-mono text-xs">{t.name}</span>
              <span className="text-muted-foreground">{t.env_ready ? t.description : ENV_HINT}</span>
            </>
          ),
        }))}
      />
      {schema && (
        <fieldset className="grid gap-3 rounded-lg border p-4">
          <legend className="px-1 text-sm font-medium">
            Params
            {chosen?.env && chosen.env !== "default" && (
              <span className="font-normal text-muted-foreground">
                {" "}
                · runs in the <code>{chosen.env}</code> environment
              </span>
            )}
          </legend>
          {Object.keys(schema.properties ?? {}).length ? (
            <SchemaForm key={props.job} ref={props.paramsForm} schema={schema} value={props.params} onChange={(params) => props.onChange({ params })} />
          ) : (
            <p className="text-sm text-muted-foreground">This job takes no params.</p>
          )}
        </fieldset>
      )}
    </>
  );
}
