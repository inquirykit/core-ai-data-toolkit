import { useEffect, useId, useRef, useState, type ComponentProps, type CSSProperties, type FormEvent, type ReactNode, type RefObject } from "react";
import { Check, CircleAlert, CircleCheck, Copy, KeyRound, LoaderCircle, Monitor, Moon, Plus, Sparkles, Sun, Trash2, X, type LucideIcon } from "lucide-react";
import { AnimatePresence, motion } from "motion/react";
import { ToggleGroup as ToggleGroupPrimitive } from "radix-ui";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
  AlertDialogTrigger,
} from "@/components/ui/alert-dialog";
import { Button, buttonVariants } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { cn } from "@/lib/utils";
import { fromItem, toItem } from "./select";
import type { Status } from "./types";
import { useTheme, type ThemeChoice } from "./theme";

const EASE = [0.22, 1, 0.36, 1] as const;

/** The product name with its mark, linking to the dashboard. */
export function Brand({ className }: { className?: string }) {
  return (
    <a
      className={cn(
        "group flex shrink-0 items-center gap-2.5 rounded-md px-2 py-1.5 font-semibold tracking-tight text-foreground outline-none focus-visible:ring-[3px] focus-visible:ring-ring/50",
        className,
      )}
      href="/admin#/overview"
    >
      <span
        className="grid size-7 place-items-center rounded-lg bg-linear-135 from-brand-1 to-brand-2 text-white shadow-[0_4px_12px_-3px_var(--brand-1)] transition-transform duration-300 group-hover:-rotate-8 group-hover:scale-105"
        aria-hidden="true"
      >
        <Sparkles className="size-4" strokeWidth={2.2} />
      </span>
      <span className="leading-tight max-md:hidden">AI &amp; Data Toolkit</span>
    </a>
  );
}

/** The bar at the top of a page without the menu (setup). ``children`` go on the right. */
export function Header({ page, children }: { page: string; children?: ReactNode }) {
  return (
    <header className="sticky top-0 z-20 flex h-14 items-center gap-3 border-b bg-background/80 px-4 backdrop-blur-md backdrop-saturate-150 lg:px-6">
      <Brand className="-ml-2" />
      <span className="h-4 w-px bg-border" aria-hidden="true" />
      <span className="mr-auto text-sm text-muted-foreground">{page}</span>
      {children}
      <ThemeToggle />
    </header>
  );
}

const THEMES: [ThemeChoice, string, typeof Sun][] = [
  ["light", "Light", Sun],
  ["dark", "Dark", Moon],
  ["system", "System", Monitor],
];

/** Light, dark or the system's: three small icon buttons. */
export function ThemeToggle() {
  const { choice, setTheme } = useTheme();
  return (
    <ToggleGroupPrimitive.Root
      type="single"
      value={choice}
      onValueChange={(value) => value && setTheme(value as ThemeChoice)}
      aria-label="Theme"
      className="inline-flex h-8 w-fit items-center gap-0.5 rounded-lg bg-muted p-0.5"
    >
      {THEMES.map(([value, label, Icon]) => (
        <Tooltip key={value}>
          <TooltipTrigger asChild>
            <ToggleGroupPrimitive.Item
              value={value}
              aria-label={label}
              className="relative grid h-7 w-7 place-items-center rounded-md text-muted-foreground outline-none transition-colors hover:text-foreground focus-visible:ring-[3px] focus-visible:ring-ring/50 data-[state=on]:text-foreground"
            >
              {choice === value && (
                <motion.span
                  layoutId="theme-pill"
                  className="absolute inset-0 rounded-md bg-background shadow-xs dark:bg-input/40"
                  transition={{ type: "spring", stiffness: 500, damping: 36 }}
                />
              )}
              <Icon className="relative size-3.5" />
            </ToggleGroupPrimitive.Item>
          </TooltipTrigger>
          <TooltipContent>{label}</TooltipContent>
        </Tooltip>
      ))}
    </ToggleGroupPrimitive.Root>
  );
}

