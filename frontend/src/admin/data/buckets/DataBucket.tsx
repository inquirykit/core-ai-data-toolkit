// A bucket's files, on its page in place of the rows a table shows (TableBrowser.tsx):
// newest first, a page at a time (its rows, GET /db/{ws}/{bucket}), with uploads of
// several files at once, a download each (with the page's key, so as a blob saved
// under its name: a plain link would carry no key) and a delete each, confirmed. Only
// the file routes write a bucket's rows, so there is no row editor or schema here.
// Above them, its remote (RemoteBar): a choice of one for the admin, how many files
// are copied there and wait to be, how its last copy went, and Copy now while it is
// behind; each file says when it was copied.
import { useEffect, useRef, useState, type ChangeEvent } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { CloudUpload, Download, Upload } from "lucide-react";
import { Button } from "@/components/ui/button";
import { useAction } from "../../../shared/action";
import { useApi } from "../../../shared/api";
import { downloadResponse } from "../../../shared/browser";
import { ConfirmButton, Notice, Segmented, SelectInput, StatusDot, SubmitButton } from "../../../shared/components";
import { failure } from "../../../shared/errors";
import { fmtAgo, fmtBytes, fmtInt } from "../../../shared/format";
import { DataGrid, type GridColumn } from "../../../shared/grid";
import { errorText, poll, useReloadOn } from "../../../shared/query";
import { copiedCell, copyBehind, copying, copyStatus, shortHash, uploadReport, type BucketFile, type BucketRemote } from "./buckets";
import { listQuery } from "../data";
import { PAGE_SIZES, Pager, useCursorPages } from "../../layout";

