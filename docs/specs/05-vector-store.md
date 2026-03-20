# Phase 5: Vector Store Integration

## Goal

Agents can query ChromaDB vector store collections for retrieval-augmented generation (RAG). Users can create collections, upload documents (txt, pdf, md), and attach collections to agents via the config editor. The research_assistant agent gains retrieval tools for each attached collection.

## Prerequisites

Phase 4 must be complete and working:
- Supervisor + sub-agents with config editor and sandbox execution
- `code_runner` has the Daytona sandbox tool
- Tool resolution in `graph.py` handles `mcp:sheerwater` and `sandbox:daytona`

## Files to Create

```
src/rhiza_agents/vectorstore/__init__.py
src/rhiza_agents/vectorstore/manager.py
src/rhiza_agents/agents/tools/vectordb.py
```

## Files to Modify

```
src/rhiza_agents/db/sqlite.py
src/rhiza_agents/agents/registry.py
src/rhiza_agents/agents/graph.py
src/rhiza_agents/main.py
src/rhiza_agents/templates/config_editor.html
src/rhiza_agents/static/config.js
src/rhiza_agents/static/style.css
docker-compose.yml
```

## Key APIs & Packages

```python
# ChromaDB
import chromadb
from chromadb.config import Settings as ChromaSettings

# LangChain text splitting
from langchain_text_splitters import RecursiveCharacterTextSplitter

# LangChain ChromaDB integration
from langchain_chroma import Chroma

# Embeddings -- use Chroma's default embedding function (all-MiniLM-L6-v2 via onnxruntime)
# This avoids needing an external embedding API key.
# ChromaDB's default uses `chromadb.utils.embedding_functions.DefaultEmbeddingFunction`
# which runs a small transformer model locally.

# LangChain tool
from langchain_core.tools import tool

# File upload
from fastapi import UploadFile, File

# PDF reading
import fitz  # PyMuPDF -- add to pyproject.toml dependencies
```

Add to `pyproject.toml` dependencies: `langchain-chroma`, `langchain-text-splitters`, `pymupdf`.

`chromadb` should already be pulled in by `langchain-chroma`.

## Implementation Details

### `vectorstore/manager.py` -- ChromaDB Collection Management

```python
import chromadb
from chromadb.config import Settings as ChromaSettings

class VectorStoreManager:
    """Manages ChromaDB collections for document storage and retrieval."""

    def __init__(self, persist_directory: str):
        """Initialize ChromaDB persistent client.

        Args:
            persist_directory: Path to ChromaDB storage directory (e.g., /data/chroma)
        """
        self.client = chromadb.PersistentClient(
            path=persist_directory,
            settings=ChromaSettings(anonymized_telemetry=False),
        )

    def create_collection(self, name: str, metadata: dict | None = None) -> chromadb.Collection:
        """Create a new collection.

        Args:
            name: Collection name (must be unique across all users).
                  Use format: {user_id}_{collection_name} to namespace per user.
            metadata: Optional metadata dict (description, etc.)

        Returns:
            The created collection.
        """
        return self.client.create_collection(
            name=name,
            metadata=metadata or {},
        )

    def get_collection(self, name: str) -> chromadb.Collection:
        """Get an existing collection by name."""
        return self.client.get_collection(name=name)

    def delete_collection(self, name: str):
        """Delete a collection."""
        self.client.delete_collection(name=name)

    def list_collections(self) -> list[chromadb.Collection]:
        """List all collections."""
        return self.client.list_collections()

    def add_documents(
        self,
        collection_name: str,
        documents: list[str],
        metadatas: list[dict] | None = None,
        ids: list[str] | None = None,
    ):
        """Add documents (already chunked) to a collection.

        Args:
            collection_name: Target collection name
            documents: List of text chunks
            metadatas: Optional metadata per chunk (source filename, chunk index, etc.)
            ids: Optional IDs per chunk (generated if not provided)
        """
        collection = self.get_collection(collection_name)
        if ids is None:
            import uuid
            ids = [str(uuid.uuid4()) for _ in documents]
        collection.add(documents=documents, metadatas=metadatas, ids=ids)

    def query(
        self,
        collection_name: str,
        query_text: str,
        n_results: int = 5,
    ) -> dict:
        """Query a collection for relevant documents.

        Args:
            collection_name: Collection to query
            query_text: The search query
            n_results: Number of results to return

        Returns:
            ChromaDB query results dict with documents, metadatas, distances
        """
        collection = self.get_collection(collection_name)
        return collection.query(query_texts=[query_text], n_results=n_results)
```

