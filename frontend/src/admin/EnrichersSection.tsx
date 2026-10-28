import { useRef, useState, type FormEvent, type ReactNode } from "react";
import { ChevronRight, Copy, Search, Trash2, Undo2 } from "lucide-react";
import { toast } from "sonner";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardAction, CardContent, CardDescription, CardFooter, CardHeader, CardTitle } from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { cn } from "@/lib/utils";
import { useAction } from "../shared/action";
import type { ApiClient } from "../shared/api";
import { useHash } from "../shared/browser";
import { failed } from "../shared/errors";
import { formValue, valueKey } from "../shared/form-schema";
import { errorText } from "../shared/query";
import { SchemaForm, useApiSchema, type SchemaFormHandle, type UiSchema } from "../shared/schema-form";
import { ConfirmButton, EmptyCard, Field, Notice, SelectField, StatusDot, StatusLabel, SubmitButton } from "../shared/components";
import {
  ENTITY_TYPES,
  type EnrichBody,
  type EnricherInfo,
  type EnricherList,
  type EnricherSpec,
  type EnricherTest,
  type EnricherTestBody,
  type EnrichResponse,
  type EnrichResult,
  type EntityType,
} from "../shared/types";
import {
  enricherHash,
  fmtFreshness,
  fmtOutput,
  locationFromHash,
  NEW_SPEC,
  origin,
  sentSpec,
  statusLabel,
  TERMS,
  usability,
  type EditedSpec,
  type EnricherLocation,
} from "./enrichers";
import { DetailSkeleton, ListCard, MasterDetail, NavRow, NewLink, NoRows } from "./layout";

/** The type a lookup names, or "" to detect it. */
type TypeChoice = EntityType | "";

/**
 * /admin#/enrichers: the enrichers on the left; on the right a lookup
 * across all of them, or the chosen one's editor and a test bench.
 */
export function EnrichersSection({ client }: { client: ApiClient }) {
  const loc = useHash(locationFromHash);
  const query = client.useQuery("get", "/enrichers");
  const list = query.data ?? null;
  const error = errorText(query.error);
  const reload = query.refetch;

  const types = list?.types.length ? list.types : ENTITY_TYPES;
  const byName = (name: string | undefined) => (name ? list?.enrichers.find((e) => e.name === name) : undefined);

  let view: ReactNode;
  if (!list) view = <DetailSkeleton />;
  else if (loc.view === "lookup") view = <LookupCard client={client} types={types} />;
  else if (loc.view === "new") {
    const source = byName(loc.dup);
    view = (
      <EditorView
        key={`new:${loc.dup ?? ""}`}
        client={client}
        types={types}
        loaded={null}
        source={source}
        onChange={reload}
      />
    );
  } else {
    const current = byName(loc.name);
    view = current ? (
      <EditorView key={current.name} client={client} types={types} loaded={current} onChange={reload} />
    ) : (
      <EmptyCard icon={Search} title={`No enricher named ${loc.name}`} body="It may have been deleted. Pick one on the left." />
    );
  }

  return (
    <MasterDetail list={<EnrichersCard list={list} loc={loc} error={error} reload={reload} />}>
      {view}
    </MasterDetail>
  );
}

// ------------------------------------------------------------------ list
function EnrichersCard(props: { list: EnricherList | null; loc: EnricherLocation; error: string; reload(): Promise<unknown> }) {
  const { list, loc } = props;
  const chosen = loc.view === "edit" ? loc.name : null;
  return (
    <ListCard
      title="Enrichers"
      description={list ? `${list.enrichers.length} · ${list.enrichers.filter((e) => e.usable).length} usable` : "Loading…"}
      label="Enrichers"
      onRefresh={props.reload}
      action={<NewLink href={enricherHash({ view: "new" })} />}
      loaded={Boolean(list)}
      error={props.error}
      footer={
        list && (
          <p className="text-xs text-muted-foreground">
            Terms allowed: {list.terms_allowed.length ? list.terms_allowed.join(", ") : "none"}.{" "}
            <code className="text-[11px]">ENRICHER_TERMS</code> changes it.
          </p>
        )
      }
    >
      <NavRow href={enricherHash({ view: "lookup" })} active={loc.view === "lookup"}>
        <Search aria-hidden="true" />
        Lookup
      </NavRow>
      {list?.enrichers.map((e) => (
        <EnricherRow key={e.name} info={e} active={e.name === chosen} />
      ))}
      {list && !list.enrichers.length && <NoRows>No enrichers yet</NoRows>}
    </ListCard>
  );
}