export function BucketFiles(props: { workspace: string; bucket: string; readOnly: boolean; onChange(): void }) {
  const client = useApi();
  const { workspace, bucket } = props;
  const path = { workspace, bucket };
  const [limit, setLimit] = useState(25);
  const pages = useCursorPages(limit);
  const picker = useRef<HTMLInputElement>(null);
  // While an upload runs: how many of how many files are in.
  const [progress, setProgress] = useState<[done: number, total: number] | null>(null);

  const files = useQuery({
    // Under the table's key, which its page invalidates when anything changes.
    queryKey: ["db", "rows", workspace, bucket, "files", limit, pages.cursor],
    queryFn: ({ signal }) =>
      Promise.all([
        client.get("/db/{workspace}/{table}", {
          params: { path: { workspace, table: bucket }, query: listQuery({ filters: [], sort: { field: "created", desc: true }, limit, cursor: pages.cursor }) },
          signal,
        }),
        client.get("/db/{workspace}/{table}/_count", { params: { path: { workspace, table: bucket } }, signal }),
      ]),
  });
  const [page, count] = files.data ? [files.data[0], files.data[1].count] : [null, null];
  // A bucket's rows are its files: the list is typed as any table's.
  const rows = (page?.rows ?? []) as BucketFile[];
  // Its remote, which the Copied column reads.
  const [remote, setRemote] = useState<string | null>(null);

  const upload = useAction(
    async (chosen: File[]) => {
      const failed: [string, string][] = [];
      setProgress([0, chosen.length]);
      try {
        for (const [i, file] of chosen.entries()) {
          try {
            await client.post("/db/{workspace}/{bucket}/_files", {
              params: { path, query: { name: file.name } },
              // The schema says a binary string; the File goes as it is, its bytes streamed by the browser.
              body: file as unknown as string,
              bodySerializer: (body: unknown) => body as BodyInit,
              headers: { "Content-Type": file.type || "application/octet-stream" },
            });
          } catch (error) {
            const why = failure(error);
            // Signed out: nothing more can go in.
            if (why === null) throw error;
            failed.push([file.name, why]);
          }
          setProgress([i + 1, chosen.length]);
        }
      } finally {
        setProgress(null);
        props.onChange();
      }
      const report = uploadReport(chosen.length, failed);
      if (report.failed) throw new Error(`${report.done} Not uploaded: ${report.failed}`);
      return report.done;
    },
    { success: (done) => done, failed: "Some files were not uploaded" },
  );

  const download = useAction(
    async (file: BucketFile) =>
      downloadResponse(await client.raw("get", "/db/{workspace}/{bucket}/_files/{file_id}", { params: { path: { ...path, file_id: file.id } } }), file.name),
    { failed: (file) => `Could not download ${file.name}` },
  );

  const remove = useAction(
    async (file: BucketFile) => {
      await client.delete("/db/{workspace}/{bucket}/_files/{file_id}", { params: { path: { ...path, file_id: file.id } } });
      props.onChange();
    },
    { success: (_, file) => `Deleted ${file.name}.`, failed: (file) => `Could not delete ${file.name}` },
  );

  const pick = (event: ChangeEvent<HTMLInputElement>) => {
    const chosen = [...(event.target.files ?? [])];
    // The same files can be picked again.
    event.target.value = "";
    if (chosen.length) void upload.run(chosen);
  };

  const columns: GridColumn<BucketFile>[] = [
    { key: "name", label: "Name", className: "max-w-80 truncate font-medium", title: (f) => f.name, render: (f) => f.name },
    { key: "size", label: "Size", align: "right", title: (f) => `${f.size.toLocaleString()} bytes`, render: (f) => fmtBytes(f.size) },
    { key: "type", label: "Type", className: "max-w-48 truncate font-mono text-xs text-muted-foreground", title: (f) => f.content_type, render: (f) => f.content_type },
    { key: "created", label: "Uploaded", className: "text-muted-foreground", title: (f) => new Date(f.created * 1000).toLocaleString(), render: (f) => fmtAgo(f.created) },
    { key: "sha256", label: "SHA-256", className: "font-mono text-xs text-muted-foreground", title: (f) => f.sha256, render: (f) => shortHash(f.sha256) },
    {
      key: "copied",
      label: "Copied",
      headTitle: "When it reached the bucket's remote",
      className: "text-muted-foreground",
      title: (f) => (f.copied ? new Date(f.copied * 1000).toLocaleString() : undefined),
      render: (f) => copiedCell(f, remote, fmtAgo),
    },
    {
      key: "actions",
      label: <span className="sr-only">Actions</span>,
      headClassName: "w-0",
      className: "text-right whitespace-nowrap",
      render: (f) => (
        <span className="inline-flex gap-1.5">
          <Button
            type="button"
            variant="outline"
            size="icon-sm"
            aria-label={`Download ${f.name}`}
            title="Download"
            disabled={download.running?.[0].id === f.id}
            onClick={() => void download.run(f)}
          >
            <Download aria-hidden="true" />
          </Button>
          {!props.readOnly && (
            <ConfirmButton
              iconOnly
              label={`Delete ${f.name}`}
              title={`Delete ${f.name}?`}
              body="The file is deleted from the bucket and from the disk. This cannot be undone."
              action="Delete"
              busy={remove.running?.[0].id === f.id}
              onConfirm={() => void remove.run(f)}
            />
          )}
        </span>
      ),
    },
  ];

  return (
    <>
      <RemoteBar workspace={workspace} bucket={bucket} readOnly={props.readOnly} onRemote={setRemote} onChange={props.onChange} />
      <div className="flex flex-wrap items-center gap-2">
        {!props.readOnly && (
          <>
            <input ref={picker} type="file" multiple className="hidden" aria-label="Files to upload" onChange={pick} />
            <SubmitButton type="button" size="sm" busy={upload.busy} icon={<Upload aria-hidden="true" />} onClick={() => picker.current?.click()}>
              {progress ? `Uploading ${Math.min(progress[0] + 1, progress[1])} of ${progress[1]}…` : "Upload files"}
            </SubmitButton>
          </>
        )}
        <Segmented
          label="Files per page"
          size="sm"
          className="ml-auto"
          options={PAGE_SIZES}
          value={String(limit)}
          onChange={(v) => setLimit(Number(v))}
        />
      </div>
      {files.error && <Notice notice={{ text: errorText(files.error), kind: "error" }} />}
      <div className="overflow-hidden rounded-lg border">
        <DataGrid
          columns={columns}
          rows={rows}
          rowKey={(f) => f.id}
          headerClassName="bg-muted/50"
          loading={files.isFetching && !page}
          empty="No files yet: upload some, or have a client post them to …/_files"
          aria-label="Files"
        />
      </div>
      <Pager
        start={pages.index * limit}
        shown={rows.length}
        count={count}
        hasPrevious={pages.hasPrevious}
        hasNext={Boolean(page?.next)}
        loading={files.isFetching}
        onPrevious={pages.previous}
        onNext={() => pages.next(page?.next ?? undefined)}
      />
    </>
  );
}

