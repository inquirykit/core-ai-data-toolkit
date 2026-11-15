// Light, dark, or the system's choice, and the style the pages are drawn in:
// modern (the default), terminal or metro, each in light and dark. Both are
// kept per browser; the class "dark" on <html> and its data-style are what the
// styles follow (globals.css, styles/). The pages' <head> sets them before the
// first paint (the same rule as ``isDark``; a stored name no style file knows
// draws as modern), so there is no flash.
import { useSyncExternalStore } from "react";

export type ThemeChoice = "light" | "dark" | "system";

/** The styles, by name: modern is globals.css alone, each other one is styles/<name>.css over it. */
export const STYLES = ["modern", "terminal", "metro"] as const;
export type StyleChoice = (typeof STYLES)[number];

const STORAGE = "ai-toolkit-theme";
const STYLE_STORAGE = "ai-toolkit-style";
const media = () => matchMedia("(prefers-color-scheme: dark)");

function read(key: string): string | null {
  try {
    return localStorage.getItem(key);
  } catch {
    return null;
  }
}

/** Keeps ``value`` under ``key``, or forgets it (``null``): the default is what nothing stored means. */
function keep(key: string, value: string | null) {
  try {
    if (value === null) localStorage.removeItem(key);
    else localStorage.setItem(key, value);
  } catch {
    /* storage unavailable: the choice is not kept, so it does not apply */
  }
}

function readChoice(): ThemeChoice {
  const value = read(STORAGE);
  return value === "light" || value === "dark" ? value : "system";
}

function readStyle(): StyleChoice {
  const value = read(STYLE_STORAGE);
  return STYLES.find((style) => style === value) ?? "modern";
}

const isDark = (choice: ThemeChoice) => choice === "dark" || (choice === "system" && media().matches);

const listeners = new Set<() => void>();
function apply() {
  const root = document.documentElement;
  const dark = isDark(readChoice());
  root.classList.toggle("dark", dark);
  root.style.colorScheme = dark ? "dark" : "light";
  root.dataset.style = readStyle();
  listeners.forEach((listener) => listener());
}

export function setTheme(choice: ThemeChoice) {
  keep(STORAGE, choice === "system" ? null : choice);
  apply();
}

export function setStyle(style: StyleChoice) {
  keep(STYLE_STORAGE, style === "modern" ? null : style);
  apply();
}

let following = false;
function subscribe(listener: () => void) {
  if (!following) {
    following = true;
    media().addEventListener("change", apply);
    // A choice made in another tab applies here too, rather than on this tab's next click.
    addEventListener("storage", apply);
  }
  listeners.add(listener);
  return () => void listeners.delete(listener);
}

const snapshot = () => {
  const root = document.documentElement;
  return `${readChoice()}|${root.classList.contains("dark") ? "dark" : "light"}|${readStyle()}`;
};

export function useTheme(): {
  choice: ThemeChoice;
  resolved: "light" | "dark";
  style: StyleChoice;
  setTheme(choice: ThemeChoice): void;
  setStyle(style: StyleChoice): void;
} {
  const [choice, resolved, style] = useSyncExternalStore(subscribe, snapshot).split("|") as [ThemeChoice, "light" | "dark", StyleChoice];
  return { choice, resolved, style, setTheme, setStyle };
}