function EnricherRow({ info, active }: { info: EnricherInfo; active: boolean }) {
  const use = usability(info);
  return (
    <NavRow href={enricherHash({ view: "edit", name: info.name })} active={active} tall>
      <span className="flex min-w-0 items-center gap-2">
        <span title={use.label} className="inline-flex">
          <StatusDot color={use.color} />
        </span>
        <span className={cn("truncate font-mono text-[13px]", active && "font-medium")}>{info.name}</span>
        <span className="ml-auto shrink-0 text-[11px] text-muted-foreground">{origin(info)}</span>
      </span>
      <span className="flex flex-wrap gap-1 pl-4">
        {info.inputs.slice(0, 4).map((t) => (
          <Badge key={t} variant="secondary" className="h-4 px-1 font-mono text-[10px] font-normal">
            {t}
          </Badge>
        ))}
        {info.inputs.length > 4 && <span className="text-[10px] text-muted-foreground">+{info.inputs.length - 4}</span>}
      </span>
    </NavRow>
  );
}

// ---------------------------------------------------------------- lookup
function TypeSelect(props: { types: EntityType[]; value: TypeChoice; onChange(v: TypeChoice): void }) {
  return (
    <SelectField
      label="Type"
      triggerClassName="w-40"
      aria-label="Type"
      value={props.value}
      onChange={(v) => props.onChange(v as TypeChoice)}
      none="Detect"
      options={props.types.map((t) => ({ value: t, label: t, className: "font-mono text-xs" }))}
    />
  );
}

function LookupCard({ client, types }: { client: ApiClient; types: EntityType[] }) {
  const [value, setValue] = useState("");
  const [type, setType] = useState<TypeChoice>("");
  const [refresh, setRefresh] = useState(false);
  const [reply, setReply] = useState<EnrichResponse | null>(null);

  const lookup = useAction(
    async (event: FormEvent) => {
      event.preventDefault();
      const body: EnrichBody = { value: value.trim() };
      if (type) body.type = type;
      if (refresh) body.refresh = true;
      setReply(await client.post("/enrich", { body }));
    },
    { notice: true },
  );

  const merged = reply ? Object.entries(reply.merged) : [];
  return (
    <div className="grid min-w-0 gap-5">
      <Card className="gap-4">
        <CardHeader>
          <CardTitle>Look a value up</CardTitle>
          <CardDescription>Every usable enricher that takes the value's type is asked; answers are cached for each enricher's TTL.</CardDescription>
        </CardHeader>
        <CardContent className="grid gap-3">
          <form className="flex flex-wrap items-end gap-2" onSubmit={lookup.run}>
            <Field label="Value" className="min-w-64 flex-1">
              <Input
                placeholder="1.2.3.4, example.com, a hash, an email, CVE-2024-3094…"
                required
                autoComplete="off"
                spellCheck={false}
                className="font-mono text-[13px]"
                value={value}
                onChange={(e) => setValue(e.target.value)}
              />
            </Field>
            <TypeSelect types={types} value={type} onChange={setType} />
            <SubmitButton busy={lookup.busy} icon={<Search aria-hidden="true" />} disabled={!value.trim()}>
              Enrich
            </SubmitButton>
          </form>
          <Label className="w-fit gap-2 font-normal">
            <Checkbox checked={refresh} onCheckedChange={(v) => setRefresh(v === true)} />
            Ignore cached results
          </Label>
          <Notice notice={lookup.notice} />
        </CardContent>
      </Card>

      {reply && (
        <>
          <p className="text-sm text-muted-foreground">
            Detected <Badge variant="outline" className="font-mono">{reply.type}</Badge> · looked up{" "}
            <code className="font-mono text-[13px] text-foreground">{reply.value}</code>
            {!reply.results.length && " · no enricher takes this type"}
          </p>
          {reply.results.map((r) => (
            <ResultCard key={r.enricher} result={r} />
          ))}
          {merged.length > 0 && (
            <Card className="gap-3">
              <CardHeader>
                <CardTitle>Merged</CardTitle>
                <CardDescription>The first ok value of each output, with the enricher it came from.</CardDescription>
              </CardHeader>
              <CardContent>
                <div className="overflow-hidden rounded-lg border">
                  <Table>
                    <TableHeader className="bg-muted/50">
                      <TableRow className="hover:bg-transparent">
                        <TableHead className="pl-4">Output</TableHead>
                        <TableHead>Value</TableHead>
                        <TableHead className="pr-4">Source</TableHead>
                      </TableRow>
                    </TableHeader>
                    <TableBody>
                      {merged.map(([name, v]) => (
                        <TableRow key={name} className="hover:bg-transparent">
                          <TableCell className="pl-4 font-mono text-[13px] font-medium">{name}</TableCell>
                          <TableCell className="max-w-[28rem] truncate font-mono text-[13px]" title={fmtOutput(v)}>
                            {fmtOutput(v)}
                          </TableCell>
                          <TableCell className="pr-4 font-mono text-xs text-muted-foreground">{reply.sources[name] ?? "–"}</TableCell>
                        </TableRow>
                      ))}
                    </TableBody>
                  </Table>
                </div>
              </CardContent>
            </Card>
          )}
        </>
      )}
    </div>
  );
}

