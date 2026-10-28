import { useEffect, useMemo, useRef, useState, type FormEvent, type KeyboardEvent, type ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
import { CircleAlert, CircleCheck, LoaderCircle, RefreshCw } from "lucide-react";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardFooter, CardHeader, CardTitle } from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { RadioGroup, RadioGroupItem } from "@/components/ui/radio-group";
import { Textarea } from "@/components/ui/textarea";
import { cn } from "@/lib/utils";
import { useAction } from "../shared/action";
import { createClient } from "../shared/api";
import { CopyButton, Field, Header, SubmitButton } from "../shared/components";
import { ADMIN_KEY_STORAGE } from "../shared/session";
import { PageMain } from "../shared/Shell";
import { writeSession } from "../shared/storage";
import type { SetupResult } from "../shared/types";
import { newKey, readForm, type SetupFields } from "./form";

type Phase =
  | { kind: "checking" }
  | { kind: "unreachable" }
  | { kind: "already" }
  | { kind: "wizard" }
  | { kind: "done"; result: SetupResult; adminKey: string; clientKey: string | null };

// Setup runs before any key exists, so requests carry none.
const client = createClient(
  () => "",
  () => {},
);

// Keys and model IDs, in the code font.
const MONO = "font-mono text-[12.5px] md:text-[12.5px]";
// Lines up nested content with the text of the choice above it (padding + control + gap).
const INDENT = "ml-10";

export function SetupApp() {
  const status = client.useQuery("get", "/admin/setup/status");
  const [done, setDone] = useState<Extract<Phase, { kind: "done" }> | null>(null);
  const phase: Phase =
    done ??
    (status.isError ? { kind: "unreachable" } : status.data ? { kind: status.data.needed ? "wizard" : "already" } : { kind: "checking" });

  return (
    <>
      <Header page="Setup" />
      <PageMain className="max-w-3xl">
        {phase.kind === "checking" && (
          <p className="flex items-center gap-2 text-sm text-muted-foreground">
            <LoaderCircle className="size-4 animate-spin" aria-hidden="true" />
            Checking this instance…
          </p>
        )}
        {phase.kind === "unreachable" && (
          <Alert variant="destructive">
            <CircleAlert aria-hidden="true" />
            <AlertTitle>The server did not answer.</AlertTitle>
          </Alert>
        )}
        {phase.kind === "already" && <AlreadySetUp />}
        {phase.kind === "wizard" && (
          <Wizard onDone={(result, adminKey, clientKey) => setDone({ kind: "done", result, adminKey, clientKey })} />
        )}
        {phase.kind === "done" && <Done {...phase} />}
      </PageMain>
    </>
  );
}

function AlreadySetUp() {
  return (
    <Card>
      <CardHeader>
        <CardTitle>
          <h2>Already set up</h2>
        </CardTitle>
        <CardDescription>This instance has its keys. Sign in to the dashboard with the admin key.</CardDescription>
      </CardHeader>
      <CardFooter>
        <Button asChild>
          <a href="/admin">Open dashboard</a>
        </Button>
      </CardFooter>
    </Card>
  );
}

function Step({ number, title, hint, children }: { number: number; title: string; hint: ReactNode; children: ReactNode }) {
  return (
    <Card>
      <CardHeader>
        <div className="flex items-center gap-3">
          <span className="grid size-7 flex-none place-items-center rounded-full bg-primary text-xs font-semibold text-primary-foreground tabular-nums ring-4 ring-muted">
            {number}
          </span>
          <CardTitle>
            <h2>{title}</h2>
          </CardTitle>
        </div>
        <CardDescription className="pl-10 leading-relaxed">{hint}</CardDescription>
      </CardHeader>
      <CardContent className="grid gap-3">{children}</CardContent>
    </Card>
  );
}

/** A bordered row with a radio button or checkbox, its title and an optional line under it; marked while on. */
function Choice({ control, title, description }: { control: ReactNode; title: ReactNode; description?: ReactNode }) {
  return (
    <Label className="cursor-pointer items-start gap-3 rounded-md border px-3 py-2.5 leading-snug transition-colors hover:bg-accent/40 has-[[data-state=checked]]:border-ring has-[[data-state=checked]]:bg-accent/50">
      <span className="mt-0.5 flex">{control}</span>
      <span className="grid gap-1">
        <span>{title}</span>
        {description && <span className="text-xs leading-relaxed font-normal text-muted-foreground">{description}</span>}
      </span>
    </Label>
  );
}

