import { useEffect, useRef, type ComponentProps, type CSSProperties, type ReactNode, type Ref } from "react";
import { motion } from "motion/react";
import { Separator } from "@/components/ui/separator";
import { cn } from "@/lib/utils";
import { Brand, ThemeToggle } from "./components";
import { NAV, NAV_GROUPS, navItem, type NavItem, type NavKey } from "./nav";

const MENU = NAV.filter((item) => !item.off);
const GROUPS = NAV_GROUPS.map((group) => ({ ...group, items: MENU.filter((item) => item.group === group.key) }));
const BOTTOM = MENU.filter((item) => item.group === "system");

/** Live state shown beside menu items, by page: a count, a spinner, a sparkline. */
export type MenuBadges = Partial<Record<NavKey, ReactNode>>;

/** A page's content area: centered, padded, its blocks rising in one after another. */
export function PageMain({ className, ...props }: ComponentProps<"main">) {
  return (
    <main
      className={cn(
        "stagger mx-auto grid w-full max-w-[1400px] min-w-0 grid-cols-[minmax(0,1fr)] content-start gap-5 p-4 lg:p-6",
        className,
      )}
      {...props}
    />
  );
}

/**
 * One menu entry: its icon on a tile in its group's hue, the pill that slides to the page shown, and its
 * ``badge`` on the right. The hue is ``--hue``, for the tile and the badge alike (globals.css).
 */
function MenuLink({ item, active, badge, ref }: { item: NavItem<NavKey>; active: boolean; badge?: ReactNode; ref?: Ref<HTMLAnchorElement> }) {
  const Icon = item.icon;
  return (
    <a
      ref={ref}
      href={item.href}
      aria-current={active ? "page" : undefined}
      style={{ "--hue": `var(--nav-${item.group})` } as CSSProperties}
      className={cn(
        "group relative flex h-9 shrink-0 items-center gap-2.5 rounded-lg px-2 text-sm font-medium whitespace-nowrap text-sidebar-foreground/80 outline-none transition-colors hover:bg-glass-hover hover:text-sidebar-foreground focus-visible:ring-[3px] focus-visible:ring-ring/50",
        active && "text-sidebar-foreground hover:bg-transparent",
      )}
    >
      {/* Shared between the items, so it slides to the one chosen: beneath them all (the menu's stacking context,
          from its backdrop filter), or it would hide the items it passes over on the way. */}
      {active && (
        <motion.span
          layoutId="nav-pill"
          className="absolute inset-0 -z-10 rounded-lg bg-glass-pill shadow-[0_1px_2px_oklch(0_0_0/0.06),0_4px_12px_-6px_var(--glass-shadow)]"
          transition={{ type: "spring", stiffness: 500, damping: 38 }}
        />
      )}
      <span className="nav-tile relative grid size-6 shrink-0 place-items-center rounded-[7px] group-hover:scale-110">
        <Icon className="size-3.5" strokeWidth={2} aria-hidden="true" />
      </span>
      <span className="relative">{item.label}</span>
      {badge && <span className="relative ml-auto flex items-center gap-1.5">{badge}</span>}
    </a>
  );
}

/**
 * The frame of /admin and /playground: the menu on the left (across the top
 * on a narrow screen), frosted over a soft gradient that drifts behind it,
 * and the page, inset, with a header naming it.
 */
export function Shell(props: {
  active: NavKey;
  /** On the right of the page header */
  actions?: ReactNode;
  /** At the bottom of the menu, e.g. Sign out */
  footer?: ReactNode;
  /** Beside the menu's items */
  badges?: MenuBadges;
  /** Above Settings at the bottom of the menu, on a wide screen: the server's health, say */
  status?: ReactNode;
  children: ReactNode;
}) {
  const page = navItem(props.active);
  const current = useRef<HTMLAnchorElement>(null);
  // On a narrow screen the menu is a strip that scrolls sideways: keep the current page in view.
  useEffect(() => {
    // A block, not an expression: newer browsers return a Promise, which React would take for a cleanup.
    current.current?.scrollIntoView({ block: "nearest", inline: "nearest" });
  }, [props.active]);
  return (
    <div className="relative isolate flex min-h-svh flex-col text-sidebar-foreground md:flex-row">
      {/* globals.css draws the blobs and moves them. */}
      <div className="shell-sky" aria-hidden="true">
        <span />
        <span />
        <span />
        <span />
      </div>

      <aside className="flex shrink-0 items-center gap-2 overflow-x-auto border-b border-glass-edge bg-glass p-2 backdrop-blur-xl backdrop-saturate-150 md:sticky md:top-2 md:m-2 md:mr-0 md:h-[calc(100svh-1rem)] md:w-60 md:flex-col md:items-stretch md:gap-1 md:overflow-x-visible md:rounded-2xl md:border-0 md:p-3 md:shadow-[inset_0_0_0_1px_var(--glass-edge),0_10px_30px_-12px_var(--glass-shadow)]">
        <Brand className="md:mb-3" />
        {/* On a short screen the groups scroll, and Settings stays at the bottom. */}
        <nav aria-label="Main" className="flex gap-1 md:-mx-1 md:grid md:min-h-0 md:flex-1 md:content-start md:gap-3 md:overflow-y-auto md:px-1">
          {GROUPS.map((group) => (
            <div key={group.key} role="group" aria-label={group.label} className="flex gap-1 md:grid md:gap-0.5">
              <div className="px-2 pb-1 text-xs font-medium text-sidebar-foreground/55 max-md:hidden" aria-hidden="true">
                {group.label}
              </div>
              {group.items.map((item) => {
                const active = item.key === props.active;
                return <MenuLink key={item.key} item={item} active={active} badge={props.badges?.[item.key]} ref={active ? current : undefined} />;
              })}
            </div>
          ))}
        </nav>
        <div className="ml-auto flex shrink-0 items-center gap-2 md:ml-0 md:grid md:gap-2 md:border-t md:pt-3">
          {props.status && <div className="max-md:hidden">{props.status}</div>}
          {BOTTOM.map((item) => {
            const active = item.key === props.active;
            return <MenuLink key={item.key} item={item} active={active} badge={props.badges?.[item.key]} ref={active ? current : undefined} />;
          })}
          <ThemeToggle />
          {props.footer}
        </div>
      </aside>

      <div className="flex min-w-0 flex-1 flex-col md:p-2">
        <div className="relative flex min-h-0 flex-1 flex-col bg-background md:h-[calc(100svh-1rem)] md:overflow-y-auto md:rounded-xl md:border md:shadow-[0_1px_2px_oklch(0_0_0/0.04),0_16px_40px_-16px_var(--glass-shadow)]">
          <header className="sticky top-0 z-20 flex min-h-14 flex-wrap items-center gap-x-4 gap-y-2 border-b bg-background/80 px-4 py-2.5 backdrop-blur-md backdrop-saturate-150 lg:px-6">
            <motion.div
              key={props.active}
              className="mr-auto flex min-w-0 items-center gap-3"
              initial={{ opacity: 0, x: -6 }}
              animate={{ opacity: 1, x: 0 }}
              transition={{ duration: 0.3, ease: [0.22, 1, 0.36, 1] }}
            >
              <h1 className="text-base font-semibold tracking-tight text-foreground">{page.label}</h1>
              <Separator orientation="vertical" className="h-4! max-sm:hidden" />
              <span className="truncate text-sm text-muted-foreground max-sm:hidden">{page.blurb}</span>
            </motion.div>
            {props.actions && <div className="flex flex-wrap items-center gap-3">{props.actions}</div>}
          </header>
          {props.children}
        </div>
      </div>
    </div>
  );
}
