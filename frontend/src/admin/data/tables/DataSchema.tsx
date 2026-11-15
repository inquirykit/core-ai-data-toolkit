// A table's schema in the Data section, the "Schema" view of a table
// (TableBrowser.tsx): its declared fields to edit, beside the fields its rows hold.
// A vector field's options sit in a panel of their own under the fields: set
// while the field is new, fixed once it is saved (the server refuses a change).
// So do a table ref's (a ref field, as the API calls it), asked as sentences: how many
// rows of which table a row names, and what deleting one of them does.
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
import { useAction } from "../../../shared/action";
import { useApi } from "../../../shared/api";
import { Notice, SelectInput, SubmitButton } from "../../../shared/components";
import { fmtInt } from "../../../shared/format";
import { errorText } from "../../../shared/query";
import {
  describeField,
  fieldAsSent,
  FIELD_TYPES,
  fieldTypeLabel,
  INDEXABLE,
  inferField,
  MAX_DIMENSIONS,
  ON_DELETE,
  onDeleteText,
  PROMPTS,
  SIMILARITIES,
  VECTOR_OPTIONS,
  type FieldInfo,
  type FieldType,
  type ObservedField,
  type SchemaInfo,
} from "../data";

type DraftField = FieldInfo & { key: string; isNew: boolean };

/** Whether a field of this type can be full-text: a plain string. */
const wordy = (f: Pick<FieldInfo, "type" | "list">) => f.type === "string" && !f.list;

/**
 * What a field's settings allow: only some types index, a list cannot be unique, unique is indexed, only a plain
 * string is full-text, and a field is indexed or full-text: the one just turned on (``change``) wins. A vector
 * field is none of those, has its similarity and quantization (COSINE and NONE unless chosen), and is not required
 * while it is embedded; any other field has no vector options. A ref field is indexed unless a list, and its
 * on_delete is one the server takes: no cascade for a list, no unset for a required ref (restrict instead).
 */
function normalized(f: DraftField, change: Partial<FieldInfo> = {}): DraftField {
  if (f.type === "vector") {
    const { to: _to, on_delete: _onDelete, ...rest } = f;
    return {
      ...rest,
      list: false,
      indexed: false,
      unique: false,
      fulltext: false,
      required: f.required && !f.model,
      similarity: f.similarity ?? "COSINE",
      quantization: f.quantization ?? "NONE",
    };
  }
  const rest = { ...f };
  for (const option of VECTOR_OPTIONS) delete rest[option];
  if (f.type === "ref") {
    const allowed = onDeleteAllowed(f);
    const on_delete = f.on_delete && allowed.includes(f.on_delete) ? f.on_delete : "restrict";
    return { ...rest, indexed: !f.list, unique: f.unique && !f.list, fulltext: false, on_delete };
  }
  delete rest.to;
  delete rest.on_delete;
  const indexable = INDEXABLE.includes(f.type);
  const unique = f.unique && indexable && !f.list && !change.fulltext;
  const indexed = indexable && (f.indexed || unique) && !change.fulltext;
  return { ...rest, unique, indexed, fulltext: f.fulltext && wordy(f) && !indexed };
}

/** The on_delete choices the server takes for a ref field: a list is not cascaded, a required ref not unset. */
const onDeleteAllowed = (f: Pick<FieldInfo, "list" | "required">) =>
  ON_DELETE.filter((v) => !(f.list && v === "cascade") && !(f.required && !f.list && v === "unset"));

/** Whether the draft's field is a vector field saved as one, among the schema's ``fields``: it keeps its options. */
const savedVector = (fields: FieldInfo[] | undefined, f: DraftField) =>
  Boolean(fields?.some((o) => o.name === f.name && o.type === "vector" && !f.isNew));

/** A field as PUT _schema takes it (fieldAsSent), without the draft's keys. */
function plain({ key: _key, isNew: _isNew, ...f }: DraftField, saved: boolean): FieldInfo {
  return fieldAsSent(f, saved);
}
const schemaKey = (strict: boolean, fields: FieldInfo[]) =>
  JSON.stringify({ strict, fields: [...fields].sort((a, b) => a.name.localeCompare(b.name)) });

