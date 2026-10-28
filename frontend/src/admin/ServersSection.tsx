import { useMemo, useRef, useState, type FormEvent, type ReactNode } from "react";
import { ArrowDown, ArrowUp, ChevronRight, CircleAlert, Copy, Pencil, Plus, Radar, Search, ServerCog, Undo2, X } from "lucide-react";
import { toast } from "sonner";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardAction, CardContent, CardDescription, CardFooter, CardHeader, CardTitle } from "@/components/ui/card";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { cn } from "@/lib/utils";
import { useAction } from "../shared/action";
import { CodeEditor } from "../shared/code-editor";
import type { ApiClient } from "../shared/api";
import { useHash } from "../shared/browser";
import { ConfirmButton, EmptyCard, Field, Heading, KVRows, Notice, SelectField, SelectInput, StatusDot, SubmitButton, type SelectOption } from "../shared/components";
import { failed } from "../shared/errors";
import { formValue } from "../shared/form-schema";
import { DataGrid, type GridColumn } from "../shared/grid";
import { errorText } from "../shared/query";
import { SchemaForm, useApiSchema, type SchemaFormHandle, type UiSchema } from "../shared/schema-form";
import type { BackendProbe, ModelServerInfo, ModelServerProbe, ModelServers, ModelTask, Provider, ProviderInfo } from "../shared/types";
import {
  BACKEND_KINDS,
  backendLabel,
  emptyBackend,
  emptyEntry,
  entryForm,
  entryFromForm,
  formKey,
  locationFromHash,
  ORIGIN,
  PROBE,
  PROVIDER_PRESETS,
  providerNameProblem,
  serversHash,
  STATUS_COLOR,
  TASKS,
  validateEntry,
  type BackendForm,
  type BackendKind,
  type EntryForm,
  type ServersLocation,
} from "./servers";
import { DetailSkeleton, ListCard, MasterDetail, NavRow, NewLink, NoRows } from "./layout";

/**
 * /admin#/servers: the model server entries on the left; on the right where
 * they come from and the providers, or one entry's editor with its backends.
 */
export function ServersSection({ client }: { client: ApiClient }) {
  const loc = useHash(locationFromHash);
  const query = client.useQuery("get", "/admin/model-servers");
  const data = query.data ?? null;
  const error = errorText(query.error);
  const reload = query.refetch;
  const byId = (id: string | undefined) => (id ? data?.models.find((m) => m.model_id === id) : undefined);

  let view: ReactNode;
  if (!data) view = error ? <EmptyCard icon={CircleAlert} title="Could not load the model servers" body={error} /> : <DetailSkeleton />;
  else if (loc.view === "overview") view = <OverviewView client={client} data={data} reload={reload} />;
  else if (loc.view === "new") {
    const source = byId(loc.dup);
    view = (
      <EditorView
        key={`new:${loc.dup ?? ""}`}
        client={client}
        data={data}
        loaded={null}
        initial={source ? { ...entryForm(source, ""), model_id: "" } : emptyEntry(data.providers[0]?.name)}
        reload={reload}
      />
    );
  } else {
    const current = byId(loc.id);
    view = current ? (
      <EditorView key={current.model_id} client={client} data={data} loaded={current} initial={entryForm(current, current.model_id)} reload={reload} />
    ) : (
      <EmptyCard icon={Search} title={`No model server entry for ${loc.id}`} body="It may have been deleted. Pick one on the left." />
    );
  }

  return (
    <MasterDetail list={<ModelsCard data={data} loc={loc} error={error} reload={reload} />}>
      {view}
    </MasterDetail>
  );
}

