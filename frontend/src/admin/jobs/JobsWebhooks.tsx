// The Jobs section's webhooks: the URLs told when a job or eval run finishes,
// whether deliveries are signed, the latest deliveries and how each went, and
// a form that sends a test event.

import { useEffect, useState, type FormEvent } from "react";
import { Send } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { useAction } from "../../shared/action";
import { useApi } from "../../shared/api";
import { Facts, Field, Heading, Notice, StatusDot, SubmitButton, StatusBadge } from "../../shared/components";
import { fmtAgo, fmtClock } from "../../shared/format";
import { DataGrid, type GridColumn } from "../../shared/grid";
import type { WebhookDelivery, Webhooks } from "../../shared/types";
import { fmtDeliveryTries, fmtWebhookTest } from "./jobs";
import { LoadedCard } from "../layout";

const DELIVERY_COLUMNS: GridColumn<WebhookDelivery>[] = [
  {
    key: "event",
    label: "Event",
    render: (d) => (
      <>
        <span className="font-mono text-[13px]">{d.event ?? "–"}</span>
        {d.about && <div className="font-mono text-xs text-muted-foreground">{d.about}</div>}
      </>
    ),
  },
  { key: "url", label: "URL", className: "max-w-64 truncate font-mono text-xs", title: (d) => d.url ?? undefined, render: (d) => d.url ?? "–" },
  { key: "status", label: "Status", render: (d) => <StatusBadge status={d.status} /> },
  {
    key: "tries",
    label: "Tries",
    headTitle: "A delivery that fails is retried for about four hours: the wait doubles from 30 s up to an hour",
    className: "whitespace-nowrap text-muted-foreground",
    render: (d) => fmtDeliveryTries(d),
  },
  { key: "error", label: "Last error", className: "max-w-56 truncate text-muted-foreground", title: (d) => d.error ?? undefined, render: (d) => d.error ?? "–" },
  {
    key: "created",
    label: "When",
    className: "whitespace-nowrap text-muted-foreground",
    title: (d) => fmtClock(d.created, true),
    render: (d) => fmtAgo(d.created),
  },
];

export function WebhooksCard(props: { webhooks: Webhooks | null; error: string }) {
  const client = useApi();
  const { webhooks } = props;
  const [url, setUrl] = useState("");
  // The first configured URL goes into the test field once, when the list arrives.
  useEffect(() => {
    if (webhooks?.urls[0]) setUrl((u) => u || webhooks.urls[0]);
  }, [webhooks]);

  const test = useAction(
    (event: FormEvent) => {
      event.preventDefault();
      return client.post("/queue/webhooks/test", { body: { url: url.trim() } });
    },
    { notice: true, success: fmtWebhookTest },
  );

  return (
    <LoadedCard
      title="Webhooks"
      description={
        <>
          Every finished job and eval run is POSTed as JSON to these URLs and to a schedule's or job's own <code>webhook_url</code>; the body is{" "}
          <code>{"{event, time, data}"}</code> and <code>X-Toolkit-Signature</code> carries an HMAC-SHA256 of the body when <code>WEBHOOK_SECRET</code> is set. A
          delivery that does not get a 2xx is retried for about four hours, across restarts.
        </>
      }
      data={webhooks}
      error={props.error}
      skeleton="h-20"
      after={
        <>
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
        </>
      }
    >
      {(w) => (
        <div className="grid gap-5">
          <Facts
            labels="8rem"
            className="gap-y-3 [&_dt]:font-medium"
            rows={[
              [
                "URLs",
                <span className="grid gap-1">
                  {w.urls.length ? (
                    w.urls.map((u) => (
                      <code key={u} className="text-[13px] [overflow-wrap:anywhere]">
                        {u}
                      </code>
                    ))
                  ) : (
                    <span className="text-muted-foreground">
                      None: set <code>WEBHOOK_URLS</code> (comma-separated) to be told of every finished job.
                    </span>
                  )}
                </span>,
              ],
              [
                "Signing secret",
                w.secret_set ? (
                  <Badge className="gap-1.5">
                    <StatusDot color="var(--good)" className="shadow-none" />
                    set
                  </Badge>
                ) : (
                  <span className="text-muted-foreground">
                    Not set: deliveries are unsigned. Set <code>WEBHOOK_SECRET</code> so receivers can check them.
                  </span>
                ),
              ],
              [
                "Events",
                <span className="flex flex-wrap gap-1">
                  {w.events.length ? (
                    w.events.map((e) => (
                      <Badge key={e} variant="secondary" className="font-mono text-[11px] font-normal">
                        {e}
                      </Badge>
                    ))
                  ) : (
                    <span className="text-muted-foreground">–</span>
                  )}
                </span>,
              ],
            ]}
          />
          <div className="grid gap-2">
            <Heading hint="Each event to each URL, newest first; kept for a week">Latest deliveries</Heading>
            <DataGrid
              columns={DELIVERY_COLUMNS}
              rows={w.deliveries}
              rowKey={(d) => d.id}
              headerClassName="bg-muted/50"
              empty="Nothing sent yet: a delivery is listed once a job or eval run finishes"
              aria-label="Latest webhook deliveries"
            />
          </div>
        </div>
      )}
    </LoadedCard>
  );
}
