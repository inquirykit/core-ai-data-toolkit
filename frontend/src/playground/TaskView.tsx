import { MousePointerClick } from "lucide-react";
import { Card, CardContent } from "@/components/ui/card";
import type { ApiClient } from "../shared/api";
import { Segmented } from "../shared/components";
import type { CatalogTask } from "../shared/model-browser/catalog";
import { EmptyState } from "../shared/model-browser/parts";
import { TryPanel } from "../shared/task-io/TryPanel";
import { modelsOf, taskLabel } from "./categories";
import { ModelPicker } from "./ModelPicker";

/**
 * A category other than Chat: its tasks to switch between (when it has more
 * than one), a model for the task, and the task's form and result (TryPanel).
 */
export function TaskView(props: {
  client: ApiClient;
  tasks: CatalogTask[];
  task: string;
  onTask(task: string): void;
  model: string;
  onModel(id: string): void;
  /** After a run: it may have loaded the model. */
  onRan(): void;
}) {
  const { client, tasks, model } = props;
  const current = tasks.find((t) => t.task === props.task) ?? tasks[0];
  const models = modelsOf([current]);
  const loaded = models.some((m) => m.id === model && m.status === "loaded");
  return (
    <Card className="gap-0 overflow-hidden p-0">
      <div className="grid gap-3 border-b bg-muted/40 px-4 py-3.5">
        {tasks.length > 1 && (
          <Segmented
            label="Task"
            size="sm"
            className="max-w-full overflow-x-auto"
            options={tasks.map((t) => [t.task, taskLabel(t.task)])}
            value={current.task}
            onChange={props.onTask}
          />
        )}
        <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
          <ModelPicker
            client={client}
            models={models}
            tasks={[current.task]}
            value={model}
            onChange={props.onModel}
            label={`Model for ${current.task}`}
            pickLabel="Use this model"
            className="w-full max-w-xl"
          />
          {current.description && <p className="min-w-0 flex-[1_1_240px] text-sm text-muted-foreground">{current.description}</p>}
        </div>
      </div>
      <CardContent className="px-4 py-5">
        {model ? (
          // Keyed, so another model or task starts without the last one's result.
          <TryPanel key={`${current.task}\n${model}`} client={client} modelId={model} task={current.task} loaded={loaded} onRan={props.onRan} />
        ) : (
          <EmptyState icon={MousePointerClick}>
            <p>Choose a model to try {taskLabel(current.task).toLowerCase()}.</p>
          </EmptyState>
        )}
      </CardContent>
    </Card>
  );
}
