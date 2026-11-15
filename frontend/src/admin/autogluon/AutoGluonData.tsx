// The AutoGluon section's data step: drop a file (uploaded to
// DATA_DIR/datasets under a name the upload takes) or pick one uploaded
// before, and the file's summary from the server: its rows, each column's
// type and spread, and its first rows.

import { useState } from "react";
import { Upload } from "lucide-react";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { useAction } from "../../shared/action";
import { useApi } from "../../shared/api";
import { Disclosure, Notice, SelectField } from "../../shared/components";
import { fmtBytes, fmtInt, fmtPct } from "../../shared/format";
import { DataGrid, type GridColumn } from "../../shared/grid";
import { errorText } from "../../shared/query";
import { cleanFileName, DATA_FILES, type ColumnSummary, type FileSummary } from "./autogluon";

/**
 * Choose a data file: one uploaded before, from the select, or a new one, uploaded at once. A file of the same
 * name is replaced only once the admin says so.
 */
export function DataPicker(props: { label: string; value: string; onChange(path: string): void; optional?: boolean }) {
  const client = useApi();
  const files = client.useQuery("get", "/jobs/data", undefined, { select: (d) => d.files });
  const [pending, setPending] = useState<{ file: File; name: string } | null>(null);

  const upload = useAction(
    async (file: File, name: string) => {
      await client.post("/jobs/data", {
        body: { file: "" },
        bodySerializer: () => {
          const form = new FormData();
          form.append("file", file, name);
          return form;
        },
      });
      setPending(null);
      await files.refetch();
      props.onChange(name);
    },
    { failed: (file) => `Could not upload ${file.name}` },
  );

  function chosen(file: File) {
    const name = cleanFileName(file.name);
    if (files.data?.some((f) => f.path === name)) setPending({ file, name });
    else void upload.run(file, name);
  }

  const options = (files.data ?? []).map((f) => ({ value: f.path, label: `${f.path} · ${fmtBytes(f.bytes)}` }));
  return (
    <div className="grid gap-2">
      <div className="flex flex-wrap items-end gap-2.5">
        <SelectField
          label={props.label}
          className="min-w-64 flex-1"
          value={props.value}
          onChange={props.onChange}
          options={options}
          none={props.optional ? "None" : undefined}
          placeholder={files.data ? (options.length ? "Pick an uploaded file" : "No files yet: upload one") : "Loading…"}
        />
        <label className="grid gap-2">
          <span className="sr-only">Upload a file</span>
          <Input
            type="file"
            accept={DATA_FILES}
            className="w-64 cursor-pointer"
            disabled={upload.busy}
            onChange={(e) => {
              const file = e.target.files?.[0];
              e.target.value = "";
              if (file) chosen(file);
            }}
          />
        </label>
      </div>
      {upload.busy && <p className="text-sm text-muted-foreground">Uploading…</p>}
      {pending && (
        <div className="flex flex-wrap items-center gap-2 rounded-md border border-warning/40 bg-warning/10 px-3 py-2 text-sm">
          <span className="mr-auto">
            A file named <code>{pending.name}</code> is uploaded already.
          </span>
          <Button type="button" size="sm" variant="outline" onClick={() => void upload.run(pending.file, pending.name)}>
            <Upload aria-hidden="true" />
            Replace it
          </Button>
          <Button type="button" size="sm" variant="ghost" onClick={() => setPending(null)}>
            Keep it
          </Button>
        </div>
      )}
      <p className="text-xs text-muted-foreground">CSV (any delimiter), TSV, Parquet or JSON Lines. A file's name keeps to letters, digits, ".", "_" and "-".</p>
      {files.error && <Notice notice={{ text: errorText(files.error), kind: "error" }} />}
    </div>
  );
}

/** A data file's summary, loaded for ``path``; null until it is chosen. */
export function useSummary(path: string) {
  const client = useApi();
  return client.useQuery("get", "/jobs/data/{path}/summary", { params: { path: { path } } }, { enabled: Boolean(path), retry: false, staleTime: 60_000 });
}

const fmtCell = (v: unknown) => (v == null ? <span className="text-muted-foreground">–</span> : typeof v === "object" ? JSON.stringify(v) : String(v));

const COLUMNS: GridColumn<ColumnSummary>[] = [
  { key: "name", label: "Column", className: "font-mono text-[13px] [overflow-wrap:anywhere]", render: (c) => c.name },
  { key: "type", label: "Type", className: "text-muted-foreground", render: (c) => c.type.toLowerCase() },
  { key: "missing", label: "Missing", align: "right", render: (c) => (c.missing ? fmtPct(c.missing) : <span className="text-muted-foreground">–</span>) },
  { key: "distinct", label: "Distinct", align: "right", headTitle: "Counted approximately: within a few percent", render: (c) => fmtInt(c.distinct) },
  { key: "min", label: "Min", className: "max-w-40 truncate", title: (c) => c.min ?? undefined, render: (c) => fmtCell(c.min) },
  { key: "max", label: "Max", className: "max-w-40 truncate", title: (c) => c.max ?? undefined, render: (c) => fmtCell(c.max) },
];

/** The rows, each column's summary, and the first rows a click away. */
export function SummaryView({ summary }: { summary: FileSummary }) {
  const head: GridColumn<Record<string, unknown>>[] = summary.columns.map((c) => ({
    key: c.name,
    label: <span className="font-mono text-xs">{c.name}</span>,
    className: "max-w-48 truncate text-xs",
    render: (row) => fmtCell(row[c.name]),
  }));
  return (
    <div className="grid gap-2">
      <p className="text-sm">
        <strong>{fmtInt(summary.rows)}</strong> rows, <strong>{summary.columns.length}</strong> columns
        <span className="ml-2 text-xs text-muted-foreground">types as DuckDB reads the file: a guide; AutoGluon types them again</span>
      </p>
      <div className="max-h-80 overflow-auto rounded-lg border">
        <DataGrid columns={COLUMNS} rows={summary.columns} rowKey={(c) => c.name} headerClassName="sticky top-0 bg-muted" empty="No columns" />
      </div>
      <Disclosure label={`First ${summary.head.length} rows`}>
        <div className="max-h-80 overflow-auto rounded-lg border">
          <DataGrid columns={head} rows={summary.head} rowKey={(_, i) => i} headerClassName="sticky top-0 bg-muted" empty="No rows" />
        </div>
      </Disclosure>
    </div>
  );
}
