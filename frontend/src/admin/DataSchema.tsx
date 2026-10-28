// A table's schema in the Data section, the "Schema" view of a table
// (TableBrowser.tsx): its declared fields to edit, beside the fields its rows hold.
import { useState } from "react";
import { Plus, X } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Checkbox } from "@/components/ui/checkbox";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { Skeleton } from "@/components/ui/skeleton";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { useAction } from "../shared/action";
import type { ApiClient } from "../shared/api";
import { Notice, SelectInput, SubmitButton } from "../shared/components";
import { fmtInt } from "../shared/format";
import { errorText } from "../shared/query";
import {
  describeField,
  FIELD_TYPES,
  INDEXABLE,
  inferField,
  type FieldInfo,
  type FieldType,
  type ObservedField,
  type SchemaInfo,
} from "./data";

type DraftField = FieldInfo & { key: string; isNew: boolean };

/** What a field's settings allow: only some types index, a list cannot be unique, unique is indexed. */
function normalized(f: DraftField): DraftField {
  const indexable = INDEXABLE.includes(f.type);
  const unique = f.unique && indexable && !f.list;
  return { ...f, unique, indexed: indexable && (f.indexed || unique) };
}

const plain = ({ key: _key, isNew: _isNew, ...f }: DraftField): FieldInfo => f;
const schemaKey = (strict: boolean, fields: FieldInfo[]) =>
  JSON.stringify({ strict, fields: [...fields].sort((a, b) => a.name.localeCompare(b.name)) });

/**
 * The table's schema as an editable list of fields, beside what its newest
 * rows actually hold. Saving replaces it (PUT _schema); the server checks
 * the rows there first and refuses, changing nothing, if any do not fit.
 */
