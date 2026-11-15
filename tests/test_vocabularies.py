"""Lists of values kept in two places because one side cannot import the other: workflows/ groups the enrichers'
statuses (a domain does not import another, AGENTS.md), and the pages hold a few of db/'s limits that the API's
schema does not carry. Where the API's schema does carry a list, it is built from the domain's own (a response's
``Literal`` is the domain's), and the pages key theirs by the schema's types, so tsc fails instead.
"""

import re
from pathlib import Path
from typing import get_args

from db import schema
from enrichers import runner as enrichers
from workflows import nodes

ROOT = Path(__file__).resolve().parent.parent
DATA_TS = ROOT / "frontend/src/admin/data/data.ts"


def test_the_enrich_node_groups_every_enricher_status_once():
    groups = [nodes.ANSWERED, nodes.UNASKED, nodes.OFF]
    every = set(get_args(enrichers.Status))
    assert set().union(*groups) == every, "workflows/nodes.py's groups and enrichers/runner.py's Status differ"
    assert sum(map(len, groups)) == len(every), "a status is in two of workflows/nodes.py's groups"


def _constant(name: str) -> str:
    found = re.search(rf"export const {name}\b[^=]*= (.+?);\n", DATA_TS.read_text(), re.S)
    assert found, f"{name} is not in {DATA_TS.relative_to(ROOT)}"
    return found.group(1)


def test_the_pages_know_which_types_an_index_covers():
    assert re.findall(r'"(\w+)"', _constant("INDEXABLE")) == list(schema.INDEXABLE)


def test_the_pages_know_how_long_a_vector_may_be():
    assert int(_constant("MAX_DIMENSIONS")) == schema.MAX_DIMENSIONS
