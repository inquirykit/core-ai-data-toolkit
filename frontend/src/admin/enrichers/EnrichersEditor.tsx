// Editing an enricher: a form made from the EnricherSpec schema (enricherUi gives its sections and wording), and its key.

import { useRef, useState, type FormEvent, type ReactNode } from "react";
import { Copy, Trash2, Undo2 } from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Card, CardAction, CardContent, CardDescription, CardFooter, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { useAction } from "../../shared/action";
import { useApi } from "../../shared/api";
import { formValue, valueKey } from "../../shared/form-schema";
import { SchemaForm, useApiSchema, type SchemaFormHandle, type UiSchema } from "../../shared/schema-form";
import { ConfirmButton, Notice, StatusDot, SubmitButton } from "../../shared/components";
import type { EnricherInfo, EnricherSpec, EntityType } from "../../shared/types";
import { enricherHash, NEW_SPEC, origin, sentSpec, TERMS, usability, type EditedSpec } from "./enrichers";
import { DetailSkeleton } from "../layout";
import { TestCard } from "./EnrichersTest";

/**
 * How the editor lays out and words an enricher's fields, beyond what its schema (EnricherSpec) says: the sections,
 * titles, placeholders and hints. A field the schema gains shows up at the end with its own title and description.
 */
function enricherUi(opts: { isNew: boolean; post: boolean; secretForm: ReactNode }): UiSchema {
  return {
    "ui:layout": [
      { title: "About", rows: [["name", "docs"], ["description"], ["inputs"], ["terms", "attribution"], ["enabled"]] },
      {
        title: "Key",
        hint: "The service's API key, read from an environment variable and put into the request as {secret}.",
        rows: [["secret", "secret_optional"]],
        extra: opts.secretForm,
      },
      { title: "Limits", rows: [["ttl", "rate_limit", "timeout"], ["value_filters"]] },
      { title: "Request", rows: [["request"]] },
      {
        title: "Outputs",
        hint: (
          <>
            Each output is a{" "}
            <a className="underline underline-offset-2 hover:text-foreground" href="https://jmespath.org" target="_blank" rel="noreferrer">
              JMESPath
            </a>{" "}
            expression over the reply, like <code>data.abuseConfidenceScore</code> or <code>data.reports[0].comment</code>.
          </>
        ),
        rows: [["outputs"]],
      },
    ],
    name: {
      "ui:placeholder": "abuseipdb",
      "ui:readonly": !opts.isNew,
      "ui:description": opts.isNew ? "Lowercase letters, digits and underscores; also the file's name" : "Fixed once saved; duplicate to rename",
    },
    docs: { "ui:title": "Docs URL", "ui:placeholder": "https://", "ui:description": "Where to read about the service and get a key" },
    description: { "ui:placeholder": "What the service tells about a value" },
    inputs: { "ui:description": "The entity types the service takes; a lookup only asks enrichers that take the value's type." },
    terms: { "ui:description": "Lookups skip enrichers whose terms ENRICHER_TERMS does not allow", "ui:enumNames": TERMS.map(([v, text]) => `${v} · ${text}`) },
    attribution: { "ui:placeholder": "Data by …", "ui:description": "Shown with every result, for services that ask for credit" },
    enabled: { "ui:description": "Off, it is skipped by lookups but can still be tested." },
    secret: { "ui:title": "Secret env var", "ui:placeholder": "ABUSEIPDB_KEY", "ui:description": "Capital letters, digits and underscores, ending in _KEY, _TOKEN or _SECRET; empty when the service needs no key" },
    secret_optional: { "ui:title": "Also run without the key", "ui:description": "For services with a free tier that a key only widens." },
    ttl: { "ui:title": "Cache TTL", "ui:placeholder": "24h", "ui:description": "How long an answer is kept: 30m, 24h, 7d" },
    rate_limit: { "ui:placeholder": "1000/day", "ui:description": "Requests per window, or empty: 1000/day, 4/min" },
    timeout: { "ui:description": "Seconds, 1 to 60" },
    value_filters: { "ui:description": "Applied to the value before the request, in this order: lower, upper, strip, then a hash." },
    request: {
      "ui:layout": [{ rows: [["method", "url"], ["query"], ["headers"], ["body_format"], ["body"]] }],
      method: { "ui:widget": "radio", "ui:options": { inline: true } },
      url: {
        "ui:title": "URL",
        "ui:placeholder": "https://api.example.com/v2/check/{value}",
        "ui:description": "{value} is the value being looked up (URL-encoded in the URL and query), {secret} the key",
      },
      query: { "ui:options": { keyPlaceholder: "ipAddress", valuePlaceholder: "{value}", addLabel: "Add parameter" } },
      headers: { "ui:options": { keyPlaceholder: "Authorization", valuePlaceholder: "Bearer {secret}", addLabel: "Add header" } },
      // A body is for POST only (the server refuses one on a GET).
      body_format: opts.post ? { "ui:description": "json: sent as JSON; form: sent URL-encoded, values as strings" } : { "ui:widget": "hidden" },
      body: opts.post ? { "ui:description": "A JSON object; {value} and {secret} are filled in its strings." } : { "ui:widget": "hidden" },
    },
    // The section's heading names them and its hint says what they are.
    outputs: {
      "ui:title": "",
      "ui:description": "",
      "ui:options": { keyLabel: "name", valueLabel: "expression", keyPlaceholder: "abuse_score", valuePlaceholder: "data.abuseConfidenceScore", addLabel: "Add output" },
    },
  };
}
/**
 * The form for one enricher (``loaded`` null for a new one, prefilled from ``source`` when duplicating), made from
 * its schema, and under it, for a saved one, the test bench. Saving PUTs the whole spec.
 */
