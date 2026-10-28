import { useEffect, useState, type FormEvent, type KeyboardEvent } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, Bookmark, Check, Database, Pencil, Trash2, X } from "lucide-react";
import { toast } from "sonner";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { Badge } from "@/components/ui/badge";
import { Button, buttonVariants } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Textarea } from "@/components/ui/textarea";
import { useAction } from "../shared/action";
import { AuthError, type ApiClient } from "../shared/api";
import { Field, Notice, SelectField, SubmitButton, type NoticeState } from "../shared/components";
import { fmtAgo } from "../shared/format";
import { EmptyState } from "../shared/model-browser/parts";
import { errorText } from "../shared/query";
import { letter } from "./chat";
import { lastReply, type Conversation, type ConversationSummary, type DatasetSummary, type ExampleBody } from "./saved";

/**
 * The saved conversations: a list to load, rename or delete them from, and
 * a step for turning one column's chat into an eval dataset example.
 */
export function SavedDialog(props: {
  client: ApiClient;
  /** For the admin-only calls: its 401 means "needs the admin key", not a sign-in. */
  quiet: ApiClient;
  open: boolean;
  onClose(): void;
  /** The conversation the chat came from, if any. */
  loadedId: string | null;
  /** Whether loading another would lose turns. */
  unsaved(): boolean;
  onLoad(conversation: Conversation): void;
  onRenamed(conversation: Conversation): void;
  onDeleted(id: string): void;
}) {
  const { client, open } = props;
  // Read again each time it opens: Save may have added one.
  const conversations = client.useQuery("get", "/playground/conversations", undefined, { enabled: open, select: (d) => d.conversations });
  const queries = useQueryClient();
  /** Show a change the server has made without asking for the list again. */
  const change = (rows: (list: ConversationSummary[]) => ConversationSummary[]) =>
    queries.setQueryData<{ conversations: ConversationSummary[] }>(
      client.queryOptions("get", "/playground/conversations").queryKey,
      (d) => d && { ...d, conversations: rows(d.conversations) },
    );
  const listError = errorText(conversations.error);
  // null until the first load; empty after a failure.
  const list = conversations.isPending ? null : listError ? [] : (conversations.data ?? []);
  const [renaming, setRenaming] = useState<{ id: string; title: string } | null>(null);
  const [confirmLoad, setConfirmLoad] = useState<ConversationSummary | null>(null);
  const [deleting, setDeleting] = useState<ConversationSummary | null>(null);
  const [adding, setAdding] = useState<Conversation | null>(null);
  /** Runs ``work`` with the list's buttons disabled, reporting a failure under it. */
  const act = useAction((work: () => Promise<void>) => work(), { notice: true });
  const busy = act.busy;
  // An action's failure, else the list's.
  const shown: NoticeState = act.notice.text ? act.notice : { text: listError, kind: "error" };

  // Each opening starts from the list.
  useEffect(() => {
    if (!open) return;
    act.clear();
    setAdding(null);
    setRenaming(null);
    // eslint-disable-next-line react-hooks/exhaustive-deps -- act.clear is the same function every render
  }, [client, open]);

  const load = (row: ConversationSummary) =>
    act.run(async () => {
      props.onLoad(await client.get("/playground/conversations/{conversation_id}", { params: { path: { conversation_id: row.id } } }));
      props.onClose();
    });

  const rename = (id: string, title: string) =>
    act.run(async () => {
      // PUT replaces the whole conversation, so it is read first.
      const path = { conversation_id: id };
      const { settings, columns } = await client.get("/playground/conversations/{conversation_id}", { params: { path } });
      const saved = await client.put("/playground/conversations/{conversation_id}", { params: { path }, body: { title, settings, columns } });
      change((rows) => rows.map((r) => (r.id === id ? { ...r, title: saved.title, updated: saved.updated } : r)));
      setRenaming(null);
      props.onRenamed(saved);
    });

  const remove = (row: ConversationSummary) =>
    act.run(async () => {
      await client.delete("/playground/conversations/{conversation_id}", { params: { path: { conversation_id: row.id } } });
      change((rows) => rows.filter((r) => r.id !== row.id));
      props.onDeleted(row.id);
      toast.success(`Deleted "${row.title}".`);
    });

  const startAdding = (row: ConversationSummary) =>
    act.run(async () => {
      setAdding(await client.get("/playground/conversations/{conversation_id}", { params: { path: { conversation_id: row.id } } }));
    });

  return (
    <Dialog open={open} onOpenChange={(next) => !next && props.onClose()}>
      <DialogContent className="sm:max-w-3xl">
        {adding ? (
          <AddExample quiet={props.quiet} conversation={adding} onBack={() => setAdding(null)} />
        ) : (
          <>
            <DialogHeader>
              <DialogTitle>Saved chats</DialogTitle>
              <DialogDescription>Load one to carry on from where it stopped, or turn one into an eval example.</DialogDescription>
            </DialogHeader>
            {list === null ? (
              <p className="py-8 text-center text-sm text-muted-foreground">Loading…</p>
            ) : list.length ? (
              <div className="max-h-[60vh] overflow-y-auto rounded-lg border">
                <Table>
                  <TableHeader className="bg-muted/50">
                    <TableRow className="hover:bg-transparent">
                      <TableHead className="pl-4">Title</TableHead>
                      <TableHead>Models</TableHead>
                      <TableHead className="text-right">Turns</TableHead>
                      <TableHead>Updated</TableHead>
                      <TableHead className="pr-3">
                        <span className="sr-only">Actions</span>
                      </TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {list.map((row) => (
                      <TableRow key={row.id}>
                        <TableCell className="max-w-[20rem] pl-4 font-medium">
                          {renaming?.id === row.id ? (
                            <RenameForm
                              title={renaming.title}
                              busy={busy}
                              onChange={(title) => setRenaming({ id: row.id, title })}
                              onSubmit={() => rename(row.id, renaming.title.trim())}
                              onCancel={() => setRenaming(null)}
                            />
                          ) : (
                            <span className="flex items-center gap-2">
                              <span className="truncate" title={row.title}>
                                {row.title}
                              </span>
                              {row.id === props.loadedId && (
                                <Badge variant="outline" className="rounded-full px-1.5 py-0 font-normal text-muted-foreground">
                                  open
                                </Badge>
                              )}
                            </span>
                          )}
                        </TableCell>
                        <TableCell>
                          <span className="inline-flex max-w-[16rem] flex-wrap gap-1">
                            {row.models.map((m, i) => (
                              <Badge key={`${m}\n${i}`} variant="secondary" className="max-w-full rounded-full font-normal text-muted-foreground">
                                <span className="truncate">{m}</span>
                              </Badge>
                            ))}
                          </span>
                        </TableCell>
                        <TableCell className="text-right tabular-nums">{row.turns}</TableCell>
                        <TableCell className="whitespace-nowrap text-muted-foreground">{fmtAgo(row.updated)}</TableCell>
                        <TableCell className="pr-3 text-right">
                          <span className="inline-flex items-center gap-0.5">
                            <Button
                              type="button"
                              variant="outline"
                              size="sm"
                              className="mr-1"
                              disabled={busy}
                              onClick={() => (props.unsaved() ? setConfirmLoad(row) : load(row))}
                            >
                              Load
                            </Button>
                            <Button
                              type="button"
                              variant="ghost"
                              size="icon-sm"
                              className="text-muted-foreground hover:text-foreground"
                              title="Rename"
                              aria-label={`Rename ${row.title}`}
                              disabled={busy}
                              onClick={() => setRenaming({ id: row.id, title: row.title })}
                            >
                              <Pencil aria-hidden="true" />
                            </Button>
                            <Button
                              type="button"
                              variant="ghost"
                              size="icon-sm"
                              className="text-muted-foreground hover:text-foreground"
                              title="Add to an eval dataset"
                              aria-label={`Add ${row.title} to a dataset`}
                              disabled={busy}
                              onClick={() => startAdding(row)}
                            >
                              <Database aria-hidden="true" />
                            </Button>
                            <Button
                              type="button"
                              variant="ghost"
                              size="icon-sm"
                              className="text-destructive hover:text-destructive"
                              title="Delete"
                              aria-label={`Delete ${row.title}`}
                              disabled={busy}
                              onClick={() => setDeleting(row)}
                            >
                              <Trash2 aria-hidden="true" />
                            </Button>
                          </span>
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </div>
            ) : (
              !shown.text && (
                <EmptyState icon={Bookmark}>
                  <p>Nothing saved yet. Save lets you come back to a chat later.</p>
                </EmptyState>
              )
            )}
            <Notice notice={shown} />
          </>
        )}

        <AlertDialog open={confirmLoad !== null} onOpenChange={(next) => !next && setConfirmLoad(null)}>
          <AlertDialogContent>
            <AlertDialogHeader>
              <AlertDialogTitle>Replace the current chat?</AlertDialogTitle>
              <AlertDialogDescription>It has turns that were not saved; loading "{confirmLoad?.title}" drops them.</AlertDialogDescription>
            </AlertDialogHeader>
            <AlertDialogFooter>
              <AlertDialogCancel>Cancel</AlertDialogCancel>
              <AlertDialogAction onClick={() => confirmLoad && load(confirmLoad)}>Load anyway</AlertDialogAction>
            </AlertDialogFooter>
          </AlertDialogContent>
        </AlertDialog>

        <AlertDialog open={deleting !== null} onOpenChange={(next) => !next && setDeleting(null)}>
          <AlertDialogContent>
            <AlertDialogHeader>
              <AlertDialogTitle>Delete "{deleting?.title}"?</AlertDialogTitle>
              <AlertDialogDescription>
                {deleting?.id === props.loadedId
                  ? "The chat stays open, but Save will store it as a new one."
                  : "Examples already added to a dataset stay."}
              </AlertDialogDescription>
            </AlertDialogHeader>
            <AlertDialogFooter>
              <AlertDialogCancel>Cancel</AlertDialogCancel>
              <AlertDialogAction className={buttonVariants({ variant: "destructive" })} onClick={() => deleting && remove(deleting)}>
                Delete
              </AlertDialogAction>
            </AlertDialogFooter>
          </AlertDialogContent>
        </AlertDialog>
      </DialogContent>
    </Dialog>
  );
}

/** The title cell while renaming: Enter saves, Escape cancels. */
function RenameForm(props: { title: string; busy: boolean; onChange(title: string): void; onSubmit(): void; onCancel(): void }) {
  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (props.title.trim()) props.onSubmit();
  };
  const onKey = (event: KeyboardEvent<HTMLInputElement>) => {
    if (event.key === "Escape") {
      event.preventDefault();
      props.onCancel();
    }
  };
  return (
    <form className="flex items-center gap-1" onSubmit={submit}>
      <Input
        autoFocus
        aria-label="Title"
        className="h-8"
        maxLength={200}
        value={props.title}
        onChange={(e) => props.onChange(e.target.value)}
        onKeyDown={onKey}
        onFocus={(e) => e.currentTarget.select()}
      />
      <Button type="submit" variant="ghost" size="icon-sm" title="Save the title" aria-label="Save the title" disabled={props.busy || !props.title.trim()}>
        <Check aria-hidden="true" />
      </Button>
      <Button type="button" variant="ghost" size="icon-sm" title="Cancel" aria-label="Cancel renaming" onClick={props.onCancel}>
        <X aria-hidden="true" />
      </Button>
    </form>
  );
}

/**
 * Turning one column's chat into an eval example: every turn up to the last
 * reply is the input, the last reply (or the text given) the reference.
 * Datasets are the admin's, so a playground-only key is told so.
 */
function AddExample(props: { quiet: ApiClient; conversation: Conversation; onBack(): void }) {
  const { quiet, conversation } = props;
  // This client's 401 means "needs the admin key", which the form says: an answer, so the load returns it.
  const loaded = useQuery({
    // Not GET /admin/datasets' own key: the reply here is a different thing, the datasets or the refusal.
    queryKey: ["playground", "datasets"],
    queryFn: ({ signal }) =>
      quiet.get("/admin/datasets", { signal }).then(
        (r): { datasets: DatasetSummary[]; refused: boolean } => ({ datasets: r.datasets, refused: false }),
        (e) => {
          if (e instanceof AuthError) return { datasets: [], refused: true };
          throw e;
        },
      ),
  });
  const datasets = loaded.data?.datasets ?? null;
  const [refused, setRefused] = useState(false);
  const needsAdmin = refused || loaded.data?.refused === true;
  const [picked, setPicked] = useState("");
  // The first dataset until another is picked.
  const datasetId = picked || (datasets?.[0]?.id ?? "");
  const [column, setColumn] = useState(0);
  const [reference, setReference] = useState(lastReply(conversation, 0) ?? "");

  const chooseColumn = (index: number) => {
    setColumn(index);
    // The reference follows the column, since it is that column's reply.
    setReference(lastReply(conversation, index) ?? "");
  };

  const submit = useAction(
    async (event: FormEvent) => {
      event.preventDefault();
      const dataset = datasets?.find((d) => d.id === datasetId);
      if (!dataset) return;
      const body: ExampleBody = { dataset_id: dataset.id, column };
      if (reference.trim()) body.reference = reference.trim();
      try {
        await quiet.post("/playground/conversations/{conversation_id}/example", { params: { path: { conversation_id: conversation.id } }, body });
      } catch (e) {
        // The quiet client's 401 is this form's answer, not the sign-in prompt's.
        if (e instanceof AuthError) return setRefused(true);
        throw e;
      }
      toast.success(`Added an example to ${dataset.name}.`);
      props.onBack();
    },
    { notice: true },
  );

  const several = conversation.columns.length > 1;
  const noReply = lastReply(conversation, column) === null;
  return (
    <form className="grid gap-4" onSubmit={submit.run}>
      <DialogHeader>
        <DialogTitle>Add to a dataset</DialogTitle>
        <DialogDescription>
          The turns of "{conversation.title}" up to the last reply become one example; the reference is what a good answer looks like.
        </DialogDescription>
      </DialogHeader>
      {needsAdmin ? (
        <Notice notice={{ text: "Needs the admin key: datasets belong to /admin. Sign in there in this browser, then try again.", kind: "error" }} />
      ) : (
        <>
          <div className="flex flex-wrap gap-3">
            <SelectField
              label="Dataset"
              className="flex-[1_1_240px]"
              aria-label="Dataset"
              placeholder={loaded.isPending ? "Loading…" : "No datasets yet: create one in /admin"}
              disabled={!datasets?.length}
              value={datasetId}
              onChange={setPicked}
              options={(datasets ?? []).map((d) => ({
                value: d.id,
                label: (
                  <>
                    <span className="min-w-0 truncate">{d.name}</span>
                    <span className="text-xs text-muted-foreground">{`${d.examples} example${d.examples === 1 ? "" : "s"}`}</span>
                  </>
                ),
              }))}
            />
            {several && (
              <SelectField
                label="Column"
                triggerClassName="w-[220px]"
                aria-label="Column"
                value={String(column)}
                onChange={(v) => chooseColumn(Number(v))}
                options={conversation.columns.map((c, i) => ({
                  value: String(i),
                  label: (
                    <>
                      <span className="font-medium">{letter(i)}</span>
                      <span className="min-w-0 truncate text-muted-foreground">{c.model}</span>
                    </>
                  ),
                }))}
              />
            )}
          </div>
          <Field
            label="Reference (optional)"
            hint={noReply ? "This column has no reply to use: give the expected answer." : "Prefilled with the column's last reply."}
          >
            <Textarea rows={5} className="max-h-[40vh]" value={reference} onChange={(e) => setReference(e.target.value)} />
          </Field>
        </>
      )}
      <Notice notice={submit.notice.text ? submit.notice : { text: errorText(loaded.error), kind: "error" }} />
      <DialogFooter className="sm:justify-between">
        <Button type="button" variant="ghost" onClick={props.onBack}>
          <ArrowLeft aria-hidden="true" />
          Back
        </Button>
        <SubmitButton busy={submit.busy} disabled={needsAdmin || !datasetId || (noReply && !reference.trim())}>
          Add example
        </SubmitButton>
      </DialogFooter>
    </form>
  );
}