/**
 * The table's schema as an editable list of fields, beside what its newest
 * rows actually hold. Saving replaces it (PUT _schema); the server checks
 * the rows there first and refuses, changing nothing, if any do not fit.
 */
export function SchemaEditor(props: { workspace: string; table: string; readOnly?: boolean; onSaved(): void }) {
  const client = useApi();
  const query = client.useQuery("get", "/db/{workspace}/{table}/_schema", {
    params: { path: { workspace: props.workspace, table: props.table }, query: { _sample: 1000 } },
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
      await client.put("/db/{workspace}/{table}/_schema", {
        params: { path: { workspace: props.workspace, table: props.table } },
        body: { strict, fields: draft.map((f) => plain(f, savedVector(info?.fields, f))) },
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

  // The embedding models the service lists, offered for a vector field's model (any other ID can be typed).
  const embedders = client.useQuery("get", "/models/catalog", undefined, {
    select: (d) => d.tasks.find((t) => t.task === "embedding")?.models.map((m) => m.id) ?? [],
    enabled: draft.some((f) => f.type === "vector"),
  });
  // The workspace's tables a ref field can name: another table, not a bucket (its rows are files), nor a kept table.
  const targets = client.useQuery(
    "get",
    "/db/{workspace}",
    { params: { path: { workspace: props.workspace } } },
    {
      select: (d) => d.tables.filter((t) => t.name !== props.table && !t.bucket && !t.kept).map((t) => t.name),
      enabled: draft.some((f) => f.type === "ref"),
    },
  );

  if (!info) return <Skeleton className="h-64 rounded-lg" />;

  const observed = new Map(info.observed.fields.map((o) => [o.name, o]));
  const undeclared = info.observed.fields.filter((o) => !draft.some((d) => d.name === o.name));
  const dirty = schemaKey(strict, draft.map((f) => plain(f, savedVector(info.fields, f)))) !== schemaKey(info.strict, info.fields);
  const update = (key: string, change: Partial<FieldInfo>) =>
    setDraft((d) => d.map((f) => (f.key === key ? normalized({ ...f, ...change }, change) : f)));
  const declare = (o: ObservedField) => setDraft((d) => [...d, { ...inferField(o), key: `new:${o.name}`, isNew: true }]);
  const addField = () =>
    setDraft((d) => [
      ...d,
      { key: `new:${Date.now()}`, isNew: true, name: "", type: "string", list: false, required: false, indexed: false, unique: false, fulltext: false },
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

  // A vector field saved as one keeps its options: only removing it and adding it again changes them.
  const saved = (f: DraftField) => savedVector(info.fields, f);
  const strings = draft.filter((f) => f.name && wordy(f)).map((f) => f.name);

  const check = (label: string, f: DraftField, key: "list" | "required" | "indexed" | "unique" | "fulltext", disabled = false) => (
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
        Without a schema, rows are free-form JSON. “In rows” shows the newest {fmtInt(info.observed.sampled)} rows. A table may
        have one vector field, which its rows are searched by nearness with.
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
              <TableHead className="text-center" title="Its words are searchable: a string field, not also indexed">
                Full text
              </TableHead>
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
              <TableCell className="text-center">–</TableCell>
              <TableCell className="text-xs">built in</TableCell>
              <TableCell />
            </TableRow>
            {draft.map((f) => {
              const indexable = INDEXABLE.includes(f.type);
              const vector = f.type === "vector";
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
                      disabled={saved(f)}
                      value={f.type}
                      onChange={(v) => update(f.key, { type: v as FieldType })}
                      options={FIELD_TYPES.map((t) => ({ value: t, label: fieldTypeLabel(t) }))}
                    />
                  </TableCell>
                  {/* A table ref's is asked in its panel: one row or several. */}
                  {check("list", f, "list", vector || f.type === "ref")}
                  {check("required", f, "required", vector && Boolean(f.model))}
                  {check("indexed", f, "indexed", !indexable || f.unique || f.type === "ref")}
                  {check("unique", f, "unique", !indexable || f.list)}
                  {check("full text", f, "fulltext", !wordy(f))}
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
      {draft
        .filter((f) => f.type === "vector")
        .map((f) => (
          <VectorOptions
            key={f.key}
            field={f}
            fixed={saved(f)}
            strings={strings}
            models={embedders.data ?? []}
            onChange={(change) => update(f.key, change)}
          />
        ))}
      {draft
        .filter((f) => f.type === "ref")
        .map((f) => (
          <RefOptions key={f.key} field={f} table={props.table} tables={targets.data ?? []} onChange={(change) => update(f.key, change)} />
        ))}
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
          <SubmitButton type="button" busy={save.busy} disabled={props.readOnly || !dirty || draft.some((f) => !f.name || (f.type === "ref" && !f.to))} onClick={save.run}>
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

/**
 * A table ref's options, under the fields, asked as sentences: whether a row names one row or several, of which other
 * table of the workspace, and what deleting one of those rows does to the rows naming it. All may change: saving
 * checks the ids already there.
 */
function RefOptions(props: { field: FieldInfo; table: string; tables: string[]; onChange(change: Partial<FieldInfo>): void }) {
  const { field: f, table } = props;
  const name = f.name || "new field";
  const label = (what: string) => `${name}: ${what}`;
  const to = f.to ?? "that table";
  return (
    <section aria-label={label("table ref")} className="grid gap-2.5 rounded-lg border border-dashed bg-muted/30 p-3 text-sm">
      <div className="font-medium">
        Table ref <span className="font-mono text-[13px]">{name}</span>
      </div>
      <div className="flex flex-wrap items-center gap-2">
        Each row of <span className="font-mono text-[13px]">{table}</span> names
        <SelectInput
          size="sm"
          className="w-36"
          aria-label={label("how many")}
          value={f.list ? "several" : "one"}
          onChange={(v) => props.onChange({ list: v === "several" })}
          options={[
            { value: "one", label: "one row" },
            { value: "several", label: "several rows" },
          ]}
        />
        of
        <SelectInput
          size="sm"
          className="w-48"
          aria-label={label("to")}
          value={f.to ?? ""}
          placeholder="a table"
          onChange={(v) => props.onChange({ to: v || null })}
          options={props.tables.map((t) => ({ value: t, label: t }))}
        />
      </div>
      <div className="flex flex-wrap items-center gap-2">
        When a row of <span className="font-mono text-[13px]">{to}</span> is deleted:
        <SelectInput
          size="sm"
          className="w-80"
          aria-label={label("on delete")}
          value={f.on_delete ?? "restrict"}
          onChange={(v) => props.onChange({ on_delete: v as FieldInfo["on_delete"] })}
          options={onDeleteAllowed(f).map((v) => ({ value: v, label: onDeleteText(v, table, name, f.list), title: v }))}
        />
      </div>
      <p className="text-xs text-muted-foreground">
        A row naming a row of {to} that is not there is refused. The rows of {table} show the name of each row they name, a link to it,
        and the rows of {to} how many rows of {table} name each. In the API the field holds {f.list ? "a list of ids" : "an id"}: a read
        follows it with <span className="font-mono">_expand={f.name || "field"}</span>, and back from {to} with{" "}
        <span className="font-mono">
          _expand={table}:{f.name || "field"}
        </span>
        .
      </p>
    </section>
  );
}

/**
 * A vector field's options, under the fields: its dimensions and similarity, and the embedding model
 * that embeds the string field ``from`` on every write (no model: rows carry their vectors). ``fixed`` once saved:
 * the server refuses a change, so the field is removed and added again instead, which drops its vectors.
 */
function VectorOptions(props: { field: FieldInfo; fixed: boolean; strings: string[]; models: string[]; onChange(change: Partial<FieldInfo>): void }) {
  const { field: f, fixed } = props;
  const label = (what: string) => `${f.name || "new field"}: ${what}`;
  const listId = `embedders-${f.name || "new"}`;
  return (
    <section aria-label={label("options")} className="grid gap-2 rounded-lg border border-dashed bg-muted/30 p-3">
      <div className="text-sm font-medium">
        Vector field <span className="font-mono text-[13px]">{f.name || "new field"}</span>
      </div>
      <div className="flex flex-wrap items-end gap-3">
        <Label className="grid gap-1 text-xs font-normal text-muted-foreground">
          Model
          <Input
            aria-label={label("model")}
            list={listId}
            placeholder="none: rows carry their vectors"
            title="The embedding model that makes each row's vector from its text; empty: rows carry their vectors"
            disabled={fixed}
            autoComplete="off"
            className="h-8 w-80 max-w-full font-mono text-[13px]"
            value={f.model ?? ""}
            onChange={(e) => props.onChange({ model: e.target.value.trim() || null, from: e.target.value.trim() ? (f.from ?? props.strings[0] ?? null) : null })}
          />
          <datalist id={listId}>
            {props.models.map((m) => (
              <option key={m} value={m} />
            ))}
          </datalist>
        </Label>
        {f.model && (
          <Label className="grid gap-1 text-xs font-normal text-muted-foreground">
            Embedded from
            <SelectInput
              size="sm"
              className="w-40"
              aria-label={label("from")}
              disabled={fixed}
              value={f.from ?? ""}
              placeholder="a string field"
              onChange={(v) => props.onChange({ from: v || null })}
              options={props.strings.map((name) => ({ value: name, label: name }))}
            />
          </Label>
        )}
        <Label className="grid gap-1 text-xs font-normal text-muted-foreground">
          Dimensions
          <Input
            aria-label={label("dimensions")}
            type="number"
            min={1}
            max={MAX_DIMENSIONS}
            placeholder={f.model ? "the model's" : "e.g. 384"}
            disabled={fixed}
            className="h-8 w-28 tabular-nums"
            value={f.dimensions ?? ""}
            onChange={(e) => props.onChange({ dimensions: e.target.value ? Number(e.target.value) : null })}
          />
        </Label>
        <Label className="grid gap-1 text-xs font-normal text-muted-foreground">
          Similarity
          <SelectInput
            size="sm"
            className="w-36"
            aria-label={label("similarity")}
            disabled={fixed}
            value={f.similarity ?? "COSINE"}
            onChange={(v) => props.onChange({ similarity: v as FieldInfo["similarity"] })}
            options={SIMILARITIES.map((v) => ({ value: v, label: v }))}
          />
        </Label>
      </div>
      {f.model && (
        <div className="flex flex-wrap items-end gap-3">
          {PROMPTS.map((option) => (
            <Label key={option} className="grid gap-1 text-xs font-normal text-muted-foreground">
              {option === "query_prompt" ? "Query prompt" : "Document prompt"}
              <Input
                aria-label={label(option === "query_prompt" ? "query prompt" : "document prompt")}
                placeholder={fixed ? "none" : "the model's own"}
                title={
                  option === "query_prompt"
                    ? "Put before a search's text; left empty when made, the model's own prompt named query. It may change later."
                    : "Put before each row's text; left empty when made, the model's own prompt named document."
                }
                disabled={fixed && option === "document_prompt"}
                autoComplete="off"
                className="h-8 w-80 max-w-full font-mono text-[13px]"
                value={f[option] ?? ""}
                onChange={(e) => props.onChange({ [option]: e.target.value })}
              />
            </Label>
          ))}
        </div>
      )}
      <p className="text-xs text-muted-foreground">
        {fixed
          ? "Set when the field was made, but the query prompt. To change them, remove the field, save, and add it again: its vectors go, and rows get new ones as they are written again."
          : f.model
            ? `Each write that sets ${f.from ?? "the text"} embeds it with the model; rows do not send the vector. Saving embeds a probe text, which finds the dimensions when left empty.`
            : "Each row carries its vector: a list of exactly that many numbers, or null."}
      </p>
    </section>
  );
}