export function EnricherEditor(props: { types: EntityType[]; loaded: EnricherInfo | null; source?: EnricherInfo; onChange(): Promise<unknown> }) {
  const client = useApi();
  const { schema, error: schemaError } = useApiSchema("EnricherSpec");
  const [loaded, setLoaded] = useState(props.loaded);
  const isNew = !loaded;
  // What the form starts from, once the schema says which of a record's fields are the spec's.
  const start = (from: EnricherInfo | null): EditedSpec =>
    from && schema ? formValue(schema, from) : props.source && schema ? { ...formValue(schema, props.source), name: "" } : NEW_SPEC;
  const [value, setValue] = useState<EditedSpec | null>(null);
  if (schema && value === null) setValue(start(loaded));
  const form = useRef<SchemaFormHandle>(null);
  const dirty = value !== null && valueKey(value) !== valueKey(start(loaded));

  const save = useAction(
    async (event: FormEvent) => {
      event.preventDefault();
      if (!value || !form.current?.validate()) throw new Error("Some fields need fixing: see under each");
      const spec = sentSpec(value as EnricherSpec);
      const saved = await client.put("/enrichers/{name}", { params: { path: { name: spec.name } }, body: spec });
      setLoaded(saved);
      setValue(null);
      await props.onChange();
      if (isNew) location.hash = enricherHash({ view: "edit", name: saved.name });
      return saved;
    },
    { notice: true, success: (saved) => `Saved ${saved.name}.` },
  );

  const remove = useAction(
    async () => {
      if (!loaded) return;
      const reset = loaded.builtin;
      await client.delete("/enrichers/{name}", { params: { path: { name: loaded.name } } });
      toast.success(reset ? `${loaded.name} is back to its built-in version.` : `Deleted ${loaded.name}.`);
      await props.onChange();
      if (!reset) location.hash = enricherHash({ view: "lookup" });
      else {
        // The list now holds the built-in spec; the view is keyed by name, so it keeps this state: load it over.
        setLoaded(await client.get("/enrichers/{name}", { params: { path: { name: loaded.name } } }));
        setValue(null);
      }
    },
    { failed: () => `Could not ${loaded?.builtin ? "reset" : "delete"} ${loaded?.name}` },
  );

  const secretForm = loaded?.secret && (
    <SecretForm
      info={loaded}
      onSaved={(isSet) => {
        setLoaded({ ...loaded, secret_set: isSet });
        props.onChange();
      }}
    />
  );
  const uiSchema = enricherUi({ isNew, post: (value as EnricherSpec | null)?.request?.method === "POST", secretForm });

  return (
    <div className="grid min-w-0 gap-5">
      <Card className="gap-4">
        <CardHeader>
          <CardTitle className="font-mono text-lg">{isNew ? "New enricher" : loaded.name}</CardTitle>
          <CardDescription>
            {isNew
              ? "A declarative HTTP call: a value goes in, outputs picked from the reply with JMESPath come out."
              : `${origin(loaded)} · ${usability(loaded).label}`}
          </CardDescription>
          {!isNew && (
            <CardAction>
              <Button type="button" variant="outline" size="sm" asChild>
                <a href={enricherHash({ view: "new", dup: loaded.name })}>
                  <Copy aria-hidden="true" />
                  Duplicate
                </a>
              </Button>
            </CardAction>
          )}
        </CardHeader>
        <form noValidate onSubmit={save.run}>
          <CardContent className="grid gap-6">
            <Notice notice={save.notice} />
            {schema && value ? (
              <SchemaForm ref={form} schema={schema} uiSchema={uiSchema} value={value} onChange={setValue} />
            ) : schemaError ? (
              <Notice notice={{ text: schemaError, kind: "error" }} />
            ) : (
              <DetailSkeleton />
            )}
          </CardContent>
          <CardFooter className="mt-6 flex flex-wrap items-center gap-3 border-t pt-4 [.border-t]:pt-4">
            {loaded && !loaded.builtin && (
              <ConfirmButton
                size="default"
                label="Delete"
                icon={<Trash2 aria-hidden="true" />}
                title={`Delete ${loaded.name}?`}
                body="Its file is removed and lookups stop asking it. This cannot be undone."
                busy={remove.busy}
                onConfirm={remove.run}
              />
            )}
            {loaded?.builtin && loaded.overridden && (
              <ConfirmButton
                size="default"
                label="Reset to built-in"
                icon={<Undo2 aria-hidden="true" />}
                title={`Reset ${loaded.name}?`}
                body="Your saved version is removed and the one shipped with the service is used again."
                busy={remove.busy}
                onConfirm={remove.run}
              />
            )}
            {loaded?.builtin && !loaded.overridden && (
              <span className="text-xs text-muted-foreground">Built in. It can be disabled or edited; edits can be reset later.</span>
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
      {loaded && <TestCard name={loaded.name} types={props.types} blocked={dirty ? "Save or undo the changes first: the test runs the saved version" : ""} />}
    </div>
  );
}
/** The key's value, kept encrypted on the server; never read back. */
function SecretForm({ info, onSaved }: { info: EnricherInfo; onSaved(set: boolean): void }) {
  const client = useApi();
  const [value, setValue] = useState("");
  const save = useAction(
    async () => {
      const r = await client.put("/enrichers/{name}/secret", { params: { path: { name: info.name } }, body: { value } });
      setValue("");
      onSaved(r.secret_set);
      return r;
    },
    { notice: true, success: (r) => (r.secret_set ? `Saved ${r.secret}.` : `Cleared ${r.secret}.`) },
  );
  return (
    <div className="grid gap-2 rounded-lg border border-dashed p-3">
      <p className="flex items-center gap-2 text-sm">
        <StatusDot color={info.secret_set ? "var(--good)" : "var(--warning)"} />
        Key <code className="font-mono text-[13px]">{info.secret}</code> is {info.secret_set ? "set" : "not set"}
      </p>
      <div className="flex flex-wrap items-center gap-2">
        <Input
          type="password"
          aria-label={`Value of ${info.secret}`}
          placeholder="Paste the key"
          autoComplete="off"
          className="h-8 max-w-sm flex-1 font-mono text-xs"
          value={value}
          onChange={(e) => setValue(e.target.value)}
        />
        <SubmitButton type="button" variant="secondary" size="sm" busy={save.busy} onClick={save.run}>
          {value ? "Save key" : "Clear key"}
        </SubmitButton>
      </div>
      <p className="text-xs text-muted-foreground">
        Kept encrypted on the server and never shown again; saving an empty value clears it.
      </p>
      <Notice notice={save.notice} />
    </div>
  );
}
