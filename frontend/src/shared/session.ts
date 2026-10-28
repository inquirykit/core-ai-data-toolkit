import { useCallback, useMemo, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { createClient, type ApiClient } from "./api";
import { readSession, writeSession } from "./storage";

export const ADMIN_KEY_STORAGE = "ai-toolkit-admin-key";

export interface Session {
  client: ApiClient;
  /** Non-null while the sign-in form should show, with the message to show on it. */
  login: { message: string } | null;
  showLogin(message: string): void;
  signIn(key: string): void;
  signOut(): void;
  /** Whether a key is stored, so "Sign out" means something. */
  hasKey: boolean;
  /** Changes on every sign-in, so pages can start over with the new key (every query loads again by itself). */
  generation: number;
}

/**
 * The key a page sends, kept for this browser tab only. ``fallbackKeys`` are
 * other pages' keys that this page's routes also accept (the admin key works
 * everywhere), so a signed-in admin needs no second key.
 */
export function useSession(storageKey: string, fallbackKeys: string[] = [], headers: Record<string, string> = {}): Session {
  const [login, setLogin] = useState<{ message: string } | null>(null);
  const [generation, setGeneration] = useState(0);
  const queries = useQueryClient();

  // The storage names are constants of the page, so this never changes.
  const readKey = useCallback(() => [storageKey, ...fallbackKeys].map(readSession).find(Boolean) ?? "", []);
  const client = useMemo(
    () => createClient(readKey, (hadKey) => setLogin({ message: hadKey ? "That key was not accepted." : "" }), headers),
    // eslint-disable-next-line react-hooks/exhaustive-deps -- the headers are constants of the page
    [readKey],
  );

  return {
    client,
    login,
    showLogin: (message) => setLogin({ message }),
    signIn: (key) => {
      writeSession(storageKey, key.trim());
      setLogin(null);
      setGeneration((g) => g + 1);
      // What failed for want of the key, and what an earlier key loaded, is asked for again with this one.
      void queries.invalidateQueries();
    },
    signOut: () => {
      for (const name of [storageKey, ...fallbackKeys]) writeSession(name, "");
      location.reload();
    },
    hasKey: Boolean(readKey()),
    generation,
  };
}
