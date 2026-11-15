import { describe, expect, it } from "vitest";
import type { Job } from "../../shared/types";
import {
  advancedSchema,
  autogluonFromHash,
  cleanFileName,
  formProblem,
  headlineScore,
  idLikeColumns,
  predictorOf,
  problemGuess,
  qualityOf,
  scoreText,
  seriesParams,
  severalSeries,
  tabularParams,
  timestampCandidates,
  type ColumnSummary,
  type SeriesForm,
  type TabularForm,
  type TrainResult,
} from "./autogluon";

const column = (name: string, type: string, distinct: number | null): ColumnSummary => ({ name, type, distinct, min: null, max: null, mean: null, missing: 0 });

describe("the section's pages", () => {
  it("are read from the hash", () => {
    expect(autogluonFromHash("#/autogluon")).toEqual({ page: "list" });
    expect(autogluonFromHash("#/autogluon/new")).toEqual({ page: "new", from: null });
    expect(autogluonFromHash("#/autogluon/new?from=ab12cd34")).toEqual({ page: "new", from: "ab12cd34" });
    expect(autogluonFromHash("#/autogluon/ab12cd34")).toEqual({ page: "predictor", id: "ab12cd34" });
  });
});

describe("a file's name for upload", () => {
  it("keeps to letters, digits, dots, underscores and dashes", () => {
    expect(cleanFileName("Churn data (1).csv")).toBe("Churn_data_1.csv");
    expect(cleanFileName("Ventes été 2026.CSV")).toBe("Ventes_ete_2026.csv");
    expect(cleanFileName(".hidden.parquet")).toBe("hidden.parquet");
    expect(cleanFileName("())).csv")).toBe("data.csv");
    expect(cleanFileName("noext")).toBe("noext");
  });
});

describe("the column to predict", () => {
  it("is guessed as yes/no, classes or a quantity", () => {
    expect(problemGuess(column("churn", "VARCHAR", 2)).problem).toBe("binary");
    expect(problemGuess(column("city", "VARCHAR", 7))).toEqual({ problem: "multiclass", text: "About 7 values: classification into 7 classes" });
    expect(problemGuess(column("stars", "BIGINT", 5)).problem).toBe("multiclass");
    expect(problemGuess(column("visits", "BIGINT", 400)).problem).toBe("regression");
    expect(problemGuess(column("income", "DOUBLE", 12)).problem).toBe("regression");
  });

  it("makes columns with a value per row likely IDs, with a margin for the approximate count", () => {
    const summary = {
      rows: 1000,
      columns: [column("id", "BIGINT", 962), column("email", "VARCHAR", 1003), column("income", "DOUBLE", 1000), column("city", "VARCHAR", 40), column("label", "VARCHAR", 990)],
    };
    expect(idLikeColumns(summary, ["label"])).toEqual(["id", "email"]);
    // Too few rows to tell: every column would look like one.
    expect(idLikeColumns({ rows: 10, columns: [column("id", "BIGINT", 10)] })).toEqual([]);
  });
});

describe("time-series columns", () => {
  it("offer dates first for the timestamp", () => {
    const columns = [column("store", "VARCHAR", 3), column("day", "DATE", 90), column("sold", "BIGINT", 30), column("at", "TIMESTAMP", 90)];
    expect(timestampCandidates(columns).map((c) => c.name)).toEqual(["day", "at", "store", "sold"]);
  });

  it("warn of several series when timestamps repeat", () => {
    expect(severalSeries(180, column("day", "DATE", 90))).toBe(true);
    expect(severalSeries(90, column("day", "DATE", 88))).toBe(false); // one series, counted approximately
    expect(severalSeries(90, undefined)).toBe(false);
  });
});

