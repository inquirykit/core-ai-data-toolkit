import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { LogOut } from "lucide-react";
import { Button } from "@/components/ui/button";
import { useHash } from "../shared/browser";
import { LoginForm, Segmented, StatusDot } from "../shared/components";
import { fmtTime } from "../shared/format";
import { sectionFromHash } from "../shared/nav";
import { errorText, poll } from "../shared/query";
import { ADMIN_KEY_STORAGE, useSession } from "../shared/session";
import { PageMain, Shell } from "../shared/Shell";
import type { Catalog, StatsWindow } from "../shared/types";
import { useMenuStatus } from "./MenuStatus";
import { MODEL_IDS_LIST, type CatalogInfo } from "./panels";
import { SECTIONS } from "./sections";

const REFRESH_MS = 5000;
const WINDOWS: [StatsWindow, string][] = [
  ["1h", "1 hour"],
  ["24h", "24 hours"],
  ["7d", "7 days"],
];
const NO_CATALOG: CatalogInfo = { tasks: [], taskOf: {} };

function catalogInfo(catalog: Catalog): CatalogInfo {
  const taskOf: Record<string, string> = {};
  for (const [task, ids] of Object.entries(catalog)) for (const id of ids) taskOf[id] ??= task;
  return { tasks: Object.keys(catalog).sort(), taskOf };
}

export function AdminApp() {
  const session = useSession(ADMIN_KEY_STORAGE);
  const { client } = session;
  const [window, setWindow] = useState<StatsWindow>("1h");
  const section = useHash(sectionFromHash);

  // Start (and start over after each sign-in, which loads every query again). A fresh instance goes through the
  // setup wizard first.
  const config = useQuery({
    queryKey: ["admin", "start"],
    queryFn: async ({ signal }) => {
      try {
        if ((await client.get("/admin/setup/status", { signal })).needed) {
          location.replace(`/admin/setup${location.hash}`);
          return new Promise<never>(() => {}); // the page is leaving
        }
      } catch {
        /* fall through to the dashboard */
      }
      return client.get("/admin/config", { signal });
    },
  });
  const ready = config.data !== undefined;
  const catalog = client.useQuery("get", "/models/list", undefined, { select: catalogInfo, enabled: ready });

  // Polled while the dashboard shows; at once when the window changes or the tab comes back.
  const live = ready && !session.login;
  const stats = client.useQuery("get", "/admin/stats", { params: { query: { window } } }, { ...poll(REFRESH_MS), enabled: live });
  const statsError = errorText(stats.error);
  const updated = statsError ? `Update failed: ${statsError}` : stats.dataUpdatedAt ? `Updated ${fmtTime(stats.dataUpdatedAt / 1000)}` : "";
  const menu = useMenuStatus(client, stats.data ?? null, statsError, live);

  const configError = errorText(config.error);
  const login = session.login ?? (configError ? { message: `The service did not answer: ${configError}` } : null);
  const view = SECTIONS[section];

  return (
    <>
      <Shell
        active={section}
        badges={menu.badges}
        status={menu.status}
        actions={
          live && (
            <>
              {view.windowed && <Segmented<StatsWindow> label="Time window" options={WINDOWS} value={window} onChange={setWindow} />}
              <span className="flex items-center gap-2 text-xs text-muted-foreground tabular-nums" aria-live="polite">
                {updated && <StatusDot color={statsError ? "var(--critical)" : "var(--good)"} live={!statsError} />}
                {updated}
              </span>
            </>
          )
        }
        footer={
          live &&
          session.hasKey && (
            <Button type="button" variant="outline" size="sm" className="md:w-full" onClick={session.signOut}>
              <LogOut aria-hidden="true" />
              Sign out
            </Button>
          )
        }
      >
        {login ? (
          <LoginForm
            title="Admin key"
            description={
              <>
                Enter <code>ADMIN_API_KEY</code>, or <code>API_KEY</code> if no admin key is set. It is kept for this browser
                tab only.
              </>
            }
            submitLabel="Open dashboard"
            message={login.message}
            onSubmit={session.signIn}
          />
        ) : (
          config.data && (
            <PageMain>
              {view.render({
                client,
                config: config.data,
                stats: stats.data ?? null,
                catalog: catalog.data ?? NO_CATALOG,
                refresh: () => void stats.refetch(),
              })}
            </PageMain>
          )
        )}
      </Shell>

      <datalist id={MODEL_IDS_LIST}>
        {Object.keys((catalog.data ?? NO_CATALOG).taskOf)
          .sort()
          .map((id) => (
            <option key={id} value={id} />
          ))}
      </datalist>
    </>
  );
}