function KeyInput(props: { label: string; value: string; onChange(value: string): void; className?: string }) {
  const ref = useRef<HTMLInputElement>(null);
  return (
    <Field label={props.label} className={props.className}>
      <span className="flex gap-2">
        <Input
          ref={ref}
          className={MONO}
          autoComplete="off"
          spellCheck={false}
          value={props.value}
          onChange={(e) => props.onChange(e.target.value)}
        />
        <Button type="button" variant="outline" onClick={() => props.onChange(newKey())}>
          <RefreshCw aria-hidden="true" />
          New
        </Button>
        <CopyButton value={props.value} fallback={ref} />
      </span>
    </Field>
  );
}

interface CatalogGroups {
  groups: [task: string, ids: string[]][];
  /** IDs with their own model file: selected to start with. The generic model's suggestions are not. */
  dedicated: string[];
}

async function loadCatalog(signal: AbortSignal): Promise<CatalogGroups> {
  const [catalog, types] = await Promise.all([client.get("/models/list", { signal }), client.get("/models/types", { signal })]);
  return {
    groups: Object.entries(catalog)
      .filter(([, ids]) => ids.length)
      .sort(([a], [b]) => a.localeCompare(b)),
    dedicated: types.model_types.filter((t) => !t.fallback).flatMap((t) => t.ids),
  };
}

