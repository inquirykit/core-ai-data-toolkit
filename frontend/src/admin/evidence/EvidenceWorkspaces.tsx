// The workspace picker: the workspaces of /db, and making a new one (admin key).

import { useState, type FormEvent } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { Plus, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card, CardAction, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { useAction } from "../../shared/action";
import { useApi, type Schemas } from "../../shared/api";
import { Notice, SelectInput, SubmitButton } from "../../shared/components";
import { evidenceHash } from "./evidence";

/** A workspace as GET /db lists it: the Data section's. */
type Workspace = Schemas["WorkspaceEntry"];

/** The workspace picker, each by its label (else its name). A new workspace is one of /db's (POST /db): a name, and
 * a label saying what it holds; its ledger is made on its first capture or case. */
export function WorkspaceCard(props: { workspaces: Workspace[] | null; active: string }) {
  const client = useApi();
  const { workspaces, active } = props;
  const queries = useQueryClient();
  const [adding, setAdding] = useState(false);
  const [name, setName] = useState("");
  const [label, setLabel] = useState("");

  const create = useAction(
    async (event: FormEvent) => {
      event.preventDefault();
      const made = await client.post("/db", { body: { name: name.trim(), label: label.trim() || undefined } });
      setName("");
      setLabel("");
      setAdding(false);
      // The workspaces here and in the Data section.
      await queries.invalidateQueries({ queryKey: ["get", "/db"] });
      location.hash = evidenceHash(made.name, null);
    },
    { notice: true },
  );

  return (
    <Card className="gap-3 py-4">
      <CardHeader className="px-4">
        <CardTitle>Workspace</CardTitle>
        <CardDescription>Each has its own ledger: cases, chain and files.</CardDescription>
        <CardAction>
          <Button type="button" size="sm" className="h-8" onClick={() => setAdding((v) => !v)} aria-expanded={adding}>
            {adding ? <X aria-hidden="true" /> : <Plus aria-hidden="true" />}
            New
          </Button>
        </CardAction>
      </CardHeader>
      <CardContent className="grid gap-2 px-4">
        <SelectInput
          className="w-full"
          aria-label="Workspace"
          placeholder={workspaces ? "Pick a workspace" : "Loading…"}
          disabled={!workspaces}
          value={active}
          onChange={(value) => (location.hash = evidenceHash(value, null))}
          options={(workspaces ?? []).map((w) => ({ value: w.name, label: w.label || w.name, title: w.label ? w.name : undefined }))}
        />
        {adding && (
          <form className="grid gap-2 pt-1" onSubmit={create.run}>
            <Input
              autoFocus
              aria-label="Workspace name"
              placeholder="Name"
              pattern="[A-Za-z][A-Za-z0-9]*(_[A-Za-z0-9]+)*"
              title="Letters, digits and single underscores, starting with a letter"
              maxLength={48}
              required
              autoComplete="off"
              className="font-mono text-xs"
              value={name}
              onChange={(e) => setName(e.target.value)}
              disabled={create.busy}
            />
            <Input
              aria-label="Workspace label"
              placeholder="Label (optional)"
              title="What it holds, in words: shown in place of its name"
              maxLength={200}
              autoComplete="off"
              value={label}
              onChange={(e) => setLabel(e.target.value)}
              disabled={create.busy}
            />
            <SubmitButton size="sm" busy={create.busy} disabled={!name.trim()}>
              Create workspace
            </SubmitButton>
            <Notice notice={create.notice} />
          </form>
        )}
      </CardContent>
    </Card>
  );
}
