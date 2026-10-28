// Browser storage can be unavailable (private windows, blocked site data), so
// every access is guarded and a failure reads as "nothing stored".

export function readSession(name: string): string {
  try {
    return sessionStorage.getItem(name) ?? "";
  } catch {
    return "";
  }
}

export function writeSession(name: string, value: string): void {
  try {
    if (value) sessionStorage.setItem(name, value);
    else sessionStorage.removeItem(name);
  } catch {
    /* storage unavailable */
  }
}

export function readLocal<T extends object>(name: string): Partial<T> {
  try {
    const value: unknown = JSON.parse(localStorage.getItem(name) ?? "null");
    return value && typeof value === "object" ? (value as Partial<T>) : {};
  } catch {
    return {};
  }
}

export function writeLocal(name: string, value: object): void {
  try {
    localStorage.setItem(name, JSON.stringify(value));
  } catch {
    /* storage unavailable */
  }
}
