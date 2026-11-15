// A dataset's Label tab: its label choices, then its examples without a
// reference one at a time, the least confident suggestion first (a run's
// answer for the example, else what the traced model said). A key per choice
// labels it, Enter takes the suggestion, S passes over it; with no choices the
// reference is typed, starting from the suggestion.

import { useEffect, useState, type FormEvent } from "react";
import { Check, RotateCcw, SkipForward } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { useAction } from "../../shared/action";
import { useApi } from "../../shared/api";
import { errorText } from "../../shared/query";
import { Field, Notice, Pane, SelectField, SubmitButton } from "../../shared/components";
import { fmtInt } from "../../shared/format";
import type { Dataset, Run } from "../../shared/types";
import { fmtShare, inputPreview, labelForKey, matchingLabel, parseLabels, parseReference, suggestionOf } from "./evals";

export function LabelTab(props: { dataset: Dataset; runs: Run[]; onChange(): Promise<unknown> }) {
  const client = useApi();
  const { dataset } = props;
  const [runId, setRunId] = useState("");
  // How many the labeler passed over: they stay at the front of the queue, so the next is after them.
  const [skip, setSkip] = useState(0);
  const query = client.useQuery("get", "/admin/datasets/{dataset_id}/to-label", {
    params: { path: { dataset_id: dataset.id }, query: { run_id: runId || undefined, skip, limit: 1 } },
  });
  const example = query.data?.examples[0] ?? null;
  const left = query.data?.left ?? null;
  const suggestion = suggestionOf(example?.suggestion);
  const labels = dataset.labels;
  const suggested = matchingLabel(suggestion, labels);
  const error = errorText(query.error);
  // Until the queue's answer for the last change is in, the example shown is the one before it: no key or button acts.
  const ready = Boolean(example) && !query.isPlaceholderData && !query.isFetching;

  const label = useAction(
    async (reference: unknown) => {
      if (!example) return;
      await client.patch("/admin/datasets/{dataset_id}/examples/{example_id}", {
        params: { path: { dataset_id: dataset.id, example_id: example.id } },
        body: { reference },
      });
      await query.refetch();
    },
    { failed: "Could not label" },
  );

  // Keys: 1-9 pick a choice, Enter the suggestion, S passes over; not while typing or choosing, nor held down.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const target = e.target as HTMLElement | null;
      if (!ready || label.busy || e.repeat || e.metaKey || e.ctrlKey || e.altKey) return;
      if (target?.closest("input, textarea, select, [contenteditable], [role=dialog], [role=listbox], [role=combobox]")) return;
      // Enter on a focused button is that button's.
      if (e.key === "Enter" && target?.closest("button, a")) return;
      const picked = labelForKey(e.key, labels) ?? (e.key === "Enter" ? suggested : null);
      if (picked) {
        e.preventDefault();
        label.run(picked);
      } else if (e.key === "s" || e.key === "S") {
        e.preventDefault();
        setSkip((n) => n + 1);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [ready, labels, suggested, label]);

  const finished = props.runs.filter((r) => r.status === "succeeded");
  return (
    <div className="grid gap-5 p-4">
      <div className="grid gap-4 lg:grid-cols-2">
        <LabelChoices dataset={dataset} onChange={props.onChange} />
        <SelectField
          label="Suggestions from"
          hint="A finished run's answers, or what the model a trace came from answered"
          value={runId}
          onChange={(v) => (setRunId(v), setSkip(0))}
          none="The traced model's answers"
          options={finished.map((r) => ({ value: r.id, label: `${r.name} (${r.model_id})` }))}
        />
      </div>

      <div className="rounded-lg border">
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1 border-b px-4 py-2 text-sm">
          <span className="font-medium">{left === null ? "Loading…" : left ? `${fmtInt(left)} to label` : "Every example has a reference"}</span>
          {skip > 0 && <span className="text-muted-foreground">{fmtInt(skip)} passed over</span>}
          {skip > 0 && (
            <Button type="button" variant="ghost" size="sm" onClick={() => setSkip(0)}>
              <RotateCcw aria-hidden="true" />
              Back to those
            </Button>
          )}
          <span className="ml-auto text-xs text-muted-foreground">Least confident first · keys: 1–9 a label, Enter the suggestion, S pass over</span>
        </div>
        {error ? (
          <p className="p-4 text-sm text-critical-text">{error}</p>
        ) : query.isPending ? (
          <p className="p-4 text-sm text-muted-foreground">Loading…</p>
        ) : !example ? (
          <p className="p-4 text-sm text-muted-foreground">{left ? "You passed over the rest." : "Nothing to label: add examples without a reference, or take one away in Examples."}</p>
        ) : (
          <div className="grid gap-4 p-4">
            <p className="max-h-72 overflow-y-auto whitespace-pre-wrap text-base [overflow-wrap:anywhere]">{inputPreview(example.input, 20_000)}</p>
            {Object.keys(example.input ?? {}).some((k) => k !== "text") && <Pane title="Input" value={example.input} />}
            {suggestion && (
              <div className="text-sm text-muted-foreground">
                Suggested: <span className="font-medium text-foreground">{suggestion.label ?? "an answer"}</span>
                {suggestion.confidence != null && ` (${fmtShare(suggestion.confidence)} sure)`}
              </div>
            )}
            {labels.length ? (
              <div className="flex flex-wrap gap-2">
                {labels.map((l, i) => (
                  <SubmitButton
                    key={l}
                    type="button"
                    variant={l === suggested ? "default" : "outline"}
                    busy={label.running?.[0] === l}
                    disabled={!ready || label.busy}
                    onClick={() => label.run(l)}
                  >
                    {i < 9 && <Badge variant="secondary" className="px-1 py-0 font-mono text-[11px]">{i + 1}</Badge>}
                    {l}
                  </SubmitButton>
                ))}
                <Button type="button" variant="ghost" onClick={() => setSkip((n) => n + 1)} disabled={!ready || label.busy}>
                  <SkipForward aria-hidden="true" />
                  Pass over
                </Button>
              </div>
            ) : (
              <TypedReference key={example.id} start={suggestion?.text ?? ""} busy={!ready || label.busy} onSave={label.run} onSkip={() => setSkip((n) => n + 1)} />
            )}
          </div>
        )}
      </div>
    </div>
  );
}

