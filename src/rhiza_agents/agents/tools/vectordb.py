"""Retrieval tool factory for vector store collections."""

from __future__ import annotations

from typing import TYPE_CHECKING

from langchain_core.tools import tool

if TYPE_CHECKING:
    from ...vectorstore.manager import VectorStoreManager


def create_retrieval_tool(
    manager: VectorStoreManager,
    collection_name: str,
    display_name: str,
    description: str,
):
    """Create a LangChain retrieval tool for a specific collection.

    Args:
        manager: VectorStoreManager instance
        collection_name: ChromaDB collection name (internal, namespaced)
        display_name: Human-readable collection name
        description: What the collection contains

    Returns:
        A LangChain tool function
    """
    # Sanitize display_name for use as tool name (must be valid identifier)
    tool_name = "search_" + "".join(c if c.isalnum() or c == "_" else "_" for c in display_name.lower())

    def search_fn(query: str) -> str:
        """Search for relevant information in the knowledge base."""
        results = manager.query(collection_name, query, n_results=5)

        if not results["documents"] or not results["documents"][0]:
            return f"No relevant documents found in '{display_name}' for query: {query}"

        passages = []
        for i, (doc, metadata) in enumerate(zip(results["documents"][0], results["metadatas"][0])):
            source = metadata.get("source", "unknown")
            passages.append(f"[{i + 1}] (source: {source})\n{doc}")

        return f"Found {len(passages)} relevant passages:\n\n" + "\n\n---\n\n".join(passages)

    # Set function name before decorating so @tool uses it as the tool name
    search_fn.__name__ = tool_name
    search_fn.__doc__ = f"Search the '{display_name}' knowledge base. {description}"

    return tool(search_fn)
