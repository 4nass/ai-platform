"""Local vector index (Qdrant in file mode, no server)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from qdrant_client import QdrantClient
from qdrant_client.models import Distance, PointStruct, VectorParams

from core.context.chunking import Chunk
from core.context.embeddings import VECTOR_SIZE

COLLECTION_NAME = "chunks"


class VectorStore:
    def __init__(self, storage_path: Path):
        self._client = QdrantClient(path=str(storage_path))

    def reset(self) -> None:
        if self._client.collection_exists(COLLECTION_NAME):
            self._client.delete_collection(COLLECTION_NAME)
        self._client.create_collection(
            collection_name=COLLECTION_NAME,
            vectors_config=VectorParams(size=VECTOR_SIZE, distance=Distance.COSINE),
        )

    def add(self, chunks: list[Chunk], vectors: list[list[float]]) -> None:
        points = [
            PointStruct(
                id=i,
                vector=vector,
                payload={
                    "path": chunk.path,
                    "kind": chunk.kind,
                    "name": chunk.name,
                    "start_line": chunk.start_line,
                    "end_line": chunk.end_line,
                    "text": chunk.text,
                },
            )
            for i, (chunk, vector) in enumerate(zip(chunks, vectors))
        ]
        if points:
            self._client.upsert(collection_name=COLLECTION_NAME, points=points)

    def search(self, query_vector: list[float], limit: int) -> list[dict[str, Any]]:
        if not self._client.collection_exists(COLLECTION_NAME):
            return []
        hits = self._client.query_points(
            collection_name=COLLECTION_NAME, query=query_vector, limit=limit
        ).points
        return [hit.payload for hit in hits]

    def close(self) -> None:
        self._client.close()
