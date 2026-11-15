// A client key's grants as a form: the tables it may read, write and delete
// in, the models it may run, and whether it manages models. The new key form
// and a key's Access dialog (KeysSection.tsx) both use it; its logic is in
// keys.ts.
import { Plus, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { ACTIONS, toggled, type GrantRow, type GrantsForm } from "./keys";

export function GrantsEditor({ form, onChange }: { form: GrantsForm; onChange(form: GrantsForm): void }) {
  const set = (patch: Partial<GrantsForm>) => onChange({ ...form, ...patch });
  const setRow = (i: number, row: GrantRow) => set({ rows: form.rows.map((r, j) => (j === i ? row : r)) });
  return (
    <div className="grid gap-4">
      <div className="grid gap-2">
        <Check checked={form.limitTables} onChange={(limitTables) => set({ limitTables })}>
          Only these tables
        </Check>
        {form.limitTables && (
          <>
            {form.rows.map((row, i) => (
              <div key={i} className="flex flex-wrap items-center gap-3">
                <Input
                  value={row.entry}
                  aria-label="Workspace and table"
                  placeholder="docs.notes"
                  className="w-56 font-mono text-xs"
                  onChange={(e) => setRow(i, { ...row, entry: e.target.value })}
                />
                {ACTIONS.map((a) => (
                  <Check key={a.value} checked={row.actions.includes(a.value)} onChange={(on) => setRow(i, { ...row, actions: toggled(row.actions, a.value, on) })}>
                    {a.label}
                  </Check>
                ))}
                <Button type="button" variant="ghost" size="icon-sm" aria-label="Remove" onClick={() => set({ rows: form.rows.filter((_, j) => j !== i) })}>
                  <X aria-hidden="true" />
                </Button>
              </div>
            ))}
            <Button type="button" variant="outline" size="sm" className="w-fit" onClick={() => set({ rows: [...form.rows, { entry: "", actions: ["read"] }] })}>
              <Plus aria-hidden="true" /> Add
            </Button>
            <p className="text-xs text-muted-foreground">
              A table (<code>docs.notes</code>), a workspace (<code>docs.*</code>) or every one (<code>*.*</code>); entries add up, and a table no
              entry names is shut. Listing a workspace, transactions, edges and the graph, views and <code>_expand</code> take the workspace's
              entry. <code>docs._evidence</code> is the workspace's evidence.
            </p>
          </>
        )}
      </div>
      <div className="grid gap-2">
        <Check checked={form.limitModels} onChange={(limitModels) => set({ limitModels })}>
          Only these models
        </Check>
        {form.limitModels && (
          <Input
            value={form.models}
            aria-label="Models"
            placeholder="BAAI/bge-small-en-v1.5, … (none: no model at all)"
            onChange={(e) => set({ models: e.target.value })}
          />
        )}
      </div>
      <Check checked={form.manageModels} onChange={(manageModels) => set({ manageModels })}>
        May load, unload and pin models
      </Check>
    </div>
  );
}

function Check(props: { checked: boolean; onChange(on: boolean): void; children: string }) {
  return (
    <label className="flex w-fit items-center gap-2 text-sm">
      <Checkbox checked={props.checked} onCheckedChange={(v) => props.onChange(v === true)} />
      {props.children}
    </label>
  );
}
