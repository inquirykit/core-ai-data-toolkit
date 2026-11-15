import { MousePointerClick } from "lucide-react";
import { Card, CardContent } from "@/components/ui/card";
import { EmptyState, Segmented } from "../../shared/components";
import { TryPanel } from "../../shared/task-io/TryPanel";
import { choicesOf, entryFor, type Choice, type TaskGroup } from "./categories";
import { ModelPicker } from "./ModelPicker";

/**
 * A tab other than Chat: its task switch (when it has more than one entry), a model for the entry, which may be
 * one of a variant's (the badge says so; its task decides the form), and the task's form and result (TryPanel).
 */
export function TaskView(props: {
  groups: TaskGroup[];
  group: TaskGroup;
  onGroup(key: string): void;
  choice: Choice | null;
  onChoice(choice: Choice): void;
  /** After a run: it may have loaded the model. */
  onRan(): void;
}) {
  const { group, choice } = props;
  const chosen = choice && entryFor(group, choice);
  const current = group.tasks.find((t) => t.task === choice?.task) ?? group.tasks[0];
  return (
    <Card className="gap-0 overflow-hidden p-0">
      <div className="grid gap-3 border-b bg-muted/40 px-4 py-3.5">
        {props.groups.length > 1 && (
          <Segmented
            label="Task"
            size="sm"
            className="max-w-full overflow-x-auto"
            options={props.groups.map((g) => [g.key, g.label])}
            value={group.key}
            onChange={props.onGroup}
          />
        )}
        <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
          <ModelPicker
            entries={choicesOf(group)}
            tasks={group.tasks.map((t) => t.task)}
            value={chosen}
            onChange={(entry) => props.onChoice({ id: entry.id, task: entry.task ?? current.task })}
            label={`Model for ${group.label.toLowerCase()}`}
            pickLabel="Use this model"
            className="w-full max-w-xl"
          />
          {current.description && <p className="min-w-0 flex-[1_1_240px] text-sm text-muted-foreground">{current.description}</p>}
        </div>
      </div>
      <CardContent className="px-4 py-5">
        {choice ? (
          // Keyed, so another model or task starts without the last one's result.
          <TryPanel
            key={`${choice.task}\n${choice.id}`}
            modelId={choice.id}
            task={choice.task}
            loaded={chosen?.status === "loaded"}
            onRan={props.onRan}
          />
        ) : (
          <EmptyState icon={MousePointerClick}>
            <p>Choose a model to try {group.label.toLowerCase()}.</p>
          </EmptyState>
        )}
      </CardContent>
    </Card>
  );
}
