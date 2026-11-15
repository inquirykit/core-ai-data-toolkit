import { useCallback, useMemo, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { createClient, type ApiClient } from "./api";
import { readSession, writeSession } from "./storage";

export const ADMIN_KEY_STORAGE = "ai-toolkit-admin-key";

export interface Session {
  client: ApiClient;
  /** Non-null while the sign-in form should show, with the message to show on it. */
  login: { message: string } | null;
  signIn(key: string): void;
  signOut(): void;
  /** Whether a key is stored, so "Sign out" means something. */
  hasKey: boolean;
}

/** The key the page sends, kept for this browser tab only under ``storageKey``. */
export function useSession(storageKey: string): Session {
  const [login, setLogin] = useState<{ message: string } | null>(null);
  const queries = useQueryClient();

  // The storage name is a constant of the page, so this never changes.
  const readKey = useCallback(() => readSession(storageKey), []);
  const client = useMemo(() => createClient(readKey, (hadKey) => setLogin({ message: hadKey ? "That key was not accepted." : "" })), [readKey]);

  return {
    client,
    login,
    signIn: (key) => {
      writeSession(storageKey, key.trim());
      setLogin(null);
      // What failed for want of the key, and what an earlier key loaded, is asked for again with this one.
      void queries.invalidateQueries();
    },
    signOut: () => {
      writeSession(storageKey, "");
      location.reload();
    },
    hasKey: Boolean(readKey()),
  };
}
