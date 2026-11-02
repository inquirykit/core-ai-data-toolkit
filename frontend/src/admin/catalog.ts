// What the dashboard knows of the model catalog (GET /models/list), as the
// forms that take a model ID use it: the tasks, each listed ID's task, and
// the id of the <datalist> of IDs (AdminApp.tsx draws it) the inputs
// complete from.

/** /models/list, as the forms use it. */
export interface CatalogInfo {
  tasks: string[];
  /** The task each listed model ID is registered for (the first, if several). */
  taskOf: Record<string, string>;
}

export const MODEL_IDS_LIST = "model-ids";
