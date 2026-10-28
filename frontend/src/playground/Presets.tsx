import { useEffect, useState, type FormEvent } from "react";
import { Save } from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { useAction } from "../shared/action";
import type { ApiClient } from "../shared/api";
import { ConfirmButton, Field, Notice, SelectField, SubmitButton } from "../shared/components";
import type { Limits } from "../shared/types";
import { settingsProblem, type Settings } from "./chat";
import type { Preset, PresetBody } from "./saved";

/**
 * The settings card's presets: a list of the saved ones to fill the system
 * prompt, temperature and max tokens from, with saving the current settings
 * as one and deleting the chosen one. ``selected`` is the preset the settings
 * came from; the chat clears it once they are edited.
 */
export function PresetPicker(props: {
  client: ApiClient;
  limits: Limits;
  settings: Settings;
  selected: string | null;
  /** A preset was chosen: fill the settings from it. */
  onPick(preset: Preset): void;
  /** The settings were saved as this preset (new or updated). */
  onSaved(preset: Preset): void;
  onDeleted(): void;
  /** Something failed outside a dialog (the database is off, say). */
  onError(error: unknown): void;
  disabled?: boolean;
}) {
  const { client, selected, onError } = props;
  const list = client.useQuery("get", "/playground/presets", undefined, { select: (d) => d.presets });
  const presets = list.data ?? [];
  // Each failed load is told once (errorUpdatedAt changes with each).
  useEffect(() => {
    if (list.error) onError(list.error);
    // eslint-disable-next-line react-hooks/exhaustive-deps -- once per failed load, not per render
  }, [list.errorUpdatedAt]);
  const [saving, setSaving] = useState(false);
  const current = presets.find((p) => p.id === selected);

  const remove = async () => {
    if (!current) return;
    try {
      await client.delete("/playground/presets/{preset_id}", { params: { path: { preset_id: current.id } } });
      toast.success(`Deleted the preset "${current.name}".`);
      props.onDeleted();
      await list.refetch();
    } catch (e) {
      onError(e);
    }
  };

  return (
    <div className="flex items-end gap-2">
      <SelectField
        label="Preset"
        triggerClassName="w-[180px]"
        contentClassName="max-w-[min(420px,calc(100vw-2rem))]"
        aria-label="Preset"
        placeholder={presets.length ? "Choose a preset" : "No presets yet"}
        disabled={props.disabled}
        value={selected ?? ""}
        onChange={(id) => {
          const preset = presets.find((p) => p.id === id);
          if (preset) props.onPick(preset);
        }}
        options={presets.map((p) => ({ value: p.id, title: p.system || undefined, label: <span className="min-w-0 truncate">{p.name}</span> }))}
      />
      <Button type="button" variant="outline" disabled={props.disabled} onClick={() => setSaving(true)}>
        <Save aria-hidden="true" />
        Save preset…
      </Button>
      {current && (
        <ConfirmButton
          iconOnly
          size="default"
          label={`Delete the preset ${current.name}`}
          action="Delete"
          title={`Delete the preset "${current.name}"?`}
          body="The settings stay as they are; only the saved preset goes."
          disabled={props.disabled}
          onConfirm={remove}
        />
      )}
      {saving && (
        <SavePresetDialog
          client={client}
          limits={props.limits}
          settings={props.settings}
          current={current}
          onClose={() => setSaving(false)}
          onSaved={async (preset) => {
            props.onSaved(preset);
            await list.refetch();
          }}
        />
      )}
    </div>
  );
}

/** Names the settings and saves them: over ``current`` when the settings came from a preset, else as a new one. */
function SavePresetDialog(props: {
  client: ApiClient;
  limits: Limits;
  settings: Settings;
  current: Preset | undefined;
  onClose(): void;
  onSaved(preset: Preset): void;
}) {
  const { current } = props;
  const [name, setName] = useState(current?.name ?? "");

  const submit = useAction(
    async (event: FormEvent) => {
      event.preventDefault();
      const problem = settingsProblem(props.settings, props.limits);
      if (problem) throw new Error(problem);
      const body: PresetBody = {
        name: name.trim(),
        system: props.settings.system,
        temperature: Number(props.settings.temperature),
        max_tokens: Number(props.settings.maxTokens),
      };
      const saved = current
        ? await props.client.put("/playground/presets/{preset_id}", { params: { path: { preset_id: current.id } }, body })
        : await props.client.post("/playground/presets", { body });
      props.onSaved(saved);
      props.onClose();
      return saved;
    },
    { notice: true, success: (saved) => `Saved the preset "${saved.name}".` },
  );

  return (
    <Dialog open onOpenChange={(open) => !open && props.onClose()}>
      <DialogContent>
        <form className="grid gap-4" onSubmit={submit.run}>
          <DialogHeader>
            <DialogTitle>{current ? "Update preset" : "Save preset"}</DialogTitle>
            <DialogDescription>
              {current
                ? `The system prompt, temperature and max new tokens replace those of "${current.name}".`
                : "The system prompt, temperature and max new tokens, to fill in again later."}
            </DialogDescription>
          </DialogHeader>
          <Field label="Name">
            <Input autoFocus required maxLength={120} placeholder="Terse assistant" value={name} onChange={(e) => setName(e.target.value)} />
          </Field>
          <Notice notice={submit.notice} />
          <DialogFooter>
            <Button type="button" variant="outline" onClick={props.onClose}>
              Cancel
            </Button>
            <SubmitButton busy={submit.busy} disabled={!name.trim()}>
              {current ? "Update" : "Save"}
            </SubmitButton>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
