// The one way the pages change things: useAction, a TanStack Query mutation
// with the pages' way of telling its outcome. ``report`` decides what is told
// (a toast, a line under the form, or nothing) apart from React, so it can be
// tested without a DOM; the hook runs the call and tells it.
import { useCallback, useLayoutEffect, useRef, useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { toast } from "sonner";
import type { NoticeState } from "./components";
import { failure } from "./errors";

/** What a call that worked says: a line, toasted; a notice, shown under the form; or nothing. */
export type Outcome = string | NoticeState | void;

export interface ActionOptions<A extends unknown[], R> {
  /** Said when the call works: a text, or one made from its reply and the arguments it ran with. */
  success?: string | ((result: R, ...args: A) => Outcome);
  /** Keep a failure under the form, as the ``notice``, instead of toasting it. */
  notice?: boolean;
  /** The error toast's title, a text or one made from the arguments; without one the error is the title. Not with ``notice``. */
  failed?: string | ((...args: A) => string);
}

export interface Action<A extends unknown[], R> {
  busy: boolean;
  /** The arguments of the call running, or null: for a row's own button among many. */
  running: A | null;
  /** The last outcome kept under the form; cleared when a call starts. */
  notice: NoticeState;
  /** Calls ``fn``: its reply, or undefined when it failed (the failure has been told). */
  run(...args: A): Promise<R | undefined>;
  /** Drop the notice: a dialog opening again, say. */
  clear(): void;
}

/** What to tell of one call: a toast, a notice for under the form (shown only if it is the newest call), or neither. */
export interface Report {
  toast?: { kind: "success" | "error"; title: string; description?: string };
  notice?: NoticeState;
}

/**
 * What the call made with ``args`` tells, by ``options``. A throw before
 * anything is sent (a form that does not validate) is told like a failed call;
 * an AuthError is told to nobody, the sign-in prompt being up.
 */
export function report<A extends unknown[], R>(
  options: ActionOptions<A, R>,
  args: A,
  outcome: { ok: true; result: R } | { ok: false; error: unknown },
): Report {
  if (outcome.ok) {
    const { success } = options;
    const said = typeof success === "function" ? success(outcome.result, ...args) : success;
    if (typeof said === "string") return { toast: { kind: "success", title: said } };
    return said ? { notice: said } : {};
  }
  const text = failure(outcome.error);
  if (text === null) return {};
  if (options.notice) return { notice: { text, kind: "error" } };
  const title = typeof options.failed === "function" ? options.failed(...args) : options.failed;
  return { toast: { kind: "error", title: title ?? text, description: title ? text : undefined } };
}

const IDLE: NoticeState = { text: "" };

/** A started call, as the mutation's variables: the mutation follows the work the button began. */
interface Call<A, R> {
  args: A;
  work: Promise<R>;
}

/**
 * ``fn`` as a button's work: ``busy`` while it runs, its failure (and, with ``options.success``, its outcome)
 * toasted, or kept in ``notice`` for a line under the form. Only the newest call's state is shown.
 */
export function useAction<A extends unknown[], R>(fn: (...args: A) => Promise<R>, options: ActionOptions<A, R> = {}): Action<A, R> {
  // ``fn`` and ``options`` are new closures each render; the call made uses the newest.
  const latest = useRef({ fn, options });
  useLayoutEffect(() => {
    latest.current = { fn, options };
  });
  const calls = useRef(0);
  const [notice, setNotice] = useState<NoticeState>(IDLE);
  const { mutateAsync, isPending, variables } = useMutation({ mutationFn: (call: Call<A, R>) => call.work });

  const run = useCallback(
    async (...args: A): Promise<R | undefined> => {
      const n = ++calls.current;
      setNotice(IDLE);
      // Begun here rather than by the mutation, which starts it a tick later: a submit's preventDefault()
      // has to run while the event is still being handled.
      const work = latest.current.fn(...args);
      let told: Report;
      let result: R | undefined;
      try {
        result = await mutateAsync({ args, work });
        told = report(latest.current.options, args, { ok: true, result });
      } catch (error) {
        told = report(latest.current.options, args, { ok: false, error });
      }
      if (told.toast?.kind === "success") toast.success(told.toast.title);
      else if (told.toast) toast.error(told.toast.title, { description: told.toast.description });
      if (told.notice && n === calls.current) setNotice(told.notice);
      return result;
    },
    [mutateAsync],
  );
  const clear = useCallback(() => setNotice(IDLE), []);

  return { busy: isPending, running: isPending && variables ? variables.args : null, notice, run, clear };
}
