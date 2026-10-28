// The Jobs section's webhooks: the URLs told when a job or eval run finishes,
// whether deliveries are signed, and a form that sends a test event.

import { useEffect, useState, type FormEvent, type ReactNode } from "react";
import { Send } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { useAction } from "../shared/action";
import type { ApiClient } from "../shared/api";
import { Field, Notice, StatusDot, SubmitButton } from "../shared/components";
import type { Webhooks } from "../shared/types";
import { fmtWebhookTest } from "./jobs";

export function WebhooksCard(props: { client: ApiClient; webhooks: Webhooks | null; error: string }) {
  const { webhooks } = props;
  const [url, setUrl] = useState("");
  // The first configured URL goes into the test field once, when the list arrives.
  useEffect(() => {
    if (webhooks?.urls[0]) setUrl((u) => u || webhooks.urls[0]);
  }, [webhooks]);

  const test = useAction(
    (event: FormEvent) => {
      event.preventDefault();
      return props.client.post("/jobs/webhooks/test", { body: { url: url.trim() } });
    },
    { notice: true, success: fmtWebhookTest },
  );

  return (
    <Card className="gap-4">
      <CardHeader>
        <CardTitle>Webhooks</CardTitle>
        <CardDescription>
          Every finished job and eval run is POSTed as JSON to these URLs and to a schedule's or job's own <code>webhook_url</code>; the body is{" "}
          <code>{"{event, time, data}"}</code> and <code>X-Toolkit-Signature</code> carries an HMAC-SHA256 of the body when <code>WEBHOOK_SECRET</code> is set.
        </CardDescription>
      </CardHeader>
      <CardContent className="grid gap-4">
        {props.error && <Notice notice={{ text: props.error, kind: "error" }} />}
        {!webhooks && !props.error && <Skeleton className="h-20" />}
        {webhooks && (
          <dl className="grid gap-3 text-sm sm:grid-cols-[8rem_minmax(0,1fr)]">
            <Term>URLs</Term>
            <dd className="grid gap-1">
              {webhooks.urls.length ? (
                webhooks.urls.map((u) => (
                  <code key={u} className="text-[13px] [overflow-wrap:anywhere]">
                    {u}
                  </code>
                ))
              ) : (
                <span className="text-muted-foreground">
                  None: set <code>WEBHOOK_URLS</code> (comma-separated) to be told of every finished job.
                </span>
              )}
            </dd>
            <Term>Signing secret</Term>
            <dd>
              {webhooks.secret_set ? (
                <Badge className="gap-1.5">
                  <StatusDot color="var(--good)" className="shadow-none" />
                  set
                </Badge>
              ) : (
                <span className="text-muted-foreground">
                  Not set: deliveries are unsigned. Set <code>WEBHOOK_SECRET</code> so receivers can check them.
                </span>
              )}
            </dd>
            <Term>Events</Term>
            <dd className="flex flex-wrap gap-1">
              {webhooks.events.length ? (
                webhooks.events.map((e) => (
                  <Badge key={e} variant="secondary" className="font-mono text-[11px] font-normal">
                    {e}
                  </Badge>
                ))
              ) : (
                <span className="text-muted-foreground">–</span>
              )}
            </dd>
          </dl>
        )}
        <form className="flex flex-wrap items-end gap-2" onSubmit={test.run}>
          <Field label="Send a test" className="min-w-64 flex-1" hint="A test event is POSTed to this URL now; the answer shows below">
            <Input
              type="url"
              placeholder="https://…"
              autoComplete="off"
              spellCheck={false}
              required
              value={url}
              onChange={(e) => setUrl(e.target.value)}
            />
          </Field>
          <SubmitButton variant="outline" busy={test.busy} icon={<Send aria-hidden="true" />} disabled={!url.trim()}>
            Send test
          </SubmitButton>
        </form>
        <Notice notice={test.notice} />
      </CardContent>
    </Card>
  );
}

function Term({ children }: { children: ReactNode }) {
  return <dt className="font-medium text-muted-foreground">{children}</dt>;
}
