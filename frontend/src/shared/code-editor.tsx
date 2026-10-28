// The editor for code the pages take: SQL and Cypher queries, JSON (a row, a
// request body, params) and JSON Lines. CodeMirror is large, so the editor
// itself (code-editor-view.tsx) loads the first time a page shows one; until
// then its frame stands in, the size the editor will be.
import { lazy, Suspense, type Ref } from "react";
import { cn } from "@/lib/utils";

export type CodeLanguage = "json" | "jsonl" | "sql" | "cypher";

/** What a page can do with the editor beyond its value: put text at the cursor (a table's name), focus it. */
export interface CodeEditorHandle {
  insert(text: string): void;
  focus(): void;
}

export interface CodeEditorProps {
  value: string;
  onChange(value: string): void;
  language: CodeLanguage;
  /** SQL only: the tables and their fields, offered as completions */
  schema?: Record<string, string[]>;
  /** Mod-Enter (Ctrl or Cmd + Enter) runs this, as a query panel's Run */
  onRun?(): void;
  placeholder?: string;
  "aria-label"?: string;
  /** The line numbers down the side: for a query, not for a field */
  lineNumbers?: boolean;
  minHeight?: string;
  maxHeight?: string;
  readOnly?: boolean;
  autoFocus?: boolean;
  /** Framed as a field whose value is wrong (aria-invalid on a textarea) */
  invalid?: boolean;
  className?: string;
  ref?: Ref<CodeEditorHandle>;
}

/** The frame around the editor, like a textarea's. */
export const editorFrame = (props: Pick<CodeEditorProps, "readOnly" | "invalid" | "className">) =>
  cn(
    "min-w-0 overflow-hidden rounded-md border border-input bg-transparent shadow-xs transition-[color,box-shadow] focus-within:border-ring focus-within:ring-[3px] focus-within:ring-ring/50 dark:bg-input/30",
    props.readOnly && "opacity-80",
    props.invalid && "border-destructive ring-destructive/20 dark:ring-destructive/40",
    props.className,
  );

const View = lazy(() => import("./code-editor-view"));

export function CodeEditor(props: CodeEditorProps) {
  return (
    <Suspense fallback={<div className={editorFrame(props)} style={{ minHeight: props.minHeight ?? "2.25rem" }} />}>
      <View {...props} />
    </Suspense>
  );
}