describe("scores", () => {
  it("show an error positive, lower is better", () => {
    expect(scoreText(-14.30706, true)).toEqual({ text: "14.307", hint: "lower is better" });
    expect(scoreText(0.98751, false)).toEqual({ text: "0.988", hint: "higher is better" });
    expect(scoreText(null, true)).toEqual({ text: "not scored", hint: "" });
    // A predictor trained before lower_is_better existed: shown as AutoGluon reported it.
    expect(scoreText(-0.25, undefined)).toEqual({ text: "-0.250", hint: "higher is better" });
  });

  it("lead with held-out rows, else the validation score", () => {
    const result = { test_score: null, validation_score: -0.1 } as TrainResult;
    expect(headlineScore(result)).toEqual({ value: -0.1, on: "validation" });
    expect(headlineScore({ ...result, test_score: 0.9 })).toEqual({ value: 0.9, on: "test" });
  });
});

describe("job params", () => {
  const common = { file: "churn.csv", name: " ", timeLimit: "300", quality: "good" as const, advanced: { test_share: 0.3 } };

  it("for a tabular predictor: the column, the ignored ones, the quality as presets", () => {
    const form: TabularForm = { ...common, label: "churn", ignored: ["id", "churn"] };
    expect(tabularParams(form)).toEqual({
      test_share: 0.3, train_file: "churn.csv", label: "churn", time_limit: 300, presets: "good_quality", ignored_columns: ["id"],
    });
    expect(formProblem("tabular", { ...form, label: "" })).toBe("Pick the column to predict.");
    expect(formProblem("tabular", form)).toBe("");
  });

  it("for a forecaster: one series without an ID column, the quality as its own presets", () => {
    const form: SeriesForm = { ...common, name: "Sales", timestamp: "day", target: "sold", id: "", steps: "7", freq: "", known: ["promo", "sold"] };
    expect(seriesParams(form)).toEqual({
      test_share: 0.3, train_file: "churn.csv", timestamp_column: "day", target: "sold", prediction_length: 7, name: "Sales",
      time_limit: 300, presets: "high_quality", known_covariates: ["promo"],
    });
    expect(seriesParams({ ...form, id: "store", freq: "D" })).toMatchObject({ id_column: "store", freq: "D" });
    expect(formProblem("timeseries", { ...form, steps: "0" })).toBe("Steps ahead is a whole number, 1 or more.");
    expect(formProblem("timeseries", { ...form, id: "day" })).toMatch(/three different columns/);
    expect(qualityOf("timeseries", "high_quality")).toBe("good");
  });

  it("under Advanced: the rest of the job's params", () => {
    const schema = { properties: { train_file: {}, label: {}, test_share: {}, eval_metric: {} }, required: ["train_file", "label"] };
    expect(advancedSchema(schema, "tabular")).toEqual({ properties: { test_share: {}, eval_metric: {} }, required: [] });
  });
});

describe("a predictor", () => {
  const job = (over: Partial<Job>): Job =>
    ({ id: "ab12cd34", job: "autogluon-tabular", status: "succeeded", params: { train_file: "/data/datasets/churn.csv", label: "churn" }, result: null, ...over }) as Job;

  it("is named by its file and column when not given a name", () => {
    expect(predictorOf(job({}))).toMatchObject({ kind: "tabular", name: "churn · churn", predicts: "churn" });
    expect(predictorOf(job({ params: { train_file: "x.csv", label: "y", name: "Churn" } }))?.name).toBe("Churn");
    const series = predictorOf(job({ job: "autogluon-timeseries", params: { train_file: "sales.csv", target: "sold", prediction_length: 7 } }));
    expect(series).toMatchObject({ kind: "timeseries", predicts: "sold, 7 steps ahead" });
    expect(predictorOf(job({ job: "text-classifier" }))).toBeNull();
  });

  it("trained before report.json and lower_is_better still reads", () => {
    const old = predictorOf(job({ result: { model_id: "autogluon:ab12cd34", validation_score: 0.9, test_score: null } }));
    expect(old?.result?.lower_is_better).toBeUndefined();
    expect(scoreText(headlineScore(old!.result!).value, old!.result!.lower_is_better).text).toBe("0.900");
  });
});
