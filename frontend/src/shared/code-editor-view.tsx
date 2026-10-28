// The editor itself, loaded only when a page shows one (code-editor.tsx):
// CodeMirror 6 through @uiw/react-codemirror, with highlighting, bracket
// matching, completion of the namespace's tables and fields in SQL, and JSON
// checked as it is typed. Its colors are the page's (globals.css, --code-*),
// so it follows light and dark without knowing which is on, and it is framed
// like a textarea.
import { useImperativeHandle, useLayoutEffect, useMemo, useRef } from "react";
import CodeMirror, { EditorView, keymap, Prec, type Extension, type ReactCodeMirrorRef } from "@uiw/react-codemirror";
import { json, jsonParseLinter } from "@codemirror/lang-json";
import { sql, StandardSQL, type SQLNamespace } from "@codemirror/lang-sql";
import { HighlightStyle, StreamLanguage, syntaxHighlighting } from "@codemirror/language";
import { cypher } from "@codemirror/legacy-modes/mode/cypher";
import { linter, type Diagnostic } from "@codemirror/lint";
import { tags as t } from "@lezer/highlight";
import { editorFrame, type CodeEditorProps, type CodeLanguage } from "./code-editor";
import { jsonLinesProblems } from "./json";


const theme = EditorView.theme({
  "&": { color: "var(--foreground)", backgroundColor: "transparent", fontSize: "13px" },
  "&.cm-focused": { outline: "none" },
  ".cm-scroller": { fontFamily: "var(--font-mono)", lineHeight: "1.6" },
  ".cm-content": { caretColor: "var(--foreground)", padding: "7px 0" },
  ".cm-line": { padding: "0 12px" },
  ".cm-gutters": { backgroundColor: "transparent", color: "var(--muted-foreground)", border: "none" },
  ".cm-activeLineGutter": { backgroundColor: "transparent", color: "var(--foreground)" },
  ".cm-activeLine": { backgroundColor: "color-mix(in oklch, var(--muted) 50%, transparent)" },
  "&.cm-focused > .cm-scroller > .cm-selectionLayer .cm-selectionBackground, .cm-selectionBackground, ::selection": {
    backgroundColor: "color-mix(in oklch, var(--ring) 22%, transparent)",
  },
  ".cm-cursor": { borderLeftColor: "var(--foreground)" },
  ".cm-placeholder": { color: "var(--muted-foreground)" },
  ".cm-matchingBracket": { backgroundColor: "color-mix(in oklch, var(--ring) 18%, transparent)", outline: "none" },
  ".cm-tooltip": {
    backgroundColor: "var(--popover)",
    color: "var(--popover-foreground)",
    border: "1px solid var(--border)",
    borderRadius: "6px",
    overflow: "hidden",
  },
  ".cm-tooltip-autocomplete > ul": { fontFamily: "var(--font-mono)", fontSize: "12px" },
  ".cm-tooltip-autocomplete > ul > li[aria-selected]": { backgroundColor: "var(--accent)", color: "var(--accent-foreground)" },
  ".cm-diagnostic": { padding: "4px 8px", fontFamily: "var(--font-sans)", fontSize: "12px" },
  ".cm-diagnostic-error": { borderLeft: "3px solid var(--critical)" },
});

const highlight = HighlightStyle.define([
  { tag: [t.keyword, t.operatorKeyword, t.modifier, t.controlKeyword, t.definitionKeyword], color: "var(--code-keyword)", fontWeight: "500" },
  { tag: [t.string, t.special(t.string)], color: "var(--code-string)" },
  { tag: [t.number, t.bool, t.null, t.atom], color: "var(--code-number)" },
  { tag: [t.propertyName, t.typeName, t.className, t.labelName, t.special(t.name)], color: "var(--code-property)" },
  { tag: [t.comment, t.lineComment, t.blockComment], color: "var(--code-comment)", fontStyle: "italic" },
  { tag: t.invalid, color: "var(--code-invalid)" },
]);

/** JSON as the forms take it: blank is left to the form (it often means "none"), anything else must parse. */
const jsonLinter = linter((view) => (view.state.doc.toString().trim() ? jsonParseLinter()(view) : []));
const jsonLinesLinter = linter((view): Diagnostic[] =>
  jsonLinesProblems(view.state.doc.toString()).map((p) => ({ ...p, severity: "error" })),
);

function languageOf(language: CodeLanguage, schema: SQLNamespace | undefined): Extension {
  switch (language) {
    case "json":
      return [json(), jsonLinter];
    case "jsonl":
      return [json(), jsonLinesLinter];
    case "sql":
      return sql({ dialect: StandardSQL, schema, upperCaseKeywords: true });
    case "cypher":
      return StreamLanguage.define(cypher);
  }
}

export default function CodeEditorView(props: CodeEditorProps) {
  const { language, schema, lineNumbers = false } = props;
  const editor = useRef<ReactCodeMirrorRef>(null);
  // The newest onRun, so the keymap made once calls what the page passes now.
  const onRun = useRef(props.onRun);
  useLayoutEffect(() => {
    onRun.current = props.onRun;
  });

  useImperativeHandle(
    props.ref,
    () => ({
      insert(text) {
        const view = editor.current?.view;
        if (!view) return;
        view.dispatch(view.state.replaceSelection(text));
        view.focus();
      },
      focus: () => editor.current?.view?.focus(),
    }),
    [],
  );

  const extensions = useMemo(
    () => [
      languageOf(language, schema),
      theme,
      syntaxHighlighting(highlight),
      EditorView.lineWrapping,
      EditorView.contentAttributes.of({
        "aria-label": props["aria-label"] ?? "Code",
        "aria-invalid": props.invalid ? "true" : "false",
        spellcheck: "false",
      }),
      // Above the default keymap, which takes Mod-Enter for a blank line.
      Prec.highest(keymap.of([{ key: "Mod-Enter", run: () => (onRun.current ? (onRun.current(), true) : false) }])),
    ],
    [language, schema, props["aria-label"], props.invalid],
  );

  return (
    <div className={editorFrame(props)}>
      <CodeMirror
        ref={editor}
        value={props.value}
        onChange={props.onChange}
        extensions={extensions}
        theme="none"
        placeholder={props.placeholder}
        minHeight={props.minHeight ?? "2.25rem"}
        maxHeight={props.maxHeight ?? "24rem"}
        readOnly={props.readOnly}
        autoFocus={props.autoFocus}
        // Tab moves on to the next field, as in a textarea; the editor indents on Enter by itself.
        indentWithTab={false}
        basicSetup={{
          lineNumbers,
          foldGutter: false,
          highlightActiveLine: lineNumbers,
          highlightActiveLineGutter: lineNumbers,
          autocompletion: language === "sql",
          searchKeymap: lineNumbers,
        }}
      />
    </div>
  );
}
