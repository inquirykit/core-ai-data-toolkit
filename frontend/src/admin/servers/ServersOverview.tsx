// Where model servers come from, and their providers.

import { CircleAlert } from "lucide-react";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Facts } from "../../shared/components";
import type { ModelServers } from "../../shared/types";
import { ProvidersCard } from "./ServersProviders";

export function OverviewView({ data, reload }: { data: ModelServers; reload(): Promise<unknown> }) {
  return (
    <div className="grid min-w-0 gap-5">
      <Card className="gap-4">
        <CardHeader>
          <CardTitle>Where model servers come from</CardTitle>
          <CardDescription>
            Each model is served by its backends, tried in order when it loads: the first that can serve it is used until it unloads.
          </CardDescription>
        </CardHeader>
        <CardContent className="grid gap-3 text-sm">
          <Facts
            labels="10rem"
            className="gap-y-2"
            rows={[
              [
                "The file",
                <span className="font-mono text-xs">
                  {data.file} {!data.file_exists && <span className="font-sans text-muted-foreground">(not there: none)</span>}
                </span>,
              ],
              [
                "Saved here",
                <span className="font-mono text-xs">
                  {data.saved_file}
                </span>,
              ],
              [
                "Commands",
                <span className="text-xs text-muted-foreground">
                  {data.commands_allowed ? (
                    <>This page may add and change the commands servers start with (MODEL_SERVERS_ALLOW_COMMANDS is on).</>
                  ) : (
                    <>
                      Only the commands the file gives can be kept here. Add new ones to the file, or turn on <code>MODEL_SERVERS_ALLOW_COMMANDS</code>: they run on this host.
                    </>
                  )}
                </span>,
              ],
            ]}
          />
          {data.problems.length > 0 && (
            <Alert variant="destructive">
              <CircleAlert aria-hidden="true" />
              <AlertDescription>
                <ul className="grid gap-1">
                  {data.problems.map((p) => (
                    <li key={p.where}>
                      <span className="font-mono text-xs">{p.where}</span>: {p.message}
                    </li>
                  ))}
                </ul>
              </AlertDescription>
            </Alert>
          )}
        </CardContent>
      </Card>
      <ProvidersCard data={data} reload={reload} />
    </div>
  );
}
