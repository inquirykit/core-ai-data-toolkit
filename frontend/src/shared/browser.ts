import { useEffect, useState } from "react";

/** What the location hash points at, by ``parse``, following the hash as it changes. */
export function useHash<T>(parse: (hash: string) => T): T {
  const [value, setValue] = useState(() => parse(location.hash));
  useEffect(() => {
    const follow = () => setValue(parse(location.hash));
    addEventListener("hashchange", follow);
    return () => removeEventListener("hashchange", follow);
  }, [parse]);
  return value;
}

/**
 * A location hash as its path, decoded part by part, and its query: "#/data/app/_graph?t=people" gives
 * parts ["data", "app", "_graph"] and t=people. Each section's parser picks what it needs out of these.
 */
export function parseHash(hash: string): { parts: string[]; params: URLSearchParams } {
  const [path, query = ""] = hash.replace(/^#\/?/, "").split("?");
  return { parts: path.split("/").filter(Boolean).map(decodeSegment), params: new URLSearchParams(query) };
}

/** ``value`` as it was ``ms`` ago, once it has stopped changing: typing in a filter is not a load per keystroke. */
export function useDebounced<T>(value: T, ms: number): T {
  const [settled, setSettled] = useState(value);
  useEffect(() => {
    const timer = setTimeout(() => setSettled(value), ms);
    return () => clearTimeout(timer);
  }, [value, ms]);
  return settled;
}

/** A hash segment decoded; one with a bad escape ("50%") as it is, rather than a URIError for the whole page. */
function decodeSegment(part: string): string {
  try {
    return decodeURIComponent(part);
  } catch {
    return part;
  }
}

/** Save ``blob`` as a file named ``name``: a link clicked for the user. */
function save(blob: Blob, name: string) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  a.click();
  URL.revokeObjectURL(url);
}

/** Save ``text`` as a file named ``name``. */
export function download(name: string, text: string, type = "application/json") {
  save(new Blob([text], { type }), name);
}

/** Save a reply's body (``client.raw``) as a file named ``name``. */
export async function downloadResponse(response: Response, name: string) {
  save(await response.blob(), name);
}
