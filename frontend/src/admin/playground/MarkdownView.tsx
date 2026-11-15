// A chat reply drawn as Markdown, loaded the first time a reply shows (Markdown.tsx): GitHub's Markdown (tables, task
// lists, strikethrough), with the line breaks the model wrote kept, and a fenced chart block (blocks.ts) drawn as a
// view's chart once it is whole. Raw HTML shows as text and links open in a new tab. An image is a link to it, not
// fetched: the page would otherwise load any address a reply names.
import { createContext, useContext, useMemo } from "react";
import ReactMarkdown, { type Components, type ExtraProps } from "react-markdown";
import remarkBreaks from "remark-breaks";
import remarkGfm from "remark-gfm";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { cn } from "@/lib/utils";
import { Boundary } from "../../shared/boundary";
import { CodeBlock } from "../../shared/components";
import { RowsView } from "../data/views/ViewDisplay";
import { CHART_LANGUAGE, chartBlock } from "./blocks";
import type { MarkdownProps } from "./Markdown";

/** The reply is still coming in: a chart block that is not whole yet shows its text, without saying what is wrong. */
const Writing = createContext(false);

// The plain elements are styled from the box around them, there being no typography plugin here.
const PROSE = cn(
  "leading-relaxed wrap-break-word [&>*+*]:mt-2.5",
  "[&_:is(h1,h2,h3,h4,h5,h6)]:leading-snug [&_:is(h1,h2,h3,h4,h5,h6)]:font-semibold [&_h1]:text-lg [&_h2]:text-base",
  "[&_:is(ul,ol)]:pl-5 [&_ol]:list-decimal [&_ul]:list-disc [&_li]:mt-1 [&_li>:is(ul,ol)]:mt-1",
  // A task list's boxes are GitHub's, read-only.
  "[&_.contains-task-list]:list-none [&_.contains-task-list]:pl-1 [&_input]:mr-1.5 [&_input]:align-[-1px]",
  "[&_blockquote]:border-l-2 [&_blockquote]:pl-3 [&_blockquote]:text-muted-foreground",
);

// While the reply is written, a blinking cursor after the last of it, whatever that is.
const CURSOR = cn(
  "[&>:last-child]:after:ml-0.5 [&>:last-child]:after:inline-block [&>:last-child]:after:h-[1.05em] [&>:last-child]:after:w-[7px]",
  "[&>:last-child]:after:animate-blink [&>:last-child]:after:rounded-[1px] [&>:last-child]:after:bg-foreground [&>:last-child]:after:align-[-2px]",
);

const PLUGINS = [remarkGfm, remarkBreaks];
const LINK = "underline underline-offset-2 hover:text-foreground";

// Each drops the hast node react-markdown passes, which is not an attribute.
const COMPONENTS: Components = {
  a: ({ node: _, ...props }) => <a {...props} target="_blank" rel="noreferrer" className={LINK} />,
  img: ({ src, alt }) => (
    <a href={typeof src === "string" ? src : undefined} target="_blank" rel="noreferrer" className={LINK}>
      {alt || "Image"}
    </a>
  ),
  code: ({ node: _, ...props }) => <code {...props} className="rounded bg-muted px-1 py-px font-mono text-[0.88em]" />,
  pre: ({ node }) => <Block node={node} />,
  // The tables of /admin (DataGrid's), scrolling sideways when wide.
  table: ({ node: _, ...props }) => (
    <div className="overflow-hidden rounded-lg border">
      <Table {...props} />
    </div>
  ),
  thead: ({ node: _, ...props }) => <TableHeader {...props} className="bg-muted/50" />,
  tbody: ({ node: _, ...props }) => <TableBody {...props} />,
  tr: ({ node: _, ...props }) => <TableRow {...props} />,
  th: ({ node: _, ...props }) => <TableHead {...props} className="h-9 px-3 whitespace-normal" />,
  td: ({ node: _, ...props }) => <TableCell {...props} className="px-3 py-1.5 whitespace-normal" />,
};

export default function MarkdownView({ text, writing }: MarkdownProps) {
  return (
    <div className={cn(PROSE, writing && CURSOR)}>
      <Writing value={writing}>
        <ReactMarkdown remarkPlugins={PLUGINS} components={COMPONENTS}>
          {text}
        </ReactMarkdown>
      </Writing>
    </div>
  );
}

/** A fenced block: a chart when its language says so, else its code. */
function Block({ node }: ExtraProps) {
  const code = node?.children[0];
  if (code?.type !== "element" || code.tagName !== "code") return null;
  const text = code.children.map((c) => (c.type === "text" ? c.value : "")).join("").replace(/\n$/, "");
  const classes = code.properties.className;
  if (Array.isArray(classes) && classes.includes(`language-${CHART_LANGUAGE}`)) return <Chart text={text} />;
  return <CodeBlock>{text}</CodeBlock>;
}

function Chart({ text }: { text: string }) {
  const writing = useContext(Writing);
  const block = useMemo(() => chartBlock(text), [text]);
  if (!block.ok) {
    return (
      <div className="grid gap-1.5">
        <CodeBlock>{text}</CodeBlock>
        {!writing && <p className="text-xs text-muted-foreground">Not drawn. {block.error}.</p>}
      </div>
    );
  }
  const { title, display, result } = block.value;
  return (
    <figure className="grid min-w-0 gap-2 rounded-lg border p-3">
      {title && <figcaption className="text-sm font-medium">{title}</figcaption>}
      <Boundary what="The chart">
        <RowsView display={display} result={result} height={220} />
      </Boundary>
    </figure>
  );
}
