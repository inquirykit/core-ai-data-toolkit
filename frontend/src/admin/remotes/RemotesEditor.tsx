// Editing a remote: a form made from the RemoteSpec schema (remoteUi gives its sections and wording), the presets a
// new one starts from, and a test of the saved one.

import { useRef, useState, type FormEvent } from "react";
import { Trash2 } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardFooter, CardHeader, CardTitle } from "@/components/ui/card";
import { useAction } from "../../shared/action";
import { useApi } from "../../shared/api";
import { formValue, valueKey } from "../../shared/form-schema";
import { SchemaForm, useApiSchema, type SchemaFormHandle, type UiSchema } from "../../shared/schema-form";
import { ConfirmButton, Notice, StatusDot, SubmitButton } from "../../shared/components";
import { DetailSkeleton } from "../layout";
import { dataHash } from "../data/data";
import { fmtSteps, newSpec, PRESETS, readiness, remoteHash, sentSpec, type EditedSpec, type RemoteInfo, type RemoteSpec, type RemoteTest } from "./remotes";

/**
 * How the editor lays out and words a remote's fields, beyond what its schema (RemoteSpec) says. While a bucket
 * names the remote, its scheme and options (where the copies are) are read-only, as the server refuses a change; so is
 * a change of the variable a root or a bucket is read from, which the server's 409 says.
 */
function remoteUi(opts: { isNew: boolean; named: boolean }): UiSchema {
  return {
    "ui:layout": [
      { title: "About", rows: [["name", "label"]] },
      {
        title: "Where",
        hint: opts.named
          ? "Buckets name it, so where its copies are cannot change: make a new remote and name that on them instead."
          : "OpenDAL's service and its options. A bucket's files go to <workspace>/<bucket>/<file id> under the root, if one is given.",
        rows: [["scheme"], ["options"]],
      },
      {
        title: "Secrets",
        hint:
          "Each secret option names the environment variable of the service that holds its value. The values stay in the service's environment: never stored, shown, or handed to jobs and model servers." +
          (opts.named ? " While buckets name it, a key's variable can change (a key rotated), not one a root or a bucket is read from." : ""),
        rows: [["secrets"]],
      },
    ],
    name: {
      "ui:placeholder": "r2",
      "ui:readonly": !opts.isNew,
      "ui:description": opts.isNew ? "Lower case letters, digits and _: what buckets name it by" : "Fixed once saved",
    },
    label: { "ui:placeholder": "Backups on R2" },
    scheme: {
      "ui:readonly": opts.named,
      "ui:placeholder": "s3",
      "ui:description": "s3 for S3 and what speaks its API (R2, B2, MinIO); b2, gcs, azblob, fs (a folder on this machine), memory (gone with the process)",
    },
    options: {
      "ui:title": "",
      "ui:description": "",
      "ui:readonly": opts.named,
      "ui:options": { keyLabel: "option", valueLabel: "value", keyPlaceholder: "bucket", valuePlaceholder: "scans", addLabel: "Add option" },
    },
    secrets: {
      "ui:title": "",
      "ui:description": "",
      "ui:options": {
        keyLabel: "option",
        valueLabel: "environment variable",
        keyPlaceholder: "secret_access_key",
        valuePlaceholder: "R2_SECRET_ACCESS_KEY",
        addLabel: "Add secret",
      },
    },
  };
}

/**
 * The form for one remote (``loaded`` null for a new one, started from ``preset`` when given), made from its schema,
 * and under it, for a saved one, the buckets that name it and its test. Saving PUTs the whole spec. ``loaded`` is the
 * list's, always: buckets name a remote from their own pages, and the list loads again when one does.
 */
