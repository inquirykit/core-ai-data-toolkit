// One output per task: the result (the task's output model) drawn for
// reading, with the input that produced it at hand.
import { TriangleAlert } from "lucide-react";
import { motion } from "motion/react";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { cn } from "@/lib/utils";
import { answerSpan, decisionSummary, norm, rankedScores, similarityMatrix } from "./build";

export interface OutputProps {
  result: unknown;
  input: unknown;
}

type Obj = Record<string, unknown>;
const obj = (value: unknown): Obj => (value && typeof value === "object" ? (value as Obj) : {});
const pct = (p: number) => `${(p * 100).toFixed(p > 0 && p < 0.001 ? 2 : 1)}%`;

/** JSON shown as is: the raw response, and results with no better form. */
export const preClass = "max-h-[480px] overflow-auto rounded-lg border bg-muted/50 p-3 font-mono text-xs leading-relaxed";
// A reply or a transcript: text as written, on a soft tint.
const textClass = "rounded-lg bg-muted px-3 py-2.5 text-sm whitespace-pre-wrap wrap-anywhere";
const headlineClass = "text-[15px] wrap-anywhere";

/** Labelled probability bars, one series, longest first; ``top`` is called out. */
function ProbBars({ rows, top, limit = 12 }: { rows: [string, number][]; top?: string; limit?: number }) {
  const shown = rows.slice(0, limit);
  return (
    <div className="grid gap-1.5">
      {shown.map(([label, p]) => (
        <div
          key={label}
          className="grid grid-cols-[minmax(80px,30%)_1fr_56px] items-center gap-2.5 text-sm"
          title={`${label}: ${pct(p)}`}
        >
          <span className={cn("wrap-anywhere text-muted-foreground", label === top && "font-semibold text-foreground")}>{label}</span>
          <span className="h-2 overflow-hidden rounded-full bg-grid">
            {/* Grows in from nothing, then follows the value. */}
            <motion.span
              className="block h-full min-w-0.5 rounded-r-full bg-series-1"
              initial={{ width: 0 }}
              animate={{ width: `${Math.max(0, Math.min(1, p)) * 100}%` }}
              transition={{ duration: 0.6, ease: [0.22, 1, 0.36, 1] }}
            />
          </span>
          <span className="text-right text-muted-foreground tabular-nums">{pct(p)}</span>
        </div>
      ))}
      {rows.length > limit && (
        <p className="text-xs text-muted-foreground">
          and {rows.length - limit} more below {pct(shown[shown.length - 1][1])}
        </p>
      )}
    </div>
  );
}

export function TextOutput({ result }: OutputProps) {
  const text = String(obj(result).text ?? "");
  return <div className={textClass}>{text || <span className="text-muted-foreground">(an empty reply)</span>}</div>;
}

export function ClassificationOutput({ result }: OutputProps) {
  const r = obj(result);
  const label = String(r.label ?? "");
  return (
    <>
      <p className={headlineClass}>
        <strong>{label}</strong> <span className="text-muted-foreground">{typeof r.confidence === "number" ? pct(r.confidence) : ""}</span>
      </p>
      <ProbBars rows={rankedScores(obj(r.scores) as Record<string, number>)} top={label} />
    </>
  );
}

export function QuestionAnsweringOutput({ result, input }: OutputProps) {
  const r = obj(result);
  const answer = String(r.answer ?? "");
  const context = String(obj(input).context ?? "");
  const span = answerSpan(context, answer);
  return (
    <>
      <p className={headlineClass}>
        <strong>{answer || "(no answer)"}</strong>{" "}
        <span className="text-muted-foreground">{typeof r.confidence === "number" ? `${pct(r.confidence)} confident` : ""}</span>
      </p>
      {span ? (
        <blockquote className="border-l-[3px] border-grid px-3 py-2.5 text-sm whitespace-pre-wrap text-muted-foreground wrap-anywhere">
          {span[0]}
          <mark className="rounded-[3px] bg-warning/35 px-px text-foreground">{span[1]}</mark>
          {span[2]}
        </blockquote>
      ) : (
        context && <p className="text-sm text-muted-foreground">The answer does not appear word for word in the context.</p>
      )}
    </>
  );
}

const MAX_COMPARED = 10;

