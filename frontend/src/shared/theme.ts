// Light, dark, or the system's choice. The choice is kept per browser; the
// class "dark" on <html> is what the styles follow. The pages' <head> sets it
// before the first paint (the same rule as ``isDark``), so there is no flash.
import { useSyncExternalStore } from "react";

export type ThemeChoice = "light" | "dark" | "system";

const STORAGE = "ai-toolkit-theme";
const media = () => matchMedia("(prefers-color-scheme: dark)");

function readChoice(): ThemeChoice {
  try {
    const value = localStorage.getItem(STORAGE);
    return value === "light" || value === "dark" ? value : "system";
  } catch {
    return "system";
  }
}

const isDark = (choice: ThemeChoice) => choice === "dark" || (choice === "system" && media().matches);

const listeners = new Set<() => void>();
function apply() {
  const dark = isDark(readChoice());
  document.documentElement.classList.toggle("dark", dark);
  document.documentElement.style.colorScheme = dark ? "dark" : "light";
  listeners.forEach((listener) => listener());
}

export function setTheme(choice: ThemeChoice) {
  try {
    if (choice === "system") localStorage.removeItem(STORAGE);
    else localStorage.setItem(STORAGE, choice);
  } catch {
    /* storage unavailable: applies to this page only */
  }
  apply();
}

let following = false;
function subscribe(listener: () => void) {
  if (!following) {
    following = true;
    media().addEventListener("change", apply);
  }
  listeners.add(listener);
  return () => void listeners.delete(listener);
}

const snapshot = () => `${readChoice()}|${document.documentElement.classList.contains("dark") ? "dark" : "light"}`;

export function useTheme(): { choice: ThemeChoice; resolved: "light" | "dark"; setTheme(choice: ThemeChoice): void } {
  const [choice, resolved] = useSyncExternalStore(subscribe, snapshot).split("|") as [ThemeChoice, "light" | "dark"];
  return { choice, resolved, setTheme };
}