export function RemoteEditor(props: { loaded: RemoteInfo | null; preset?: string; onChange(): Promise<unknown> }) {
  const client = useApi();
  const { schema, error: schemaError } = useApiSchema("RemoteSpec");
  const loaded = props.loaded;
  const isNew = !loaded;
  const named = Boolean(loaded?.buckets.length);
  // What the form starts from, once the schema says which of a record's fields are the spec's.
  const start = (from: RemoteInfo | null): EditedSpec => (from && schema ? formValue(schema, from) : newSpec(props.preset));
  const [value, setValue] = useState<EditedSpec | null>(null);
  if (schema && value === null) setValue(start(loaded));
  const form = useRef<SchemaFormHandle>(null);
  const dirty = value !== null && valueKey(value) !== valueKey(start(loaded));

  const save = useAction(
    async (event: FormEvent) => {
      event.preventDefault();
      if (!value || !form.current?.validate()) throw new Error("Some fields need fixing: see under each");
      const spec = sentSpec(value as RemoteSpec);
      const saved = await client.put("/remotes/{name}", { params: { path: { name: spec.name } }, body: spec });
      // The list first, which this form starts from again.
      await props.onChange();
      setValue(null);
      if (isNew) location.hash = remoteHash({ view: "edit", name: saved.name });
      return saved;
    },
    { notice: true, success: (saved) => `Saved ${saved.name}.` },
  );

  const remove = useAction(
    async () => {
      if (!loaded) return;
      await client.delete("/remotes/{name}", { params: { path: { name: loaded.name } } });
      await props.onChange();
      location.hash = remoteHash({ view: "intro" });
    },
    { success: () => `Deleted ${loaded?.name}.`, failed: () => `Could not delete ${loaded?.name}` },
  );

  const ready = loaded ? readiness(loaded) : null;
  return (
    <div className="grid min-w-0 gap-5">
      <Card className="gap-4">
        <CardHeader>
          <CardTitle className="font-mono text-lg">{isNew ? "New remote" : loaded.name}</CardTitle>
          <CardDescription className="flex flex-wrap items-center gap-2">
            {isNew ? (
              "A place outside the service that buckets' files are copied to."
            ) : (
              <>
                <StatusDot color={ready!.color} />
                {ready!.label}
              </>
            )}
          </CardDescription>
        </CardHeader>
        <form noValidate onSubmit={save.run}>
          <CardContent className="grid gap-6">
            {isNew && <PresetLinks current={props.preset} />}
            {loaded && loaded.missing.length > 0 && (
              <Notice
                notice={{
                  kind: "error",
                  text: `Not set in the service's environment: ${loaded.missing.join(", ")}. Set ${loaded.missing.length === 1 ? "it" : "them"} where the service starts (its .env file, or the container's environment), then restart it: the copies fail until then.`,
                }}
              />
            )}
            <Notice notice={save.notice} />
            {schema && value ? (
              <SchemaForm ref={form} schema={schema} uiSchema={remoteUi({ isNew, named })} value={value} onChange={setValue} />
            ) : schemaError ? (
              <Notice notice={{ text: schemaError, kind: "error" }} />
            ) : (
              <DetailSkeleton />
            )}
            {loaded && <NamedBy buckets={loaded.buckets} />}
          </CardContent>
          <CardFooter className="mt-6 flex flex-wrap items-center gap-3 border-t pt-4 [.border-t]:pt-4">
            {loaded && (
              <ConfirmButton
                size="default"
                label="Delete"
                icon={<Trash2 aria-hidden="true" />}
                title={`Delete ${loaded.name}?`}
                body="The remote is forgotten here. What was copied there stays there: delete it at the remote if it should go."
                hint={named ? "Buckets name it: set theirs to another remote, or none, first" : undefined}
                busy={remove.busy}
                onConfirm={remove.run}
              />
            )}
            <div className="ml-auto flex items-center gap-3">
              {dirty && <span className="text-xs text-muted-foreground">Unsaved changes</span>}
              <SubmitButton busy={save.busy} disabled={!isNew && !dirty}>
                {isNew ? "Create" : "Save"}
              </SubmitButton>
            </div>
          </CardFooter>
        </form>
      </Card>
      {loaded && <TestCard name={loaded.name} blocked={dirty ? "Save or undo the changes first: the test tries the saved remote" : ""} />}
    </div>
  );
}

/** The presets a new remote can start from, as links: each fills the form anew. */
function PresetLinks({ current }: { current?: string }) {
  return (
    <div className="flex flex-wrap items-center gap-2 text-sm">
      <span className="text-muted-foreground">Start from</span>
      {PRESETS.map((p) => (
        <Button key={p.key} type="button" size="sm" variant={p.key === current ? "secondary" : "outline"} asChild>
          <a href={remoteHash({ view: "new", preset: p.key })}>{p.label}</a>
        </Button>
      ))}
    </div>
  );
}

/** The buckets that name the remote, each a link to its page in Data. */
function NamedBy({ buckets }: { buckets: string[] }) {
  return (
    <p className="flex flex-wrap items-center gap-1.5 text-sm text-muted-foreground">
      {buckets.length ? "Copied here:" : "No bucket names it yet: name it on a bucket's page in Data."}
      {buckets.map((ref) => {
        const [workspace, table] = ref.split("/");
        return (
          <Badge key={ref} variant="outline" className="font-mono" asChild>
            <a href={dataHash({ workspace, table })}>{ref}</a>
          </Badge>
        );
      })}
    </p>
  );
}

/** Tries the saved remote now: a probe object written, read back, listed and deleted, or the remote's error. */
function TestCard({ name, blocked }: { name: string; blocked: string }) {
  const client = useApi();
  const [test, setTest] = useState<RemoteTest | null>(null);
  const run = useAction(
    async () => {
      setTest(null);
      const done = await client.post("/remotes/{name}/test", { params: { path: { name } } });
      setTest(done);
      return done;
    },
    { notice: true, success: (done) => ({ text: `It works: ${fmtSteps(done)}.`, kind: "ok" }) },
  );
  return (
    <Card className="gap-4">
      <CardHeader>
        <CardTitle>Test</CardTitle>
        <CardDescription>Writes a small probe object under .toolkit-probe/ at its root, reads it back, lists it and deletes it.</CardDescription>
      </CardHeader>
      <CardContent className="grid gap-3">
        <div className="flex flex-wrap items-center gap-3">
          {/* A disabled button takes no pointer events, so the reason sits on a wrapper. */}
          <span className="inline-flex" title={blocked || undefined}>
            <SubmitButton type="button" busy={run.busy} disabled={Boolean(blocked)} onClick={() => void run.run()}>
              Test
            </SubmitButton>
          </span>
          {test && <span className="text-xs text-muted-foreground">{test.scheme}</span>}
        </div>
        <Notice notice={run.notice} />
      </CardContent>
    </Card>
  );
}
