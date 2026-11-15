// The search box above the cases, and its results across them.

import { useState, type FormEvent } from "react";
import { ChevronRight, ListOrdered, Search, X } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { useApi } from "../../shared/api";
import { errorText } from "../../shared/query";
import { SelectInput } from "../../shared/components";
import { fmtIso, plural } from "../../shared/format";
import { DataGrid, type GridColumn } from "../../shared/grid";
import type { EvidenceCase, SearchHit } from "../../shared/types";
import { captureLabel, EMPTY_SEARCH, evidenceHash, marked, searchHash, searchQuery, searchTerms, type EvidenceSearch, type SearchSort } from "./evidence";
import { LoadedCard, NavRow, Pager, useCursorPages } from "../layout";

/** The search box on the left: words to find across the workspace's cases, and every capture newest first. */
export function SearchBox({ workspace, search }: { workspace: string; search: EvidenceSearch | null }) {
  const [q, setQ] = useState(search?.q ?? "");
  const submit = (event: FormEvent) => {
    event.preventDefault();
    location.hash = searchHash(workspace, { ...(search ?? EMPTY_SEARCH), q });
  };
  return (
    <Card className="gap-2 py-3">
      <CardContent className="grid gap-2 px-3">
        <form role="search" className="flex gap-2" onSubmit={submit}>
          <Input
            type="search"
            aria-label="Search captures"
            placeholder="Search captures"
            title='The pages, addresses, titles, tags and notes of every case. Every word must be there; "a phrase" as written, -word must not, word* starts so.'
            maxLength={500}
            className="h-8"
            value={q}
            onChange={(e) => setQ(e.target.value)}
          />
          <Button type="submit" variant="secondary" size="icon" className="size-8 shrink-0" aria-label="Search">
            <Search aria-hidden="true" />
          </Button>
        </form>
        <NavRow href={searchHash(workspace, EMPTY_SEARCH)} active={Boolean(search && !search.q && !search.case && !search.tag && !search.since && !search.until)}>
          <ListOrdered aria-hidden="true" />
          <span className="truncate">All captures, newest first</span>
        </NavRow>
      </CardContent>
    </Card>
  );
}
const SORT_OPTIONS: { value: SearchSort; label: string }[] = [
  { value: "", label: "Best match first" },
  { value: "newest", label: "Newest first" },
  { value: "oldest", label: "Oldest first" },
];
/** A search's captures across the workspace's cases (GET /evidence/search), with the form that refines it. */
export function SearchView(props: { workspace: string; search: EvidenceSearch; cases: EvidenceCase[] }) {
  const client = useApi();
  const { workspace, search } = props;
  const [form, setForm] = useState(search);
  // A new search (the hash changed) fills the form and starts at the first page.
  const [shown, setShown] = useState(search);
  if (search !== shown) {
    setShown(search);
    setForm(search);
  }
  const pages = useCursorPages(search);
  const query = client.useQuery("get", "/evidence/search", { params: { query: searchQuery(workspace, search, pages.cursor) } });
  const page = query.data ?? null;
  const error = errorText(query.error);
  const caseNames = new Map(props.cases.map((c) => [c.id, c.name]));
  const terms = searchTerms(search.q);
  const set = (patch: Partial<EvidenceSearch>) => setForm((f) => ({ ...f, ...patch }));
  const submit = (event: FormEvent) => {
    event.preventDefault();
    location.hash = searchHash(workspace, form);
  };

  const columns: GridColumn<SearchHit>[] = [
    { key: "received", label: "Received", className: "whitespace-nowrap align-top tabular-nums", render: (c) => fmtIso(c.receivedAt) },
    { key: "case", label: "Case", className: "max-w-40 truncate align-top", render: (c) => caseNames.get(c.caseId) ?? c.caseId },
    {
      key: "page",
      label: "Page",
      className: "max-w-[32rem] align-top",
      render: (c) => (
        <>
          <div className="truncate font-medium">{captureLabel(c)}</div>
          {c.title && c.url && <div className="truncate text-xs text-muted-foreground">{c.url}</div>}
          {c.snippet && (
            <p className="mt-1 line-clamp-2 text-xs text-muted-foreground">
              {marked(c.snippet, terms).map((part, i) =>
                part.hit ? (
                  <mark key={i} className="rounded-sm bg-warning/25 px-0.5 text-foreground">
                    {part.text}
                  </mark>
                ) : (
                  <span key={i}>{part.text}</span>
                ),
              )}
            </p>
          )}
        </>
      ),
    },
    {
      key: "tags",
      label: "Tags",
      className: "align-top",
      render: (c) => (
        <span className="flex flex-wrap gap-1">
          {c.tags.map((t) => (
            <Badge key={t} variant="secondary" className="font-normal">
              {t}
            </Badge>
          ))}
        </span>
      ),
    },
    { key: "open", headClassName: "w-8", className: "align-top", render: () => <ChevronRight className="size-4 text-muted-foreground" aria-hidden="true" /> },
  ];

  const filtered = search.case || search.tag || search.since || search.until;
  return (
    <LoadedCard
      title={search.q ? `Captures with “${search.q}”` : "Captures"}
      description={(p) => `${plural(p.count, "capture")} across ${search.case ? "one case" : "every case"} of the workspace`}
      data={page}
      error={error}
      onRefresh={() => void query.refetch()}
      skeleton="h-40"
      top={
        <>
          <form className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3" onSubmit={submit}>
            <Input
              type="search"
              aria-label="Words"
              placeholder='Words, "a phrase", -not, start*'
              maxLength={500}
              className="h-8 sm:col-span-2 lg:col-span-3"
              value={form.q}
              onChange={(e) => set({ q: e.target.value })}
            />
            <SelectInput
              size="sm"
              aria-label="Case"
              value={form.case}
              none="Every case"
              onChange={(v) => set({ case: v })}
              options={props.cases.map((c) => ({ value: c.id, label: c.name }))}
            />
            <Input aria-label="Tag" placeholder="Tag, as written" maxLength={64} className="h-8" value={form.tag} onChange={(e) => set({ tag: e.target.value })} />
            <SelectInput size="sm" aria-label="Order" value={form.sort} onChange={(v) => set({ sort: v as SearchSort })} options={SORT_OPTIONS} />
            <label className="flex items-center gap-2 text-sm text-muted-foreground">
              From
              <Input type="date" aria-label="Received from" className="h-8" value={form.since} onChange={(e) => set({ since: e.target.value })} />
            </label>
            <label className="flex items-center gap-2 text-sm text-muted-foreground">
              to
              <Input type="date" aria-label="Received up to" className="h-8" value={form.until} onChange={(e) => set({ until: e.target.value })} />
            </label>
            <div className="flex gap-2">
              <Button type="submit" variant="secondary" size="sm">
                <Search aria-hidden="true" />
                Search
              </Button>
              {(filtered || search.q) && (
                <Button type="button" variant="ghost" size="sm" onClick={() => (location.hash = searchHash(workspace, EMPTY_SEARCH))}>
                  <X aria-hidden="true" />
                  Clear
                </Button>
              )}
            </div>
          </form>
          <p className="text-xs text-muted-foreground">Dates are UTC. The words of a page are those of its HTML and MHTML snapshots, read when it was captured.</p>
        </>
      }
      after={
        page &&
        (pages.hasPrevious || page.next) && (
          <Pager
            start={pages.index * 50}
            shown={page.captures.length}
            count={page.count}
            hasPrevious={pages.hasPrevious}
            hasNext={Boolean(page.next)}
            loading={query.isFetching}
            onPrevious={pages.previous}
            onNext={() => pages.next(page.next ?? undefined)}
          />
        )
      }
    >
      {(p) => (
        <div className="overflow-hidden rounded-lg border">
          <DataGrid
            columns={columns}
            rows={p.captures}
            rowKey={(c) => c.captureId}
            rowHref={(c) => evidenceHash(workspace, c.caseId, c.captureId)}
            empty={search.q || filtered ? "No captures match" : "No captures in this workspace yet"}
          />
        </div>
      )}
    </LoadedCard>
  );
}
