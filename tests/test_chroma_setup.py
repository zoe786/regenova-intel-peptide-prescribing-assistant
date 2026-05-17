"""Tests for shared Chroma collection configuration."""

from __future__ import annotations

import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))

from pipelines.common import chroma


class _FakeOpenAIEmbeddingFunction:
    def __init__(self, api_key: str, model_name: str) -> None:
        self.api_key = api_key
        self.model_name = model_name


class _FakeClient:
    def __init__(self, path: str, raise_embedding_conflict: bool = False) -> None:
        self.path = path
        self.calls: list[dict] = []
        self.raise_embedding_conflict = raise_embedding_conflict

    def get_or_create_collection(self, **kwargs):
        if self.raise_embedding_conflict:
            raise RuntimeError("embedding function already exists in collection configuration")
        self.calls.append(kwargs)
        return {"kwargs": kwargs}

    def get_collection(self, **kwargs):
        self.calls.append({"fallback": kwargs})
        return {"kwargs": kwargs}


def _install_fake_chromadb(monkeypatch, *, raise_embedding_conflict: bool = False):
    client_holder: dict[str, _FakeClient] = {}

    def _persistent_client(path: str):
        client = _FakeClient(path, raise_embedding_conflict=raise_embedding_conflict)
        client_holder["client"] = client
        return client

    embedding_functions = types.SimpleNamespace(
        OpenAIEmbeddingFunction=_FakeOpenAIEmbeddingFunction,
    )
    chromadb_mod = types.ModuleType("chromadb")
    chromadb_mod.PersistentClient = _persistent_client

    chromadb_utils_mod = types.ModuleType("chromadb.utils")
    chromadb_utils_mod.embedding_functions = embedding_functions

    monkeypatch.setitem(sys.modules, "chromadb", chromadb_mod)
    monkeypatch.setitem(sys.modules, "chromadb.utils", chromadb_utils_mod)
    return client_holder


def test_get_collection_uses_local_embedding_when_no_openai_key(monkeypatch):
    client_holder = _install_fake_chromadb(monkeypatch)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)

    chroma.get_collection("./data/chroma_db")

    call = client_holder["client"].calls[0]
    assert call["name"] == chroma.CHROMA_COLLECTION_NAME
    assert call["metadata"] == chroma.CHROMA_COLLECTION_METADATA
    assert isinstance(call["embedding_function"], chroma.LocalDeterministicEmbeddingFunction)


def test_get_collection_uses_openai_embedding_when_key_exists(monkeypatch):
    client_holder = _install_fake_chromadb(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    chroma.get_collection("./data/chroma_db")

    call = client_holder["client"].calls[0]
    embedding = call["embedding_function"]
    assert isinstance(embedding, _FakeOpenAIEmbeddingFunction)
    assert embedding.api_key == "test-key"
    assert embedding.model_name == chroma.CHROMA_EMBEDDING_MODEL


def test_get_collection_falls_back_when_embedding_conflicts(monkeypatch):
    client_holder = _install_fake_chromadb(monkeypatch, raise_embedding_conflict=True)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)

    chroma.get_collection("./data/chroma_db")

    call = client_holder["client"].calls[0]
    assert call["fallback"]["name"] == chroma.CHROMA_COLLECTION_NAME


def test_local_embedding_supports_query_and_documents_interfaces():
    embedding_fn = chroma.LocalDeterministicEmbeddingFunction(dimensions=16)
    doc_embeddings = embedding_fn.embed_documents(["BPC-157 tendon healing"])
    query_embedding = embedding_fn.embed_query("BPC-157 healing")
    list_query_embedding = embedding_fn.embed_query(["BPC-157 healing"])
    assert len(doc_embeddings) == 1
    assert len(doc_embeddings[0]) == 16
    assert len(query_embedding) == 1
    assert len(query_embedding[0]) == 16
    assert len(list_query_embedding) == 1
    assert len(list_query_embedding[0]) == 16
