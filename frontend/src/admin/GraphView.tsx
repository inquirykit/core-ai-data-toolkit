import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent, type ReactNode } from "react";
import {
  ArrowLeftRight,
  ArrowRight,
  Download,
  EyeOff,
  Expand,
  ListFilter,
  LoaderCircle,
  Maximize,
  Pause,
  Play,
  RotateCw,
  Search,
  Table2,
  Waypoints,
  X,
  ZoomIn,
  ZoomOut,
} from "lucide-react";
import { toast } from "sonner";
import { Badge } from "@/components/ui/badge";
import { Button, buttonVariants } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import { cn } from "@/lib/utils";
import { AuthError, type ApiClient } from "../shared/api";
import { download } from "../shared/browser";
import { message } from "../shared/errors";
import { Field, Notice, Segmented } from "../shared/components";
import { fmtInt } from "../shared/format";
import { errorText } from "../shared/query";
import { useTheme } from "../shared/theme";
import { cellKind, dataHash, preview, type Namespace } from "./data";
import {
  computeView,
  defaultLabelField,
  describeRule,
  edgeKey,
  EXPAND_LIMIT,
  exportView,
  fieldStats,
  graphQuery,
  labelFields,
  labelOf,
  matchesSearch,
  NODE_LIMITS,
  OPS,
  PSEUDO_FIELDS,
  splitKey,
  tableColor,
  UNARY,
  validRule,
  type FilterMode,
  type GraphEdge,
  type GraphNode,
  type GraphResponse,
  type Neighbor,
  type NodeLimit,
  type Op,
  type Rule,
  type SizeBy,
} from "./graph";
import { useGraphStore, withAll } from "./graph-store";
import { layoutMs, useForceLayout, useSigma, type Drawing } from "./use-sigma";

const MODES: [FilterMode, string][] = [
  ["dim", "Dim"],
  ["hide", "Hide"],
];
const SIZES: [SizeBy, string][] = [
  ["degree", "Degree"],
  ["uniform", "Uniform"],
];

/**
 * The namespace's graph tables drawn with sigma.js: rows as nodes
 * colored by table, relationships (edges) as arrows. GET /db/{ns}/_graph loads
 * a sample of every graph table and the edges among them; a node expands to its neighbors
 * (_neighbors). The filters (graph.ts) run in the browser: tables and edge
 * types switch off, rules on fields dim or hide, a search highlights. The
 * loaded rows live in a GraphStore (graph-store.ts), the drawing in use-sigma.ts.
 * It is drawn inside the Graph query's card (DataQuery.tsx), which draws what
 * a query returned (``data``) in place of the sample.
 */
