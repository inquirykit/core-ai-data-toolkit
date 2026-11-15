// A chat reply as Markdown, with its chart blocks drawn (MarkdownView.tsx). The Markdown parser is large, so it
// loads the first time a reply shows; until then the reply is its plain text. Kept from drawing again while other
// replies stream, since each piece of one would parse every reply anew.
import { lazy, memo, Suspense } from "react";

export interface MarkdownProps {
  text: string;
  /** More of the reply is coming: a blinking cursor follows it, and a chart block not yet whole shows as its text */
  writing: boolean;
}

const View = lazy(() => import("./MarkdownView"));

export const Markdown = memo(function Markdown(props: MarkdownProps) {
  return (
    <Suspense fallback={<p className="leading-relaxed wrap-anywhere whitespace-pre-wrap">{props.text}</p>}>
      <View {...props} />
    </Suspense>
  );
});