/** One enricher's answer: its outputs, the error if any, and the raw reply to unfold. */
function ResultCard({ result }: { result: EnrichResult }) {
  const status = statusLabel(result);
  const outputs = Object.entries(result.outputs);
  return (
    <Card className="gap-3 py-4">
      <CardHeader className="px-5">
        <CardTitle className="flex items-center gap-2 font-mono text-base">
          {result.enricher}
          <Badge variant="outline" className="font-sans font-normal">
            <StatusLabel status={status} className="gap-1.5" />
          </Badge>
          {result.http_status != null && result.status === "error" && (
            <span className="text-xs font-normal text-muted-foreground">HTTP {result.http_status}</span>
          )}
        </CardTitle>
        <CardAction className="text-xs text-muted-foreground tabular-nums">{fmtFreshness(result)}</CardAction>
      </CardHeader>
      <CardContent className="grid gap-3 px-5">
        {result.status === "error" && result.error && <Notice notice={{ text: result.error, kind: "error" }} />}
        {outputs.length > 0 ? (
          <div className="overflow-hidden rounded-lg border">
            <Table>
              <TableBody>
                {outputs.map(([name, v]) => (
                  <TableRow key={name} className="hover:bg-transparent">
                    <TableCell className="w-48 pl-4 font-mono text-[13px] font-medium">{name}</TableCell>
                    <TableCell className="max-w-[28rem] truncate pr-4 font-mono text-[13px]" title={fmtOutput(v)}>
                      {fmtOutput(v)}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>
        ) : (
          result.status === "ok" && <p className="text-sm text-muted-foreground">No outputs mapped</p>
        )}
        {result.raw != null && (
          <Collapsible>
            <CollapsibleTrigger className="group flex items-center gap-1.5 rounded-md text-xs font-medium text-muted-foreground outline-none hover:text-foreground focus-visible:ring-[3px] focus-visible:ring-ring/50">
              <ChevronRight className="size-3.5 transition-transform duration-200 group-data-[state=open]:rotate-90" aria-hidden="true" />
              Raw reply
            </CollapsibleTrigger>
            <CollapsibleContent className="overflow-hidden data-[state=closed]:animate-collapsible-up data-[state=open]:animate-collapsible-down">
              <pre className="mt-2 max-h-80 overflow-auto rounded-lg border bg-muted/40 p-3 font-mono text-xs leading-relaxed">
                {JSON.stringify(result.raw, null, 2)}
              </pre>
            </CollapsibleContent>
          </Collapsible>
        )}
        {result.attribution && <p className="text-xs text-muted-foreground">{result.attribution}</p>}
      </CardContent>
    </Card>
  );
}

// ---------------------------------------------------------------- editor
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
    secret: { "ui:title": "Secret env var", "ui:placeholder": "ABUSEIPDB_KEY", "ui:description": "Capital letters, digits and underscores; empty when the service needs no key" },
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
function EditorView(props: { client: ApiClient; types: EntityType[]; loaded: EnricherInfo | null; source?: EnricherInfo; onChange(): Promise<unknown> }) {
  const { client } = props;
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

  const remove = () => {
    if (!loaded) return;
    const reset = loaded.builtin;
    client
      .delete("/enrichers/{name}", { params: { path: { name: loaded.name } } })
      .then(async () => {
        toast.success(reset ? `${loaded.name} is back to its built-in version.` : `Deleted ${loaded.name}.`);
        await props.onChange();
        if (!reset) location.hash = enricherHash({ view: "lookup" });
        else {
          // The list now holds the built-in spec; the view is keyed by name, so it keeps this state: load it over.
          setLoaded(await client.get("/enrichers/{name}", { params: { path: { name: loaded.name } } }));
          setValue(null);
        }
      }, failed(reset ? `Could not reset ${loaded.name}` : `Could not delete ${loaded.name}`));
  };

  const secretForm = loaded?.secret && (
    <SecretForm
      client={client}
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
                onConfirm={remove}
              />
            )}
            {loaded?.builtin && loaded.overridden && (
              <ConfirmButton
                size="default"
                label="Reset to built-in"
                icon={<Undo2 aria-hidden="true" />}
                title={`Reset ${loaded.name}?`}
                body="Your saved version is removed and the one shipped with the service is used again."
                onConfirm={remove}
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
      {loaded && <TestCard client={client} name={loaded.name} types={props.types} blocked={dirty ? "Save or undo the changes first: the test runs the saved version" : ""} />}
    </div>
  );
}

/** The key's value, written to the server's .env; never read back. */
function SecretForm({ client, info, onSaved }: { client: ApiClient; info: EnricherInfo; onSaved(set: boolean): void }) {
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
        Written to the server's <code>.env</code> file and never shown again; saving an empty value clears it.
      </p>
      <Notice notice={save.notice} />
    </div>
  );
}

// ------------------------------------------------------------------ test
/** Runs the saved enricher against one value, without the cache, and shows the request it made. */
function TestCard({ client, name, types, blocked }: { client: ApiClient; name: string; types: EntityType[]; blocked: string }) {
  const [value, setValue] = useState("");
  const [type, setType] = useState<TypeChoice>("");
  const [test, setTest] = useState<EnricherTest | null>(null);

  const run = useAction(
    async (event: FormEvent) => {
      event.preventDefault();
      const body: EnricherTestBody = { value: value.trim() };
      if (type) body.type = type;
      setTest(await client.post("/enrichers/{name}/test", { params: { path: { name } }, body }));
    },
    { notice: true },
  );

  const headers = test?.request ? Object.entries(test.request.headers) : [];
  return (
    <Card className="gap-4">
      <CardHeader>
        <CardTitle>Test</CardTitle>
        <CardDescription>Runs the saved version, without the cache, even when disabled.</CardDescription>
      </CardHeader>
      <CardContent className="grid gap-4">
        <form className="flex flex-wrap items-end gap-2" onSubmit={run.run}>
          <Field label="Value" className="min-w-64 flex-1">
            <Input
              placeholder="1.2.3.4"
              required
              autoComplete="off"
              spellCheck={false}
              className="font-mono text-[13px]"
              value={value}
              onChange={(e) => setValue(e.target.value)}
            />
          </Field>
          <TypeSelect types={types} value={type} onChange={setType} />
          {/* A disabled button takes no pointer events, so the reason sits on a wrapper. */}
          <span className="inline-flex" title={blocked || undefined}>
            <SubmitButton busy={run.busy} disabled={Boolean(blocked) || !value.trim()}>
              Test
            </SubmitButton>
          </span>
        </form>
        <Notice notice={run.notice} />
        {test && (
          <>
            <p className="text-sm text-muted-foreground">
              As <Badge variant="outline" className="font-mono">{test.type}</Badge> ·{" "}
              <code className="font-mono text-[13px] text-foreground">{test.value}</code>
            </p>
            {test.request && (
              <div className="grid gap-2">
                <span className="text-sm font-medium">Request</span>
                <pre className="overflow-x-auto rounded-lg border bg-muted/40 p-3 font-mono text-xs leading-relaxed">
                  {test.request.method} {test.request.url}
                </pre>
                {headers.length > 0 && (
                  <div className="overflow-hidden rounded-lg border">
                    <Table>
                      <TableBody>
                        {headers.map(([k, v]) => (
                          <TableRow key={k} className="hover:bg-transparent">
                            <TableCell className="w-48 pl-4 font-mono text-xs font-medium">{k}</TableCell>
                            <TableCell className="max-w-[28rem] truncate pr-4 font-mono text-xs" title={v}>
                              {v}
                            </TableCell>
                          </TableRow>
                        ))}
                      </TableBody>
                    </Table>
                  </div>
                )}
              </div>
            )}
            <ResultCard result={test.result} />
          </>
        )}
      </CardContent>
    </Card>
  );
}
