// What shows in place of a part of a page that threw while it was drawn: an /admin section, a playground tab, or the
// whole page as a last resort (shared/mount.tsx). Without it one throw blanks the whole page, menu included. It
// catches only what throws while React draws (a bug, a reply read wrongly, a script chunk that did not load); a
// failed load or action is reported by the query or action that ran it. React still logs the error to the console.
import { Component, type ReactNode } from "react";
import { CircleAlert, RotateCcw } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardFooter, CardHeader, CardTitle } from "@/components/ui/card";
import { CodeBlock } from "./components";
import { message } from "./errors";

interface Props {
  /** What it holds, named in the title: "Evidence", "Vision", "The page". */
  what: string;
  /** When it changes, an error shown is cleared and the children are drawn again: the hash, on /admin. */
  resetKey?: unknown;
  /** It holds the whole page, so what it shows has no page around it to sit in. */
  page?: boolean;
  children: ReactNode;
}

// ``failed`` apart from ``error``: anything can be thrown, undefined included.
interface State {
  failed: boolean;
  error: unknown;
}

export class Boundary extends Component<Props, State> {
  state: State = { failed: false, error: undefined };

  static getDerivedStateFromError(error: unknown): State {
    return { failed: true, error };
  }

  componentDidUpdate(prev: Props, prevState: State) {
    // Not on the update that caught it: the change that led to the throw may be the one that changed the key.
    if (this.state.failed && prevState.failed && prev.resetKey !== this.props.resetKey) this.retry();
  }

  retry = () => this.setState({ failed: false, error: undefined });

  render() {
    if (!this.state.failed) return this.props.children;
    const failed = <Failed what={this.props.what} error={this.state.error} retry={this.retry} />;
    return this.props.page ? <main className="mx-auto w-full max-w-2xl p-4 lg:p-6">{failed}</main> : failed;
  }
}

function Failed({ what, error, retry }: { what: string; error: unknown; retry(): void }) {
  return (
    <Card role="alert">
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <CircleAlert className="size-4 shrink-0 text-critical-text" aria-hidden="true" />
          {what} could not be shown
        </CardTitle>
        <CardDescription>
          It failed while it was drawn. Try again, or reload the page if it fails again; the browser's console has the
          details.
        </CardDescription>
      </CardHeader>
      <CardContent>
        <CodeBlock wrap className="max-h-48">
          {message(error)}
        </CodeBlock>
      </CardContent>
      <CardFooter className="gap-2">
        <Button type="button" onClick={retry}>
          <RotateCcw aria-hidden="true" />
          Try again
        </Button>
        <Button type="button" variant="outline" onClick={() => location.reload()}>
          Reload the page
        </Button>
      </CardFooter>
    </Card>
  );
}