**Document ingestion helper:**

```python
from langchain_text_splitters import RecursiveCharacterTextSplitter

def chunk_text(text: str, chunk_size: int = 1000, chunk_overlap: int = 200) -> list[str]:
    """Split text into chunks for embedding.

    Args:
        text: Full document text
        chunk_size: Target chunk size in characters
        chunk_overlap: Overlap between chunks

    Returns:
        List of text chunks
    """
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
        length_function=len,
    )
    return splitter.split_text(text)
```

**File content extraction:**

```python
def extract_text_from_file(filename: str, content: bytes) -> str:
    """Extract text content from an uploaded file.

    Supports: .txt, .md, .pdf

    Args:
        filename: Original filename (used to determine type)
        content: Raw file bytes

    Returns:
        Extracted text content

    Raises:
        ValueError: If file type is not supported
    """
    suffix = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""

    if suffix in ("txt", "md"):
        return content.decode("utf-8")
    elif suffix == "pdf":
        import fitz  # PyMuPDF
        doc = fitz.open(stream=content, filetype="pdf")
        text_parts = []
        for page in doc:
            text_parts.append(page.get_text())
        doc.close()
        return "\n\n".join(text_parts)
    else:
        raise ValueError(f"Unsupported file type: .{suffix}. Supported: .txt, .md, .pdf")
```

### `agents/tools/vectordb.py` -- Retrieval Tool Factory

Create one LangChain tool per vector store collection attached to an agent:

```python
from langchain_core.tools import tool

def create_retrieval_tool(
    manager: "VectorStoreManager",
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

    @tool(name=f"search_{display_name}", description=f"Search the '{display_name}' knowledge base. {description}")
    def search_collection(query: str) -> str:
        """Search for relevant information in the knowledge base.

        Args:
            query: The search query describing what information you need.

        Returns:
            Relevant text passages from the knowledge base.
        """
        results = manager.query(collection_name, query, n_results=5)

        if not results["documents"] or not results["documents"][0]:
            return f"No relevant documents found in '{display_name}' for query: {query}"

        passages = []
        for i, (doc, metadata) in enumerate(
            zip(results["documents"][0], results["metadatas"][0])
        ):
            source = metadata.get("source", "unknown")
            passages.append(f"[{i+1}] (source: {source})\n{doc}")

        return f"Found {len(passages)} relevant passages:\n\n" + "\n\n---\n\n".join(passages)

    return search_collection
```

### Modifications to `db/sqlite.py` -- user_vectorstores Table

Add a new table to `_init_db()`:

```sql
CREATE TABLE IF NOT EXISTS user_vectorstores (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    collection_name TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    description TEXT DEFAULT '',
    document_count INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_user_vectorstores_user_id ON user_vectorstores(user_id);
```

The `collection_name` is the internal ChromaDB collection name (namespaced as `{user_id}_{display_name_sanitized}`). The `display_name` is the user-facing name.

Add CRUD methods:

```python
async def create_vectorstore(self, id: str, user_id: str, collection_name: str,
                              display_name: str, description: str = "") -> dict:
    """Register a new vector store."""

async def list_vectorstores(self, user_id: str) -> list[dict]:
    """List all vector stores for a user."""

async def get_vectorstore(self, id: str, user_id: str) -> dict | None:
    """Get a vector store by ID, with ownership check."""

async def update_vectorstore_doc_count(self, id: str, count: int):
    """Update the document count for a vector store."""

async def delete_vectorstore(self, id: str, user_id: str):
    """Delete a vector store record."""
```

### Modifications to `agents/registry.py`

Update research_assistant to document that it uses vectordb tools:

```python
AgentConfig(
    id="research_assistant",
    name="Research Assistant",
    type="worker",
    system_prompt=(
        "You are a research assistant. You answer questions using knowledge from "
        "uploaded documents and knowledge bases. Use your search tools to find "
        "relevant information before answering. Cite your sources when possible. "
        "If you don't have relevant documents, say so clearly."
    ),
    model="claude-sonnet-4-20250514",
    tools=[],  # Populated dynamically from vectorstore_ids
    vectorstore_ids=[],  # Populated per-user from config
    enabled=True,
)
```

The research_assistant's tools are not specified statically -- they are generated dynamically from `vectorstore_ids` at graph build time.

### Modifications to `agents/graph.py`

Update tool resolution to handle `vectordb:{collection_id}` and vectorstore_ids:

```python
async def _resolve_tools(
    config: AgentConfig,
    mcp_tools: list,
    daytona_api_key: str,
    vectorstore_manager: "VectorStoreManager | None",
    db: "Database",
) -> list:
    """Resolve tool identifiers to actual LangChain tool objects."""
    tools = []
    for tool_id in config.tools:
        if tool_id == "mcp:sheerwater":
            tools.extend(mcp_tools)
        elif tool_id == "sandbox:daytona":
            if daytona_api_key:
                from .tools.sandbox import create_sandbox_tool
                tools.append(create_sandbox_tool(daytona_api_key))
        # Explicit vectordb tool IDs in the tools list are also supported
        elif tool_id.startswith("vectordb:"):
            vs_id = tool_id.split(":", 1)[1]
            if vectorstore_manager:
                vs_record = await db.get_vectorstore_by_id(vs_id)
                if vs_record:
                    from .tools.vectordb import create_retrieval_tool
                    tools.append(create_retrieval_tool(
                        vectorstore_manager,
                        vs_record["collection_name"],
                        vs_record["display_name"],
                        vs_record["description"],
                    ))

    # Also resolve vectorstore_ids from the agent config
    if config.vectorstore_ids and vectorstore_manager:
        for vs_id in config.vectorstore_ids:
            vs_record = await db.get_vectorstore_by_id(vs_id)
            if vs_record:
                from .tools.vectordb import create_retrieval_tool
                tools.append(create_retrieval_tool(
                    vectorstore_manager,
                    vs_record["collection_name"],
                    vs_record["display_name"],
                    vs_record["description"],
                ))

    return tools
```

Update `build_graph` and `get_or_build_graph` to accept `vectorstore_manager` and `db` parameters.

### Modifications to `main.py`

**Startup:**
1. Add `CHROMA_PERSIST_DIR` to config (default: `/data/chroma`)
2. Create `VectorStoreManager(config.chroma_persist_dir)` in lifespan
3. Store as global