/**
 * The bucket's remote: for the admin a choice of none or one of the remotes (each file is copied to a new one), how
 * many files are copied there and wait to be, and how its latest copy went, asked again while one is under way; while
 * the copy is behind (one gave up, say), Copy now names the same remote again, which starts it at once. When the
 * copied count changes, the files are loaded again for their Copied cells.
 */
function RemoteBar(props: {
  workspace: string;
  bucket: string;
  readOnly: boolean;
  onRemote(remote: string | null): void;
  onChange(): void;
}) {
  const client = useApi();
  const { workspace, bucket } = props;
  const path = { workspace, bucket };
  const queries = useQueryClient();
  const state = useQuery({
    // Under the table's key, which its page invalidates when anything changes (an upload queues a copy).
    queryKey: ["db", "rows", workspace, bucket, "remote"],
    queryFn: ({ signal }) => client.get("/db/{workspace}/{bucket}/_remote", { params: { path }, signal }),
    ...poll((d?: BucketRemote) => (copying(d) ? 2000 : 0)),
  });
  const remotes = client.useQuery("get", "/remotes", {}, { enabled: !props.readOnly, select: (d) => d.remotes.map((r) => r.name) });
  const current = state.data ?? null;
  const { onRemote } = props;
  const named = current?.remote ?? null;
  useEffect(() => onRemote(named), [named, onRemote]);
  useReloadOn(current?.copied ?? null, () => queries.invalidateQueries({ queryKey: ["db", "rows", workspace, bucket, "files"] }));

  const choose = useAction(
    async (remote: string) => {
      const next = await client.put("/db/{workspace}/{bucket}/_remote", { params: { path }, body: { remote: remote || null } });
      queries.setQueryData(["db", "rows", workspace, bucket, "remote"], next);
      // The remotes list the buckets that name them.
      void queries.invalidateQueries({ queryKey: ["get", "/remotes"] });
      props.onChange();
      return next;
    },
    {
      success: (_next, remote) =>
        remote ? `Each file of ${bucket} is copied to ${remote}.` : `${bucket} has no remote now: what was copied stays at the remote.`,
      failed: "Could not change the remote",
    },
  );

  const again = useAction(
    async (remote: string) => {
      const next = await client.put("/db/{workspace}/{bucket}/_remote", { params: { path }, body: { remote } });
      queries.setQueryData(["db", "rows", workspace, bucket, "remote"], next);
      return next;
    },
    { success: (_next, remote) => `Copying ${bucket} to ${remote}.`, failed: "Could not start the copy" },
  );

  const status = current ? copyStatus(current) : null;
  const names = remotes.data ?? [];
  return (
    <div className="grid gap-1.5 rounded-lg border px-3 py-2">
      <div className="flex flex-wrap items-center gap-2 text-sm">
        <CloudUpload className="size-4 text-muted-foreground" aria-hidden="true" />
        <span className="font-medium">Remote</span>
        {props.readOnly ? (
          <span className="font-mono text-[13px]">{current?.remote ?? "none"}</span>
        ) : (
          <SelectInput
            aria-label="Remote"
            size="sm"
            className="h-8 min-w-36 font-mono text-[13px]"
            none="none"
            value={current?.remote ?? ""}
            disabled={!current || choose.busy}
            options={[...new Set([...names, ...(current?.remote ? [current.remote] : [])])].map((name) => ({ value: name, label: name }))}
            onChange={(remote) => void choose.run(remote)}
          />
        )}
        {current?.remote && (
          <span className="text-muted-foreground tabular-nums">
            {fmtInt(current.copied)} of {fmtInt(current.count)} copied · {fmtInt(current.waiting)} waiting
          </span>
        )}
        {!props.readOnly && current?.remote && copyBehind(current) && (
          <Button type="button" variant="outline" size="sm" className="h-7" disabled={again.busy} onClick={() => void again.run(current.remote!)}>
            Copy now
          </Button>
        )}
        <a className="ml-auto text-xs text-muted-foreground underline-offset-2 hover:text-foreground hover:underline" href="#/remotes">
          {names.length || props.readOnly ? "Remotes" : "Make a remote"}
        </a>
      </div>
      {state.error && <Notice notice={{ text: errorText(state.error), kind: "error" }} />}
      {status && (
        <p className="flex items-start gap-2 text-xs text-muted-foreground [overflow-wrap:anywhere]">
          <StatusDot color={status.color} className="mt-1" />
          {status.label}
          {current?.task?.finished && current.task.status === "succeeded" && ` · last copy ${fmtAgo(current.task.finished)}`}
        </p>
      )}
    </div>
  );
}