// ------------------------------------------------------------------ list
function ModelsCard(props: { data: ModelServers | null; loc: ServersLocation; error: string; reload(): Promise<unknown> }) {
  const { data, loc } = props;
  const chosen = loc.view === "model" ? loc.id : null;
  const loaded = data?.models.filter((m) => m.status === "loaded").length ?? 0;
  return (
    <ListCard
      title="Models"
      description={data ? `${data.models.length} · ${loaded} loaded` : "Loading…"}
      label="Model servers"
      onRefresh={props.reload}
      action={data?.editable && <NewLink href={serversHash({ view: "new" })} />}
      loaded={Boolean(data || props.error)}
      // Without data the failure is the whole view, on the right.
      error={data ? props.error : ""}
    >
      <NavRow href={serversHash({ view: "overview" })} active={loc.view === "overview"}>
        <ServerCog aria-hidden="true" />
        Providers & files
      </NavRow>
      {data?.models.map((m) => (
        <ModelRow key={m.model_id} info={m} active={m.model_id === chosen} />
      ))}
      {data && !data.models.length && <NoRows>No model servers yet</NoRows>}
    </ListCard>
  );
}

function ModelRow({ info, active }: { info: ModelServerInfo; active: boolean }) {
  return (
    <NavRow href={serversHash({ view: "model", id: info.model_id })} active={active} tall>
      <span className="flex min-w-0 items-center gap-2">
        <span title={info.serving ? `${info.status}: ${info.serving}` : info.status} className="inline-flex">
          <StatusDot color={STATUS_COLOR[info.status] ?? "var(--muted-foreground)"} />
        </span>
        <span className={cn("truncate font-mono text-[13px]", active && "font-medium")}>{info.model_id}</span>
        <span className="ml-auto shrink-0 text-[11px] text-muted-foreground">{ORIGIN[info.origin]}</span>
      </span>
      {/* The backends in the order they are tried. */}
      <span className="truncate pl-4 text-[11px] text-muted-foreground">{info.backends.map(backendLabel).join(" → ")}</span>
    </NavRow>
  );
}

// -------------------------------------------------------------- overview
function OverviewView({ client, data, reload }: { client: ApiClient; data: ModelServers; reload(): Promise<unknown> }) {
  return (
    <div className="grid min-w-0 gap-5">
      <Card className="gap-4">
        <CardHeader>
          <CardTitle>Where model servers come from</CardTitle>
          <CardDescription>
            Each model is served by its backends, tried in order when it loads: the first that can serve it is used until it unloads.
          </CardDescription>
        </CardHeader>
        <CardContent className="grid gap-3 text-sm">
          <dl className="grid gap-x-6 gap-y-2 sm:grid-cols-[10rem_minmax(0,1fr)]">
            <dt className="text-muted-foreground">The file</dt>
            <dd className="min-w-0 font-mono text-xs [overflow-wrap:anywhere]">
              {data.file} {!data.file_exists && <span className="font-sans text-muted-foreground">(not there: none)</span>}
            </dd>
            <dt className="text-muted-foreground">Saved here</dt>
            <dd className="min-w-0 font-mono text-xs [overflow-wrap:anywhere]">
              {data.saved_file ?? <span className="font-sans text-muted-foreground">nowhere: MODEL_SERVERS_SAVED_FILE is empty, so this page is read-only</span>}
            </dd>
            <dt className="text-muted-foreground">Commands</dt>
            <dd className="text-xs text-muted-foreground">
              {data.commands_allowed ? (
                <>This page may add and change the commands servers start with (MODEL_SERVERS_ALLOW_COMMANDS is on).</>
              ) : (
                <>
                  Only the commands the file gives can be kept here. Add new ones to the file, or turn on <code>MODEL_SERVERS_ALLOW_COMMANDS</code>: they run on this host.
                </>
              )}
            </dd>
          </dl>
          {data.problems.length > 0 && (
            <Alert variant="destructive">
              <CircleAlert aria-hidden="true" />
              <AlertDescription>
                <ul className="grid gap-1">
                  {data.problems.map((p) => (
                    <li key={p.where}>
                      <span className="font-mono text-xs">{p.where}</span>: {p.message}
                    </li>
                  ))}
                </ul>
              </AlertDescription>
            </Alert>
          )}
        </CardContent>
      </Card>
      <ProvidersCard client={client} data={data} reload={reload} />
    </div>
  );
}

