// One entry's editor: its task and its backends in order, probed as it stands, saved whole.

import { useMemo, useState, type FormEvent } from "react";
import { Copy, Plus, Radar, Undo2 } from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Card, CardAction, CardContent, CardDescription, CardFooter, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { cn } from "@/lib/utils";
import { useAction } from "../../shared/action";
import { useApi } from "../../shared/api";
import { ConfirmButton, Field, Heading, Notice, SelectField, SubmitButton } from "../../shared/components";
import type { ModelServerInfo, ModelServerProbe, ModelServers, ModelTask } from "../../shared/types";
import { emptyBackend, entryForm, entryFromForm, formKey, ORIGIN, serversHash, validateEntry, type BackendForm, type EntryForm } from "./servers";
import { BackendEditor } from "./ServersBackendEditor";

/**
 * The form for one entry (``loaded`` null for a new one): its task and its
 * backends in order. Test probes the form as it stands; saving PUTs it whole.
 */
export function EntryEditor(props: { data: ModelServers; loaded: ModelServerInfo | null; initial: EntryForm; reload(): Promise<unknown> }) {
  const client = useApi();
  const { data, loaded } = props;
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

  const remove = useAction(
    async () => {
      if (!loaded) return;
      const revert = loaded.origin === "replaced";
      await client.delete("/admin/model-servers/models/{model_id}", { params: { path: { model_id: loaded.model_id } } });
      toast.success(revert ? `${loaded.model_id} is back to the file's entry.` : `Deleted ${loaded.model_id}.`);
      if (revert) setForm(entryForm(loaded.file_entry!, loaded.model_id));
      else location.hash = serversHash({ view: "overview" });
      await props.reload();
    },
    { failed: () => `Could not ${loaded?.origin === "replaced" ? "revert" : "delete"} ${loaded?.model_id}` },
  );

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
          {!isNew && (
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
                  options={(data.tasks as ModelTask[]).map((t) => ({ value: t, label: t, className: "font-mono text-xs" }))}
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
            {loaded?.origin === "saved" && (
              <ConfirmButton
                size="default"
                title={`Delete ${loaded.model_id}?`}
                body="Its entry is removed; if it is loaded, it is unloaded once its requests finish."
                busy={remove.busy}
                onConfirm={remove.run}
              />
            )}
            {loaded?.origin === "replaced" && (
              <ConfirmButton
                size="default"
                label="Revert to the file"
                icon={<Undo2 aria-hidden="true" />}
                title={`Revert ${loaded.model_id}?`}
                body="The saved entry is removed and the file's is used again."
                action="Revert"
                busy={remove.busy}
                onConfirm={remove.run}
              />
            )}
            {loaded?.origin === "file" && <span className="text-xs text-muted-foreground">From the file. Saving keeps an edited copy here, which can be reverted.</span>}
            <div className="ml-auto flex items-center gap-3">
              {dirty && <span className="text-xs text-muted-foreground">Unsaved changes</span>}
              <SubmitButton type="button" variant="outline" busy={test.busy} icon={<Radar aria-hidden="true" />} onClick={test.run}>
                Test
              </SubmitButton>
              <SubmitButton busy={save.busy} disabled={!isNew && !dirty}>
                {isNew ? "Create" : "Save"}
              </SubmitButton>
            </div>
          </CardFooter>
        </form>
      </Card>
    </div>
  );
}
