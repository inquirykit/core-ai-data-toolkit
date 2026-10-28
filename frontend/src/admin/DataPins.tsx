// The tables pinned in the Data section: kept in this browser, listed first in
// the sidebar, and pinned or unpinned there, on the namespace's page or on the
// table's own.
import { useEffect, useState } from "react";
import { Pin } from "lucide-react";
import { Toggle } from "@/components/ui/toggle";
import { cn } from "@/lib/utils";
import { readLocal, writeLocal } from "../shared/storage";
import { pinKey } from "./data";

const STORAGE = "ai-toolkit.data.pins";

export interface Pins {
  /** "namespace/table", in the order pinned */
  keys: string[];
  has(namespace: string, table: string): boolean;
  toggle(namespace: string, table: string): void;
}

export function usePins(): Pins {
  const [keys, setKeys] = useState(() => {
    const stored = readLocal<{ tables: unknown }>(STORAGE).tables;
    return Array.isArray(stored) ? stored.filter((k): k is string => typeof k === "string") : [];
  });
  useEffect(() => writeLocal(STORAGE, { tables: keys }), [keys]);
  return {
    keys,
    has: (namespace, table) => keys.includes(pinKey(namespace, table)),
    toggle: (namespace, table) => {
      const key = pinKey(namespace, table);
      setKeys((ks) => (ks.includes(key) ? ks.filter((k) => k !== key) : [...ks, key]));
    },
  };
}

/**
 * Pins or unpins ``table`` of ``namespace``; its pin is filled while it is pinned. With ``text``, a button
 * that says Pin (the table's page); without, a small icon for a row of a list. A click goes no further,
 * so a row that opens on a click stays put.
 */
export function PinToggle(props: { pins: Pins; namespace: string; table: string; text?: boolean; className?: string }) {
  const pinned = props.pins.has(props.namespace, props.table);
  return (
    <Toggle
      variant={props.text ? "outline" : "default"}
      size="sm"
      pressed={pinned}
      // Not preventDefault: Radix's toggle then ignores the click.
      onClick={(e) => e.stopPropagation()}
      onPressedChange={() => props.pins.toggle(props.namespace, props.table)}
      aria-label={props.text ? undefined : `Pin ${props.table}`}
      title={pinned ? `Pinned at the top of the sidebar, in this browser. Click to unpin ${props.table}.` : `Pin ${props.table} at the top of the sidebar, in this browser`}
      className={cn(
        props.text ? "px-2.5" : "size-6 min-w-6 p-0 text-muted-foreground hover:bg-muted hover:text-foreground data-[state=on]:bg-transparent data-[state=on]:text-foreground [&_svg]:size-3.5",
        props.className,
      )}
    >
      <Pin className={cn(pinned && "fill-current")} aria-hidden="true" />
      {props.text && "Pin"}
    </Toggle>
  );
}
