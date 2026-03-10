# Database Schema Reference

This document describes the database schema for rhiza-agents and the storage strategy for all persistent data.

---

## Storage Architecture

rhiza-agents uses three separate storage locations, all on the same PVC:

| Path | Purpose | Managed By |
|------|---------|------------|
| `/data/app.db` | App database (conversations, configs, settings) | Application code via `databases` library |
| `/data/checkpoints.db` | LangGraph graph state and message history | `langgraph-checkpoint-sqlite` |
| `/data/chroma/` | ChromaDB vector store for document embeddings | ChromaDB library |

This separation ensures:
- Each component manages its own schema independently
- No schema conflicts between the app, LangGraph, and ChromaDB
- Each can be migrated to a different backend independently (e.g., app DB to Postgres, checkpointer to Postgres, ChromaDB to a managed service)

---

## App Database

**Library**: `databases[aiosqlite]` (migrate to `databases[asyncpg]` for Postgres later)

**URL**: Set via `DATABASE_URL` env var. SQLite: `sqlite:///./path.db`, Postgres: `postgresql://user:pass@host/db`

### Schema

```sql
CREATE TABLE conversations (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    title TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_conversations_user_id ON conversations(user_id);

CREATE TABLE user_agent_configs (
    user_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    config_json TEXT NOT NULL,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (user_id, agent_id)
);

CREATE TABLE user_vectorstores (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    name TEXT NOT NULL,
    collection_name TEXT NOT NULL,
    description TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_user_vectorstores_user_id ON user_vectorstores(user_id);

CREATE TABLE settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
```

### Table: conversations

Stores conversation metadata only. **Messages do not live here** -- they live in the LangGraph checkpointer (see below).

| Column | Type | Description |
|--------|------|-------------|
| `id` | `TEXT PRIMARY KEY` | Conversation UUID. Also used as `thread_id` for the LangGraph checkpointer. |
| `user_id` | `TEXT NOT NULL` | Keycloak user ID (`sub` claim from OIDC token). |
| `title` | `TEXT` | Conversation title. Auto-generated from first message, can be updated by user. |
| `created_at` | `TIMESTAMP` | When the conversation was created. |
| `updated_at` | `TIMESTAMP` | Bumped on each new message. Used for ordering conversations by recency. |

