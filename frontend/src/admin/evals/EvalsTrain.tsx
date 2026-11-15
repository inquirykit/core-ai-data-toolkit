// The dialog that trains a classifier on a dataset's labeled examples: it
// writes them as the text-classifier job's training file, then submits the
// job with the params chosen here (a form made from the job's schema).

import { useMemo, useRef, useState } from "react";
import { GraduationCap } from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { useAction } from "../../shared/action";
import { useApi } from "../../shared/api";
import { Notice, SubmitButton } from "../../shared/components";
import { formSchema } from "../../shared/form-schema";
import { plural } from "../../shared/format";
import { SchemaForm, type SchemaFormHandle } from "../../shared/schema-form";
import type { Dataset } from "../../shared/types";
import { trainingSchema } from "./evals";

const JOB = "text-classifier";

export function TrainDialog({ dataset }: { dataset: Dataset }) {
  const [open, setOpen] = useState(false);
  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <Button type="button" variant="outline" size="sm" onClick={() => setOpen(true)}>
        <GraduationCap aria-hidden="true" />
        Train
      </Button>
      {/* Mounted only while open: the job's schema loads then, and the form starts afresh each time. */}
      <DialogContent className="max-h-[90svh] overflow-y-auto sm:max-w-xl">
        <TrainForm dataset={dataset} onDone={() => setOpen(false)} />
      </DialogContent>
    </Dialog>
  );
}

function TrainForm({ dataset, onDone }: { dataset: Dataset; onDone(): void }) {
  const client = useApi();
  const types = client.useQuery("get", "/jobs/types", undefined, { select: (d) => d.jobs });
  const job = types.data?.find((t) => t.name === JOB);
  const schema = useMemo(() => (job ? trainingSchema(formSchema(job.params)) : null), [job]);
  const [params, setParams] = useState<Record<string, unknown>>({});
  const form = useRef<SchemaFormHandle>(null);

  const train = useAction(
    async () => {
      if (!(form.current?.validate() ?? true)) throw new Error("Some params need fixing");
      const file = await client.post("/admin/datasets/{dataset_id}/training-file", { params: { path: { dataset_id: dataset.id } } });
      const row = await client.post("/jobs", { body: { job: JOB, params: { ...params, train_file: file.path } } });
      toast.success(`Training on ${plural(file.rows, "example")}`, {
        description: `${Object.entries(file.labels).map(([label, n]) => `${label} ${n}`).join(", ")}. When job ${row.id} is done, start a run of trained:${row.id} over a dataset.`,
        action: { label: "Jobs", onClick: () => (location.hash = "#/jobs") },
      });
      onDone();
    },
    { notice: true },
  );

  return (
    <>
      <DialogHeader>
        <DialogTitle>Train a classifier</DialogTitle>
        <DialogDescription>
          Fine-tunes a small encoder on this dataset's labeled examples (a text input and a reference each), holding out a share to pick the best epoch. It runs as a job; its model is then
          served as <code>trained:&lt;job id&gt;</code>.
        </DialogDescription>
      </DialogHeader>
      {schema ? (
        <SchemaForm ref={form} schema={schema} value={params} onChange={setParams} />
      ) : (
        <p className="text-sm text-muted-foreground">{types.error ? "Could not load the jobs" : types.data ? `No ${JOB} job is registered` : "Loading…"}</p>
      )}
      <Notice notice={train.notice} />
      <DialogFooter>
        <SubmitButton type="button" busy={train.busy} disabled={!schema} icon={<GraduationCap aria-hidden="true" />} onClick={train.run}>
          Train
        </SubmitButton>
      </DialogFooter>
    </>
  );
}
