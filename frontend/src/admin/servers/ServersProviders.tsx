// The providers: their list, and a form made from the Provider schema, with its API key, and presets for the common local
// servers and hosted services (servers.ts).

import { useRef, useState, type FormEvent } from "react";
import { Pencil, Plus, Undo2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { useAction } from "../../shared/action";
import { useApi } from "../../shared/api";
import { ConfirmButton, Field, Notice, StatusDot, SubmitButton } from "../../shared/components";
import { formValue } from "../../shared/form-schema";
import { DataGrid, type GridColumn } from "../../shared/grid";
import { SchemaForm, useApiSchema, type SchemaFormHandle, type UiSchema } from "../../shared/schema-form";
import type { ModelServers, Provider, ProviderInfo } from "../../shared/types";
import { HOSTED_PROVIDER_PRESETS, ORIGIN, PROVIDER_PRESETS, providerNameProblem } from "./servers";

export function ProvidersCard({ data, reload }: { data: ModelServers; reload(): Promise<unknown> }) {
  const client = useApi();
  // The provider being edited ("" for a new one), with the form it starts from.
  const [editing, setEditing] = useState<{ isNew: boolean; name: string; initial: Partial<Provider> | ProviderInfo } | null>(null);

  const remove = useAction(
    async (p: ProviderInfo) => {
      await client.delete("/admin/model-servers/providers/{name}", { params: { path: { name: p.name } } });
      await reload();
    },
    {
      success: (_, p) => (p.origin === "replaced" ? `${p.name} is back to the file's version.` : `Deleted ${p.name}.`),
      failed: (p) => `Could not delete ${p.name}`,
    },
  );

  const columns: GridColumn<ProviderInfo>[] = [
    {
      key: "name",
      label: "Name",
      className: "font-mono text-xs",
      render: (p) => (
        <>
          {p.name}
          <span className="ml-2 font-sans text-[11px] text-muted-foreground">{ORIGIN[p.origin]}</span>
        </>
      ),
    },
    { key: "url", label: "URL", className: "font-mono text-xs", render: (p) => p.url },
    {
      key: "apiKey",
      label: "API key",
      className: "text-xs",
      render: (p) =>
        p.key_set ? (
          <span className="inline-flex items-center gap-2">
            <StatusDot color="var(--good)" />
            saved
          </span>
        ) : (
          <span className="text-muted-foreground">none</span>
        ),
    },
    { key: "evict", label: "On evict", className: "text-xs", render: (p) => (p.on_evict === "unload" ? "unload the model" : "nothing") },
    { key: "used", label: "Used by", className: "text-xs", render: (p) => (p.used_by.length ? p.used_by.join(", ") : <span className="text-muted-foreground">none</span>) },
    {
      key: "actions",
      headClassName: "w-0",
      render: (p) => (
        <div className="flex justify-end gap-1">
          <Button type="button" variant="outline" size="icon-sm" aria-label={`Edit ${p.name}`} title="Edit" onClick={() => setEditing({ isNew: false, name: p.name, initial: p })}>
            <Pencil aria-hidden="true" />
          </Button>
          {p.origin !== "file" && (
            <ConfirmButton
              iconOnly
              label={p.origin === "replaced" ? `Revert ${p.name}` : `Delete ${p.name}`}
              icon={p.origin === "replaced" ? <Undo2 aria-hidden="true" /> : undefined}
              hint={p.origin === "saved" && p.used_by.length ? `Used by ${p.used_by.join(", ")}` : undefined}
              title={p.origin === "replaced" ? `Revert ${p.name} to the file's version?` : `Delete ${p.name}?`}
              body={p.origin === "replaced" ? "The saved settings are removed and the file's are used again." : "Backends can no longer name it."}
              action={p.origin === "replaced" ? "Revert" : "Delete"}
              busy={remove.running?.[0].name === p.name}
              onConfirm={() => remove.run(p)}
            />
          )}
        </div>
      ),
    },
  ];

  // A preset already added is not offered again.
  const presetButtons = (presets: typeof PROVIDER_PRESETS) =>
    presets
      .filter(([, name]) => !data.providers.some((p) => p.name === name))
      .map(([label, name, settings]) => (
        <Button key={label} type="button" variant="outline" size="sm" onClick={() => setEditing({ isNew: true, name, initial: settings })}>
          <Plus aria-hidden="true" />
          {label}
        </Button>
      ));
  const hosted = presetButtons(HOSTED_PROVIDER_PRESETS);

  return (
    <Card className="gap-4">
      <CardHeader>
        <CardTitle>Providers</CardTitle>
        <CardDescription>Servers and hosted services that run elsewhere, named once for every backend on them, with their API key if they ask for one. A backend takes their settings unless it sets its own.</CardDescription>
      </CardHeader>
      <CardContent className="grid gap-4">
        {data.providers.length > 0 ? (
          <div className="overflow-hidden rounded-lg border">
            <DataGrid columns={columns} rows={data.providers} rowKey={(p) => p.name} empty="No providers yet" />
          </div>
        ) : (
          <p className="text-sm text-muted-foreground">No providers yet.</p>
        )}
        {editing ? (
          <ProviderEditor key={`${editing.isNew}:${editing.name}`} isNew={editing.isNew} name={editing.name} initial={editing.initial} onDone={async (saved) => {
            setEditing(null);
            if (saved) await reload();
          }} />
        ) : (
          <div className="grid gap-2">
            <div className="flex flex-wrap items-center gap-2">
              <span className="text-sm text-muted-foreground">Add</span>
              {presetButtons(PROVIDER_PRESETS)}
              <Button type="button" variant="outline" size="sm" onClick={() => setEditing({ isNew: true, name: "", initial: {} })}>
                <Plus aria-hidden="true" />
                Other
              </Button>
            </div>
            {hosted.length > 0 && (
              <div className="flex flex-wrap items-center gap-2">
                <span className="text-sm text-muted-foreground">Add hosted</span>
                {hosted}
              </div>
            )}
          </div>
        )}
      </CardContent>
    </Card>
  );
}
/** How the provider editor lays out and words the Provider schema's fields. */
const PROVIDER_UI: UiSchema = {
  "ui:layout": [{ rows: [["url"], ["health_path", "max_concurrency", "request_timeout", "on_evict"], ["description"]] }],
  url: { "ui:title": "URL", "ui:placeholder": "http://127.0.0.1:11434", "ui:description": "What comes before /v1. From a container, the host is host.docker.internal" },
  health_path: { "ui:title": "Health path", "ui:description": "A model list here is checked for the model" },
  max_concurrency: { "ui:title": "Concurrency" },
  request_timeout: { "ui:title": "Request timeout", "ui:description": "Seconds" },
  on_evict: { "ui:title": "On evict", "ui:description": "Unload: Ollama frees the memory at once", "ui:enumNames": ["Nothing", "Unload the model"] },
};
/** A provider's name (it is in the path), its API key (saved apart, and never shown again) and its settings, a form made from the Provider schema. */
function ProviderEditor(props: { isNew: boolean; name: string; initial: Partial<Provider> | ProviderInfo; onDone(saved: boolean): void }) {
  const client = useApi();
  const { schema, error } = useApiSchema("Provider");
  const [name, setName] = useState(props.name);
  const [apiKey, setApiKey] = useState("");
  const [value, setValue] = useState<Partial<Provider> | null>(null);
  if (schema && value === null) setValue(formValue(schema, props.initial as Record<string, unknown>) as Partial<Provider>);
  const form = useRef<SchemaFormHandle>(null);
  const keySaved = "key_set" in props.initial && props.initial.key_set;

  const save = useAction(
    async (event: FormEvent) => {
      event.preventDefault();
      const problem = providerNameProblem(name);
      if (!(form.current?.validate() ?? false) || problem) throw new Error(problem || "Some fields need fixing: see under each");
      const path = { params: { path: { name: name.trim() } } };
      await client.put("/admin/model-servers/providers/{name}", { ...path, body: value as Provider });
      if (apiKey.trim()) await client.put("/admin/model-servers/providers/{name}/key", { ...path, body: { value: apiKey.trim() } });
      props.onDone(true);
    },
    { notice: true, success: () => `Saved ${name.trim()}.` },
  );

  const removeKey = useAction(
    async () => {
      await client.put("/admin/model-servers/providers/{name}/key", { params: { path: { name: props.name } }, body: { value: "" } });
      props.onDone(true);
    },
    { success: `Removed the API key of ${props.name}.`, failed: "Could not remove the key" },
  );

  return (
    <form noValidate onSubmit={save.run} className="grid gap-3 rounded-lg border p-4">
      <h4 className="text-sm font-semibold">{props.isNew ? "New provider" : `Edit ${name}`}</h4>
      <Notice notice={save.notice} />
      <Field label="Name" className="sm:w-1/2" hint={props.isNew ? "What backends call it: lower case letters, digits, _ and -" : "Fixed once saved"}>
        <Input autoComplete="off" spellCheck={false} readOnly={!props.isNew} className="font-mono text-[13px]" placeholder="ollama" value={name} onChange={(e) => setName(e.target.value)} />
      </Field>
      <div className="flex flex-wrap items-end gap-2">
        <Field
          label="API key"
          className="min-w-64 flex-1 sm:max-w-[50%]"
          hint={keySaved ? "A key is saved and is not shown. Type another to replace it." : "Sent with every request to this server. Local servers usually need none."}
        >
          <Input
            type="password"
            autoComplete="new-password"
            spellCheck={false}
            className="font-mono text-[13px]"
            placeholder={keySaved ? "••••••••" : "Paste the key"}
            value={apiKey}
            onChange={(e) => setApiKey(e.target.value)}
          />
        </Field>
        {keySaved && (
          <ConfirmButton
            label="Remove key"
            title={`Remove the API key of ${props.name}?`}
            body="Requests to this server go without a key from now on."
            action="Remove"
            busy={removeKey.busy}
            onConfirm={() => removeKey.run()}
          />
        )}
      </div>
      {schema && value ? (
        <SchemaForm ref={form} schema={schema} uiSchema={PROVIDER_UI} value={value} onChange={setValue} />
      ) : error ? (
        <Notice notice={{ text: error, kind: "error" }} />
      ) : (
        <Skeleton className="h-32 rounded-lg" />
      )}
      <div className="flex justify-end gap-2">
        <Button type="button" variant="ghost" onClick={() => props.onDone(false)}>
          Cancel
        </Button>
        <SubmitButton busy={save.busy}>{props.isNew ? "Add" : "Save"}</SubmitButton>
      </div>
    </form>
  );
}