function Wizard({ onDone }: { onDone(result: SetupResult, adminKey: string, clientKey: string | null): void }) {
  const [fields, setFields] = useState<Omit<SetupFields, "selectedModels">>(() => ({
    code: new URLSearchParams(location.hash.slice(1)).get("code") ?? "",
    adminKey: newKey(),
    requireClientKey: true,
    clientKey: newKey(),
    modelsMode: "list",
    corsMode: "list",
    corsOrigins: "",
  }));
  const set = <K extends keyof typeof fields>(name: K, value: (typeof fields)[K]) => setFields((f) => ({ ...f, [name]: value }));

  const [selected, setSelected] = useState<Set<string>>(new Set());
  const catalog = useQuery({ queryKey: ["setup", "catalog"], queryFn: ({ signal }) => loadCatalog(signal) });
  // The models a file of their own serves start out chosen, once the catalog has come.
  useEffect(() => {
    const c = catalog.data;
    if (!c) return;
    const listed = new Set(c.groups.flatMap(([, ids]) => ids));
    setSelected((s) => new Set([...s, ...c.dedicated.filter((id) => listed.has(id))]));
  }, [catalog.data]);
  // Any failure, a 401 included.
  const failed = catalog.isError && !catalog.data;
  const [added, setAdded] = useState<string[]>([]);
  const [extra, setExtra] = useState("");

  const toggle = (id: string, on: boolean) =>
    setSelected((s) => {
      const next = new Set(s);
      if (on) next.add(id);
      else next.delete(id);
      return next;
    });

  const addModel = () => {
    const id = extra.trim();
    if (!id) return;
    setAdded((a) => (a.includes(id) ? a : [...a, id]));
    toggle(id, true);
    setExtra("");
    set("modelsMode", "list");
  };

  const groups = useMemo(() => {
    const base = catalog.data?.groups ?? [];
    return added.length ? [...base, ["added", added] as [string, string[]]] : base;
  }, [catalog.data, added]);

  // Save and Skip share the error shown above them: each starts by clearing the other's.
  const save = useAction(
    async (event: FormEvent) => {
      event.preventDefault();
      skip.clear();
      const body = readForm({ ...fields, selectedModels: [...selected] });
      const result = await client.post("/admin/setup", { body });
      // Signed in to the dashboard with the new admin key.
      writeSession(ADMIN_KEY_STORAGE, body.admin_api_key);
      history.replaceState(null, "", location.pathname); // drop #code
      onDone(result, body.admin_api_key, body.api_key ?? null);
      window.scrollTo(0, 0);
    },
    { notice: true },
  );

  const skip = useAction(
    async () => {
      save.clear();
      const code = fields.code.trim();
      if (!code) throw new Error("Enter the setup code from the server log.");
      if (!confirm("Run without keys? Anyone who can reach this server can use it and load any model. You can set keys later in .env.")) {
        return;
      }
      await client.post("/admin/setup/skip", { body: { code } });
      location.replace("/admin");
    },
    { notice: true },
  );
  const error = save.notice.text || skip.notice.text;

  const onExtraKey = (e: KeyboardEvent) => {
    if (e.key === "Enter") {
      e.preventDefault();
      addModel();
    }
  };

  return (
    <>
      <Card>
        <CardHeader>
          <CardTitle className="text-lg">
            <h2>Set up this instance</h2>
          </CardTitle>
          <CardDescription className="leading-relaxed">
            No API key is configured yet, so anyone who can reach this server can use it and make it download models. Choose who
            may use it; the settings are saved to the server's <code>.env</code> file.
          </CardDescription>
        </CardHeader>
      </Card>

      <form className="stagger grid gap-5" noValidate onSubmit={save.run}>
        <Step
          number={1}
          title="Setup code"
          hint={
            <>
              The server printed it to its log at startup, next to “Not set up yet” (with Docker:{" "}
              <code>docker logs &lt;container&gt;</code>). It proves you run this server.
            </>
          }
        >
          <Input
            className={MONO}
            autoComplete="off"
            spellCheck={false}
            aria-label="Setup code"
            value={fields.code}
            onChange={(e) => set("code", e.target.value)}
          />
        </Step>

        <Step number={2} title="Keys" hint="Generated in your browser. You can paste your own instead (16 or more characters, no spaces).">
          <KeyInput
            label="Admin key: for this dashboard and for loading and unloading models"
            value={fields.adminKey}
            onChange={(v) => set("adminKey", v)}
          />
          <div className="mt-2">
            <Choice
              control={<Checkbox checked={fields.requireClientKey} onCheckedChange={(on) => set("requireClientKey", on === true)} />}
              title="Require a key from API clients"
              description={
                <>
                  Recommended. Apps calling <code>/inference</code> or <code>/v1</code> send it as{" "}
                  <code>Authorization: Bearer &lt;key&gt;</code>.
                </>
              }
            />
          </div>
          {fields.requireClientKey && (
            <KeyInput
              className={INDENT}
              label="Client key: give this to the apps that call the API"
              value={fields.clientKey}
              onChange={(v) => set("clientKey", v)}
            />
          )}
        </Step>

        <Step
          number={3}
          title="Models clients may use"
          hint="Any other model ID is refused, so clients cannot make the server download arbitrary repositories."
        >
          <RadioGroup
            aria-label="Models clients may use"
            value={fields.modelsMode}
            onValueChange={(value) => set("modelsMode", value as typeof fields.modelsMode)}
          >
            <Choice control={<RadioGroupItem value="list" />} title="Only the models selected below" />
            <div className={cn(INDENT, "grid gap-4")}>
              {catalog.isFetching && (
                <p className="flex items-center gap-2 text-sm text-muted-foreground">
                  <LoaderCircle className="size-4 animate-spin" aria-hidden="true" />
                  Loading the catalog…
                </p>
              )}
              {failed && <p className="text-sm text-muted-foreground">Could not load the catalog; add model IDs below.</p>}
              {groups.map(([task, ids]) => (
                <div key={task} className="grid gap-0.5">
                  <h3 className="mb-1 text-xs font-medium tracking-wider text-muted-foreground uppercase">{task}</h3>
                  {ids.map((id) => (
                    <Label
                      key={id}
                      className="-ml-1.5 min-w-0 cursor-pointer rounded-md px-1.5 py-1 leading-snug font-normal transition-colors hover:bg-accent"
                    >
                      <Checkbox checked={selected.has(id)} onCheckedChange={(on) => toggle(id, on === true)} />
                      <span className={cn(MONO, "min-w-0 wrap-anywhere")}>{id}</span>
                    </Label>
                  ))}
                </div>
              ))}
            </div>
            <div className={cn(INDENT, "flex items-end gap-2")}>
              <Field label="Add another model ID" className="flex-1">
                <Input
                  className={MONO}
                  placeholder="org/model"
                  autoComplete="off"
                  spellCheck={false}
                  value={extra}
                  onChange={(e) => setExtra(e.target.value)}
                  onKeyDown={onExtraKey}
                />
              </Field>
              <Button type="button" variant="outline" onClick={addModel}>
                Add
              </Button>
            </div>
            <div className="mt-1">
              <Choice
                control={<RadioGroupItem value="any" />}
                title="Any Hugging Face model"
                description="Not recommended outside development."
              />
            </div>
          </RadioGroup>
        </Step>

        <Step
          number={4}
          title="Browser access"
          hint="Which websites may call the API from a browser (CORS). Servers and this dashboard are not affected."
        >
          <RadioGroup
            aria-label="Browser access"
            value={fields.corsMode}
            onValueChange={(value) => set("corsMode", value as typeof fields.corsMode)}
          >
            <Choice
              control={<RadioGroupItem value="list" />}
              title="Only these origins"
              description={
                <>
                  One per line, e.g. <code>http://localhost:3000</code>. Leave empty if only servers call the API.
                </>
              }
            />
            <Textarea
              className={cn(INDENT, "w-auto")}
              rows={3}
              spellCheck={false}
              aria-label="Origins"
              placeholder="http://localhost:3000"
              value={fields.corsOrigins}
              onChange={(e) => setFields((f) => ({ ...f, corsOrigins: e.target.value, corsMode: "list" }))}
            />
            <Choice
              control={<RadioGroupItem value="any" />}
              title={
                <>
                  Any website (<code>*</code>)
                </>
              }
              description="Any page a user opens could call the API with the key it has."
            />
          </RadioGroup>
        </Step>

        {error && (
          <Alert variant="destructive">
            <CircleAlert aria-hidden="true" />
            <AlertDescription>{error}</AlertDescription>
          </Alert>
        )}
        <div className="flex flex-wrap items-center justify-between gap-2.5">
          <SubmitButton busy={save.busy}>Save settings</SubmitButton>
          <Button type="button" variant="outline" onClick={skip.run}>
            Skip: run without keys (local development)
          </Button>
        </div>
      </form>
    </>
  );
}

