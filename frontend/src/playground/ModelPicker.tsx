import { useState } from "react";
import { Library } from "lucide-react";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import type { ApiClient } from "../shared/api";
import { SelectInput, StatusDot, type SelectOption } from "../shared/components";
import type { CatalogModel } from "../shared/model-browser/catalog";
import { ModelBrowser } from "../shared/model-browser/ModelBrowser";

/**
 * Choosing a model: a list of the catalog's models for the tasks (loaded
 * first, with their state), and Browse for the full catalog, its filters and
 * any other ID the task takes. A model chosen from outside the list stays in it.
 */
export function ModelPicker(props: {
  client: ApiClient;
  /** In the order to list them (see modelsOf). */
  models: CatalogModel[];
  /** The tasks Browse offers. */
  tasks: string[];
  value: string;
  onChange(id: string): void;
  /** What the Browse dialog's button says, e.g. "Use in column A". */
  pickLabel: string;
  label: string;
  disabled?: boolean;
  className?: string;
}) {
  const { models, value } = props;
  const [browsing, setBrowsing] = useState(false);
  const listed = !value || models.some((m) => m.id === value);
  return (
    <div className={cn("flex min-w-0 items-center gap-2", props.className)}>
      <SelectInput
        size="sm"
        className="min-w-0 flex-1 bg-background dark:bg-background"
        contentClassName="max-h-80 max-w-[min(560px,calc(100vw-2rem))]"
        aria-label={props.label}
        placeholder={models.length ? "Choose a model" : "No models listed: browse for one"}
        disabled={props.disabled}
        value={value}
        onChange={props.onChange}
        options={[...(listed ? [] : [modelOption(value)]), ...models.map((m) => modelOption(m.id, m.status))]}
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
        pick={{ label: props.pickLabel, onPick: (m) => props.onChange(m.id) }}
      />
    </div>
  );
}

/** A model's option: a dot for a loaded (or loading) one, a gap the same size otherwise, so the IDs line up. */
function modelOption(id: string, status?: CatalogModel["status"]): SelectOption {
  return {
    value: id,
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
      </>
    ),
  };
}