function ProvidersCard({ client, data, reload }: { client: ApiClient; data: ModelServers; reload(): Promise<unknown> }) {
  // The provider being edited ("" for a new one), with the form it starts from.
  const [editing, setEditing] = useState<{ isNew: boolean; name: string; initial: Partial<Provider> | ProviderInfo } | null>(null);

  const remove = (p: ProviderInfo) =>
    client.delete("/admin/model-servers/providers/{name}", { params: { path: { name: p.name } } }).then(async () => {
      toast.success(p.origin === "replaced" ? `${p.name} is back to the file's version.` : `Deleted ${p.name}.`);
      await reload();
    }, failed(`Could not delete ${p.name}`));

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
    { key: "evict", label: "On evict", className: "text-xs", render: (p) => (p.on_evict === "unload" ? "unload the model" : "nothing") },
    { key: "used", label: "Used by", className: "text-xs", render: (p) => (p.used_by.length ? p.used_by.join(", ") : <span className="text-muted-foreground">none</span>) },
    {
      key: "actions",
      headClassName: "w-0",
      render: (p) => (
        <div className="flex justify-end gap-1">
          {data.editable && (
            <Button type="button" variant="outline" size="icon-sm" aria-label={`Edit ${p.name}`} title="Edit" onClick={() => setEditing({ isNew: false, name: p.name, initial: p })}>
              <Pencil aria-hidden="true" />
            </Button>
          )}
          {data.editable && p.origin !== "file" && (
            <ConfirmButton
              iconOnly
              label={p.origin === "replaced" ? `Revert ${p.name}` : `Delete ${p.name}`}
              icon={p.origin === "replaced" ? <Undo2 aria-hidden="true" /> : undefined}
              hint={p.origin === "saved" && p.used_by.length ? `Used by ${p.used_by.join(", ")}` : undefined}
              title={p.origin === "replaced" ? `Revert ${p.name} to the file's version?` : `Delete ${p.name}?`}
              body={p.origin === "replaced" ? "The saved settings are removed and the file's are used again." : "Backends can no longer name it."}
              action={p.origin === "replaced" ? "Revert" : "Delete"}
              onConfirm={() => remove(p)}
            />
          )}
        </div>
      ),
    },
  ];

  return (
    <Card className="gap-4">
      <CardHeader>
        <CardTitle>Providers</CardTitle>
        <CardDescription>Servers that run elsewhere, named once for every backend on them. A backend takes their settings unless it sets its own.</CardDescription>
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
          <ProviderEditor key={`${editing.isNew}:${editing.name}`} client={client} isNew={editing.isNew} name={editing.name} initial={editing.initial} onDone={async (saved) => {
            setEditing(null);
            if (saved) await reload();
          }} />
        ) : (
          data.editable && (
            <div className="flex flex-wrap items-center gap-2">
              <span className="text-sm text-muted-foreground">Add</span>
              {PROVIDER_PRESETS.filter(([, name]) => !data.providers.some((p) => p.name === name)).map(([label, name, settings]) => (
                <Button key={label} type="button" variant="outline" size="sm" onClick={() => setEditing({ isNew: true, name, initial: settings })}>
                  <Plus aria-hidden="true" />
                  {label}
                </Button>
              ))}
              <Button type="button" variant="outline" size="sm" onClick={() => setEditing({ isNew: true, name: "", initial: {} })}>
                <Plus aria-hidden="true" />
                Other
              </Button>
            </div>
          )
        )}
      </CardContent>
    </Card>
  );
}

/** How the provider editor lays out and words the Provider schema's fields. */
const PROVIDER_UI: UiSchema = {
  "ui:layout": [{ rows: [["url"], ["health_path", "max_concurrency", "request_timeout", "on_evict"], ["description"]] }],
  url: { "ui:title": "URL", "ui:placeholder": "http://127.0.0.1:11434", "ui:description": "From a container, the host is host.docker.internal" },
  health_path: { "ui:title": "Health path", "ui:description": "A model list here is checked for the model" },
  max_concurrency: { "ui:title": "Concurrency" },
  request_timeout: { "ui:title": "Request timeout", "ui:description": "Seconds" },
  on_evict: { "ui:title": "On evict", "ui:description": "Unload: Ollama frees the memory at once", "ui:enumNames": ["Nothing", "Unload the model"] },
};