/** A label above its control (input, select, textarea), the shadcn way. */
export function Field(props: { label: ReactNode; hint?: ReactNode; className?: string; children: ReactNode }) {
  return (
    <label className={cn("grid min-w-0 content-start gap-2", props.className)}>
      <span className="text-sm leading-none font-medium">{props.label}</span>
      {props.children}
      {props.hint && <span className="text-xs text-muted-foreground">{props.hint}</span>}
    </label>
  );
}

export interface SelectOption {
  value: string;
  label: ReactNode;
  disabled?: boolean;
  className?: string;
  title?: string;
}

// Radix takes no "" for an item's value: the option for the empty value goes by a stand-in.

/**
 * The control of a SelectField on its own, for a toolbar or a table cell (it takes an ``aria-label`` then).
 * ``none`` names the option for "" and is offered first; without one, and without an option whose value is
 * "", an empty ``value`` shows the ``placeholder``.
 */
export function SelectInput(props: {
  value: string;
  onChange(value: string): void;
  options: SelectOption[];
  none?: string;
  placeholder?: string;
  /** What the closed control shows, when not the chosen option's label (a short name for a long option) */
  display?: ReactNode;
  disabled?: boolean;
  size?: "sm" | "default";
  className?: string;
  contentClassName?: string;
  "aria-label"?: string;
}) {
  const options = props.none === undefined ? props.options : [{ value: "", label: props.none }, ...props.options];
  const hasEmpty = options.some((o) => o.value === "");
  return (
    <Select value={hasEmpty ? toItem(props.value) : props.value} onValueChange={(v) => props.onChange(fromItem(v))} disabled={props.disabled}>
      <SelectTrigger size={props.size} className={props.className} aria-label={props["aria-label"]}>
        <SelectValue placeholder={props.placeholder}>{props.display}</SelectValue>
      </SelectTrigger>
      <SelectContent className={props.contentClassName}>
        {options.map((o) => (
          <SelectItem key={toItem(o.value)} value={toItem(o.value)} disabled={o.disabled} className={o.className} title={o.title}>
            {o.label}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  );
}

/** A label above a select (a Field around a SelectInput): ``className`` sizes the field, ``triggerClassName`` the control. */
export function SelectField({
  label,
  hint,
  className,
  triggerClassName,
  ...input
}: ComponentProps<typeof SelectInput> & { label: ReactNode; hint?: ReactNode; triggerClassName?: string }) {
  return (
    <Field label={label} hint={hint} className={className}>
      <SelectInput {...input} className={cn("w-full", triggerClassName)} />
    </Field>
  );
}

/** A form's section title, with a line of help under it. */
export function Heading({ children, hint }: { children: ReactNode; hint?: ReactNode }) {
  return (
    <div className="grid gap-0.5 border-b pb-2">
      <h3 className="text-sm font-semibold">{children}</h3>
      {hint && <p className="text-xs text-muted-foreground">{hint}</p>}
    </div>
  );
}

/** One editable row of a dict field: what KVRows edits. */
export interface KV {
  key: string;
  value: string;
}

/** Rows of key and value with add and remove, for a dict field (query parameters, headers, environment variables). */
export function KVRows(props: {
  label: string;
  /** What the rows' accessible names start with; the label when left out */
  ariaLabel?: string;
  rows: KV[];
  onChange(rows: KV[]): void;
  keyLabel: string;
  valueLabel: string;
  keyPlaceholder?: string;
  valuePlaceholder?: string;
  addLabel: string;
  /** Shown as they are, with no adding or removing. */
  readOnly?: boolean;
}) {
  const update = (i: number, change: Partial<KV>) => props.onChange(props.rows.map((r, j) => (j === i ? { ...r, ...change } : r)));
  return (
    <div className="grid gap-2">
      <span className="text-sm leading-none font-medium">{props.label}</span>
      {props.rows.map((row, i) => (
        <div key={i} className="flex items-center gap-2">
          <Input
            aria-label={`${props.ariaLabel ?? props.label} ${i + 1}: ${props.keyLabel}`}
            placeholder={props.keyPlaceholder ?? props.keyLabel}
            autoComplete="off"
            spellCheck={false}
            readOnly={props.readOnly}
            className="h-8 w-44 font-mono text-xs"
            value={row.key}
            onChange={(e) => update(i, { key: e.target.value })}
          />
          <Input
            aria-label={`${props.ariaLabel ?? props.label} ${i + 1}: ${props.valueLabel}`}
            placeholder={props.valuePlaceholder ?? props.valueLabel}
            autoComplete="off"
            spellCheck={false}
            readOnly={props.readOnly}
            className="h-8 min-w-0 flex-1 font-mono text-xs"
            value={row.value}
            onChange={(e) => update(i, { value: e.target.value })}
          />
          {!props.readOnly && (
            <Button
              type="button"
              variant="ghost"
              size="icon"
              className="size-8 shrink-0 text-muted-foreground"
              aria-label={`Remove ${props.label.toLowerCase()} ${row.key || i + 1}`}
              onClick={() => props.onChange(props.rows.filter((_, j) => j !== i))}
            >
              <X />
            </Button>
          )}
        </div>
      ))}
      {props.readOnly ? (
        !props.rows.length && <span className="text-xs text-muted-foreground">None</span>
      ) : (
        <div>
          <Button type="button" variant="outline" size="sm" onClick={() => props.onChange([...props.rows, { key: "", value: "" }])}>
            <Plus aria-hidden="true" />
            {props.addLabel}
          </Button>
        </div>
      )}
    </div>
  );
}

/** A colored status dot with a soft halo; ``live`` makes the halo pulse. */
export function StatusDot({ color, live, className }: { color: string; live?: boolean; className?: string }) {
  return (
    <span
      aria-hidden="true"
      className={cn(
        "inline-block size-2 shrink-0 rounded-full shadow-[0_0_0_3px_color-mix(in_oklab,currentColor_18%,transparent)]",
        live && "animate-pulse-ring",
        className,
      )}
      style={{ background: color, color } as CSSProperties}
    />
  );
}

/** A status's dot and its words; ``live`` pulses the dot (running, loaded). */
export function StatusLabel({ status, live, className, title }: { status: Status; live?: boolean; className?: string; title?: string }) {
  return (
    <span className={cn("inline-flex items-center gap-2", className)} title={title}>
      <StatusDot color={status.color} live={live} />
      {status.label}
    </span>
  );
}

export function LoginForm(props: {
  title: string;
  description: ReactNode;
  submitLabel: string;
  message: string;
  onSubmit(key: string): void;
}) {
  const [key, setKey] = useState("");
  const submit = (event: FormEvent) => {
    event.preventDefault();
    props.onSubmit(key);
    setKey("");
  };
  return (
    <section className="mx-auto w-full max-w-sm px-4 py-[12vh]">
      <motion.div
        initial={{ opacity: 0, y: 16, scale: 0.98 }}
        animate={{ opacity: 1, y: 0, scale: 1 }}
        transition={{ duration: 0.45, ease: EASE }}
      >
        <Card className="shadow-lg">
          <CardHeader>
            <span
              className="mb-3 grid size-10 place-items-center rounded-xl bg-linear-135 from-brand-1 to-brand-2 text-white shadow-[0_6px_16px_-4px_var(--brand-1)]"
              aria-hidden="true"
            >
              <KeyRound className="size-5" />
            </span>
            <CardTitle className="text-lg">{props.title}</CardTitle>
            <CardDescription className="leading-relaxed">{props.description}</CardDescription>
          </CardHeader>
          <CardContent>
            <form className="grid gap-3" onSubmit={submit}>
              <Input
                type="password"
                autoComplete="current-password"
                aria-label={props.title}
                aria-invalid={Boolean(props.message) || undefined}
                placeholder="••••••••••••"
                required
                autoFocus
                value={key}
                onChange={(e) => setKey(e.target.value)}
              />
              <AnimatePresence initial={false}>
                {props.message && (
                  <motion.p
                    key={props.message}
                    className="flex items-center gap-1.5 text-sm text-destructive"
                    initial={{ opacity: 0, x: -6 }}
                    animate={{ opacity: 1, x: [0, -4, 4, -2, 0] }}
                    exit={{ opacity: 0 }}
                    transition={{ duration: 0.35 }}
                  >
                    <CircleAlert className="size-4 shrink-0" aria-hidden="true" />
                    {props.message}
                  </motion.p>
                )}
              </AnimatePresence>
              <Button type="submit" className="w-full">
                {props.submitLabel}
              </Button>
            </form>
          </CardContent>
        </Card>
      </motion.div>
    </section>
  );
}

export interface NoticeState {
  text: string;
  kind?: "ok" | "error";
}

/** A one-line status under a form: neutral, done (green) or failed (red). Empty takes no room. */
export function Notice({ notice, className }: { notice: NoticeState; className?: string }) {
  return (
    <div
      aria-live="polite"
      className={cn(
        "text-sm empty:hidden",
        notice.kind === "ok" && "text-good-text",
        notice.kind === "error" && "text-critical-text",
        !notice.kind && "text-muted-foreground",
        className,
      )}
    >
      {notice.text && (
        // A new key per message, so each one fades in.
        <motion.span
          key={notice.text}
          className="inline-flex items-start gap-1.5"
          initial={{ opacity: 0, y: 4 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.25, ease: EASE }}
        >
          {notice.kind === "ok" && <CircleCheck className="mt-0.5 size-4 shrink-0" aria-hidden="true" />}
          {notice.kind === "error" && <CircleAlert className="mt-0.5 size-4 shrink-0" aria-hidden="true" />}
          {notice.text}
        </motion.span>
      )}
    </div>
  );
}

/**
 * The button that runs an action (useAction): disabled and spinning while ``busy``. The spinner takes
 * the place of ``icon``, if any. A submit button unless ``type`` says otherwise.
 */
export function SubmitButton({ busy, icon, children, disabled, type = "submit", ...props }: ComponentProps<typeof Button> & { busy?: boolean; icon?: ReactNode }) {
  return (
    <Button type={type} disabled={busy || disabled} {...props}>
      {busy ? <LoaderCircle className="animate-spin" aria-hidden="true" /> : icon}
      {children}
    </Button>
  );
}

/**
 * Buttons of which one is on (shadcn Tabs look), with a pill that slides to
 * the chosen one. Each control needs its own pill, hence the ``useId``.
 */
export function Segmented<T extends string>(props: {
  label: string;
  options: [value: T, label: ReactNode][];
  value: T;
  onChange(value: T): void;
  size?: "sm" | "default";
  className?: string;
}) {
  const id = useId();
  return (
    <ToggleGroupPrimitive.Root
      type="single"
      value={props.value}
      // A click on the chosen one would clear it: keep it.
      onValueChange={(value) => value && props.onChange(value as T)}
      aria-label={props.label}
      className={cn(
        "inline-flex w-fit shrink-0 items-center rounded-lg bg-muted p-[3px] text-muted-foreground",
        props.size === "sm" ? "h-8" : "h-9",
        props.className,
      )}
    >
      {props.options.map(([value, label]) => (
        <ToggleGroupPrimitive.Item
          key={value}
          value={value}
          className={cn(
            "relative inline-flex h-full items-center justify-center gap-1.5 rounded-md px-3 font-medium whitespace-nowrap outline-none transition-colors hover:text-foreground focus-visible:ring-[3px] focus-visible:ring-ring/50 data-[state=on]:text-foreground",
            props.size === "sm" ? "text-xs" : "text-sm",
          )}
        >
          {props.value === value && (
            <motion.span
              layoutId={`seg-${id}`}
              className="absolute inset-0 rounded-md bg-background shadow-sm dark:border dark:border-input dark:bg-input/30"
              transition={{ type: "spring", stiffness: 500, damping: 36 }}
            />
          )}
          <span className="relative">{label}</span>
        </ToggleGroupPrimitive.Item>
      ))}
    </ToggleGroupPrimitive.Root>
  );
}

/**
 * Copies ``value``, falling back to selecting ``fallback`` and the old copy
 * command where the clipboard API is missing (it needs https or localhost).
 */
export function CopyButton({ value, fallback }: { value: string; fallback?: RefObject<HTMLInputElement | null> }) {
  const [copied, setCopied] = useState(false);
  const timer = useRef<number | undefined>(undefined);
  useEffect(() => () => clearTimeout(timer.current), []);

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(value);
    } catch {
      fallback?.current?.select();
      document.execCommand("copy");
    }
    setCopied(true);
    clearTimeout(timer.current);
    timer.current = window.setTimeout(() => setCopied(false), 1200);
  };
  return (
    <Button type="button" variant="outline" onClick={copy}>
      <AnimatePresence mode="popLayout" initial={false}>
        <motion.span
          key={copied ? "done" : "copy"}
          className="inline-flex"
          initial={{ opacity: 0, scale: 0.5 }}
          animate={{ opacity: 1, scale: 1 }}
          exit={{ opacity: 0, scale: 0.5 }}
          transition={{ duration: 0.18 }}
        >
          {copied ? <Check className="text-good-text" aria-hidden="true" /> : <Copy aria-hidden="true" />}
        </motion.span>
      </AnimatePresence>
      {copied ? "Copied" : "Copy"}
    </Button>
  );
}

/**
 * A destructive button (Delete, by default) that asks first; with a ``hint`` it is disabled and the hint says why.
 * ``iconOnly`` shows just the icon, with ``label`` as its accessible name; ``action`` names the dialog's confirm
 * button when it should differ from ``label``.
 */
export function ConfirmButton(props: {
  title: ReactNode;
  body: string;
  onConfirm(): void;
  label?: string;
  action?: string;
  cancel?: string;
  icon?: ReactNode;
  iconOnly?: boolean;
  size?: "sm" | "default";
  disabled?: boolean;
  hint?: string;
}) {
  const label = props.label ?? "Delete";
  const button = (
    <Button
      type="button"
      variant="outline"
      size={props.iconOnly ? (props.size === "default" ? "icon" : "icon-sm") : (props.size ?? "sm")}
      className="text-destructive hover:text-destructive"
      disabled={props.disabled || Boolean(props.hint)}
      aria-label={props.iconOnly ? label : undefined}
      title={props.iconOnly ? label : undefined}
    >
      {props.icon ?? <Trash2 aria-hidden="true" />}
      {!props.iconOnly && label}
    </Button>
  );
  // A disabled button takes no pointer events, so the hint goes on a wrapper, and nothing opens.
  if (props.hint) {
    return (
      <span title={props.hint} className="inline-flex">
        {button}
      </span>
    );
  }
  return (
    <AlertDialog>
      <AlertDialogTrigger asChild>{button}</AlertDialogTrigger>
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle className="[overflow-wrap:anywhere]">{props.title}</AlertDialogTitle>
          <AlertDialogDescription>{props.body}</AlertDialogDescription>
        </AlertDialogHeader>
        <AlertDialogFooter>
          <AlertDialogCancel>{props.cancel ?? "Cancel"}</AlertDialogCancel>
          <AlertDialogAction className={buttonVariants({ variant: "destructive" })} onClick={props.onConfirm}>
            {props.action ?? label}
          </AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}

/** A card saying there is nothing here yet: an icon, a title and a line of help. */
export function EmptyCard({ icon: Icon, title, body }: { icon: LucideIcon; title: string; body: ReactNode }) {
  return (
    <Card className="items-center gap-3 py-16 text-center">
      <span className="grid size-11 place-items-center rounded-xl bg-muted text-muted-foreground" aria-hidden="true">
        <Icon className="size-5" />
      </span>
      <CardTitle>{title}</CardTitle>
      <CardDescription className="max-w-md px-6">{body}</CardDescription>
    </Card>
  );
}
