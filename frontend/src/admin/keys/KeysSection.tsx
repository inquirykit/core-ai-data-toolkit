// /admin#/keys: the client keys (GET /admin/keys), one for each server or
// machine that calls the service. A form makes one, whose token is shown once;
// the list says each key's state, expiry, last use and what it may reach (its
// grants, changed in a dialog), and revokes it. The admin's own key stays
// ADMIN_API_KEY, which no one revokes from here. Helpers in keys.ts.
import { useRef, useState, type FormEvent } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { Ban, KeyRound, Pencil, TriangleAlert } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { useAction } from "../../shared/action";
import { useApi } from "../../shared/api";
import { ConfirmButton, CopyButton, Disclosure, Field, Notice, SelectField, SelectInput, StatusLabel, SubmitButton } from "../../shared/components";
import { fmtAgo, fmtClock } from "../../shared/format";
import { DataGrid, type GridColumn } from "../../shared/grid";
import { errorText } from "../../shared/query";
import { LoadedCard, Pager, useCursorPages } from "../layout";
import { GrantsEditor } from "./GrantsEditor";
import {
  DEFAULT_EXPIRY,
  EXPIRY_OPTIONS,
  expiryText,
  grantsBody,
  grantsDetail,
  grantsForm,
  grantsText,
  keysQuery,
  KEYS_PAGE,
  keyStatus,
  newKeyBody,
  NO_GRANTS,
  STATE_OPTIONS,
  type ClientKey,
  type ClientKeyMade,
  type KeyFilter,
} from "./keys";

export function KeysSection() {
  const [made, setMade] = useState<ClientKeyMade | null>(null);
  return (
    <div className="grid gap-4">
      <NewKeyCard onMade={setMade} />
      {made && <TokenCard made={made} onDone={() => setMade(null)} />}
      <KeysCard />
    </div>
  );
}

function NewKeyCard({ onMade }: { onMade(made: ClientKeyMade): void }) {
  const client = useApi();
  const queries = useQueryClient();
  const [name, setName] = useState("");
  const [expiry, setExpiry] = useState(DEFAULT_EXPIRY);
  const [grants, setGrants] = useState(NO_GRANTS);
  const make = useAction(
    async (event: FormEvent) => {
      event.preventDefault();
      const body = newKeyBody(name, expiry, grantsBody(grants));
      if (typeof body === "string") throw new Error(body);
      const made = await client.post("/admin/keys", { body });
      setName("");
      setGrants(NO_GRANTS);
      onMade(made);
      await queries.invalidateQueries({ queryKey: ["get", "/admin/keys"] });
    },
    { notice: true },
  );
  return (
    <Card>
      <CardHeader>
        <CardTitle>New client key</CardTitle>
        <CardDescription>
          One key for each server or machine that calls the service, so each can expire or be revoked on its own. A client key opens the client
          routes (inference, <code>/v1</code>, <code>/db</code> rows, evidence…), never the admin's, which stay with <code>ADMIN_API_KEY</code>. Its
          access narrows which tables and models it reaches.
        </CardDescription>
      </CardHeader>
      <CardContent>
        <form className="grid gap-3" onSubmit={make.run}>
          <div className="flex flex-wrap items-end gap-3">
            <Field label="Name" className="min-w-56 flex-1">
              <Input value={name} maxLength={100} placeholder="worker-3, gpu-box-2…" onChange={(e) => setName(e.target.value)} />
            </Field>
            <SelectField label="Expires" className="w-36" value={expiry} onChange={setExpiry} options={EXPIRY_OPTIONS} />
            <SubmitButton busy={make.busy} icon={<KeyRound aria-hidden="true" />}>
              Make key
            </SubmitButton>
          </div>
          <Disclosure label="Access: every table and model unless limited here" contentClassName="pt-3">
            <GrantsEditor form={grants} onChange={setGrants} />
          </Disclosure>
        </form>
        <Notice notice={make.notice} className="mt-3" />
      </CardContent>
    </Card>
  );
}

function TokenCard({ made, onDone }: { made: ClientKeyMade; onDone(): void }) {
  const field = useRef<HTMLInputElement>(null);
  return (
    <Card className="border-warning/40">
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <TriangleAlert className="size-4 text-warning" aria-hidden="true" />
          Copy the key for {made.name} now
        </CardTitle>
        <CardDescription>
          It is not shown again: only its hash is kept. Send it as <code>Authorization: Bearer &lt;key&gt;</code> or <code>X-API-Key</code>.
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-wrap items-center gap-2">
        <Input ref={field} readOnly value={made.token} aria-label="The new key" className="min-w-0 flex-1 font-mono text-xs" onFocus={(e) => e.target.select()} />
        <CopyButton value={made.token} fallback={field} />
        <Button type="button" variant="ghost" onClick={onDone}>
          Done
        </Button>
      </CardContent>
    </Card>
  );
}