function SavedKey({ label, value }: { label: string; value: string }) {
  const ref = useRef<HTMLInputElement>(null);
  return (
    <Field label={label}>
      <span className="flex gap-2">
        <Input ref={ref} value={value} readOnly className={cn(MONO, "flex-1")} />
        <CopyButton value={value} fallback={ref} />
      </span>
    </Field>
  );
}

function Done({ result, adminKey, clientKey }: { result: SetupResult; adminKey: string; clientKey: string | null }) {
  return (
    <Card>
      <CardHeader>
        <span className="mb-2 grid size-10 place-items-center rounded-full bg-good/12 text-good-text" aria-hidden="true">
          <CircleCheck className="size-5" />
        </span>
        <CardTitle className="text-lg">
          <h2>Setup saved</h2>
        </CardTitle>
        <CardDescription className="leading-relaxed">
          Written to <code>{result.env_file}</code>. Copy the keys now: this page will not show them again.
        </CardDescription>
      </CardHeader>
      <CardContent className="grid gap-5">
        <div className="grid gap-4">
          <SavedKey label="Admin key" value={adminKey} />
          {clientKey ? (
            <SavedKey label="Client key" value={clientKey} />
          ) : (
            <p className="text-sm text-muted-foreground">API clients need no key.</p>
          )}
        </div>
        {result.notes.length > 0 && (
          <div className="text-sm">
            <strong className="font-medium">One more step</strong>
            <ol className="mt-2 grid list-decimal gap-1 pl-5 leading-relaxed">
              {result.notes.map((n) => (
                <li key={n}>{n}</li>
              ))}
            </ol>
          </div>
        )}
      </CardContent>
      <CardFooter>
        <Button type="button" onClick={() => location.assign("/admin")}>
          Open dashboard
        </Button>
      </CardFooter>
    </Card>
  );
}