/** A provider's name (it is in the path) and its settings, a form made from the Provider schema. */
function ProviderEditor(props: { client: ApiClient; isNew: boolean; name: string; initial: Partial<Provider> | ProviderInfo; onDone(saved: boolean): void }) {
  const { schema, error } = useApiSchema("Provider");
  const [name, setName] = useState(props.name);
  const [value, setValue] = useState<Partial<Provider> | null>(null);
  if (schema && value === null) setValue(formValue(schema, props.initial as Record<string, unknown>) as Partial<Provider>);
  const form = useRef<SchemaFormHandle>(null);

  const save = useAction(
    async (event: FormEvent) => {
      event.preventDefault();
      const problem = providerNameProblem(name);
      if (!(form.current?.validate() ?? false) || problem) throw new Error(problem || "Some fields need fixing: see under each");
      await props.client.put("/admin/model-servers/providers/{name}", { params: { path: { name: name.trim() } }, body: value as Provider });
      props.onDone(true);
    },
    { notice: true, success: () => `Saved ${name.trim()}.` },
  );

  return (
    <form noValidate onSubmit={save.run} className="grid gap-3 rounded-lg border p-4">
      <h4 className="text-sm font-semibold">{props.isNew ? "New provider" : `Edit ${name}`}</h4>
      <Notice notice={save.notice} />
      <Field label="Name" className="sm:w-1/2" hint={props.isNew ? "What backends call it: lower case letters, digits, _ and -" : "Fixed once saved"}>
        <Input autoComplete="off" spellCheck={false} readOnly={!props.isNew} className="font-mono text-[13px]" placeholder="ollama" value={name} onChange={(e) => setName(e.target.value)} />
      </Field>
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

// ---------------------------------------------------------------- editor
/**
 * The form for one entry (``loaded`` null for a new one): its task and its
 * backends in order. Test probes the form as it stands; saving PUTs it whole.
 */
function EditorView(props: { client: ApiClient; data: ModelServers; loaded: ModelServerInfo | null; initial: EntryForm; reload(): Promise<unknown> }) {
  const { client, data, loaded } = props;
  const [form, setForm] = useState(props.initial);
  const [probe, setProbe] = useState<{ key: string; result: ModelServerProbe } | null>(null);
  const isNew = !loaded;
  const providers = data.providers.map((p) => p.name);
  const key = formKey(form);
  const dirty = useMemo(() => key !== formKey(props.initial), [key, props.initial]);
  // A probe describes the form it ran on; after an edit it is shown faded.
  const stale = probe !== null && probe.key !== key;

  const set = (change: Partial<EntryForm>) => setForm((f) => ({ ...f, ...change }));
  const setBackend = (i: number, change: Partial<BackendForm>) => set({ backends: form.backends.map((b, j) => (j === i ? { ...b, ...change } : b)) });
  const move = (i: number, by: number) => {
    const backends = [...form.backends];
    [backends[i], backends[i + by]] = [backends[i + by], backends[i]];
    set({ backends });
  };

  /** The entry the form describes; throws what is wrong with it, which the action shows. */
  const checked = () => {
    const errors = validateEntry(form, providers);
    if (errors.length) throw new Error(errors.join(" · "));
    return entryFromForm(form);
  };

  // Test and Save share the line under the form: each starts by clearing the other's.
  const test = useAction(
    async () => {
      save.clear();
      const entry = checked();
      const result = await client.post("/admin/model-servers/probe", { body: { model_id: form.model_id.trim(), entry } });
      setProbe({ key, result });
    },
    { notice: true },
  );

  const save = useAction(
    async (event: FormEvent) => {
      event.preventDefault();
      test.clear();
      const entry = checked();
      const id = form.model_id.trim();
      const listing = await client.put("/admin/model-servers/models/{model_id}", { params: { path: { model_id: id } }, body: entry });
      toast.success(`Saved ${id}.`, listing.notes.length ? { description: listing.notes.join(" ") } : undefined);
      // As the server keeps it (tags trimmed, say), so the form is not left looking edited.
      const saved = listing.models.find((m) => m.model_id === id);
      if (saved) setForm(entryForm(saved, id));
      await props.reload();
      if (isNew) location.hash = serversHash({ view: "model", id });
    },
    { notice: true },
  );

  const remove = () => {
    if (!loaded) return;
    const revert = loaded.origin === "replaced";
    client.delete("/admin/model-servers/models/{model_id}", { params: { path: { model_id: loaded.model_id } } }).then(async () => {
      toast.success(revert ? `${loaded.model_id} is back to the file's entry.` : `Deleted ${loaded.model_id}.`);
      if (revert) setForm(entryForm(loaded.file_entry!, loaded.model_id));
      else location.hash = serversHash({ view: "overview" });
      await props.reload();
    }, failed(revert ? `Could not revert ${loaded.model_id}` : `Could not delete ${loaded.model_id}`));
  };

  const addBackend = () => set({ backends: [...form.backends, emptyBackend(providers.length ? "provider" : "url", providers[0])] });
  const summary = probe && (probe.result.chosen === null ? "No backend can serve it now." : `A load now would use backend ${probe.result.chosen + 1}.`);

  return (
    <div className="grid min-w-0 gap-5">
      <Card className="gap-4">
        <CardHeader>
          <CardTitle className="font-mono text-lg [overflow-wrap:anywhere]">{isNew ? "New model server" : loaded.model_id}</CardTitle>
          <CardDescription>
            {isNew
              ? "A model ID clients can name, and the backends that can serve it, tried in order."
              : [ORIGIN[loaded.origin], loaded.status, loaded.serving && `served by ${loaded.serving}`].filter(Boolean).join(" · ")}
          </CardDescription>
          {!isNew && data.editable && (
            <CardAction>
              <Button type="button" variant="outline" size="sm" asChild>
                <a href={serversHash({ view: "new", dup: loaded.model_id })}>
                  <Copy aria-hidden="true" />
                  Duplicate
                </a>
              </Button>
            </CardAction>
          )}
        </CardHeader>
        <form onSubmit={save.run}>
          <CardContent className="grid gap-6">
            {!data.editable && <Notice notice={{ text: "Read-only: MODEL_SERVERS_SAVED_FILE is empty, so changes cannot be saved." }} />}
            <Notice notice={save.notice.text ? save.notice : test.notice} />

            <section className="grid gap-3">
              <Heading>About</Heading>
              <div className="grid gap-3 sm:grid-cols-2">
                <Field label="Model ID" hint={isNew ? "What clients send as model; letters, digits and ._:/@+-" : "Fixed once saved; duplicate to rename"}>
                  <Input autoComplete="off" spellCheck={false} readOnly={!isNew} className="font-mono text-[13px]" placeholder="qwen3.5-4b" value={form.model_id} onChange={(e) => set({ model_id: e.target.value })} />
                </Field>
                <SelectField
                  label="Task"
                  hint="Vision-language takes images; the others text, audio or documents"
                  triggerClassName="font-mono text-xs"
                  value={form.task}
                  onChange={(v) => set({ task: v as ModelTask })}
                  options={(data.tasks.length ? (data.tasks as ModelTask[]) : TASKS).map((t) => ({ value: t, label: t, className: "font-mono text-xs" }))}
                />
              </div>
              <div className="grid gap-3 sm:grid-cols-[minmax(0,2fr)_minmax(0,1fr)]">
                <Field label="Description" hint="One line the model browser shows">
                  <Input value={form.description} onChange={(e) => set({ description: e.target.value })} />
                </Field>
                <Field label="Tags" hint="Comma-separated, e.g. chat, image">
                  <Input autoComplete="off" value={form.tags} onChange={(e) => set({ tags: e.target.value })} />
                </Field>
              </div>
            </section>

            <section className="grid gap-3">
              <Heading hint="Tried in this order when the model loads. The first that can serve it is used until the model unloads; one that fails to start is skipped for a minute.">
                Backends
              </Heading>
              {form.backends.map((b, i) => (
                <BackendEditor
                  key={i}
                  index={i}
                  count={form.backends.length}
                  form={b}
                  providers={data.providers}
                  commandsAllowed={data.commands_allowed}
                  probe={probe?.result.backends[i]}
                  chosen={probe?.result.chosen === i}
                  stale={stale}
                  onChange={(change) => setBackend(i, change)}
                  onMove={(by) => move(i, by)}
                  onRemove={() => set({ backends: form.backends.filter((_, j) => j !== i) })}
                />
              ))}
              <div className="flex flex-wrap items-center gap-3">
                <Button type="button" variant="outline" size="sm" onClick={addBackend}>
                  <Plus aria-hidden="true" />
                  Add backend
                </Button>
                {summary && <span className={cn("text-sm", stale ? "text-muted-foreground" : probe.result.chosen === null ? "text-critical-text" : "text-good-text")}>{summary}{stale && " (before your last change)"}</span>}
              </div>
            </section>
          </CardContent>
          <CardFooter className="mt-6 flex flex-wrap items-center gap-3 border-t pt-4 [.border-t]:pt-4">
            {loaded && data.editable && loaded.origin === "saved" && (
              <ConfirmButton size="default" title={`Delete ${loaded.model_id}?`} body="Its entry is removed; if it is loaded, it is unloaded once its requests finish." onConfirm={remove} />
            )}
            {loaded && data.editable && loaded.origin === "replaced" && (
              <ConfirmButton
                size="default"
                label="Revert to the file"
                icon={<Undo2 aria-hidden="true" />}
                title={`Revert ${loaded.model_id}?`}
                body="The saved entry is removed and the file's is used again."
                action="Revert"
                onConfirm={remove}
              />
            )}
            {loaded?.origin === "file" && <span className="text-xs text-muted-foreground">From the file. Saving keeps an edited copy here, which can be reverted.</span>}
            <div className="ml-auto flex items-center gap-3">
              {dirty && <span className="text-xs text-muted-foreground">Unsaved changes</span>}
              <SubmitButton type="button" variant="outline" busy={test.busy} icon={<Radar aria-hidden="true" />} onClick={test.run}>
                Test
              </SubmitButton>
              <SubmitButton busy={save.busy} disabled={!data.editable || (!isNew && !dirty)}>
                {isNew ? "Create" : "Save"}
              </SubmitButton>
            </div>
          </CardFooter>
        </form>
      </Card>
    </div>
  );
}

function BackendEditor(props: {
  index: number;
  count: number;
  form: BackendForm;
  providers: ProviderInfo[];
  commandsAllowed: boolean;
  probe?: BackendProbe;
  chosen: boolean;
  stale: boolean;
  onChange(change: Partial<BackendForm>): void;
  onMove(by: number): void;
  onRemove(): void;
}) {
  const { form, index } = props;
  const n = index + 1;
  // A command from the file is kept as it is unless MODEL_SERVERS_ALLOW_COMMANDS is on.
  const locked = form.kind === "cmd" && !props.commandsAllowed;
  const provider = props.providers.find((p) => p.name === form.provider);
  const elsewhere = form.kind === "provider" || form.kind === "url";
  // On evict: unset leaves it to the provider (or the default); the choices depend on the kind.
  const evictDefault = provider ? `Provider's (${provider.on_evict === "unload" ? "unload" : "nothing"})` : "Default (stop)";
  const evictOptions: SelectOption[] = [
    { value: "stop", label: form.kind === "cmd" ? "Stop the server" : elsewhere ? "Nothing" : "Unload" },
    ...(form.kind === "cmd" ? [{ value: "sleep", label: "Sleep (vLLM sleep mode)" }] : []),
    ...(elsewhere ? [{ value: "unload", label: "Unload the model (Ollama)" }] : []),
  ];

  return (
    <div className={cn("grid gap-3 rounded-lg border p-3", props.chosen && !props.stale && "border-good-text/50")}>
      <div className="flex flex-wrap items-center gap-2">
        <Badge variant="secondary" className="tabular-nums">
          {n}
        </Badge>
        <SelectInput
          className="h-8 w-36"
          aria-label={`Backend ${n}: kind`}
          disabled={locked}
          value={form.kind}
          onChange={(v) => props.onChange({ kind: v as BackendKind })}
          display={BACKEND_KINDS.find(([value]) => value === form.kind)?.[1]}
          options={BACKEND_KINDS.map(([value, label, hint]) => ({
            value,
            disabled: value === "cmd" && !props.commandsAllowed && form.kind !== "cmd",
            label: (
              <>
                <span>{label}</span>
                <span className="text-xs text-muted-foreground">{value === "cmd" && !props.commandsAllowed ? "only in the file" : hint}</span>
              </>
            ),
          }))}
        />
        <div className="ml-auto flex gap-1">
          <Button type="button" variant="ghost" size="icon-sm" aria-label={`Move backend ${n} up`} disabled={index === 0} onClick={() => props.onMove(-1)}>
            <ArrowUp aria-hidden="true" />
          </Button>
          <Button type="button" variant="ghost" size="icon-sm" aria-label={`Move backend ${n} down`} disabled={index === props.count - 1} onClick={() => props.onMove(1)}>
            <ArrowDown aria-hidden="true" />
          </Button>
          <Button type="button" variant="ghost" size="icon-sm" className="text-muted-foreground" aria-label={`Remove backend ${n}`} disabled={props.count === 1} onClick={props.onRemove}>
            <X aria-hidden="true" />
          </Button>
        </div>
      </div>
      {props.probe && (
        <p className={cn("flex items-start gap-2 text-xs [overflow-wrap:anywhere]", props.stale && "opacity-50")}>
          <StatusDot color={PROBE[props.probe.state].color} className="mt-1" />
          <span>
            <span className="font-medium">{PROBE[props.probe.state].label}</span>: {props.probe.detail}
          </span>
          {props.chosen && <Badge className="h-4 shrink-0 px-1 text-[10px]">used</Badge>}
        </p>
      )}
      {form.kind === "cmd" && (
        <Field label="Command" hint={locked ? "From the file; MODEL_SERVERS_ALLOW_COMMANDS is off, so it is kept as it is" : "{port} is replaced by a free port; it runs on this host"}>
          <Input autoComplete="off" spellCheck={false} readOnly={locked} className="font-mono text-[13px]" placeholder="mlx_vlm.server --model mlx-community/Qwen3.5-4B-4bit --host 127.0.0.1 --port {port}" value={form.cmd} onChange={(e) => props.onChange({ cmd: e.target.value })} />
        </Field>
      )}

      <div className="grid gap-3 sm:grid-cols-2">
        {form.kind === "provider" && (
          <SelectField
            label="Provider"
            triggerClassName="font-mono text-xs"
            aria-label={`Backend ${n}: provider`}
            placeholder={props.providers.length ? "Pick one" : "Add one under Providers & files"}
            value={form.provider}
            onChange={(v) => props.onChange({ provider: v })}
            options={props.providers.map((p) => ({
              value: p.name,
              className: "font-mono text-xs",
              label: (
                <>
                  {p.name}
                  <span className="text-muted-foreground">{p.url}</span>
                </>
              ),
            }))}
          />
        )}
        {form.kind === "url" && (
          <Field label="URL" hint="Its base URL, without /v1">
            <Input type="url" autoComplete="off" spellCheck={false} className="font-mono text-[13px]" placeholder="http://gpu-box:8000" value={form.url} onChange={(e) => props.onChange({ url: e.target.value })} />
          </Field>
        )}
        {form.kind === "hub" && (
          <Field label="Hub model" hint="Run here by the model file that serves it; often too big unquantized">
            <Input autoComplete="off" spellCheck={false} className="font-mono text-[13px]" placeholder="Qwen/Qwen3.5-4B" value={form.hub} onChange={(e) => props.onChange({ hub: e.target.value })} />
          </Field>
        )}
        {form.kind !== "hub" && (
          <Field label="Upstream model" hint={form.kind === "provider" ? "The server's name for it: Ollama qwen3.5:4b, LM Studio qwen/qwen3.5-4b" : "The model sent to the server; empty: the model ID"}>
            <Input autoComplete="off" spellCheck={false} className="font-mono text-[13px]" value={form.upstream_model} onChange={(e) => props.onChange({ upstream_model: e.target.value })} />
          </Field>
        )}
      </div>

      <Collapsible>
        <CollapsibleTrigger className="group flex items-center gap-1.5 rounded-md text-xs font-medium text-muted-foreground outline-none hover:text-foreground focus-visible:ring-[3px] focus-visible:ring-ring/50">
          <ChevronRight className="size-3.5 transition-transform duration-200 group-data-[state=open]:rotate-90" aria-hidden="true" />
          More settings
        </CollapsibleTrigger>
        <CollapsibleContent className="grid gap-3 pt-3">
          <div className="grid gap-3 sm:grid-cols-4">
            <Field label="Memory GB" hint={elsewhere ? "Empty: none (it runs elsewhere)" : "Empty: estimated from the weights"}>
              <Input type="number" min={0} step={0.1} className="font-mono text-[13px]" value={form.memory_gb} onChange={(e) => props.onChange({ memory_gb: e.target.value })} />
            </Field>
            {form.kind !== "hub" && (
              <Field label="Concurrency" hint={provider ? `Empty: the provider's ${provider.max_concurrency}` : "Empty: 8"}>
                <Input type="number" min={1} step={1} className="font-mono text-[13px]" value={form.max_concurrency} onChange={(e) => props.onChange({ max_concurrency: e.target.value })} />
              </Field>
            )}
            {form.kind !== "hub" && (
              <Field label="Request timeout" hint={provider ? `Seconds; empty: ${provider.request_timeout}` : "Seconds; empty: 600"}>
                <Input type="number" min={1} step={1} className="font-mono text-[13px]" value={form.request_timeout} onChange={(e) => props.onChange({ request_timeout: e.target.value })} />
              </Field>
            )}
            {form.kind === "cmd" && (
              <Field label="Startup timeout" hint="Seconds; empty: 600">
                <Input type="number" min={1} step={1} className="font-mono text-[13px]" value={form.startup_timeout} onChange={(e) => props.onChange({ startup_timeout: e.target.value })} />
              </Field>
            )}
            {form.kind !== "hub" && (
              <Field label="Health path" hint={provider ? `Empty: ${provider.health_path}` : "Empty: /v1/models"}>
                <Input autoComplete="off" spellCheck={false} className="font-mono text-[13px]" value={form.health_path} onChange={(e) => props.onChange({ health_path: e.target.value })} />
              </Field>
            )}
            {form.kind !== "hub" && (
              <SelectField
                label="On evict"
                aria-label={`Backend ${n}: on evict`}
                value={form.on_evict}
                onChange={(v) => props.onChange({ on_evict: v as BackendForm["on_evict"] })}
                none={evictDefault}
                options={evictOptions}
              />
            )}
          </div>
          {form.kind === "cmd" && (
            <KVRows
              label="Environment"
              ariaLabel={`Backend ${props.index + 1} environment`}
              rows={form.env}
              onChange={(env) => props.onChange({ env })}
              keyLabel="variable"
              valueLabel="value"
              keyPlaceholder="NAME"
              addLabel="Add variable"
              readOnly={locked}
            />
          )}
          {form.kind !== "hub" && (
            <Field label="Extra request fields" hint="A JSON object added to each chat request; the request's own fields win. E.g. a thinking switch.">
              <CodeEditor
                aria-label="Extra request fields"
                language="json"
                minHeight="4rem"
                placeholder={'{"chat_template_kwargs": {"enable_thinking": false}}'}
                value={form.extra}
                onChange={(extra) => props.onChange({ extra })}
              />
            </Field>
          )}
        </CollapsibleContent>
      </Collapsible>
    </div>
  );
}
