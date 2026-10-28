"""
The embedded ArcadeDB database every store shares (engine.py), document
collections with vector search (store.py), and namespaced tables of JSON rows
(schema.py: what a table is; tables.py: the store; graph.py: edges between rows).
"""

from .engine import BatchWriter, Database, DocumentStore, StoreError, Unavailable, on_pool
from .store import Collection, CollectionError, CollectionStore, FIELD_TYPES, NotFound

__all__ = ["BatchWriter", "Collection", "CollectionError", "CollectionStore", "Database", "DocumentStore",
           "FIELD_TYPES", "NotFound", "StoreError", "Unavailable", "on_pool"]
