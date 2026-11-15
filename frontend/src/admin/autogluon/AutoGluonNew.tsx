// The AutoGluon section's New predictor page, top to bottom: what to predict
// (a column, or a series ahead), the data (with its summary), the columns,
// and how to train; Train submits the kind's job and opens the predictor's
// page. Retrain (#/autogluon/new?from=<job id>) starts from that job's
// settings.

import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { GraduationCap, TriangleAlert } from "lucide-react";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { useAction } from "../../shared/action";
import { useApi } from "../../shared/api";
import { Disclosure, Field, Notice, Segmented, SelectField, SubmitButton } from "../../shared/components";
import { formSchema } from "../../shared/form-schema";
import { errorText } from "../../shared/query";
import { SchemaForm, type SchemaFormHandle } from "../../shared/schema-form";
import type { Job, JobType } from "../../shared/types";
import { DetailSkeleton } from "../layout";
import {
  advancedSchema,
  autogluonHref,
  baseName,
  defaultName,
  formProblem,
  idLikeColumns,
  isNumber,
  KIND_OF_JOB,
  problemGuess,
  qualityOf,
  QUALITIES,
  seriesParams,
  SET_HERE,
  severalSeries,
  tabularParams,
  TIME_LIMITS,
  timestampCandidates,
  TRAIN_JOB,
  type Kind,
  type Quality,
  type SeriesForm,
  type TabularForm,
} from "./autogluon";
import { DataPicker, SummaryView, useSummary } from "./AutoGluonData";

type Form = TabularForm & SeriesForm;

const EMPTY: Form = {
  file: "", name: "", timeLimit: "300", quality: "fast", advanced: {},
  label: "", ignored: [],
  timestamp: "", target: "", id: "", steps: "7", freq: "", known: [],
};

const str = (v: unknown) => (typeof v === "string" ? v : "");

/** The form as a job left it (retraining it), or empty. */
function startFrom(job: Job | null): { kind: Kind; form: Form } {
  const kind = (job && KIND_OF_JOB[job.job ?? ""]) || "tabular";
  if (!job) return { kind, form: EMPTY };
  const p = job.params;
  const advanced = Object.fromEntries(Object.entries(p).filter(([k, v]) => !SET_HERE[kind].includes(k) && v !== null));
  return {
    kind,
    form: {
      ...EMPTY,
      file: baseName(str(p.train_file)),
      name: str(p.name),
      timeLimit: String(p.time_limit ?? EMPTY.timeLimit),
      quality: qualityOf(kind, p.presets),
      advanced,
      label: str(p.label),
      ignored: Array.isArray(p.ignored_columns) ? (p.ignored_columns as string[]) : [],
      timestamp: str(p.timestamp_column),
      target: str(p.target),
      id: str(p.id_column),
      steps: String(p.prediction_length ?? EMPTY.steps),
      freq: str(p.freq),
      known: Array.isArray(p.known_covariates) ? (p.known_covariates as string[]) : [],
    },
  };
}

export function NewPredictor({ from, types }: { from: string | null; types: JobType[] }) {
  const client = useApi();
  const source = client.useQuery("get", "/jobs/{id}", { params: { path: { id: from ?? "" } } }, { enabled: Boolean(from) });
  if (from && source.error) return <Notice notice={{ text: errorText(source.error), kind: "error" }} />;
  if (from && !source.data) return <DetailSkeleton />;
  return <NewForm types={types} source={source.data ?? null} />;
}

function Step({ n, title, hint, children }: { n: number; title: string; hint?: ReactNode; children: ReactNode }) {
  return (
    <section className="grid gap-3 border-t pt-5 first:border-t-0 first:pt-0" aria-label={title}>
      <div>
        <h3 className="text-sm font-semibold">
          <span className="mr-2 inline-grid size-5 place-items-center rounded-full bg-muted text-xs tabular-nums">{n}</span>
          {title}
        </h3>
        {hint && <p className="mt-1 text-xs text-muted-foreground">{hint}</p>}
      </div>
      {children}
    </section>
  );
}

/** Checkboxes, one per column, wrapping. */
function Columns(props: { label: string; hint?: ReactNode; columns: string[]; checked: string[]; onChange(checked: string[]): void }) {
  if (!props.columns.length) return null;
  return (
    <div className="grid gap-2">
      <span className="text-sm leading-none font-medium">
        {props.label}
        {props.hint && <span className="ml-2 text-xs font-normal text-muted-foreground">{props.hint}</span>}
      </span>
      <div className="flex flex-wrap gap-x-4 gap-y-2">
        {props.columns.map((c) => (
          <label key={c} className="flex items-center gap-2 font-mono text-[13px]">
            <Checkbox
              checked={props.checked.includes(c)}
              onCheckedChange={(on) => props.onChange(on === true ? [...props.checked, c] : props.checked.filter((x) => x !== c))}
            />
            {c}
          </label>
        ))}
      </div>
    </div>
  );
}

