// Testing an enricher on a value now: the request it sent, its key masked, and the result.

import { useState, type FormEvent } from "react";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { useAction } from "../../shared/action";
import { useApi } from "../../shared/api";
import { CodeBlock, Facts, Field, Notice, SubmitButton } from "../../shared/components";
import type { EnricherTest, EnricherTestBody, EntityType } from "../../shared/types";
import { OutputValue, ResultCard, type TypeChoice, TypeSelect } from "./EnrichersLookup";

/** Runs the saved enricher against one value, without the cache, and shows the request it made. */
export function TestCard({ name, types, blocked }: { name: string; types: EntityType[]; blocked: string }) {
  const client = useApi();
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
                <CodeBlock>
                  {test.request.method} {test.request.url}
                </CodeBlock>
                {headers.length > 0 && <Facts boxed mono rows={headers.map(([k, v]) => [k, <OutputValue text={v} />])} />}
              </div>
            )}
            <ResultCard result={test.result} />
          </>
        )}
      </CardContent>
    </Card>
  );
}