function KeysCard() {
  const client = useApi();
  const queries = useQueryClient();
  const [state, setState] = useState<KeyFilter>("");
  const [editing, setEditing] = useState<ClientKey | null>(null);
  const pages = useCursorPages(state);
  const query = client.useQuery("get", "/admin/keys", { params: { query: keysQuery(state, pages.cursor) } });
  const data = query.data ?? null;
  const revoke = useAction(
    async (key: ClientKey) => {
      await client.post("/admin/keys/{key_id}/revoke", { params: { path: { key_id: key.id } } });
      await queries.invalidateQueries({ queryKey: ["get", "/admin/keys"] });
    },
    { success: (_, key) => `Revoked ${key.name}.`, failed: (key) => `Could not revoke ${key.name}` },
  );

  const columns: GridColumn<ClientKey>[] = [
    {
      key: "name",
      label: "Name",
      className: "max-w-64",
      render: (k) => (
        <>
          <div className="truncate font-medium">{k.name}</div>
          <div className="truncate font-mono text-xs text-muted-foreground">aitk_{k.id}_…</div>
        </>
      ),
    },
    { key: "state", label: "State", className: "whitespace-nowrap", render: (k) => <StatusLabel status={keyStatus(k.state)} /> },
    { key: "made", label: "Made", className: "whitespace-nowrap tabular-nums", title: (k) => fmtClock(k.created_at, true), render: (k) => fmtAgo(k.created_at) },
    {
      key: "expires",
      label: "Expires",
      className: "whitespace-nowrap tabular-nums",
      title: (k) => (k.expires_at != null ? fmtClock(k.expires_at, true) : undefined),
      render: (k) => (k.state === "revoked" ? `Revoked ${fmtAgo(k.revoked_at)}` : expiryText(k)),
    },
    {
      key: "used",
      label: "Last used",
      className: "whitespace-nowrap tabular-nums text-muted-foreground",
      title: (k) => (k.last_used_at != null ? fmtClock(k.last_used_at, true) : undefined),
      render: (k) => (k.last_used_at != null ? fmtAgo(k.last_used_at) : "Never"),
    },
    {
      key: "access",
      label: "Access",
      className: "max-w-64",
      title: (k) => grantsDetail(k.grants),
      render: (k) => (
        <div className="flex items-center gap-1">
          <span className="truncate text-muted-foreground">{grantsText(k.grants)}</span>
          {k.state !== "revoked" && (
            <Button type="button" variant="ghost" size="icon-sm" aria-label={`Change what ${k.name} may reach`} onClick={() => setEditing(k)}>
              <Pencil aria-hidden="true" />
            </Button>
          )}
        </div>
      ),
    },
    {
      key: "revoke",
      align: "right",
      render: (k) =>
        k.state !== "revoked" && (
          <ConfirmButton
            title={`Revoke ${k.name}?`}
            body="From its next request on, the key is refused. It stays in this list and in the audit log, and cannot be made live again: make a new key instead."
            label="Revoke"
            icon={<Ban aria-hidden="true" />}
            busy={revoke.running?.[0].id === k.id}
            onConfirm={() => void revoke.run(k)}
          />
        ),
    },
  ];

  const keys = data?.keys ?? null;
  return (
    <LoadedCard
      title="Client keys"
      description={
        data
          ? data.clients_need_a_key
            ? "The client routes need a key: API_KEY or a live client key (the admin key opens them too). Revoking every client key does not open them again."
            : "The client routes take any request until the first client key is made (or API_KEY is set)."
          : "Loading…"
      }
      data={keys}
      error={errorText(query.error)}
      onRefresh={() => void query.refetch()}
      skeleton="h-40"
      top={
        <SelectInput
          size="sm"
          className="w-36"
          aria-label="State"
          value={state}
          none="Any state"
          onChange={(v) => setState(v as KeyFilter)}
          options={STATE_OPTIONS}
        />
      }
      after={
        keys &&
        (pages.hasPrevious || data?.next) && (
          <Pager
            start={pages.index * KEYS_PAGE}
            shown={keys.length}
            count={null}
            hasPrevious={pages.hasPrevious}
            hasNext={Boolean(data?.next)}
            loading={query.isFetching}
            onPrevious={pages.previous}
            onNext={() => pages.next(data?.next ?? undefined)}
          />
        )
      }
    >
      {(list) => (
        <div className="overflow-x-auto rounded-lg border">
          <DataGrid columns={columns} rows={list} rowKey={(k) => k.id} headerClassName="bg-muted/50" empty={state ? "No keys in this state" : "No client keys yet"} />
          {editing && <GrantsDialog clientKey={editing} onClose={() => setEditing(null)} />}
        </div>
      )}
    </LoadedCard>
  );
}

function GrantsDialog({ clientKey, onClose }: { clientKey: ClientKey; onClose(): void }) {
  const client = useApi();
  const queries = useQueryClient();
  const [form, setForm] = useState(() => grantsForm(clientKey.grants));
  const save = useAction(
    async (event: FormEvent) => {
      event.preventDefault();
      await client.patch("/admin/keys/{key_id}", { params: { path: { key_id: clientKey.id } }, body: { grants: grantsBody(form) } });
      await queries.invalidateQueries({ queryKey: ["get", "/admin/keys"] });
      onClose();
    },
    { notice: true },
  );
  return (
    <Dialog open onOpenChange={(open) => !open && onClose()}>
      <DialogContent className="sm:max-w-2xl">
        <form className="grid gap-4" onSubmit={save.run}>
          <DialogHeader>
            <DialogTitle>What {clientKey.name} may reach</DialogTitle>
            <DialogDescription>From the key's next request on, under the same token.</DialogDescription>
          </DialogHeader>
          <GrantsEditor form={form} onChange={setForm} />
          <Notice notice={save.notice} />
          <DialogFooter>
            <Button type="button" variant="ghost" onClick={onClose}>
              Cancel
            </Button>
            <SubmitButton busy={save.busy}>Save</SubmitButton>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
