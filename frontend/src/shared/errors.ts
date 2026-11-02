import { AuthError } from "./api";

/** An error's text, for a notice or a toast. */
export const message = (e: unknown) => (e instanceof Error ? e.message : String(e));

/** The text of a failed call, or null for an AuthError: the sign-in prompt answers that one, nothing else should. */
export const failure = (e: unknown): string | null => (e instanceof AuthError ? null : message(e));
