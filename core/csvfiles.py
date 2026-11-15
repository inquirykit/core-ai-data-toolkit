"""
How a CSV file is written: its delimiter and its decimal mark, guessed from its first lines.

A data file's summary (jobs/data.py, read by DuckDB) and the AutoGluon jobs (jobs/autogluon/_common.py, read by
pandas, in another environment) read a file by this one guess, so the columns the page shows are the columns training
finds. A spreadsheet saved in many locales writes ";" between fields and "," in numbers ("1,5"); left to its own
sniffer, DuckDB would split such a file on the commas. The standard library only, so any environment can import it.
"""

import csv
import re
from typing import Tuple

HEAD_CHARS = 64 * 1024
DELIMITERS = ",;\t|"
_COMMA_DECIMAL = re.compile(r"-?\d+,\d+")
_DOT_DECIMAL = re.compile(r"-?\d+\.\d+")


def dialect(path: str, default: str = ",") -> Tuple[str, str]:
    """(delimiter, decimal mark) of the CSV file at ``path``: ``default`` when the delimiter cannot be told, and a
    decimal comma only for a file not split by commas whose numbers are written with one."""
    with open(path, encoding="utf-8", errors="replace", newline="") as f:
        head = f.read(HEAD_CHARS)
    head = head[:head.rfind("\n")] or head  # whole lines only
    try:
        delimiter = csv.Sniffer().sniff(head, delimiters=DELIMITERS).delimiter
    except csv.Error:
        delimiter = default
    if delimiter == ",":
        return delimiter, "."
    fields = [field.strip().strip('"') for line in head.splitlines()[1:] for field in line.split(delimiter)]
    commas = sum(1 for field in fields if _COMMA_DECIMAL.fullmatch(field))
    dots = sum(1 for field in fields if _DOT_DECIMAL.fullmatch(field))
    return delimiter, "," if commas > dots else "."