function NewForm({ types, source }: { types: JobType[]; source: Job | null }) {
  const client = useApi();
  const start = useMemo(() => startFrom(source), [source]);
  const [kind, setKind] = useState<Kind>(start.kind);
  const [f, setF] = useState<Form>(start.form);
  const set = (patch: Partial<Form>) => setF((prev) => ({ ...prev, ...patch }));
  const summary = useSummary(f.file);
  const s = summary.data ?? null;
  const names = s?.columns.map((c) => c.name) ?? [];
  const job = types.find((t) => t.name === TRAIN_JOB[kind]);
  const schema = useMemo(() => (job ? advancedSchema(formSchema(job.params), kind) : null), [job, kind]);
  const advancedForm = useRef<SchemaFormHandle>(null);

  // A new file's columns: the likely IDs ticked to be ignored, a date column as the timestamp; a retrain keeps its own.
  const [defaultsFor, setDefaultsFor] = useState(source ? start.form.file : "");
  useEffect(() => {
    if (!s || defaultsFor === f.file) return;
    setDefaultsFor(f.file);
    const has = (c: string) => s.columns.some((x) => x.name === c);
    const label = has(f.label) ? f.label : "";
    set({
      label,
      ignored: idLikeColumns(s, [label]),
      timestamp: has(f.timestamp) ? f.timestamp : (s.columns.find((c) => /^(DATE|TIMESTAMP)/.test(c.type))?.name ?? ""),
      target: has(f.target) ? f.target : "",
      id: has(f.id) ? f.id : "",
      known: f.known.filter(has),
    });
  }, [s, f.file]);

  const train = useAction(
    async () => {
      const problem = formProblem(kind, f);
      if (problem) throw new Error(problem);
      if (!(advancedForm.current?.validate() ?? true)) throw new Error("Some settings under Advanced need fixing");
      const params = kind === "tabular" ? tabularParams(f) : seriesParams(f);
      const row = await client.post("/jobs", { body: { job: TRAIN_JOB[kind], params } });
      location.hash = autogluonHref(row.id);
    },
    { notice: true },
  );

  const column = (name: string) => s?.columns.find((c) => c.name === name);
  const labelColumn = column(f.label);
  const tsColumn = column(f.timestamp);
  const mixed = kind === "timeseries" && s && !f.id && severalSeries(s.rows, tsColumn);
  const placeholder = defaultName(f.file, kind === "tabular" ? f.label : f.target);

  return (
    <Card className="gap-5">
      <CardHeader>
        <CardTitle>{source ? "Retrain a predictor" : "New predictor"}</CardTitle>
        <CardDescription>
          AutoGluon tries many models on your table and keeps the best, or a blend of them. The predictor is then served as <code>autogluon:&lt;job id&gt;</code>, like any model here.
          {source && ` These are the settings job ${source.id} trained with; the new one gets a new ID.`}
        </CardDescription>
      </CardHeader>
      <CardContent className="grid gap-5">
        <Step n={1} title="What to predict">
          <Segmented
            label="Kind of predictor"
            value={kind}
            onChange={(k) => {
              setKind(k);
              set({ advanced: {} });
            }}
            options={[
              ["tabular", "Predict a column"],
              ["timeseries", "Forecast a series"],
            ]}
          />
          <p className="text-xs text-muted-foreground">
            {kind === "tabular"
              ? "From the other columns of a row: a category (churn, a label) or a number (a price)."
              : "The next values of one or many series, from their history: a row per time step. Every quality uses small pretrained forecasting models (Chronos-2, Toto-2) beside the statistical ones; the first training downloads them from the Hugging Face Hub."}
          </p>
        </Step>

        <Step n={2} title="Data">
          <DataPicker label="Training data" value={f.file} onChange={(file) => set({ file })} />
          {summary.error && <Notice notice={{ text: errorText(summary.error), kind: "error" }} />}
          {f.file && !s && !summary.error && <p className="text-sm text-muted-foreground">Reading the file…</p>}
          {s && <SummaryView summary={s} />}
        </Step>

        {s && kind === "tabular" && (
          <Step n={3} title="The column to predict">
            <SelectField
              label="Predict"
              className="max-w-sm"
              value={f.label}
              onChange={(label) => set({ label, ignored: idLikeColumns(s, [label]) })}
              options={names.map((n) => ({ value: n, label: n }))}
              placeholder="Pick a column"
              hint={labelColumn ? `${problemGuess(labelColumn).text}. AutoGluon decides in the end.` : "Two values make yes/no classification, a few make classes, many numbers make regression."}
            />
            <Columns
              label="Ignore these columns"
              hint="IDs and the like, which say nothing about new rows; ticked: a value per row"
              columns={names.filter((n) => n !== f.label)}
              checked={f.ignored}
              onChange={(ignored) => set({ ignored })}
            />
          </Step>
        )}

        {s && kind === "timeseries" && (
          <Step n={3} title="The series">
            <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
              <SelectField label="Timestamp" value={f.timestamp} onChange={(timestamp) => set({ timestamp })} options={timestampCandidates(s.columns).map((c) => ({ value: c.name, label: c.name }))} placeholder="Pick a column" />
              <SelectField label="Value to forecast" value={f.target} onChange={(target) => set({ target })} options={s.columns.filter(isNumber).map((c) => ({ value: c.name, label: c.name }))} placeholder="Pick a number column" />
              <SelectField
                label="Series ID"
                value={f.id}
                onChange={(id) => set({ id })}
                none="None: the file is one series"
                options={names.filter((n) => n !== f.timestamp && n !== f.target).map((n) => ({ value: n, label: n }))}
              />
              <Field label="Steps ahead" hint="time steps to forecast">
                <Input type="number" min={1} step={1} value={f.steps} onChange={(e) => set({ steps: e.target.value })} />
              </Field>
            </div>
            {mixed && (
              <p className="flex items-start gap-1.5 text-sm">
                <TriangleAlert className="mt-0.5 size-4 shrink-0 text-warning" aria-hidden="true" />
                Its timestamps repeat ({tsColumn?.distinct} values over {s.rows} rows): the file looks like several series. Pick the column that tells them apart as the series ID.
              </p>
            )}
          </Step>
        )}

        {s && (
          <Step n={4} title="Train">
            <div className="grid gap-3 sm:grid-cols-[minmax(0,1fr)_auto_auto] sm:items-end">
              <Field label="Name" hint="in this list and the model lists">
                <Input value={f.name} placeholder={placeholder} maxLength={200} onChange={(e) => set({ name: e.target.value })} />
              </Field>
              <SelectField label="Time limit" className="w-36" value={f.timeLimit} onChange={(timeLimit) => set({ timeLimit })} options={TIME_LIMITS.map(([value, label]) => ({ value, label }))} />
              <div className="grid gap-2">
                <span className="text-sm leading-none font-medium">Quality</span>
                <Segmented label="Quality" value={f.quality} onChange={(quality: Quality) => set({ quality })} options={QUALITIES} />
              </div>
            </div>
            <p className="text-xs text-muted-foreground">
              Better quality tries more models and blends them, for longer: Fast is AutoGluon's medium_quality, Best its best_quality. Training stops at the time limit with the best found so far.
            </p>
            <Disclosure label="Advanced">
              <div className="grid gap-3 pt-2">
                {kind === "timeseries" && (
                  <>
                    <Field label="Frequency" hint="a pandas frequency (D, h, W, MS); empty: worked out from the timestamps" className="max-w-xs">
                      <Input value={f.freq} placeholder="inferred" onChange={(e) => set({ freq: e.target.value })} />
                    </Field>
                    <Columns
                      label="Known covariates"
                      hint="columns known ahead (holidays, prices): a forecast then needs their future values"
                      columns={names.filter((n) => ![f.timestamp, f.target, f.id].includes(n))}
                      checked={f.known}
                      onChange={(known) => set({ known })}
                    />
                  </>
                )}
                {schema && <SchemaForm ref={advancedForm} schema={schema} value={f.advanced} onChange={(advanced: Record<string, unknown>) => set({ advanced })} />}
              </div>
            </Disclosure>
            <div className="flex flex-wrap items-center gap-3">
              <SubmitButton type="button" busy={train.busy} disabled={Boolean(formProblem(kind, f))} icon={<GraduationCap aria-hidden="true" />} onClick={train.run}>
                Train
              </SubmitButton>
              <span className="text-sm text-muted-foreground">{formProblem(kind, f) || "It runs as a job; its page shows the progress."}</span>
            </div>
            <Notice notice={train.notice} />
          </Step>
        )}
      </CardContent>
    </Card>
  );
}
