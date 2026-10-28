// Forms made from the JSON Schema the API serves: a job's params, a task's
// input, an enricher, a model server's provider. The schema decides the
// fields, their labels, hints, bounds and defaults, and form-schema.ts gives
// each field its editor by its shape; a page adds only what a schema cannot
// say, as a uiSchema: a title or placeholder, a field hidden, and sections
// ("ui:layout": headings over rows of fields side by side). RJSF and its
// validator are large, so the form itself (schema-form-view.tsx) loads the
// first time a page shows one. It is part of the page's <form>, which the page
// sends after validate(); that form takes noValidate, so the fields' checks are
// the schema's, said under each field, not the browser's.
import { lazy, Suspense, useMemo, type ReactNode, type Ref } from "react";
import { useQuery } from "@tanstack/react-query";
import type { RJSFSchema, UiSchema } from "@rjsf/utils";
import { Skeleton } from "@/components/ui/skeleton";
import { formSchema } from "./form-schema";
import { errorText } from "./query";

export type { RJSFSchema as FormSchema, UiSchema };

/** A section of a form's "ui:layout": a heading, rows of field names side by side, and anything else that belongs in it. */
export interface FormSection {
  title?: string;
  hint?: ReactNode;
  rows: string[][];
  extra?: ReactNode;
}

export interface SchemaFormHandle {
  /** Check the form, showing what is wrong under each field (and from then on as the reader types): whether it can be sent. */
  validate(): boolean;
}

export interface SchemaFormProps<T> {
  /** Made plain by formSchema (useApiSchema does it) */
  schema: RJSFSchema;
  uiSchema?: UiSchema;
  value: T;
  onChange(value: T): void;
  /** Told what is wrong with the value whenever it changes, and when the form first shows ("Text: required"): for a page that sends without a submit, such as a task's input */
  onProblems?(problems: string[]): void;
  /** Check as the reader types from the start (a task's input, sent at once), not only once validate() is called */
  live?: boolean;
  disabled?: boolean;
  readOnly?: boolean;
  ref?: Ref<SchemaFormHandle>;
}

const View = lazy(() => import("./schema-form-view"));

export function SchemaForm<T>(props: SchemaFormProps<T>) {
  return (
    <Suspense fallback={<Skeleton className="h-40 rounded-lg" />}>
      <View {...(props as SchemaFormProps<unknown>)} />
    </Suspense>
  );
}

/**
 * The schema named ``name`` among the API's (its OpenAPI document's components, which every page may read), made
 * plain for a form. The document is asked for once per page.
 */
export function useApiSchema(name: string): { schema: RJSFSchema | null; error: string } {
  const doc = useQuery({
    queryKey: ["openapi.json"],
    queryFn: async ({ signal }) => {
      const response = await fetch("/openapi.json", { signal });
      if (!response.ok) throw new Error(`Could not read the API's schema (HTTP ${response.status})`);
      return (await response.json()) as RJSFSchema;
    },
    staleTime: Infinity,
  });
  const schema = useMemo(() => {
    const node = (doc.data?.components as { schemas?: Record<string, RJSFSchema> } | undefined)?.schemas?.[name];
    return node && doc.data ? formSchema(node, doc.data) : null;
  }, [doc.data, name]);
  return { schema, error: errorText(doc.error) || (doc.data && !schema ? `The API has no schema named ${name}` : "") };
}