**New API routes:**

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/vectorstores` | List user's vector stores |
| POST | `/api/vectorstores` | Create a new vector store |
| DELETE | `/api/vectorstores/{id}` | Delete a vector store |
| POST | `/api/vectorstores/{id}/upload` | Upload documents to a vector store |

**POST /api/vectorstores** request:
```json
{
    "name": "Research Papers",
    "description": "Collection of climate research papers"
}
```

Handler:
1. Generate a UUID for the vectorstore ID
2. Sanitize the name for use as collection_name: lowercase, replace spaces with underscores, prefix with user_id
3. Create ChromaDB collection via manager
4. Save record to `user_vectorstores` table
5. Return the created record

**POST /api/vectorstores/{id}/upload:**

Accepts multipart file upload. The endpoint accepts one or more files.

Handler:
1. Verify vectorstore belongs to user
2. For each uploaded file:
   a. Extract text content using `extract_text_from_file`
   b. Chunk the text using `chunk_text`
   c. Add chunks to the ChromaDB collection with metadata (source filename, chunk index)
3. Update document count in `user_vectorstores` table
4. Return success with updated document count

```python
@app.post("/api/vectorstores/{vs_id}/upload")
async def upload_documents(
    request: Request,
    vs_id: str,
    files: list[UploadFile] = File(...),
    user: dict = Depends(require_auth),
):
```

**DELETE /api/vectorstores/{id}:**
1. Verify vectorstore belongs to user
2. Delete ChromaDB collection
3. Delete record from `user_vectorstores` table
4. Remove the vectorstore_id from any agent configs that reference it (clean up user_agent_configs)
5. Invalidate graph cache

### Modifications to `templates/config_editor.html`

Add a vector store management section to the config editor. This can be a new tab or section below the agent list.

**Vector Store Section:**
```
+------------------------------------------+
| Vector Stores                             |
+------------------------------------------+
| [Research Papers] 15 docs  [x]           |
| [Meeting Notes]   8 docs   [x]           |
|                                           |
| [+ Create New Vector Store]              |
+------------------------------------------+
```

Each vector store shows:
- Display name
- Document count
- Delete button (x)
- Click to expand: upload form with file input, description field

**Agent config -- vectorstore attachment:**

In the agent detail panel, add a section below tools:

```
Knowledge Bases:
[x] Research Papers
[ ] Meeting Notes
```

When a checkbox is toggled, add/remove the vectorstore_id from the agent's `vectorstore_ids` list. Save triggers a PUT to update the agent config.

### Modifications to `static/config.js`

Add functions:
- `loadVectorStores()` -- GET /api/vectorstores
- `createVectorStore(name, description)` -- POST /api/vectorstores
- `deleteVectorStore(id)` -- DELETE /api/vectorstores/{id}
- `uploadDocuments(vsId, files)` -- POST /api/vectorstores/{id}/upload (multipart form data)
- `toggleVectorStore(agentId, vsId, attached)` -- update agent config's vectorstore_ids

### Modifications to `static/style.css`

Add styles for the vector store section:
- Vector store list items with document count badges
- File upload area (drag-and-drop style or simple file input)
- Upload progress indicator (simple "Uploading..." text)

### Modifications to `docker-compose.yml`

Ensure the data volume is mounted for ChromaDB persistence. Add env var:

```yaml
CHROMA_PERSIST_DIR: /data/chroma
```

The `/data` directory should already be a volume mount from Phase 1 (for SQLite). ChromaDB stores its data in a subdirectory.

## Reference Files

| File | What to learn |
|------|---------------|
| `docs/ARCHITECTURE.md` | Vector store integration section |
| `src/rhiza_agents/agents/graph.py` | Tool resolution to extend |
| `src/rhiza_agents/agents/registry.py` | Agent configs to update |
| `src/rhiza_agents/db/sqlite.py` | Database patterns to follow |
| `src/rhiza_agents/main.py` | Routes to add |
| `src/rhiza_agents/templates/config_editor.html` | Config editor to extend |
| `src/rhiza_agents/static/config.js` | Config JS to extend |

## Acceptance Criteria

1. Navigate to config editor, see "Vector Stores" section (empty)
2. Create a new vector store called "Test Docs" with description "Test documents"
3. Upload a text file to it (simple .txt file with some content)
4. See document count update to show chunks added
5. Attach "Test Docs" to the research_assistant agent via the config editor
6. In chat, ask a question about the content of the uploaded document
7. Supervisor routes to research_assistant, which calls `search_test_docs` tool
8. Response includes relevant passages from the document with source attribution
9. Upload a .pdf file to the vector store -- text is extracted and chunked
10. Upload a .md file -- text is extracted and chunked
11. Delete the vector store -- ChromaDB collection is removed, agent config is updated
12. After deletion, research_assistant no longer has the search tool (responds conversationally)

## What NOT to Do

- **No complex document preprocessing** -- just extract text, chunk it, embed it. No OCR, no table extraction, no HTML parsing.
- **No web scraping or URL ingestion** -- only file upload (.txt, .md, .pdf).
- **No custom embedding models** -- use ChromaDB's default embedding function (all-MiniLM-L6-v2). No API keys needed for embeddings.
- **No vector store sharing between users** -- each user's collections are namespaced and isolated.
- **No collection editing** -- once created, you can add documents but cannot remove individual documents. Delete the whole collection and recreate to start fresh.
- **No large file handling** -- no progress bars, no background processing. Upload is synchronous. Keep file sizes reasonable (< 10MB).
- **No streaming** -- Phase 6.

## Implementation Notes (Post-Implementation)

### Key changes beyond spec

- **`config.py`**: Added `chroma_persist_dir` field (env: `CHROMA_PERSIST_DIR`, default `./chroma_data`).
- **`_resolve_tools` made async**: Tool resolution now `await`s DB lookups for vectorstore metadata.
- **`get_or_build_graph` and `build_graph`**: Accept `vectorstore_manager` and `db` params, threaded through from `get_agent_graph`.
- **`get_agent_graph`**: Accepts `vectorstore_manager` param, passes to graph builder.
- **All `get_agent_graph` calls in `main.py`** updated to pass `vectorstore_manager=vectorstore_manager`.
- **Line length fixes**: `get_agent_graph(...)` calls broken across multiple lines for ruff compliance.

### Config editor UI additions

- **Knowledge Bases sidebar section**: Lists vector stores with document counts, upload buttons (triggers native file picker), and delete (x) buttons.
- **Agent detail panel**: "Knowledge Bases" checkbox section appears when vector stores exist, allowing users to attach/detach collections from agents. Checkboxes map to `vectorstore_ids` in the agent config.
- **New Vector Store modal**: Name + description fields, creates via `POST /api/vectorstores`.
- **Upload flow**: File picker accepts `.txt,.md,.pdf`, uploads via multipart `POST /api/vectorstores/{id}/upload`, shows "Uploading..." state on button.

### `db/sqlite.py` additions

- `get_vectorstore_by_id(id)`: No ownership check, used internally by graph builder to resolve vectorstore_ids to collection metadata.

### Delete cleanup

- Deleting a vector store iterates `user_agent_configs` rows, removes the vectorstore ID from any `vectorstore_ids` lists, saves updated configs, and invalidates the graph cache.
