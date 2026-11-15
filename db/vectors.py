"""
Vector fields: a part of TableStore (tables.py, where the model is told). A
field with a model is embedded from its text by the async writes, before
their blocking part (``_embedded``), once the owner, the rows and the
table's room are checked; its dimensions are found by embedding a probe
text when it is made (``_probed``); ``search`` finds the nearest rows,
ranked by the text's words too (hybrid, ``fused``), or read again by a
rerank model (``_reranked``).

The field's prompts go before the texts its model embeds: the document
prompt before a row's, the query prompt before a search's. Left out when
the field is made, they are the model's own prompts named ``query`` and
``document`` (``prompts``: sentence-transformers' config, through the
model manager), or none; a model whose prompts have other names (Harrier's
web_search_query) is refused, its prompts listed, since which one a search
wants is the admin's to say.
"""

import logging
from dataclasses import replace
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence, Tuple

from core.errors import TooLarge, Unavailable

from .engine import quote
from .schema import (row_type, any_words,
    check_name, check_row, EMPTY,
    Field, MAX_K, NotFound, TableError, TableSchema)
from .rows import Filters, Row

logger = logging.getLogger(__name__)

# The rows a table with a vector field holds at most unless told otherwise (VECTOR_MAX_ROWS is the service's).
VECTOR_MAX_ROWS = 100_000

# A vector field's distance to the search's vector ($_tk_vector), lower nearer, by its similarity: 1 minus the cosine,
# the squared distance, and -(1 + the dot product) / 2 (-1 to 0 for unit vectors).
DISTANCES = {
    "COSINE": "array_cosine_distance({f}, {v})",
    "EUCLIDEAN": "pow(array_distance({f}, {v}), 2)",
    "DOT_PRODUCT": "-(1 + array_inner_product({f}, {v})) / 2",
}

# How many times k rows a hybrid search takes from each of its two rankings before it fuses them, and a search with a
# reranker gives it to read: at most MAX_K.
DEPTH = 4
# Reciprocal rank fusion's constant: a row's score is the sum of 1 / (RRF_K + its rank) over the rankings it is in.
# 60 is the paper's (Cormack, Clarke and Büttcher, 2009), and Elasticsearch's default.
RRF_K = 60


# What a vector field's dimensions are found by, when it is made with a model and without them.
PROBE = "dimension probe"


# An embedding model's vectors for texts, and the tokens they took: (model, texts) -> (vectors, tokens).
Embed = Callable[[str, List[str]], Awaitable[Tuple[List[List[float]], int]]]
# An embedding model's own prompts, by name: model -> {name: text}.
Prompts = Callable[[str], Awaitable[Dict[str, str]]]
# A rerank model's scores for documents, in their order, and the tokens they took: (model, query, documents) ->
# (scores, tokens).
Rerank = Callable[[str, str, List[str]], Awaitable[Tuple[List[float], int]]]


