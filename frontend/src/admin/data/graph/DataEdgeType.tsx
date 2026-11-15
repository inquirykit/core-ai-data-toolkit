// A relationship (an edge type) of the Data section (#/data/<ws>/_edges/<type>): the tables it
// joins (GET /db/{ws}/_edges) and its edges, filtered by an SQL condition
// (DataGraphRows.tsx). Its drawing is the Graph query's, started with its
// edges; an admin can drop it. An empty one shows how its first edge is written.
import { useIsFetching, useQueryClient } from "@tanstack/react-query";
import { LoaderCircle, Network, RotateCw, Spline } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button, buttonVariants } from "@/components/ui/button";
import { Card, CardAction, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { useAction } from "../../../shared/action";
import { useApi } from "../../../shared/api";
import { CodeBlock, ConfirmButton, CopyButton, Notice } from "../../../shared/components";
import { fmtInt, plural } from "../../../shared/format";
import { dataHash, type Workspace } from "../data";
import { joinLabel } from "./edges";
import { EdgeRows } from "./DataGraphRows";

/** GET /db/{ws}/_edges, keyed under the workspace's edge loads: the workspace page and every edge type page read it. */
export function useEdgeTypes(workspace: string) {
  const client = useApi();
  return client.useQuery("get", "/db/{workspace}/_edges", { params: { path: { workspace } } }, { select: (d) => d.edges });
}

export function EdgeTypeBrowser(props: { workspace: Workspace; type: string; onChange(): void }) {
  const client = useApi();
  const { type } = props;
  const workspace = props.workspace.name;
  const info = props.workspace.edges.find((e) => e.name === type);
  const detail = useEdgeTypes(workspace).data?.find((e) => e.name === type);
  const managed = props.workspace.managed ?? null;
  const queries = useQueryClient();
  const loading = useIsFetching({ queryKey: ["db", "edges", workspace, type] }) > 0;

  const refresh = () => {
    void queries.invalidateQueries({ queryKey: ["db", "edges", workspace, type] });
    void queries.invalidateQueries({ queryKey: ["get", "/db/{workspace}/_edges"] });
    props.onChange();
  };

  const drop = useAction(
    async () => {
      await client.delete("/db/{workspace}/_edges/{edge_type}", { params: { path: { workspace, edge_type: type } } });
      location.hash = dataHash({ workspace });
      props.onChange();
    },
    { success: `Dropped the relationship ${type}.`, failed: `Could not drop ${type}` },
  );

  const count = info?.count ?? 0;
  return (
    <Card className="min-w-0 gap-4">
      <CardHeader>
        <CardTitle className="flex items-center gap-1.5 font-mono text-lg">
          <a className="text-muted-foreground hover:text-foreground" href={dataHash({ workspace })}>
            {workspace}
          </a>
          <span className="text-muted-foreground">/</span>
          <Spline className="size-4 text-muted-foreground" aria-hidden="true" />
          {type}
        </CardTitle>
        <CardDescription className="flex flex-wrap items-center gap-x-2 gap-y-1">
          <span>{plural(count, "relationship")}</span>
          {detail && detail.joins.length > 0 && (
            <span className="font-mono text-xs" title="The tables it goes from and to, with how many relationships join each pair">
              {detail.joins.map((j) => `${joinLabel(j)}${detail.joins.length > 1 ? ` · ${fmtInt(j.count)}` : ""}`).join(", ")}
            </span>
          )}
          {loading && <LoaderCircle className="size-3.5 animate-spin" aria-label="Loading" />}
          <Badge variant="secondary" className="gap-1" title="A relationship: it links rows of tables (PUT /db/{workspace}/_edges)">
            <Spline aria-hidden="true" />
            relationship
          </Badge>
        </CardDescription>
        <CardAction className="flex flex-wrap justify-end gap-2">
          <a
            className={buttonVariants({ variant: "outline", size: "sm" })}
            href={dataHash({ workspace, graph: true, graphEdge: type })}
            title="Open the Graph query with these relationships and the rows they join"
          >
            <Network className="text-query-graph" aria-hidden="true" />
            Graph query
          </a>
          <Button type="button" variant="outline" size="sm" onClick={refresh}>
            <RotateCw aria-hidden="true" />
            Refresh
          </Button>
          {!managed && (
            <ConfirmButton
              label="Drop relationship"
              title={`Drop the relationship ${type}?`}
              body={`All ${fmtInt(count)} ${type} relationships are deleted; the rows they join stay. This cannot be undone.`}
              busy={drop.busy}
              onConfirm={drop.run}
            />
          )}
        </CardAction>
      </CardHeader>
      <CardContent className="grid gap-3">
        {managed && <Notice notice={{ text: `Read-only here: kept by the service (${managed}).` }} />}
        <EdgeRows workspace={workspace} type={type} />
        {count === 0 && !managed && <FirstEdge workspace={props.workspace} type={type} />}
      </CardContent>
    </Card>
  );
}

/** How a client writes the first relationship of the type (an edge), the workspace's tables standing in for its ends. */
function FirstEdge({ workspace, type }: { workspace: Workspace; type: string }) {
  const nodes = workspace.tables.filter((t) => t.graph).map((t) => t.name);
  const [from, to] = [nodes[0] ?? "<table>", nodes[1] ?? nodes[0] ?? "<table>"];
  const example = `curl -X PUT ${location.origin}/db/${workspace.name}/_edges \\
  -H 'X-API-Key: <API_KEY>' -H 'Content-Type: application/json' \\
  -d '{"edges": [{"type": "${type}", "from": "${from}/<id>", "to": "${to}/<id>"}]}'`;
  return (
    <div className="grid gap-2">
      <p className="text-sm text-muted-foreground">None yet. Clients add them like this, each linking two rows (adding one again replaces its details):</p>
      <CodeBlock>{example}</CodeBlock>
      <div>
        <CopyButton value={example} />
      </div>
    </div>
  );
}
