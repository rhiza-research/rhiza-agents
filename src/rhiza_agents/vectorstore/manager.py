"""ChromaDB collection management for document storage and retrieval."""

import logging
import uuid

import chromadb
from chromadb.config import Settings as ChromaSettings
from langchain_text_splitters import RecursiveCharacterTextSplitter

logger = logging.getLogger(__name__)


class VectorStoreManager:
    """Manages ChromaDB collections for document storage and retrieval."""

    def __init__(self, persist_directory: str):
        self.client = chromadb.PersistentClient(
            path=persist_directory,
            settings=ChromaSettings(anonymized_telemetry=False),
        )

    def create_collection(self, name: str, metadata: dict | None = None) -> chromadb.Collection:
        """Create a new collection."""
        return self.client.create_collection(name=name, metadata=metadata or {})

    def get_collection(self, name: str) -> chromadb.Collection:
        """Get an existing collection by name."""
        return self.client.get_collection(name=name)

    def delete_collection(self, name: str):
        """Delete a collection."""
        self.client.delete_collection(name=name)

    def add_documents(
        self,
        collection_name: str,
        documents: list[str],
        metadatas: list[dict] | None = None,
        ids: list[str] | None = None,
    ):
        """Add documents (already chunked) to a collection."""
        collection = self.get_collection(collection_name)
        if ids is None:
            ids = [str(uuid.uuid4()) for _ in documents]
        collection.add(documents=documents, metadatas=metadatas, ids=ids)

    def query(
        self,
        collection_name: str,
        query_text: str,
        n_results: int = 5,
    ) -> dict:
        """Query a collection for relevant documents."""
        collection = self.get_collection(collection_name)
        return collection.query(query_texts=[query_text], n_results=n_results)


def chunk_text(text: str, chunk_size: int = 1000, chunk_overlap: int = 200) -> list[str]:
    """Split text into chunks for embedding."""
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
        length_function=len,
    )
    return splitter.split_text(text)


def extract_text_from_file(filename: str, content: bytes) -> str:
    """Extract text content from an uploaded file.

    Supports: .txt, .md, .pdf

    Raises:
        ValueError: If file type is not supported
    """
    suffix = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""

    if suffix in ("txt", "md"):
        return content.decode("utf-8")
    elif suffix == "pdf":
        import fitz

        doc = fitz.open(stream=content, filetype="pdf")
        text_parts = []
        for page in doc:
            text_parts.append(page.get_text())
        doc.close()
        return "\n\n".join(text_parts)
    else:
        raise ValueError(f"Unsupported file type: .{suffix}. Supported: .txt, .md, .pdf")