class Vectors:
    """TableStore's vector fields: embedding, room, nearest rows (a mixin: tables.py builds the store)."""

    def _check_room(self, workspace: str, table: str, schema: TableSchema, ids: Sequence[str], db=None) -> None:
        """TooLarge if writing rows with these ids would take a table with a vector field past ``vector_max_rows``."""
        if schema.vector is None or not ids or not self._has(workspace, table):
            return
        t, ids = row_type(workspace, table), list(dict.fromkeys(ids))
        have = self._query(f"SELECT count(*) AS n FROM {t}", db=db)[0]["n"]
        if have + len(ids) <= self.vector_max_rows:
            return
        there = self._query(f"SELECT count(*) AS n FROM {t} WHERE id = ANY($ids)", {"ids": ids}, db)[0]["n"]
        if have + len(ids) - there > self.vector_max_rows:
            raise TooLarge(f"{workspace}/{table} would hold {have + len(ids) - there} rows, past VECTOR_MAX_ROWS "
                           f"({self.vector_max_rows}): a search computes every row's distance, so its time grows "
                           "with them")

    async def _embedding(self, model: str, texts: List[str]) -> Tuple[List[List[float]], int]:
        if self.embed is None:
            raise Unavailable("Vector fields with a model need the model manager to embed with")
        return await self.embed(model, texts)

    async def _probed(self, workspace: str, table: str, schema: TableSchema, owner: Optional[str]) -> TableSchema:
        """The schema, its vector field's dimensions found by embedding a probe text with its model when the field is
        made now: so a model that does not embed, or one outside ALLOWED_MODELS, is refused before anything changes,
        and dimensions given are checked against the model's. Prompts left out are the field's as it is, or, for a
        field made now, the model's (``_model_prompts``). The owner is checked first."""
        field = schema.vector
        if field is None or field.model is None:
            return schema
        await self._loaded_now()
        self._check_owner(workspace, [table], owner, rows=False)
        was = self._tables(workspace).get(table, EMPTY).vector
        if was is not None and (was.name, was.model) == (field.name, field.model):
            field = replace(field, query_prompt=_given(field.query_prompt, was.query_prompt),
                            document_prompt=_given(field.document_prompt, was.document_prompt))
        if was is not None and replace(field, dimensions=was.dimensions).vector_options == was.vector_options \
                and field.dimensions in (None, was.dimensions):
            dimensions = was.dimensions  # the field as it is: no model time spent
        else:
            vectors, _ = await self._embedding(field.model, [PROBE])
            dimensions = len(vectors[0])
            if field.dimensions is not None and field.dimensions != dimensions:
                raise TableError(f"{field.name}: {field.model} makes vectors of {dimensions} numbers, "
                                 f"not {field.dimensions}")
            if field.query_prompt is None or field.document_prompt is None:
                query, document = await self._model_prompts(field)
                field = replace(field, query_prompt=_given(field.query_prompt, query),
                                document_prompt=_given(field.document_prompt, document))
        return replace(schema, fields={**schema.fields, field.name: replace(field, dimensions=dimensions)})

    async def _model_prompts(self, field: Field) -> Tuple[str, str]:
        """The query and document prompts of the field's model: those it names query and document, "" for one it
        lacks. TableError when the query prompt is left out and the model has prompts but none named query."""
        prompts = await self.prompts(field.model) if self.prompts is not None else {}
        if field.query_prompt is None and prompts and "query" not in prompts:
            named = "; ".join(f"{name}: {text!r}" for name, text in prompts.items())
            raise TableError(f"{field.name}: {field.model} names its prompts {named}, none of them query: give the "
                             f"field's query_prompt (and document_prompt, \"\" for none)")
        return prompts.get("query", ""), prompts.get("document", "")

    async def search(self, workspace: str, table: str, *, vector: Optional[List[float]] = None,
                     text: Optional[str] = None, k: int = 10, filters: Filters = (),
                     words: Optional[str] = None, hybrid: bool = False, rerank: Optional[str] = None) -> Dict[str, Any]:
        """The ``k`` rows nearest ``vector``, or ``text`` embedded with the vector field's model, nearest first,
        among those the filters (as the list's) and the full-text ``words`` (as its _search) select: ``{results:
        [{row, distance}], tokens}``, the rows without their vector, ``tokens`` only when a text was embedded. A
        distance is the field's similarity's (``DISTANCES``), lower nearer: for COSINE 1 minus the cosine, for
        EUCLIDEAN the squared distance (0 for both is the same vector), for DOT_PRODUCT -(1 + the dot product) / 2
        (-1 to 0 for unit vectors). Every row the filters and words select is compared: the nearest are exact.

        With ``hybrid``, the text's words rank the rows too: the nearest rows and those the full-text index scores
        best for the words (schema.any_words), ``DEPTH`` times ``k`` of each, are fused by their ranks
        (``fused``), best first, each with its ``score``; a row only the words found has no distance.

        With a ``rerank`` model, ``DEPTH`` times ``k`` rows found so are read by it, the text against each row's
        ``from`` text, and the ``k`` it scores best come back, best first, each with its score (``_reranked``)."""
        await self._loaded_now()
        if not self._has(workspace, table):
            raise NotFound(f"No table {table!r} in {workspace!r}")
        field = self._schema(workspace, table).vector
        if field is None:
            raise TableError(f"{table} has no vector field to search: add one to its schema")
        if (vector is None) == (text is None):
            raise TableError("Search by a vector or by a text: one of them")
        if not 1 <= k <= MAX_K:
            raise TableError(f"k is 1 to {MAX_K}")
        if hybrid and text is None:
            raise TableError("hybrid ranks by a text's words as well as its meaning: search by a text")
        if hybrid and not self._schema(workspace, table).fulltext:
            raise TableError(f"{table} has no full-text field for hybrid to rank its words by: make a string field "
                             "full-text in its schema")
        if rerank is not None and text is None:
            raise TableError("A reranker reads the text against each row's: search by a text")
        asked, ranking = text, text if hybrid else None
        tokens = None
        if text is not None:
            if field.model is None:
                raise TableError(f"{table}.{field.name} has no model to embed a text with: search by a vector of "
                                 f"{field.dimensions} numbers")
            text = (field.query_prompt or "") + text
            if self.embed_max_chars is not None and len(text) > self.embed_max_chars:
                raise TableError(f"The text with {field.name}'s query prompt is {len(text)} characters, past "
                                 f"MAX_INPUT_CHARS ({self.embed_max_chars})")
            vectors, tokens = await self._embedding(field.model, [text])
            vector = vectors[0]
        field.check({field.name: vector})
        depth = min(MAX_K, DEPTH * k) if hybrid or rerank is not None else k
        keep = depth if rerank is not None else k
        # With words to find, the full-text index may be rebuilt first: a long call (db/engine.py, Database).
        results = await self._run(self._search, workspace, table, field, vector, keep, depth, filters, words, ranking,
                                  long=words is not None or ranking is not None)
        if rerank is not None:
            results, read = await self._reranked(rerank, asked, field, results, k)
            tokens += read
        return {"results": results, **({"tokens": tokens} if tokens is not None else {})}

    def _search(self, workspace: str, table: str, field: Field, vector: List[float], keep: int, depth: int,
                filters: Filters, words: Optional[str], ranking: Optional[str]) -> List[Dict[str, Any]]:
        """The search's hits: the ``keep`` nearest rows, or with a ``ranking`` text, the ``keep`` best of the
        ``depth`` nearest and the ``depth`` its words find best, fused."""
        nearest = self._nearest(workspace, table, field, vector, depth, filters, words)
        scores = None
        ids = [i for i, _ in nearest][:keep]
        if ranking is not None:
            best = fused([i for i, _ in nearest], self._best_by_words(workspace, table, filters, words, ranking,
                                                                      depth))[:keep]
            ids, scores = [i for i, _ in best], dict(best)
        distances = dict(nearest)
        # The rows are read apart: a row's own field named distance would be lost among the neighbors' fields.
        rows = {r["id"]: r for r in self._bulk_get(workspace, table, ids)}
        return [{"row": rows[i], **({"distance": distances[i]} if i in distances else {}),
                 **({"score": scores[i]} if scores is not None else {})} for i in ids if i in rows]

    def _nearest(self, workspace: str, table: str, field: Field, vector: List[float], k: int, filters: Filters,
                 words: Optional[str]) -> List[Tuple[str, float]]:
        """The ids of the ``k`` rows nearest ``vector`` among those the filters and words select, with their
        distances, nearest first."""
        source, where, params = self._filtered(workspace, table, filters, words)
        distance = DISTANCES[field.similarity].format(f=quote(field.name), v=f"$_tk_vector::FLOAT[{int(field.dimensions)}]")
        has = f"{quote(field.name)} IS NOT NULL"
        where = f"{where} AND {has}" if where else f" WHERE {has}"
        params.update(_tk_vector=[float(x) for x in vector], _tk_k=k)
        hits = self._query(f"SELECT id, {distance} AS distance FROM {source or row_type(workspace, table)}{where} "
                           "ORDER BY distance, id LIMIT $_tk_k", params)
        return [(h["id"], h["distance"]) for h in hits]

    def _best_by_words(self, workspace: str, table: str, filters: Filters, words: Optional[str], text: str,
                       limit: int) -> List[str]:
        """The ids of the ``limit`` rows the full-text index scores best for the text's words (any of them; those
        of ``words`` as well, when given), among those the filters select, best first."""
        rank = any_words(text)
        if rank is None:
            return []
        source, where, params = self._filtered(workspace, table, filters, words, rank)
        params["_tk_limit"] = limit
        rows = self._query(f"SELECT id FROM {source}{where} ORDER BY _score DESC NULLS FIRST, id LIMIT $_tk_limit",
                           params)
        return [r["id"] for r in rows]

    async def _reranked(self, model: str, text: str, field: Field, hits: List[Dict[str, Any]],
                        k: int) -> Tuple[List[Dict[str, Any]], int]:
        """The ``k`` hits the rerank ``model`` scores best for ``text``, reading each row's ``from`` text, best first,
        each with the reranker's score in place of the fused one, and the tokens it took. A row with no such text
        to read (one a hybrid search found by the words of other full-text fields) comes after them, unscored."""
        if self.rerank is None:
            raise Unavailable("A search with a reranker needs the model manager to rerank with")
        has_text = [isinstance(h["row"].get(field.from_), str) and h["row"][field.from_].strip() != "" for h in hits]
        read = [h for h, has in zip(hits, has_text) if has]
        # A row written before its table had the vector field was never checked for length (nothing embedded it):
        # its text is cut to MAX_INPUT_CHARS, the longest a model is given.
        documents = [h["row"][field.from_][:self.embed_max_chars] for h in read]
        scores, tokens = await self.rerank(model, text, documents) if read else ([], 0)
        ranked = [{**h, "score": score} for score, h in sorted(zip(scores, read), key=lambda pair: -pair[0])]
        unread = [{key: value for key, value in h.items() if key != "score"} for h, has in zip(hits, has_text) if not has]
        return (ranked + unread)[:k], tokens

    async def _embedded(self, workspace: str, puts: Dict[str, List[Row]], owner: Optional[str],
                        patch: bool = False, touched: Sequence[str] = ()) -> Dict[str, List[Row]]:
        """``puts`` (table -> rows) with the vector of each table's field with a model embedded from the row's text,
        or null for a row without one: one batch per model, whatever the rows' tables. As a ``patch``, a row that
        leaves the text out leaves the vector as it is. TableError for a row that sends the vector itself, or whose
        text is longer than the embedder takes. Before any model time is spent, what the blocking write would
        refuse anyway is checked: the owner of every table the write touches (``puts``' and ``touched``: a
        transaction's deletes and edges), and, when there are texts to embed, the rows as their tables' schemas
        have them (not a patch's, which is merged into a row first) and the room left in a table with a vector
        field (``vector_max_rows``). What only the write itself finds (an insert's id taken, a unique value) comes
        after the model has run. The rows given are not changed."""
        await self._loaded_now()
        tables = self._tables(workspace)
        self._check_owner(workspace, [*puts, *touched], owner)
        fields = {table: schema.vector for table, schema in tables.items()
                  if table in puts and schema.vector is not None and schema.vector.model is not None}
        if not fields:
            return puts
        wanted: Dict[str, List[Tuple[Row, Field, str]]] = {}
        out: Dict[str, List[Row]] = {}
        for table, rows in puts.items():
            schema, field = tables.get(check_name("table", table), EMPTY), fields.get(table)
            out[table] = []
            for row in rows:
                if field is not None and isinstance(row, dict):
                    if row.get(field.name) is not None:
                        raise TableError(f"{field.name} is embedded from {field.from_}: a row does not send it")
                    if not patch or field.from_ in row:
                        row = {**row, field.name: None}
                        text = row.get(field.from_)
                        if isinstance(text, str) and text.strip():
                            text = (field.document_prompt or "") + text
                            self._check_text(table, row, field, text)
                            wanted.setdefault(field.model, []).append((row, field, text))
                if not patch:
                    check_row(row, schema)
                out[table].append(row)
        if wanted and not patch:
            def check_room() -> None:
                for table in fields:
                    self._check_room(workspace, table, tables[table], [row["id"] for row in out[table]])
            await self._run(check_room)
        for model, items in wanted.items():
            vectors, _ = await self._embedding(model, [text for _, _, text in items])
            for (row, field, _), embedded in zip(items, vectors):
                if len(embedded) != field.dimensions:
                    raise TableError(f"{model} made a vector of {len(embedded)} numbers, but {field.name} holds "
                                     f"{field.dimensions}")
                row[field.name] = embedded
        return out

    def _check_text(self, table: str, row: Row, field: Field, text: str) -> None:
        """TableError, naming the row, its text field and the limit, for a text (its prompt in) longer than the
        embedder takes: its own refusal would say only that the request was invalid."""
        if self.embed_max_chars is not None and len(text) > self.embed_max_chars:
            which = f"Row {row['id']!r} of {table}" if isinstance(row.get("id"), str) else f"A row of {table}"
            what = f"{field.from_} with its prompt" if field.document_prompt else field.from_
            raise TableError(f"{which}: {what} is {len(text)} characters, past MAX_INPUT_CHARS "
                             f"({self.embed_max_chars}), the longest text a model is given to embed")


def _given(value: Optional[str], otherwise: Optional[str]) -> Optional[str]:
    """``value``, or ``otherwise`` when it was left out (None)."""
    return otherwise if value is None else value


def fused(*rankings: Sequence[str]) -> List[Tuple[str, float]]:
    """Rankings of ids (best first) as one, by reciprocal rank fusion: each id scores the sum of 1 / (RRF_K + its
    rank, from 1) over the rankings it is in, so two unlike scores (a distance, a full-text score) need no common
    scale. Best first; ids that tie stay in the order first met."""
    scores: Dict[str, float] = {}
    for ranking in rankings:
        for rank, id in enumerate(ranking, 1):
            scores[id] = scores.get(id, 0.0) + 1 / (RRF_K + rank)
    return sorted(scores.items(), key=lambda item: -item[1])
