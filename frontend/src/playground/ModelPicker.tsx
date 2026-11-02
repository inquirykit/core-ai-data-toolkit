import { useState } from "react";
import { Library } from "lucide-react";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import type { ApiClient } from "../shared/api";
import { SelectInput, StatusDot, type SelectOption } from "../shared/components";
import { ModelBrowser } from "../shared/model-browser/ModelBrowser";
import type { PickerEntry } from "./categories";

/** An entry's key in the list: its ID, with its task when the list mixes tasks (the same model can be under two). */
const keyOf = (e: PickerEntry) => (e.task ? `${e.task}\n${e.id}` : e.id);

/**
 * Choosing a model: a list of the catalog's models for the tasks (loaded
 * first, with their state), and Browse for the full catalog, its filters and
 * any other ID the task takes. A model chosen from outside the list stays in it.
 */
export function ModelPicker(props: {
  client: ApiClient;
  /** In the order to list them (see modelsOf, choicesOf). */
  entries: PickerEntry[];
  /** The tasks Browse offers. */
  tasks: string[];
  value: PickerEntry | null;
  /** The entry chosen; from Browse, with the task it was chosen for. */
  onChange(entry: PickerEntry): void;
  /** What the Browse dialog's button says, e.g. "Use in column A". */
  pickLabel: string;
  label: string;
  disabled?: boolean;
  className?: string;
}) {
  const { entries, value } = props;
  const [browsing, setBrowsing] = useState(false);
  const shown = !value || entries.some((e) => keyOf(e) === keyOf(value)) ? entries : [value, ...entries];
  return (
    <div className={cn("flex min-w-0 items-center gap-2", props.className)}>
      <SelectInput
        size="sm"
        className="min-w-0 flex-1 bg-background dark:bg-background"
        contentClassName="max-h-80 max-w-[min(560px,calc(100vw-2rem))]"
        aria-label={props.label}
        placeholder={entries.length ? "Choose a model" : "No models listed: browse for one"}
        disabled={props.disabled}
        value={value ? keyOf(value) : ""}
        onChange={(key) => {
          const entry = shown.find((e) => keyOf(e) === key);
          if (entry) props.onChange(entry);
        }}
        options={shown.map(entryOption)}
      />
      <Button
        type="button"
        variant="outline"
        size="sm"
        title="Choose from the model catalog, or enter another ID"
        disabled={props.disabled}
        onClick={() => setBrowsing(true)}
      >
        <Library aria-hidden="true" />
        Browse
      </Button>
      <ModelBrowser
        client={props.client}
        open={browsing}
        onClose={() => setBrowsing(false)}
        tasks={props.tasks}
        pick={{ label: props.pickLabel, onPick: (m) => props.onChange({ id: m.id, task: m.task }) }}
      />
    </div>
  );
}

/**
 * A model's option: a dot for a loaded (or loading) one, a gap the same size otherwise, so the IDs line up; a
 * variant's models end with its badge.
 */
function entryOption(entry: PickerEntry): SelectOption {
  const { id, status, badge } = entry;
  return {
    value: keyOf(entry),
    className: "min-w-0",
    title: status === "loaded" ? "Loaded" : status === "loading" ? "Loading" : undefined,
    label: (
      <>
        {status === "loaded" || status === "loading" ? (
          <StatusDot color={status === "loaded" ? "var(--good)" : "var(--warning)"} live={status === "loading"} />
        ) : (
          <span className="size-2 shrink-0" aria-hidden="true" />
        )}
        <span className="min-w-0 truncate">{id}</span>
        {badge && <span className="shrink-0 rounded-sm border px-1.5 text-[11px] leading-4 text-muted-foreground">{badge}</span>}
      </>
    ),
  };
}
