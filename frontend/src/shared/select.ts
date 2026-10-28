// Radix's Select has no empty value: "" means "show the placeholder". The
// pages' empty choices (none, any, detect…) travel as this item instead.
export const EMPTY_ITEM = "\u0000empty";

/** The item the select shows for ``value``. */
export const toItem = (value: string): string => (value === "" ? EMPTY_ITEM : value);

/** The value the page gets for the ``item`` picked. */
export const fromItem = (item: string): string => (item === EMPTY_ITEM ? "" : item);