export function EmbeddingOutput({ result, input }: OutputProps) {
  const vectors = (obj(result).embeddings as number[][] | undefined) ?? [];
  const texts = (obj(input).texts as string[] | undefined) ?? [];
  const compared = vectors.slice(0, MAX_COMPARED);
  const matrix = compared.length > 1 ? similarityMatrix(compared) : null;
  const tokens = obj(result).tokens;
  return (
    <>
      <p className={headlineClass}>
        <strong>
          {vectors.length} vector{vectors.length === 1 ? "" : "s"} of {vectors[0]?.length ?? 0} dimensions
        </strong>
        {typeof tokens === "number" && tokens > 0 && <span className="text-muted-foreground"> · {tokens} tokens read</span>}
      </p>
      <div className="rounded-lg border">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>#</TableHead>
              <TableHead>Text</TableHead>
              <TableHead className="text-right">Length</TableHead>
              <TableHead>First values</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {vectors.map((v, i) => (
              <TableRow key={i}>
                <TableCell className="text-muted-foreground tabular-nums">{i + 1}</TableCell>
                <TableCell className="max-w-80 min-w-40 whitespace-normal wrap-anywhere">{texts[i]}</TableCell>
                <TableCell className="text-right tabular-nums" title="Euclidean norm; 1 when the model normalizes its vectors">
                  {norm(v).toFixed(3)}
                </TableCell>
                <TableCell className="font-mono text-xs text-muted-foreground">
                  [{v.slice(0, 4).map((x) => x.toFixed(3)).join(", ")}
                  {v.length > 4 ? ", …" : ""}]
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </div>
      {matrix && (
        <>
          <h4 className="mt-1.5 text-sm font-semibold">
            Cosine similarity
            <span className="font-normal text-muted-foreground">
              {" "}
              1 is the same meaning; darker is closer{vectors.length > MAX_COMPARED ? `; first ${MAX_COMPARED} texts` : ""}
            </span>
          </h4>
          <div className="w-fit max-w-full rounded-lg border">
            <Table className="w-auto">
              <TableHeader>
                <TableRow className="hover:bg-transparent">
                  <TableHead />
                  {compared.map((_, j) => (
                    <TableHead key={j} className="text-center tabular-nums">
                      {j + 1}
                    </TableHead>
                  ))}
                </TableRow>
              </TableHeader>
              <TableBody>
                {matrix.map((row, i) => (
                  <TableRow key={i} className="hover:bg-transparent">
                    <TableHead className="text-center tabular-nums">{i + 1}</TableHead>
                    {row.map((s, j) => (
                      <TableCell
                        key={j}
                        className="min-w-[52px] text-center tabular-nums"
                        title={`${i + 1} “${texts[i] ?? ""}” vs ${j + 1} “${texts[j] ?? ""}”: ${s.toFixed(3)}`}
                        // One hue, light to dark with similarity; capped so the number stays readable.
                        style={{ background: `color-mix(in oklab, var(--series-1) ${Math.round(Math.max(0, s) * 45)}%, transparent)` }}
                      >
                        {s.toFixed(2)}
                      </TableCell>
                    ))}
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>
        </>
      )}
    </>
  );
}

export function TranscriptOutput({ result }: OutputProps) {
  const r = obj(result);
  return (
    <>
      {typeof r.language === "string" && r.language && <p className="text-sm text-muted-foreground">Language: {r.language}</p>}
      <div className={textClass}>{String(r.text ?? "") || <span className="text-muted-foreground">(no speech found)</span>}</div>
    </>
  );
}

export function DecisionOutput({ result }: OutputProps) {
  const answers = obj(obj(result).answers);
  return (
    <div className="stagger grid gap-2.5">
      {Object.entries(answers).map(([name, answer]) => {
        const s = decisionSummary(obj(answer));
        return (
          <section key={name} className="grid gap-2 rounded-lg border bg-card p-3">
            <p className={headlineClass}>
              <span className="text-muted-foreground">{name}</span> <strong>{s.headline}</strong>
            </p>
            {s.flags.map((f) => (
              <p key={f} className="flex items-start gap-1.5 text-xs text-muted-foreground">
                <TriangleAlert className="mt-px size-3.5 shrink-0 text-warning" aria-hidden="true" />
                {f}
              </p>
            ))}
            {s.probs.length > 0 && <ProbBars rows={s.probs} top={s.probs[0][0]} />}
          </section>
        );
      })}
    </div>
  );
}

/** Any other result: its text fields as text, the rest as JSON. */
export function GenericOutput({ result }: OutputProps) {
  const r = obj(result);
  const entries = Object.entries(r);
  if (entries.length && entries.every(([, v]) => typeof v === "string")) {
    return (
      <dl className="grid grid-cols-[max-content_minmax(0,1fr)] items-baseline gap-x-5 gap-y-2 text-sm">
        {entries.map(([k, v]) => (
          <div key={k} className="contents">
            <dt className="text-muted-foreground">{k}</dt>
            <dd className={cn(textClass, "min-w-0")}>{String(v)}</dd>
          </div>
        ))}
      </dl>
    );
  }
  return <pre className={preClass}>{JSON.stringify(result, null, 2)}</pre>;
}