**Indexed on**: `user_id` (for listing a user's conversations).

**Relationship to checkpointer**: The conversation `id` is passed as `thread_id` to the LangGraph graph invocation. The checkpointer stores the full message history and graph state for that thread_id. This table only tracks metadata (title, ownership, timestamps).

### Table: user_agent_configs

Stores per-user, per-agent configuration as a JSON blob. This allows users to customize agent behavior (enable/disable agents, change prompts, adjust parameters).

| Column | Type | Description |
|--------|------|-------------|
| `user_id` | `TEXT NOT NULL` | Keycloak user ID. |
| `agent_id` | `TEXT NOT NULL` | Agent identifier (matches keys in `agents/registry.py`). |
| `config_json` | `TEXT NOT NULL` | Full AgentConfig serialized as JSON. |
| `updated_at` | `TIMESTAMP` | Last time this config was modified. |

**Primary key**: `(user_id, agent_id)` -- one config per user per agent.

**Default behavior**: If no row exists for a `(user_id, agent_id)` pair, the system uses the default config from `agents/registry.py`. A row is only created when the user explicitly customizes an agent.

**Disabling agents**: To disable an agent, the config JSON includes `"enabled": false`. This acts as a tombstone -- the agent exists in the registry but is excluded from the user's supervisor graph.

**Example config_json** (matches `AgentConfig` model fields):
```json
{
  "id": "data_analyst",
  "name": "Data Analyst",
  "type": "worker",
  "system_prompt": "You are a data analyst specializing in weather forecast models...",
  "model": "claude-sonnet-4-20250514",
  "tools": ["mcp:sheerwater"],
  "vectorstore_ids": [],
  "enabled": true
}
```

### Table: user_vectorstores

Tracks ChromaDB collection registrations per user. Each row represents a knowledge base that a user has created.

| Column | Type | Description |
|--------|------|-------------|
| `id` | `TEXT PRIMARY KEY` | Vectorstore UUID. |
| `user_id` | `TEXT NOT NULL` | Keycloak user ID. |
| `name` | `TEXT NOT NULL` | Display name chosen by the user (e.g., "Research Papers"). |
| `collection_name` | `TEXT NOT NULL` | Actual ChromaDB collection name. Should be unique and URL-safe. |
| `description` | `TEXT` | Optional description of the knowledge base contents. |
| `created_at` | `TIMESTAMP` | When the vectorstore was created. |

**Indexed on**: `user_id` (for listing a user's vectorstores).

**Relationship to ChromaDB**: The `collection_name` maps directly to a ChromaDB collection. The ChromaDB collection contains the actual document chunks and embeddings. This table is just a registry for the UI.

### Table: settings

Global key-value store for application-wide settings.

| Column | Type | Description |
|--------|------|-------------|
| `key` | `TEXT PRIMARY KEY` | Setting name. |
| `value` | `TEXT NOT NULL` | Setting value (always stored as text). |

**Upsert pattern**:
```sql
INSERT INTO settings (key, value) VALUES (:key, :value)
ON CONFLICT(key) DO UPDATE SET value = :value
```

---

## LangGraph Checkpointer

**Library**: `langgraph-checkpoint-sqlite` 3.0.3

**Location**: `/data/checkpoints.db` (separate from the app database)

### What It Stores

The checkpointer stores the **full graph state** for each `thread_id`:
- All messages (human, AI, tool calls, tool results)
- Intermediate graph states (useful for debugging)
- Checkpoint metadata (timestamps, step counts)

### Schema

The schema is **internal to the checkpointer library**. Do not create, modify, or query these tables directly. The checkpointer manages them automatically.

### Usage

```python
from langgraph.checkpoint.sqlite import SqliteSaver

# Create checkpointer (sync version)
checkpointer = SqliteSaver.from_conn_string("/data/checkpoints.db")

# Attach to graph at compile time
graph = supervisor_graph.compile(checkpointer=checkpointer)

# thread_id links to a conversation
result = graph.invoke(
    {"messages": [HumanMessage(content="...")]},
    config={"configurable": {"thread_id": conversation_id}}
)
```

### Key Points

- The `thread_id` passed to `graph.invoke()` must match the `conversations.id` in the app database.
- When a graph is invoked with an existing thread_id, the checkpointer loads all previous state automatically.
- Do not store messages in the app database -- they live in the checkpointer.
- The app database `conversations` table only stores metadata (title, user_id, timestamps).

### Postgres Migration

When migrating to Postgres:
1. Install `langgraph-checkpoint-postgres`
2. Replace `SqliteSaver` with `PostgresSaver` from `langgraph.checkpoint.postgres`
3. `PostgresSaver` manages its own schema in the Postgres database
4. The app database and checkpointer can share the same Postgres instance (different tables)

---

## ChromaDB

**Library**: `chromadb`

**Location**: `/data/chroma/` (persistent directory on PVC)

### What It Stores

- Document chunks (text segments from uploaded files)
- Vector embeddings for each chunk
- Chunk metadata (source file, page number, etc.)

### Schema

The schema is **internal to ChromaDB**. Do not manage ChromaDB's files or database directly. Use the ChromaDB API.

### Usage

```python
import chromadb

# Persistent client pointing at PVC directory
client = chromadb.PersistentClient(path="/data/chroma")

# Create or get a collection
collection = client.get_or_create_collection(name="user_research_papers")

# Add documents
collection.add(
    documents=["chunk text here"],
    metadatas=[{"source": "paper.pdf", "page": 1}],
    ids=["chunk-uuid"],
)

# Query
results = collection.query(
    query_texts=["search query"],
    n_results=5,
)
```

### Relationship to user_vectorstores Table

The `user_vectorstores` table in the app database is a registry. Each row's `collection_name` corresponds to a ChromaDB collection. The app database tracks which user owns which collection; ChromaDB stores the actual data.

---

## Schema Initialization Pattern

Following the existing sheerwater-chat pattern, use `CREATE TABLE IF NOT EXISTS` in an async `_init_db()` method:

```python
class Database:
    def __init__(self, database_url: str):
        self.database = DatabaseConnection(database_url)

    async def connect(self):
        await self.database.connect()
        await self._init_db()

    async def disconnect(self):
        await self.database.disconnect()

    async def _init_db(self):
        await self.database.execute("""
            CREATE TABLE IF NOT EXISTS conversations (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                title TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await self.database.execute(
            "CREATE INDEX IF NOT EXISTS idx_conversations_user_id ON conversations(user_id)"
        )

        await self.database.execute("""
            CREATE TABLE IF NOT EXISTS user_agent_configs (
                user_id TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                config_json TEXT NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (user_id, agent_id)
            )
        """)

        await self.database.execute("""
            CREATE TABLE IF NOT EXISTS user_vectorstores (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                name TEXT NOT NULL,
                collection_name TEXT NOT NULL,
                description TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await self.database.execute(
            "CREATE INDEX IF NOT EXISTS idx_user_vectorstores_user_id ON user_vectorstores(user_id)"
        )

        await self.database.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
```

No ORM. No migration framework. Raw SQL with `CREATE TABLE IF NOT EXISTS` for idempotent initialization.
