// The form itself, loaded only when a page shows one (schema-form.tsx): RJSF
// with its shadcn theme, checked by its AJV validator, with the pages' own
// editors where the theme's would not do: a switch for a yes or no, rows of
// key and value for a dict of strings (KVRows), the code editor for JSON, and
// sections laid out as "ui:layout" says. It is a part of the page's form,
// not a form of its own: the page sends it, after validate().
import { useEffect, useId, useImperativeHandle, useMemo, useRef, useState, type ComponentType, type ReactNode } from "react";
import Form, { Templates, Widgets } from "@rjsf/shadcn";
import type CoreForm from "@rjsf/core";
import validator from "@rjsf/validator-ajv8";
import {
  canExpand,
  getUiOptions,
  type FieldProps,
  type ObjectFieldTemplateProps,
  type RJSFValidationError,
  type UiSchema,
  type WidgetProps,
} from "@rjsf/utils";
import { Switch } from "@/components/ui/switch";
import { cn } from "@/lib/utils";
import { CodeEditor } from "./code-editor";
import { Heading, KVRows, Segmented, SelectInput, type KV } from "./components";
import { mergeUi, uiDefaults, valueKey } from "./form-schema";
import type { FormSection, SchemaFormProps } from "./schema-form";

const Hint = ({ children }: { children: ReactNode }) => <span className="text-xs text-muted-foreground">{children}</span>;

/** A yes or no: a switch, with its title and description beside it (every boolean field). */
function SwitchWidget({ id, value, label, schema, options, disabled, readonly, onChange }: WidgetProps) {
  const description = options.description ?? schema.description;
  return (
    <label htmlFor={id} className="flex items-start gap-3 py-1">
      <Switch id={id} checked={Boolean(value)} disabled={disabled || readonly} onCheckedChange={(checked) => onChange(checked)} />
      <span className="grid gap-1">
        <span className="text-sm leading-none font-medium">{label}</span>
        {description && <Hint>{description}</Hint>}
      </span>
    </label>
  );
}

/**
 * One of a list: the pages' own select, like every other on them. Radix takes only strings, so each option goes by
 * its place in the list; an optional field offers "unset" first. Several of a list: the theme's.
 */
function SelectWidget(props: WidgetProps) {
  const { options, value, multiple, required, disabled, readonly, label, placeholder, onChange } = props;
  if (multiple) {
    const Themed = Widgets.SelectWidget as ComponentType<WidgetProps>;
    return <Themed {...props} />;
  }
  const choices = options.enumOptions ?? [];
  const at = choices.findIndex((o) => o.value === value);
  return (
    <SelectInput
      className="w-full"
      aria-label={label}
      value={at < 0 ? "" : String(at)}
      onChange={(i) => onChange(i === "" ? options.emptyValue : choices[Number(i)].value)}
      none={required ? undefined : "unset"}
      placeholder={placeholder || "Choose…"}
      disabled={disabled || readonly}
      options={choices.map((o, i) => ({ value: String(i), label: o.label }))}
    />
  );
}

/** A few choices side by side ("ui:widget": "radio"): the pages' segmented control, each choice by its place in the list. */
function SegmentedWidget({ options, value, label, disabled, readonly, onChange }: WidgetProps) {
  const choices = options.enumOptions ?? [];
  const at = choices.findIndex((o) => o.value === value);
  return (
    <Segmented
      label={label}
      className={cn((disabled || readonly) && "pointer-events-none opacity-60")}
      options={choices.map((o, i) => [String(i), o.label])}
      value={at < 0 ? "" : String(at)}
      onChange={(i) => onChange(choices[Number(i)].value)}
    />
  );
}

const toRows = (value: unknown): KV[] => Object.entries((value ?? {}) as Record<string, unknown>).map(([key, v]) => ({ key, value: String(v ?? "") }));
const fromRows = (rows: KV[]) => Object.fromEntries(rows.filter((r) => r.key.trim()).map((r) => [r.key.trim(), r.value]));

/**
 * A dict of strings as rows of key and value. The rows are kept here, since a row being written (no key yet, or a
 * key twice) is not yet the dict; a dict changed elsewhere (a value loaded) replaces them.
 */
