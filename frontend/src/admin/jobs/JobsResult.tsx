// What a finished job made, opened under its row of the jobs list: its
// result's fields, the files in its folder to download, and Open for the
// model it trained. And the notice that offers to set up an environment
// jobs need (the setup-env job).

import { useState } from "react";
import { Download, ExternalLink, Wrench } from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { useAction } from "../../shared/action";
import { useApi } from "../../shared/api";
import { downloadResponse } from "../../shared/browser";
import { CodeBlock, Disclosure, Facts, SubmitButton } from "../../shared/components";
import { fmtBytes } from "../../shared/format";
import { errorText } from "../../shared/query";
import type { Job, JobType } from "../../shared/types";
import { envsToSetUp, resultFacts, trainedModelHref } from "./jobs";

/** Files past these are not listed here (GET /jobs/{id}/files lists them all). */
const SHOWN_FILES = 30;

export function JobResult({ job }: { job: Job }) {
  const client = useApi();
  const files = client.useQuery("get", "/jobs/{id}/files", { params: { path: { id: job.id } } }, { select: (d) => d.files });
  const download = useAction(
    async (path: string) => downloadResponse(await client.raw("get", "/jobs/{id}/files/{path}", { params: { path: { id: job.id, path } } }), path.split("/").pop() ?? path),
    { failed: (path) => `Could not download ${path}` },
  );
  const open = trainedModelHref(job);
  const facts = resultFacts(job.result);
  // A trained model's own files (a predictor's, a fine-tuned encoder's) are many: its outputs come first.
  const listed = (files.data ?? []).filter((f) => !/^(predictor|model)\//.test(f.path));
  return (
    <div className="grid gap-3 px-3 py-1 lg:grid-cols-2">
      <div className="grid content-start gap-2">
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-sm font-medium">Result</span>
          {open && (
            <Button type="button" size="sm" variant="outline" asChild>
              <a href={open}>
                <ExternalLink aria-hidden="true" />
                Open the model it trained
              </a>
            </Button>
          )}
        </div>
        {facts.length ? <Facts size="xs" mono rows={facts} /> : <p className="text-sm text-muted-foreground">{job.result == null ? "No result" : "Only lists and objects: see its JSON"}</p>}
        {job.result != null && (
          <Disclosure label="Its JSON" size="xs">
            <CodeBlock className="max-h-64 text-xs">{JSON.stringify(job.result, null, 2)}</CodeBlock>
          </Disclosure>
        )}
      </div>
      <div className="grid content-start gap-2">
        <span className="text-sm font-medium">Files</span>
        {files.error && <p className="text-sm text-critical-text">{errorText(files.error)}</p>}
        <ul className="grid gap-1 text-sm">
          {listed.slice(0, SHOWN_FILES).map((f) => (
            <li key={f.path} className="flex items-center gap-2">
              <SubmitButton type="button" variant="ghost" size="sm" className="h-7 px-2" busy={download.running?.[0] === f.path} icon={<Download aria-hidden="true" />} onClick={() => void download.run(f.path)}>
                <span className="font-mono text-xs">{f.path}</span>
              </SubmitButton>
              <span className="text-xs text-muted-foreground">{fmtBytes(f.bytes)}</span>
            </li>
          ))}
        </ul>
        {listed.length > SHOWN_FILES && <p className="text-xs text-muted-foreground">and {listed.length - SHOWN_FILES} more</p>}
        {files.data && files.data.length > listed.length && <p className="text-xs text-muted-foreground">The model's own files ({files.data.length - listed.length}) are not listed.</p>}
      </div>
    </div>
  );
}

/**
 * The environments some jobs need that are not set up (or were set up from an older requirements file), each with a
 * button that queues the setup-env job for it. Nothing installs unless clicked.
 */
export function EnvNotice({ types, onQueued }: { types: JobType[] | null; onQueued?(): unknown }) {
  const client = useApi();
  const [queued, setQueued] = useState<string[]>([]);
  const setUp = useAction(
    async (env: string) => {
      await client.post("/jobs", { body: { job: "setup-env", params: { name: env }, priority: "high" } });
      setQueued((q) => [...q, env]);
      toast.success(`Installing the ${env} environment`, { description: "The setup-env job installs it; its jobs can be submitted once it succeeds." });
      await onQueued?.();
    },
    { failed: (env) => `Could not set up ${env}` },
  );
  const envs = envsToSetUp(types ?? []);
  if (!envs.length) return null;
  return (
    <div className="grid gap-1.5 rounded-md border bg-muted/40 px-3 py-2 text-sm">
      {envs.map(({ env, update }) => (
        <div key={env} className="flex flex-wrap items-center gap-2">
          <span className="mr-auto">
            The <code>{env}</code> environment {update ? "was set up from an older requirements file" : "is not set up, so its jobs cannot run"}.
          </span>
          <SubmitButton type="button" size="sm" variant="outline" busy={setUp.running?.[0] === env} disabled={queued.includes(env)} icon={<Wrench aria-hidden="true" />} onClick={() => void setUp.run(env)}>
            {queued.includes(env) ? "Queued" : update ? "Update" : "Set up"}
          </SubmitButton>
        </div>
      ))}
    </div>
  );
}