export default function GraphView({ client, namespace, startNode, data }: {
  client: ApiClient;
  namespace: Namespace;
  /** This row (``table/id``) is loaded with its neighbors if need be, selected and centered */
  startNode?: string;
  /** What to draw, rather than a sample of the graph tables: a graph query's rows and edges */
  data?: GraphResponse;
}) {
  const ns = namespace.name;
  const graphTables = useMemo(() => namespace.tables.filter((t) => t.graph).map((t) => t.name), [namespace]);
  const { resolved } = useTheme();
  const dark = resolved === "dark";

  const { store, version, rebuild, addNode, addNeighbors, dismiss } = useGraphStore();
  const [limit, setLimit] = useState<NodeLimit>("500");
  const [info, setInfo] = useState<Pick<GraphResponse, "outside" | "more"> | null>(null);

  const [tables, setTables] = useState<Set<string>>(() => new Set(graphTables));
  const [types, setTypes] = useState<Set<string>>(() => new Set(namespace.edges.map((e) => e.name)));
  const [rules, setRules] = useState<Rule[]>([]);
  const [search, setSearch] = useState("");
  const [mode, setMode] = useState<FilterMode>("dim");
  const [hideIsolated, setHideIsolated] = useState(false);
  const [labelChoice, setLabelChoice] = useState<string | null>(null);
  const [sizeBy, setSizeBy] = useState<SizeBy>("degree");
  const [edgeLabels, setEdgeLabels] = useState(false);
  const [selected, setSelected] = useState<string | null>(null);

  // ------------------------------------------------------------ derived
  // From the store, as of ``version``: the deps name it in place of what is read.
  const stats = useMemo(() => fieldStats(store.nodes.values()), [store, version]);
  const labelField = labelChoice ?? defaultLabelField(stats, store.nodes.size);
  const labelOptions = useMemo(() => labelFields(stats), [stats]);
  const totalPerTable = useMemo(() => store.perTable(), [store, version]);
  const totalPerType = useMemo(() => store.perType(), [store, version]);
  // Every table and edge type seen: the namespace's first (colors stay put), then any an expansion brought.
  const allTables = useMemo(() => [...withAll(new Set(graphTables), totalPerTable.keys())], [graphTables, totalPerTable]);
  const allTypes = useMemo(() => [...withAll(new Set(namespace.edges.map((e) => e.name)), totalPerType.keys())], [namespace, totalPerType]);
  const colorOf = useCallback((table: string) => tableColor(allTables.indexOf(table), dark), [allTables, dark]);
  const view = useMemo(
    () => computeView(store.nodes, store.edges, { tables, types, rules, search, mode, hideIsolated }),
    [store, version, tables, types, rules, search, mode, hideIsolated],
  );

  // -------------------------------------------------------------- graph
  const layout = useForceLayout(store.graph);
  const { start: startLayout, drop: dropLayout } = layout;

  /** Adds what an expansion brought, switches its tables and edge types on, and lets the layout place it. */
  const grow = useCallback(
    (around: string, neighbors: Neighbor[]) => {
      const added = addNeighbors(around, neighbors);
      setTables((t) => withAll(t, added.tables));
      setTypes((t) => withAll(t, added.types));
      if (added.nodes) startLayout(1500);
      return added;
    },
    [addNeighbors, startLayout],
  );

  const expand = useCallback(
    async (key: string) => {
      const [table, id] = splitKey(key);
      try {
        const r = await client.get("/db/{namespace}/{table}/{row_id}/_neighbors", {
          params: { path: { namespace: ns, table, row_id: id }, query: { _direction: "both", _limit: EXPAND_LIMIT } },
        });
        const added = grow(key, r.neighbors);
        const more = r.next ? ` (the first ${r.neighbors.length}; there are more)` : "";
        toast.success(added.nodes || added.edges ? `Added ${added.nodes} rows and ${added.edges} relationships around ${id}${more}.` : `Nothing new around ${id}${more}.`);
      } catch (e) {
        if (!(e instanceof AuthError)) toast.error(`Could not expand ${id}`, { description: message(e) });
      }
    },
    [client, grow, ns],
  );

  // -------------------------------------------------------------- sigma
  const container = useRef<HTMLDivElement>(null);
  const drawing: Drawing = { nodes: store.nodes, view, colorOf, labelField, sizeBy, selected, edgeLabels, dark };
  const sigmaRef = useSigma(container, store.graph, drawing, { expand, select: setSelected });

  const center = useCallback(
    (key: string, ratio?: number) => {
      const sigma = sigmaRef.current;
      const at = sigma?.getNodeDisplayData(key);
      if (!sigma || !at) return;
      const camera = sigma.getCamera();
      camera.animate({ x: at.x, y: at.y, ratio: ratio ?? Math.min(camera.ratio, 0.4) }, { duration: 350 });
    },
    [sigmaRef],
  );

  // ------------------------------------------------------------ loading
  const graphLoad = client.useQuery(
    "get",
    "/db/{namespace}/_graph",
    { params: { path: { namespace: ns }, query: graphQuery(Number(limit)) } },
    // Each load is a new value, even one equal to the last, since loading again is asking for a fresh layout.
    { structuralSharing: false, enabled: !data },
  );
  const loaded = data ?? graphLoad.data;
  const error = errorText(graphLoad.error);
  const loading = graphLoad.isFetching;
  const reload = () => void graphLoad.refetch();
  // Each graph loaded replaces the one drawn: a fresh layout places it, from the whole of it in view.
  useEffect(() => {
    if (!loaded) return;
    setSelected(null);
    dropLayout();
    rebuild(loaded);
    // A query's result shows whole: its tables and edge types switch on.
    if (loaded === data) {
      setTables((t) => withAll(t, loaded.tables));
      setTypes((t) => withAll(t, loaded.edges.map((e) => e.type)));
    }
    setInfo({ outside: loaded.outside, more: loaded.more });
    sigmaRef.current?.getCamera().animatedReset({ duration: 0 });
    startLayout(layoutMs(store.graph.order));
  }, [loaded, data, dropLayout, startLayout, rebuild, sigmaRef, store]);

  // A row to start at: loaded with its neighbors when the sample missed it, then selected and centered.
  const started = useRef<string | undefined>(undefined);
  useEffect(() => {
    if (!startNode || loading || started.current === startNode || !info) return;
    started.current = startNode;
    (async () => {
      const [table, id] = splitKey(startNode);
      if (!store.nodes.has(startNode)) {
        try {
          const path = { namespace: ns, table, row_id: id };
          const row = await client.get("/db/{namespace}/{table}/{row_id}", { params: { path } });
          addNode({ table, row });
          const r = await client.get("/db/{namespace}/{table}/{row_id}/_neighbors", { params: { path, query: { _direction: "both", _limit: EXPAND_LIMIT } } });
          grow(startNode, r.neighbors);
        } catch (e) {
          if (!(e instanceof AuthError)) toast.error(`Could not load ${startNode}`, { description: message(e) });
          return;
        }
      }
      setTables((t) => withAll(t, [table]));
      setSelected(startNode);
      // After the layout has had a moment to place it.
      window.setTimeout(() => center(startNode, 0.3), 400);
    })();
  }, [startNode, loading, info, client, ns, store, addNode, grow, center]);

  // ------------------------------------------------------------- render
  const totalNodes = store.nodes.size;
  const totalEdges = store.edges.size;
  const toggle = (set: Set<string>, name: string) => {
    const next = new Set(set);
    if (next.has(name)) next.delete(name);
    else next.add(name);
    return next;
  };
  const selectedNode = selected ? store.nodes.get(selected) : undefined;
  const stamp = new Date().toISOString().slice(0, 19).replace(/[:T]/g, "-");

  const counts = (
    <>
      <span className="tabular-nums">
        {fmtInt(view.shownNodes)} of {fmtInt(totalNodes)} rows · {fmtInt(view.shownEdges)} of {fmtInt(totalEdges)} relationships
      </span>
      {info?.more.nodes && (
        <span title="Each table contributes its first rows by id, the limit shared among them. Raise the limit, or double-click a row to load its neighbors.">
          · a sample: raise the limit or expand rows for more
        </span>
      )}
      {info && info.outside > 0 && <span className="tabular-nums">· {fmtInt(info.outside)} relationships lead to rows not loaded</span>}
      {loading && <LoaderCircle className="size-3.5 animate-spin" aria-label="Loading" />}
    </>
  );
  // A query's result is downloaded and loaded again by its page: the controls are the sample's.
  const actions = !data && (
    <>
      <Field label="Sample size">
        <Select value={limit} onValueChange={(v) => setLimit(v as NodeLimit)}>
          <SelectTrigger size="sm" className="w-24" aria-label="Sample size" title="Rows loaded at most, shared evenly among the tables">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {NODE_LIMITS.map((n) => (
              <SelectItem key={n} value={n}>
                {n}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </Field>
      <Button type="button" variant="outline" size="sm" onClick={reload} disabled={loading} title="Load the graph again">
        <RotateCw aria-hidden="true" />
        Reload
      </Button>
      <Button
        type="button"
        variant="outline"
        size="sm"
        disabled={!totalNodes}
        title="Download the rows and relationships shown, as JSON"
        onClick={() => download(`${ns}-graph-${stamp}.json`, JSON.stringify(exportView(store.nodes, store.edges, view), null, 2))}
      >
        <Download aria-hidden="true" />
        JSON
      </Button>
    </>
  );

  const body = (
    <>
      {/* The legend doubles as the switches: Neo4j Browser's labels and relationship types. */}
      <div className="grid gap-1.5">
        <LegendRow label="Tables" onAll={() => setTables(new Set(allTables))} onNone={() => setTables(new Set())}>
          {allTables.map((t) => (
            <Chip
              key={t}
              on={tables.has(t)}
              color={colorOf(t)}
              label={t}
              count={`${fmtInt(view.perTable.get(t) ?? 0)}/${fmtInt(totalPerTable.get(t) ?? 0)}`}
              title={`${t}: ${fmtInt(view.perTable.get(t) ?? 0)} of ${fmtInt(totalPerTable.get(t) ?? 0)} loaded rows shown. Click to switch the table ${tables.has(t) ? "off" : "on"}.`}
              onClick={() => setTables((s) => toggle(s, t))}
            />
          ))}
          {!allTables.length && <span className="text-xs text-muted-foreground">No tables in this namespace yet.</span>}
        </LegendRow>
        <LegendRow label="Relationships" onAll={() => setTypes(new Set(allTypes))} onNone={() => setTypes(new Set())}>
          {allTypes.map((t) => (
            <Chip
              key={t}
              on={types.has(t)}
              label={t}
              line
              count={`${fmtInt(view.perType.get(t) ?? 0)}/${fmtInt(totalPerType.get(t) ?? 0)}`}
              title={`${t}: ${fmtInt(view.perType.get(t) ?? 0)} of ${fmtInt(totalPerType.get(t) ?? 0)} loaded shown. Click to switch them ${types.has(t) ? "off" : "on"}.`}
              onClick={() => setTypes((s) => toggle(s, t))}
            />
          ))}
          {!allTypes.length && <span className="text-xs text-muted-foreground">No relationships yet (PUT /db/{ns}/_edges).</span>}
        </LegendRow>
      </div>

      {/* Filters: a search that highlights, rules that dim or hide, the degree. */}
      <div className="flex flex-wrap items-center gap-2">
        <div className="relative">
          <Search className="pointer-events-none absolute top-2 left-2 size-4 text-muted-foreground" aria-hidden="true" />
          <Input
            aria-label="Search"
            placeholder="Highlight rows containing…"
            autoComplete="off"
            className="h-8 w-56 pl-8 text-xs"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
          {search && (
            <button type="button" aria-label="Clear search" className="absolute top-2 right-2 text-muted-foreground hover:text-foreground" onClick={() => setSearch("")}>
              <X className="size-4" />
            </button>
          )}
        </div>
        <RuleForm tables={allTables} fields={[...PSEUDO_FIELDS, ...stats.map((s) => s.name)]} onAdd={(r) => setRules((rs) => [...rs, r])} />
        <div className="ml-auto flex flex-wrap items-center gap-3">
          <Segmented<FilterMode> label="Rows failing a rule are" size="sm" options={MODES} value={mode} onChange={setMode} />
          <Label className="flex items-center gap-2 text-xs font-normal">
            <Switch checked={hideIsolated} onCheckedChange={setHideIsolated} aria-label="Hide isolated rows" />
            Hide isolated
          </Label>
        </div>
      </div>
      {rules.length > 0 && (
        <div className="flex flex-wrap items-center gap-1.5">
          {rules.map((r, i) => (
            <Badge key={i} variant="secondary" className="gap-1 pr-1 font-mono">
              {describeRule(r)}
              <button
                type="button"
                aria-label={`Remove rule ${describeRule(r)}`}
                className="grid size-4 place-items-center rounded-sm hover:bg-background"
                onClick={() => setRules((rs) => rs.filter((_, j) => j !== i))}
              >
                <X className="size-3" />
              </button>
            </Badge>
          ))}
          <Button type="button" variant="ghost" size="sm" className="h-6 px-2 text-xs" onClick={() => setRules([])}>
            Clear
          </Button>
        </div>
      )}

      {/* Styling and the layout. */}
      <div className="flex flex-wrap items-center gap-2">
        <Field label="Label by">
          <Select value={labelField} onValueChange={setLabelChoice}>
            <SelectTrigger size="sm" className="w-36 font-mono text-xs" aria-label="Label by">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {labelOptions.map((f) => (
                <SelectItem key={f} value={f} className="font-mono text-xs">
                  {f}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </Field>
        <Field label="Size by">
          <Segmented<SizeBy> label="Size by" size="sm" options={SIZES} value={sizeBy} onChange={setSizeBy} />
        </Field>
        <Label className="flex items-center gap-2 self-end pb-1.5 text-xs font-normal">
          <Switch checked={edgeLabels} onCheckedChange={setEdgeLabels} aria-label="Relationship labels" />
          Relationship labels
        </Label>
        <div className="ml-auto flex items-center gap-1 self-end">
          <Button
            type="button"
            variant="outline"
            size="sm"
            disabled={!totalNodes}
            onClick={() => (layout.on ? layout.stop() : layout.start())}
            title={layout.on ? "Pause the force layout" : "Run the force layout (ForceAtlas2)"}
          >
            {layout.on ? <Pause aria-hidden="true" /> : <Play aria-hidden="true" />}
            Layout
          </Button>
          <Button type="button" variant="outline" size="icon" className="size-8" aria-label="Zoom in" onClick={() => sigmaRef.current?.getCamera().animatedZoom({ duration: 200 })}>
            <ZoomIn />
          </Button>
          <Button type="button" variant="outline" size="icon" className="size-8" aria-label="Zoom out" onClick={() => sigmaRef.current?.getCamera().animatedUnzoom({ duration: 200 })}>
            <ZoomOut />
          </Button>
          <Button
            type="button"
            variant="outline"
            size="icon"
            className="size-8"
            aria-label="Fit the whole graph"
            title="Fit the whole graph"
            onClick={() => {
              sigmaRef.current?.setCustomBBox(null);
              sigmaRef.current?.getCamera().animatedReset({ duration: 300 });
            }}
          >
            <Maximize />
          </Button>
        </div>
      </div>

      {error && <Notice notice={{ text: error, kind: "error" }} />}

      <div className={cn("grid gap-3", selectedNode && "lg:grid-cols-[minmax(0,1fr)_19rem]")}>
        <div className="relative h-[62vh] min-h-[420px] overflow-hidden rounded-lg border bg-card">
          <div ref={container} className="absolute inset-0" />
          {!loading && !totalNodes && !error && (
            <div className="pointer-events-none absolute inset-0 grid place-items-center p-6 text-center text-sm text-muted-foreground">
              <p className="max-w-sm">
                {graphTables.length
                  ? "No rows in the tables yet. Write rows, then link them with relationships (PUT /db/{ns}/_edges)."
                  : "No tables that relationships can join. Create one on the namespace's page (New, Table), then link its rows with relationships."}
              </p>
            </div>
          )}
          {!loading && totalNodes > 0 && view.shownNodes === 0 && (
            <div className="pointer-events-none absolute inset-x-0 top-3 grid place-items-center">
              <span className="rounded-md border bg-background/90 px-3 py-1.5 text-xs text-muted-foreground shadow-sm backdrop-blur">
                {[...view.nodes.values()].some((s) => s === "dimmed") ? "Nothing matches the search" : "Everything is filtered out"}
              </span>
            </div>
          )}
        </div>
        {selectedNode && selected && (
          <NodeDetails
            ns={ns}
            node={selectedNode}
            nodeKey={selected}
            color={colorOf(selectedNode.table)}
            degree={view.degree.get(selected) ?? 0}
            edges={store.edgesOf(selected)}
            nodes={store.nodes}
            labelField={labelField}
            highlight={search}
            onClose={() => setSelected(null)}
            onExpand={() => expand(selected)}
            onDismiss={() => {
              dismiss(selected);
              setSelected(null);
            }}
            onGo={(key) => {
              setSelected(key);
              center(key);
            }}
          />
        )}
      </div>
      <p className="text-xs text-muted-foreground">
        Drag to pan, scroll to zoom, drag a row to move it. Click a row for its fields and relationships; double-click to load its
        neighbors. Switch tables and relationships off above; rules {mode === "dim" ? "gray out" : "hide"} the rows that fail them.
      </p>
    </>
  );

  // The page is the card: the counts and the load's controls make a toolbar.
  return (
    <div className="grid min-w-0 gap-3">
      <div className="flex flex-wrap items-end gap-2">
        <p className="flex flex-wrap items-center gap-x-2 gap-y-1 self-center text-sm text-muted-foreground">{counts}</p>
        <div className="ml-auto flex flex-wrap items-end gap-2">{actions}</div>
      </div>
      {body}
    </div>
  );
}

function LegendRow(props: { label: string; onAll(): void; onNone(): void; children: ReactNode }) {
  return (
    <div className="flex flex-wrap items-center gap-1.5 text-xs">
      <span className="mr-1 w-20 shrink-0 text-muted-foreground">{props.label}</span>
      {props.children}
      <span className="ml-1 flex gap-1 text-muted-foreground">
        <button type="button" className="hover:text-foreground hover:underline" onClick={props.onAll}>
          all
        </button>
        ·
        <button type="button" className="hover:text-foreground hover:underline" onClick={props.onNone}>
          none
        </button>
      </span>
    </div>
  );
}

/** A table or edge type in the legend: a switch with its swatch and how many of its items show. */
function Chip(props: { on: boolean; color?: string; line?: boolean; label: string; count: string; title: string; onClick(): void }) {
  const color = props.color ?? "var(--muted-foreground)";
  return (
    <button
      type="button"
      aria-pressed={props.on}
      title={props.title}
      className={cn(
        "inline-flex h-7 items-center gap-1.5 rounded-md border bg-background px-2 font-mono text-xs transition-colors hover:bg-muted",
        !props.on && "border-dashed text-muted-foreground",
      )}
      onClick={props.onClick}
    >
      {props.line ? (
        <span className="h-0.5 w-3 shrink-0 rounded-full" style={{ background: props.on ? color : "transparent", boxShadow: `inset 0 0 0 1px ${color}` }} aria-hidden="true" />
      ) : (
        <span className="size-2.5 shrink-0 rounded-full" style={{ background: props.on ? color : "transparent", boxShadow: `inset 0 0 0 1.5px ${color}` }} aria-hidden="true" />
      )}
      {props.label}
      <span className={cn("tabular-nums", props.on ? "text-muted-foreground" : "opacity-70")}>{props.count}</span>
    </button>
  );
}

/** Scope, field, operator and value of a new rule. */
function RuleForm(props: { tables: string[]; fields: string[]; onAdd(rule: Rule): void }) {
  const [table, setTable] = useState<string>("*");
  const [field, setField] = useState("");
  const [op, setOp] = useState<Op>("=");
  const [value, setValue] = useState("");
  const listId = "graph-fields";
  const rule: Rule = { table: table === "*" ? null : table, field: field.trim(), op, value };
  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (!validRule(rule)) return;
    props.onAdd(rule);
    setField("");
    setValue("");
  };
  return (
    <form className="flex flex-wrap items-center gap-1.5" onSubmit={submit}>
      <ListFilter className="size-4 text-muted-foreground" aria-hidden="true" />
      <Select value={table} onValueChange={setTable}>
        <SelectTrigger size="sm" className="h-8 w-32 text-xs" aria-label="Rule applies to">
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          <SelectItem value="*">any table</SelectItem>
          {props.tables.map((t) => (
            <SelectItem key={t} value={t} className="font-mono text-xs">
              {t}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
      <Input
        aria-label="Field"
        placeholder="field"
        list={listId}
        required
        pattern="[A-Za-z_][A-Za-z0-9_]*"
        autoComplete="off"
        className="h-8 w-32 font-mono text-xs"
        value={field}
        onChange={(e) => setField(e.target.value)}
      />
      <Select value={op} onValueChange={(v) => setOp(v as Op)}>
        <SelectTrigger size="sm" className="h-8 w-28 font-mono text-xs" aria-label="Operator">
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          {OPS.map((o) => (
            <SelectItem key={o} value={o} className="font-mono text-xs">
              {o}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
      {!UNARY.includes(op) && (
        <Input
          aria-label="Value"
          placeholder='value, 30, true, "30"'
          title='Numbers, true, false and null match as such; quote to match text: "30". A list field matches when it holds the value.'
          autoComplete="off"
          required
          className="h-8 w-40 font-mono text-xs"
          value={value}
          onChange={(e) => setValue(e.target.value)}
        />
      )}
      <Button type="submit" variant="secondary" size="sm" disabled={!validRule(rule)}>
        Add rule
      </Button>
      <datalist id={listId}>
        {props.fields.map((f) => (
          <option key={f} value={f} />
        ))}
      </datalist>
    </form>
  );
}

/** The selected row: its fields, the edges touching it, and what to do with it. */
function NodeDetails(props: {
  ns: string;
  node: GraphNode;
  nodeKey: string;
  color: string;
  degree: number;
  edges: GraphEdge[];
  nodes: Map<string, GraphNode>;
  labelField: string;
  highlight: string;
  onClose(): void;
  onExpand(): void;
  onDismiss(): void;
  onGo(key: string): void;
}) {
  const { node } = props;
  const fields = Object.entries(node.row).filter(([k]) => k !== "id");
  const matched = props.highlight.trim() && matchesSearch(node, props.highlight);
  const other = (e: GraphEdge) => (e.from === props.nodeKey ? e.to : e.from);
  const describe = (key: string) => {
    const n = props.nodes.get(key);
    return n ? labelOf(n, props.labelField, 28) : splitKey(key)[1];
  };
  const properties = (e: GraphEdge) =>
    Object.entries(e)
      .filter(([k]) => k !== "type" && k !== "from" && k !== "to")
      .map(([k, v]) => `${k}: ${preview(v, 30)}`)
      .join(", ");
  return (
    <div className="grid max-h-[62vh] min-h-0 content-start gap-3 overflow-y-auto rounded-lg border p-3 text-sm">
      <div className="flex items-start gap-2">
        <span className="mt-1.5 size-3 shrink-0 rounded-full" style={{ background: props.color }} aria-hidden="true" />
        <div className="min-w-0 flex-1">
          <div className="truncate font-mono text-[13px] font-medium" title={node.row.id}>
            {node.row.id}
          </div>
          <div className="flex flex-wrap items-center gap-1.5 text-xs text-muted-foreground">
            <Badge variant="secondary" className="gap-1 font-mono font-normal">
              <Waypoints aria-hidden="true" />
              {node.table}
            </Badge>
            <span className="tabular-nums">{props.degree} {props.degree === 1 ? "relationship" : "relationships"} shown</span>
            {matched && <Badge variant="outline">matches the search</Badge>}
          </div>
        </div>
        <Button type="button" variant="ghost" size="icon" className="size-7 shrink-0" aria-label="Close" onClick={props.onClose}>
          <X />
        </Button>
      </div>
      <div className="flex flex-wrap gap-1.5">
        <Button type="button" size="sm" variant="outline" onClick={props.onExpand} title="Load the rows one relationship away (double-click does too)">
          <Expand aria-hidden="true" />
          Expand
        </Button>
        <Button type="button" size="sm" variant="outline" onClick={props.onDismiss} title="Take the row out of the view (it stays in the table)">
          <EyeOff aria-hidden="true" />
          Dismiss
        </Button>
        <a className={buttonVariants({ variant: "outline", size: "sm" })} href={dataHash({ namespace: props.ns, table: node.table })} title="Open the table's rows">
          <Table2 aria-hidden="true" />
          Table
        </a>
      </div>
      {fields.length > 0 ? (
        <dl className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-3 gap-y-1 text-xs">
          {fields.map(([k, v]) => (
            <div key={k} className="contents">
              <dt className="truncate font-mono text-muted-foreground" title={k}>
                {k}
              </dt>
              <dd className="min-w-0">
                <Value value={v} />
              </dd>
            </div>
          ))}
        </dl>
      ) : (
        <p className="text-xs text-muted-foreground">No fields besides the id.</p>
      )}
      <div className="grid gap-1">
        <span className="text-xs font-medium">Relationships · {props.edges.length}</span>
        {!props.edges.length && <p className="text-xs text-muted-foreground">None loaded. Expand the row to load them.</p>}
        {props.edges.map((e) => {
          const target = other(e);
          const out = e.from === props.nodeKey;
          const self = e.from === e.to;
          const extra = properties(e);
          return (
            <button
              key={edgeKey(e)}
              type="button"
              className="grid grid-cols-[auto_auto_minmax(0,1fr)] items-center gap-1.5 rounded-md border px-2 py-1 text-left text-xs transition-colors hover:bg-muted"
              title={`${e.type}: ${e.from} → ${e.to}${extra ? ` (${extra})` : ""}. Click to select the other end.`}
              onClick={() => props.onGo(target)}
            >
              {self ? <ArrowLeftRight className="size-3 text-muted-foreground" aria-label="to itself" /> : out ? <ArrowRight className="size-3 text-muted-foreground" aria-label="out" /> : <ArrowRight className="size-3 rotate-180 text-muted-foreground" aria-label="in" />}
              <span className="font-mono text-muted-foreground">{e.type}</span>
              <span className="truncate">
                {describe(target)}
                <span className="ml-1 text-muted-foreground">{splitKey(target)[0]}</span>
              </span>
              {extra && <span className="col-span-3 truncate font-mono text-[11px] text-muted-foreground">{extra}</span>}
            </button>
          );
        })}
      </div>
    </div>
  );
}

/** One field's value, by its JSON kind. */
function Value({ value }: { value: unknown }) {
  switch (cellKind(value)) {
    case "null":
      return <span className="font-mono text-muted-foreground italic">null</span>;
    case "boolean":
      return <span className={cn("font-mono", value ? "text-good-text" : "text-muted-foreground")}>{String(value)}</span>;
    case "number":
      return <span className="font-mono tabular-nums">{String(value)}</span>;
    case "string":
      return (
        <span className="break-words" title={value as string}>
          {value as string}
        </span>
      );
    default:
      return (
        <span className="block truncate font-mono text-muted-foreground" title={JSON.stringify(value, null, 2)}>
          {preview(value, 80)}
        </span>
      );
  }
}
