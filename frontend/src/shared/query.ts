// How the pages load what they show: TanStack Query. Every load is a query
// in the page's one cache, keyed by what it asks for (the client's
// ``useQuery("get", "/jobs", ...)`` keys by method, path and options; a load
// of several calls names its own key), so two views asking for the same
// thing share one request, and a write marks what it changed for loading
// again (``invalidateQueries``) instead of each view reloading by hand.
import { useEffect, useRef } from "react";
import { keepPreviousData, QueryClient } from "@tanstack/react-query";
import { failure } from "./errors";

/**
 * The cache of one page. Unlike TanStack Query's defaults, a failed load is
 * not asked again (a poll or a reload will), a query is loaded again while it
 * shows only when it polls, and the last value stays while the options change
 * (a new page of rows, a new range) until the new one arrives.
 */
export function createQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: { retry: false, refetchOnWindowFocus: false, placeholderData: keepPreviousData },
    },
  });
}

/**
 * Options for a query loaded again every ``every`` ms while the tab shows, and at once when it comes back:
 * a number, or one worked out from the data; 0 for none.
 */
export function poll<T>(every: number | ((data: T | undefined) => number)) {
  return {
    refetchInterval: (query: { state: { data: T | undefined } }) => (typeof every === "function" ? every(query.state.data) : every) || false,
    refetchOnWindowFocus: true,
  } as const;
}

/**
 * Load again (``refetch``) when ``value`` changes, not when the component first shows it: for a load that
 * depends on something it does not send, such as a job ending or a model being loaded elsewhere.
 */
export function useReloadOn(value: unknown, refetch: () => unknown): void {
  const last = useRef(value);
  useEffect(() => {
    if (Object.is(last.current, value)) return;
    last.current = value;
    void refetch();
  }, [value, refetch]);
}

/** A load's error as the page shows it: "" for none, and for an AuthError, which the sign-in prompt answers. */
export const errorText = (error: unknown): string => (error == null ? "" : (failure(error) ?? ""));