export function SchemaEditor(props: { client: ApiClient; namespace: string; table: string; readOnly?: boolean; onSaved(): void }) {
  const query = props.client.useQuery("get", "/db/{namespace}/{table}/_schema", {
    params: { path: { namespace: props.namespace, table: props.table }, query: { _sample: 1000 } },
  });
  const info = query.data ?? null;
  const error = errorText(query.error);
  const [draft, setDraft] = useState<DraftField[]>([]);
  const [strict, setStrict] = useState(false);
  // The schema the draft started from. Each schema loaded (the first, after saving) starts the draft over, in
  // the same render as it shows; so does Reset, which forgets it (a schema loaded again unchanged is the same one).
  const [drafted, setDrafted] = useState<SchemaInfo | null>(null);
  const reload = () => {
    setDrafted(null);
    return query.refetch();
  };

  const save = useAction(
    async () => {
      await props.client.put("/db/{namespace}/{table}/_schema", {
        params: { path: { namespace: props.namespace, table: props.table } },
        body: { strict, fields: draft.map(plain) },
      });
      void reload();
      props.onSaved();
    },
    { notice: true, success: () => `Saved the schema of ${props.table}.` },
  );

  if (info && info !== drafted) {
    setDrafted(info);
    setDraft(info.fields.map((f) => ({ ...f, key: f.name, isNew: false })));
    setStrict(info.strict);
  }

  if (!info) return <Skeleton className="h-64 rounded-lg" />;

  const observed = new Map(info.observed.fields.map((o) => [o.name, o]));
  const undeclared = info.observed.fields.filter((o) => !draft.some((d) => d.name === o.name));
  const dirty = schemaKey(strict, draft.map(plain)) !== schemaKey(info.strict, info.fields);
  const update = (key: string, change: Partial<FieldInfo>) =>
    setDraft((d) => d.map((f) => (f.key === key ? normalized({ ...f, ...change }) : f)));
  const declare = (o: ObservedField) => setDraft((d) => [...d, { ...inferField(o), key: `new:${o.name}`, isNew: true }]);
  const addField = () =>
    setDraft((d) => [
      ...d,
      { key: `new:${Date.now()}`, isNew: true, name: "", type: "string", list: false, required: false, indexed: false, unique: false },
    ]);

  const seen = (name: string) => {
    const o = observed.get(name);
    if (!o) return <span className="text-muted-foreground">–</span>;
    return (
      <span className="flex flex-wrap items-center gap-1" title={Object.entries(o.types).map(([k, n]) => `${k}: ${n}`).join(", ")}>
        <span className="tabular-nums">{Math.round((100 * o.present) / Math.max(1, info.observed.sampled))}%</span>
        {Object.keys(o.types)
          .slice(0, 2)
          .map((k) => (
            <Badge key={k} variant="secondary" className="font-mono font-normal">
              {k}
            </Badge>
          ))}
      </span>
    );
  };

  const check = (label: string, f: DraftField, key: "list" | "required" | "indexed" | "unique", disabled = false) => (
    <TableCell className="text-center">
      <Checkbox
        aria-label={`${f.name || "new field"}: ${label}`}
        checked={f[key]}
        disabled={disabled}
        onCheckedChange={(v) => update(f.key, { [key]: v === true })}
      />
    </TableCell>
  );

  return (
    <div className="grid gap-4">
      <p className="text-sm text-muted-foreground">
        Declared fields are checked on every write: rows hold that type there, or leave the field out unless it is required.
        Without a schema, rows are free-form JSON. “In rows” shows the newest {fmtInt(info.observed.sampled)} rows.
      </p>
      <div className="overflow-x-auto rounded-lg border">
        <Table>
          <TableHeader className="bg-muted/50">
            <TableRow className="hover:bg-transparent">
              <TableHead className="pl-4">Field</TableHead>
              <TableHead>Type</TableHead>
              <TableHead className="text-center">List</TableHead>
              <TableHead className="text-center">Required</TableHead>
              <TableHead className="text-center">Indexed</TableHead>
              <TableHead className="text-center">Unique</TableHead>
              <TableHead>In rows</TableHead>
              <TableHead className="pr-4" />
            </TableRow>
          </TableHeader>
          <TableBody>
            <TableRow className="text-muted-foreground hover:bg-transparent">
              <TableCell className="pl-4 font-mono text-[13px] font-medium text-foreground">id</TableCell>
              <TableCell>string</TableCell>
              <TableCell className="text-center">–</TableCell>
              <TableCell className="text-center">
                <Checkbox aria-label="id: required" checked disabled />
              </TableCell>
              <TableCell className="text-center">
                <Checkbox aria-label="id: indexed" checked disabled />
              </TableCell>
              <TableCell className="text-center">
                <Checkbox aria-label="id: unique" checked disabled />
              </TableCell>
              <TableCell className="text-xs">built in</TableCell>
              <TableCell />
            </TableRow>
            {draft.map((f) => {
              const indexable = INDEXABLE.includes(f.type);
              return (
                <TableRow key={f.key} className="hover:bg-transparent">
                  <TableCell className="pl-4">
                    {f.isNew ? (
                      <Input
                        aria-label="Field name"
                        placeholder="name"
                        autoFocus={!f.name}
                        pattern="[A-Za-z][A-Za-z0-9_]*"
                        className="h-8 w-40 font-mono text-[13px]"
                        value={f.name}
                        onChange={(e) => update(f.key, { name: e.target.value.trim() })}
                      />
                    ) : (
                      <span className="font-mono text-[13px] font-medium">{f.name}</span>
                    )}
                  </TableCell>
                  <TableCell>
                    <SelectInput
                      size="sm"
                      className="w-28"
                      aria-label={`${f.name || "new field"}: type`}
                      value={f.type}
                      onChange={(v) => update(f.key, { type: v as FieldType })}
                      options={FIELD_TYPES.map((t) => ({ value: t, label: t }))}
                    />
                  </TableCell>
                  {check("list", f, "list")}
                  {check("required", f, "required")}
                  {check("indexed", f, "indexed", !indexable || f.unique)}
                  {check("unique", f, "unique", !indexable || f.list)}
                  <TableCell className="text-xs">{seen(f.name)}</TableCell>
                  <TableCell className="pr-4 text-right">
                    <Button
                      type="button"
                      variant="ghost"
                      size="icon"
                      className="size-8 text-muted-foreground"
                      aria-label={`Remove ${f.name || "new field"}`}
                      onClick={() => setDraft((d) => d.filter((x) => x.key !== f.key))}
                    >
                      <X />
                    </Button>
                  </TableCell>
                </TableRow>
              );
            })}
          </TableBody>
        </Table>
      </div>
      <div>
        <Button type="button" variant="outline" size="sm" onClick={addField}>
          <Plus aria-hidden="true" />
          Add field
        </Button>
      </div>

      {undeclared.length > 0 && (
        <div className="grid gap-2 rounded-lg border border-dashed p-3">
          <div className="flex items-center justify-between gap-2">
            <span className="text-sm font-medium">In the rows, not declared</span>
            <Button type="button" variant="ghost" size="sm" className="h-7 text-xs" onClick={() => undeclared.forEach(declare)}>
              Declare all
            </Button>
          </div>
          <div className="flex flex-wrap gap-1.5">
            {undeclared.map((o) => (
              <button
                key={o.name}
                type="button"
                title={`Declare ${o.name} as ${describeField(inferField(o))}`}
                className="inline-flex items-center gap-1.5 rounded-md border px-2 py-1 font-mono text-xs transition-colors hover:bg-muted"
                onClick={() => declare(o)}
              >
                <Plus className="size-3 text-muted-foreground" aria-hidden="true" />
                {o.name}
                <span className="text-muted-foreground">
                  {Object.keys(o.types)[0]} · {Math.round((100 * o.present) / Math.max(1, info.observed.sampled))}%
                </span>
              </button>
            ))}
          </div>
        </div>
      )}

      <Label className="flex items-start gap-3 font-normal">
        <Switch checked={strict} onCheckedChange={setStrict} />
        <span className="grid gap-1">
          <span className="font-medium">Strict</span>
          <span className="text-xs text-muted-foreground">Refuse rows with fields not declared here.</span>
        </span>
      </Label>

      <div className="flex flex-wrap items-center justify-between gap-3 border-t pt-4">
        {/* A save's outcome, else a failed load (on Reset or after saving): what was loaded before stays. */}
        <Notice notice={save.notice.text ? save.notice : { text: error, kind: "error" }} className="min-w-0 flex-1" />
        <div className="ml-auto flex gap-2">
          <Button type="button" variant="outline" disabled={!dirty || save.busy} onClick={reload}>
            Reset
          </Button>
          <SubmitButton type="button" busy={save.busy} disabled={props.readOnly || !dirty || draft.some((f) => !f.name)} onClick={save.run}>
            Save schema
          </SubmitButton>
        </div>
      </div>
      <p className="-mt-2 text-xs text-muted-foreground">
        Saving checks the rows already there. If any break the new rules, nothing changes and the first few are named.
      </p>
    </div>
  );
}