/** The dataset's label choices, typed as a list. */
function LabelChoices(props: { dataset: Dataset; onChange(): Promise<unknown> }) {
  const client = useApi();
  const [text, setText] = useState(props.dataset.labels.join(", "));
  const save = useAction(
    async (event: FormEvent) => {
      event.preventDefault();
      await client.patch("/admin/datasets/{dataset_id}", { params: { path: { dataset_id: props.dataset.id } }, body: { labels: parseLabels(text) } });
      await props.onChange();
    },
    { notice: true, success: () => ({ text: "Saved", kind: "ok" }) },
  );
  return (
    <form className="grid content-start gap-2" onSubmit={save.run}>
      <Field label="Labels" hint="Comma-separated; none: type each reference">
        <div className="flex gap-2">
          <Input value={text} onChange={(e) => setText(e.target.value)} placeholder="positive, negative, neutral" />
          <SubmitButton size="sm" variant="outline" busy={save.busy} disabled={parseLabels(text).join() === props.dataset.labels.join()}>
            Save
          </SubmitButton>
        </div>
      </Field>
      <Notice notice={save.notice} />
    </form>
  );
}

/** A reference typed for a dataset without label choices, starting from the suggestion. */
function TypedReference(props: { start: string; busy: boolean; onSave(reference: unknown): unknown; onSkip(): void }) {
  const [text, setText] = useState(props.start);
  // JSON when it parses as a list or an object (a ranking's relevant documents), else the text, as in Examples.
  const save = () => text.trim() && !props.busy && props.onSave(parseReference(text));
  return (
    <div className="grid gap-2">
      <Textarea
        aria-label="Reference"
        value={text}
        onChange={(e) => setText(e.target.value)}
        onKeyDown={(e) => (e.metaKey || e.ctrlKey) && e.key === "Enter" && (e.preventDefault(), save())}
        rows={4}
      />
      <div className="flex gap-2">
        <SubmitButton type="button" busy={props.busy} disabled={!text.trim()} icon={<Check aria-hidden="true" />} onClick={save}>
          Save reference
        </SubmitButton>
        <Button type="button" variant="ghost" onClick={props.onSkip} disabled={props.busy}>
          <SkipForward aria-hidden="true" />
          Pass over
        </Button>
        <span className="self-center text-xs text-muted-foreground">⌘/Ctrl+Enter saves</span>
      </div>
    </div>
  );
}