function KeyValueField({ formData, onChange, fieldPathId, schema, uiSchema, name, readonly, disabled }: FieldProps) {
  const options = getUiOptions(uiSchema);
  const [rows, setRows] = useState(() => toRows(formData));
  const [shown, setShown] = useState<unknown>(formData);
  if (formData !== shown) {
    setShown(formData);
    if (valueKey(fromRows(rows)) !== valueKey(formData ?? {})) setRows(toRows(formData));
  }
  const change = (next: KV[]) => {
    const value = fromRows(next);
    setRows(next);
    setShown(value);
    onChange(value, fieldPathId.path);
  };
  const label = String(options.title ?? schema.title ?? name);
  const description = options.description ?? schema.description;
  return (
    <div className="grid gap-1.5">
      <KVRows
        label={label}
        // Named for screen readers by the schema even when the page shows no label (a section's heading names it).
        ariaLabel={String(schema.title ?? name)}
        rows={rows}
        onChange={change}
        keyLabel={String(options.keyLabel ?? "name")}
        valueLabel={String(options.valueLabel ?? "value")}
        keyPlaceholder={options.keyPlaceholder as string | undefined}
        valuePlaceholder={options.valuePlaceholder as string | undefined}
        addLabel={String(options.addLabel ?? "Add")}
        readOnly={readonly || disabled}
      />
      {description && <Hint>{description}</Hint>}
    </div>
  );
}

const jsonText = (value: unknown) => (value === undefined || value === null ? "" : typeof value === "string" ? value : JSON.stringify(value, null, 2));

/**
 * JSON in the code editor, for an object without declared fields. Text that is not JSON goes to the form as it is,
 * so the schema (which wants an object) refuses it and the form cannot be sent with it; blank is unset.
 */
function JsonField({ formData, onChange, fieldPathId, schema, uiSchema, name, readonly, disabled, rawErrors }: FieldProps) {
  const options = getUiOptions(uiSchema);
  const [text, setText] = useState(() => jsonText(formData));
  const [shown, setShown] = useState<unknown>(formData);
  if (formData !== shown) {
    setShown(formData);
    if (typeof formData !== "string") setText(jsonText(formData));
  }
  const change = (next: string) => {
    let value: unknown = next.trim() ? next : undefined;
    try {
      if (next.trim()) value = JSON.parse(next);
    } catch {
      /* kept as text, which the schema refuses */
    }
    setText(next);
    setShown(value);
    onChange(value, fieldPathId.path);
  };
  const label = String(options.title ?? schema.title ?? name);
  const description = options.description ?? schema.description;
  return (
    <div className="grid gap-2">
      <span className="text-sm leading-none font-medium">{label}</span>
      <CodeEditor
        aria-label={label}
        language="json"
        value={text}
        onChange={change}
        invalid={Boolean(rawErrors?.length)}
        readOnly={readonly || disabled}
        placeholder={options.placeholder as string | undefined}
        minHeight={String(options.minHeight ?? "4.5rem")}
      />
      {description && <Hint>{description}</Hint>}
    </div>
  );
}

const COLUMNS = ["", "", "sm:grid-cols-2", "sm:grid-cols-3", "sm:grid-cols-4"];

/**
 * An object's fields in the sections its "ui:layout" names, each a heading over rows of fields side by side; the
 * fields no section names follow at the end, so a field new to the schema still shows. Without a layout, its fields
 * one under another under its title; one that takes fields of any name (rare: dicts have KeyValueField), the theme's.
 */
function LayoutObjectTemplate(props: ObjectFieldTemplateProps) {
  const layout = props.uiSchema?.["ui:layout"] as FormSection[] | undefined;
  if (!layout && canExpand(props.schema, props.uiSchema, props.formData)) {
    const Themed = Templates.ObjectFieldTemplate as ComponentType<ObjectFieldTemplateProps>;
    return <Themed {...props} />;
  }
  if (!layout) {
    return (
      <div className="grid gap-4">
        {props.title && <Heading hint={props.description}>{props.title}</Heading>}
        {props.properties.map((p) => (
          <div key={p.name} className={cn("min-w-0", p.hidden && "hidden")}>
            {p.content}
          </div>
        ))}
      </div>
    );
  }
  const byName = new Map(props.properties.map((p) => [p.name, p]));
  const placed = new Set(layout.flatMap((s) => s.rows.flat()));
  const rest = props.properties.filter((p) => !placed.has(p.name) && !p.hidden);
  const row = (names: string[], key: string) => {
    const cells = names.map((n) => byName.get(n)).filter((p) => p && !p.hidden);
    if (!cells.length) return null;
    return (
      <div key={key} className={cn("grid items-start gap-3", COLUMNS[cells.length])}>
        {cells.map((p) => (
          <div key={p!.name} className="min-w-0">
            {p!.content}
          </div>
        ))}
      </div>
    );
  };
  return (
    <div className="grid gap-6">
      {layout.map((section, i) => (
        <section key={section.title ?? i} className="grid gap-3">
          {section.title && <Heading hint={section.hint}>{section.title}</Heading>}
          {section.rows.map((names, j) => row(names, `${i}:${j}`))}
          {section.extra}
        </section>
      ))}
      {rest.length > 0 && <section className="grid gap-3">{rest.map((p) => row([p.name], p.name))}</section>}
      <div className="hidden">{props.properties.filter((p) => p.hidden).map((p) => p.content)}</div>
    </div>
  );
}

