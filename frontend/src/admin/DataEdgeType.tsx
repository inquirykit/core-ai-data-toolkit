// A relationship (an edge type) of the Data section (#/data/<ns>/_edges/<type>): the tables it
// joins (GET /db/{ns}/_edges) and its edges, filtered by a Cypher condition
// (DataGraphRows.tsx). Its drawing is the Graph query's, started with its
// edges; an admin can drop it. An empty one shows how its first edge is written.
import { useIsFetching, useQueryClient } from "@tanstack/react-query";
import { LoaderCircle, Network, RotateCw, Spline } from "lucide-react";
import { toast } from "sonner";
import { Badge } from "@/components/ui/badge";
import { Button, buttonVariants } from "@/components/ui/button";
import { Card, CardAction, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import type { ApiClient } from "../shared/api";
import { failed } from "../shared/errors";
import { ConfirmButton, CopyButton, Notice } from "../shared/components";
import { fmtInt } from "../shared/format";
import { dataHash, joinLabel, type Namespace } from "./data";
import { EdgeRows } from "./DataGraphRows";

/** GET /db/{ns}/_edges, keyed under the namespace's edge loads: the namespace page and every edge type page read it. */
export function useEdgeTypes(client: ApiClient, namespace: string) {
  return client.useQuery("get", "/db/{namespace}/_edges", { params: { path: { namespace } } }, { select: (d) => d.edges });
}

export function EdgeTypeBrowser(props: { client: ApiClient; namespace: Namespace; type: string; onChange(): void }) {
  const { client, type } = props;
  const namespace = props.namespace.name;
  const info = props.namespace.edges.find((e) => e.name === type);
  const detail = useEdgeTypes(client, namespace).data?.find((e) => e.name === type);
  const managed = props.namespace.managed ?? null;
  const queries = useQueryClient();
  const loading = useIsFetching({ queryKey: ["db", "edges", namespace, type] }) > 0;

  const refresh = () => {
    void queries.invalidateQueries({ queryKey: ["db", "edges", namespace, type] });
    void queries.invalidateQueries({ queryKey: ["get", "/db/{namespace}/_edges"] });
    props.onChange();
  };

  const drop = () =>
    client.delete("/db/{namespace}/_edges/{edge_type}", { params: { path: { namespace, edge_type: type } } }).then(() => {
      toast.success(`Dropped the relationship ${type}.`);
      location.hash = dataHash({ namespace });
      props.onChange();
    }, failed(`Could not drop ${type}`));

  const count = info?.count ?? 0;
  return (
    <Card className="min-w-0 gap-4">
      <CardHeader>
        <CardTitle className="flex items-center gap-1.5 font-mono text-lg">
          <a className="text-muted-foreground hover:text-foreground" href={dataHash({ namespace })}>
            {namespace}
          </a>
          <span className="text-muted-foreground">/</span>
          <Spline className="size-4 text-muted-foreground" aria-hidden="true" />
          {type}
        </CardTitle>
        <CardDescription className="flex flex-wrap items-center gap-x-2 gap-y-1">
          <span>
            {fmtInt(count)} {count === 1 ? "relationship" : "relationships"}
          </span>
          {detail && detail.joins.length > 0 && (
            <span className="font-mono text-xs" title="The tables it goes from and to, with how many relationships join each pair">
              {detail.joins.map((j) => `${joinLabel(j)}${detail.joins.length > 1 ? ` · ${fmtInt(j.count)}` : ""}`).join(", ")}
            </span>
          )}
          {loading && <LoaderCircle className="size-3.5 animate-spin" aria-label="Loading" />}
          <Badge variant="secondary" className="gap-1" title="A relationship: it links rows of tables (PUT /db/{ns}/_edges)">
            <Spline aria-hidden="true" />
            relationship
          </Badge>
        </CardDescription>
        <CardAction className="flex flex-wrap justify-end gap-2">
          <a
            className={buttonVariants({ variant: "outline", size: "sm" })}
            href={dataHash({ namespace, graph: true, graphEdge: type })}
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
              onConfirm={drop}
            />
          )}
        </CardAction>
      </CardHeader>
      <CardContent className="grid gap-3">
        {managed && <Notice notice={{ text: `Read-only here: kept by the service (${managed}).` }} />}
        <EdgeRows client={client} namespace={namespace} type={type} />
        {count === 0 && !managed && <FirstEdge namespace={props.namespace} type={type} />}
      </CardContent>
    </Card>
  );
}

/** How a client writes the first relationship of the type (an edge), the namespace's tables standing in for its ends. */
function FirstEdge({ namespace, type }: { namespace: Namespace; type: string }) {
  const nodes = namespace.tables.filter((t) => t.graph).map((t) => t.name);
  const [from, to] = [nodes[0] ?? "<table>", nodes[1] ?? nodes[0] ?? "<table>"];
  const example = `curl -X PUT ${location.origin}/db/${namespace.name}/_edges \\
  -H 'X-API-Key: <API_KEY>' -H 'Content-Type: application/json' \\
  -d '{"edges": [{"type": "${type}", "from": "${from}/<id>", "to": "${to}/<id>"}]}'`;
  return (
    <div className="grid gap-2">
      <p className="text-sm text-muted-foreground">None yet. Clients add them like this, each linking two rows (adding one again replaces its details):</p>
      <pre className="overflow-x-auto rounded-lg border bg-muted/40 p-3 font-mono text-xs leading-relaxed">{example}</pre>
      <div>
        <CopyButton value={example} />
      </div>
    </div>
  );
}
