// Looking a value up across the enrichers, and the result cards the test shares.

import { useState, type FormEvent } from "react";
import { Search } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Card, CardAction, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { useAction } from "../../shared/action";
import type { ApiClient } from "../../shared/api";
import { DataGrid, type GridColumn } from "../../shared/grid";
import { CodeBlock, Disclosure, Facts, Field, Notice, SelectField, StatusLabel, SubmitButton } from "../../shared/components";
import type { EnrichBody, EnrichResponse, EnrichResult, EntityType } from "../../shared/types";
import { fmtFreshness, fmtOutput, statusLabel } from "./enrichers";

/** The type a lookup names, or "" to detect it. */
export type TypeChoice = EntityType | "";

// ---------------------------------------------------------------- lookup
export function TypeSelect(props: { types: EntityType[]; value: TypeChoice; onChange(v: TypeChoice): void }) {
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
export function LookupCard({ client, types }: { client: ApiClient; types: EntityType[] }) {
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
  const mergedColumns: GridColumn<[string, unknown]>[] = [
    { key: "output", label: "Output", headClassName: "pl-4", className: "pl-4 font-mono text-[13px] font-medium", render: ([name]) => name },
    { key: "value", label: "Value", className: "max-w-[28rem] truncate font-mono text-[13px]", title: ([, v]) => fmtOutput(v), render: ([, v]) => fmtOutput(v) },
    { key: "source", label: "Source", headClassName: "pr-4", className: "pr-4 font-mono text-xs text-muted-foreground", render: ([name]) => reply?.sources[name] ?? "–" },
  ];
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
                  <DataGrid
                    columns={mergedColumns}
                    rows={merged}
                    rowKey={([name]) => name}
                    headerClassName="bg-muted/50"
                    rowProps={() => ({ className: "hover:bg-transparent" })}
                    empty="No outputs"
                  />
                </div>
              </CardContent>
            </Card>
          )}
        </>
      )}
    </div>
  );
}
/** An output's or a header's value in a Facts row: one line of code, cut with its whole text as the tooltip. */
export function OutputValue({ text }: { text: string }) {
  return (
    <span className="block max-w-[28rem] truncate font-mono text-[13px]" title={text}>
      {text}
    </span>
  );
}
/** One enricher's answer: its outputs, the error if any, and the raw reply to unfold. */
export function ResultCard({ result }: { result: EnrichResult }) {
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
          <Facts boxed mono rows={outputs.map(([name, v]) => [name, <OutputValue text={fmtOutput(v)} />])} />
        ) : (
          result.status === "ok" && <p className="text-sm text-muted-foreground">No outputs mapped</p>
        )}
        {result.raw != null && (
          <Disclosure label="Raw reply" size="xs">
            <CodeBlock className="max-h-80">{JSON.stringify(result.raw, null, 2)}</CodeBlock>
          </Disclosure>
        )}
        {result.attribution && <p className="text-xs text-muted-foreground">{result.attribution}</p>}
      </CardContent>
    </Card>
  );
}