/** What an error says under the field it is about, in words rather than in JSON Schema's: the hint above it says the rest. */
function errorMessage(e: RJSFValidationError): string | undefined {
  const limit = e.params?.limit as number | undefined;
  switch (e.name) {
    case "required":
      return "required";
    case "minLength":
      return limit === 1 ? "required" : `at least ${limit} characters`;
    case "maxLength":
      return `at most ${limit} characters`;
    case "minItems":
      return `pick at least ${limit}`;
    case "pattern":
      return "not in the form the hint gives";
    case "type":
      // Text that is not JSON where an object is wanted (JsonField).
      return e.params?.type === "object" ? "not valid JSON (an object)" : undefined;
  }
  return undefined;
}

const transformErrors = (errors: RJSFValidationError[]) => errors.map((e) => ({ ...e, message: errorMessage(e) ?? e.message }));

const WIDGETS = { CheckboxWidget: SwitchWidget, SelectWidget, RadioWidget: SegmentedWidget };
const FIELDS = { keyValue: KeyValueField, json: JsonField };
const TEMPLATES = { ObjectFieldTemplate: LayoutObjectTemplate };
// The form is a part of the page's: its title and description are the page's to give, and its button too.
const ROOT: UiSchema = { "ui:title": "", "ui:description": "" };
const NO_BUTTON: UiSchema = { "ui:submitButtonOptions": { norender: true } };

/** An error as a line on its own: the field's title (or name) and what is wrong with it. */
function problemText(error: RJSFValidationError, schema: SchemaFormProps<unknown>["schema"]): string {
  const name = (error.property ?? "").replace(/^\./, "").split(".")[0];
  const field = (schema.properties as Record<string, { title?: string }> | undefined)?.[name];
  return `${field?.title ?? (name || "The input")}: ${error.message}`;
}

export default function SchemaFormView(props: SchemaFormProps<unknown>) {
  const form = useRef<CoreForm>(null);
  // Field ids of its own, so two forms on a page (a playground's tabs stay mounted) do not share them.
  const idPrefix = `form${useId().replace(/[^a-zA-Z0-9]/g, "")}`;
  // Checked as the reader types once validate() has shown what is wrong, so the messages go as they are fixed.
  const [checked, setChecked] = useState(false);
  useImperativeHandle(
    props.ref,
    () => ({
      validate() {
        setChecked(true);
        return form.current?.validateForm() ?? false;
      },
    }),
    [],
  );
  const { onProblems, schema, value } = props;
  useEffect(() => {
    if (onProblems) onProblems(validator.validateFormData(value, schema, undefined, transformErrors).errors.map((e) => problemText(e, schema)));
  }, [onProblems, schema, value]);
  const uiSchema = useMemo(
    () => mergeUi(mergeUi(mergeUi(ROOT, uiDefaults(props.schema)), props.uiSchema), NO_BUTTON),
    [props.schema, props.uiSchema],
  );
  return (
    <Form
      ref={form}
      tagName="div"
      idPrefix={idPrefix}
      schema={props.schema}
      uiSchema={uiSchema}
      formData={props.value}
      onChange={(e) => props.onChange(e.formData)}
      validator={validator}
      widgets={WIDGETS}
      fields={FIELDS}
      templates={TEMPLATES}
      transformErrors={transformErrors}
      liveValidate={props.live || checked ? "onChange" : undefined}
      showErrorList={false}
      // The errors show under their fields; RJSF would also log them.
      onError={() => {}}
      noHtml5Validate
      disabled={props.disabled}
      readonly={props.readOnly}
    />
  );
}
