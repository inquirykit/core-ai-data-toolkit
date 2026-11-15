// One backend of an entry, by hand: which fields show depends on its kind, and empty ones take the provider's.

import { ArrowDown, ArrowUp, X } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { cn } from "@/lib/utils";
import { CodeEditor } from "../../shared/code-editor";
import { Disclosure, Field, KVRows, SelectField, SelectInput, StatusDot, type SelectOption } from "../../shared/components";
import type { BackendProbe, ProviderInfo } from "../../shared/types";
import { BACKEND_KINDS, PROBE, type BackendForm, type BackendKind } from "./servers";

export function BackendEditor(props: {
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
        {form.kind === "hub" && (
          <Field label="Revision" hint="A branch, tag or commit of the Hub repo; empty: main">
            <Input autoComplete="off" spellCheck={false} className="font-mono text-[13px]" value={form.revision} onChange={(e) => props.onChange({ revision: e.target.value })} />
          </Field>
        )}
        {form.kind !== "hub" && (
          <Field label="Upstream model" hint={form.kind === "provider" ? "The server's name for it: Ollama qwen3.5:4b, LM Studio qwen/qwen3.5-4b" : "The model sent to the server; empty: the model ID"}>
            <Input autoComplete="off" spellCheck={false} className="font-mono text-[13px]" value={form.upstream_model} onChange={(e) => props.onChange({ upstream_model: e.target.value })} />
          </Field>
        )}
      </div>

      <Disclosure label="More settings" size="xs" contentClassName="grid gap-3 pt-3">
          <div className="grid gap-3 sm:grid-cols-4">
            <Field label="Memory GB" hint={elsewhere ? "Empty: none (it runs elsewhere)" : "Empty: estimated from the weights"}>
              <Input type="number" min={0} step={0.1} className="font-mono text-[13px]" value={form.memory_gb} onChange={(e) => props.onChange({ memory_gb: e.target.value })} />
            </Field>
            {form.kind === "hub" && (
              <Field label="Max length" hint="Tokens it reads of each text or pair; empty: the model's own (embedding, rerank)">
                <Input type="number" min={1} step={1} className="font-mono text-[13px]" value={form.max_length} onChange={(e) => props.onChange({ max_length: e.target.value })} />
              </Field>
            )}
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
      </Disclosure>
    </div>
  );
}
