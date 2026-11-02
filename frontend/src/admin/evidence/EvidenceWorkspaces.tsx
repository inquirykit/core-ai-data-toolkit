// The workspace picker: the workspaces, and making a new one (admin key).

import { useState, type FormEvent } from "react";
import { Plus, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card, CardAction, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { useAction } from "../../shared/action";
import type { ApiClient } from "../../shared/api";
import { Notice, SelectInput, SubmitButton } from "../../shared/components";
import type { EvidenceWorkspace } from "../../shared/types";
import { evidenceHash } from "./evidence";

/** The workspace picker. A new workspace takes a name and, optionally, the id of the workspace it stands for
 * in your own application (else one is made). */
export function WorkspaceCard(props: { client: ApiClient; workspaces: EvidenceWorkspace[] | null; active: string; reload(): Promise<unknown> }) {
  const { workspaces, active } = props;
  const [adding, setAdding] = useState(false);
  const [name, setName] = useState("");
  const [id, setId] = useState("");

  const create = useAction(
    async (event: FormEvent) => {
      event.preventDefault();
      const trimmed = name.trim();
      if (!trimmed) return;
      const body = id.trim() ? { name: trimmed, id: id.trim() } : { name: trimmed };
      const made = await props.client.post("/evidence/workspaces", { body });
      setName("");
      setId("");
      setAdding(false);
      await props.reload();
      location.hash = evidenceHash(made.workspace.id, null);
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
          options={(workspaces ?? []).map((w) => ({ value: w.id, label: w.name }))}
        />
        {adding && (
          <form className="grid gap-2 pt-1" onSubmit={create.run}>
            <Input
              autoFocus
              aria-label="Workspace name"
              placeholder="Name"
              maxLength={200}
              value={name}
              onChange={(e) => setName(e.target.value)}
              disabled={create.busy}
            />
            <Input
              aria-label="Workspace id"
              placeholder="Id (optional): your application's"
              maxLength={36}
              className="font-mono text-xs"
              value={id}
              onChange={(e) => setId(e.target.value)}
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
