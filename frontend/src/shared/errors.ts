import { toast } from "sonner";
import { AuthError } from "./api";

/** An error's text, for a notice or a toast. */
export const message = (e: unknown) => (e instanceof Error ? e.message : String(e));

/** The text of a failed call, or null for an AuthError: the sign-in prompt answers that one, nothing else should. */
export const failure = (e: unknown): string | null => (e instanceof AuthError ? null : message(e));

/** A catch handler that toasts ``title`` with the error; an AuthError is left to the sign-in prompt. */
export const failed = (title: string) => (e: unknown) => {
  const text = failure(e);
  if (text !== null) toast.error(title, { description: text });
};
