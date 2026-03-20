"""App database for conversation metadata and user agent configs.

Messages are NOT stored here -- they live in the LangGraph checkpointer.
"""

import json
from datetime import UTC, datetime

from databases import Database as DatabaseConnection


class Database:
    """Async database for conversation metadata."""

    def __init__(self, database_url: str):
        self.database = DatabaseConnection(database_url)

    async def connect(self):
        """Connect to the database and initialize schema."""
        await self.database.connect()
        await self._init_db()

    async def disconnect(self):
        """Disconnect from the database."""
        await self.database.disconnect()

    async def _init_db(self):
        """Initialize database schema."""
        await self.database.execute("""
            CREATE TABLE IF NOT EXISTS conversations (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                title TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await self.database.execute("CREATE INDEX IF NOT EXISTS idx_conversations_user_id ON conversations(user_id)")

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
                collection_name TEXT NOT NULL UNIQUE,
                display_name TEXT NOT NULL,
                description TEXT DEFAULT '',
                document_count INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await self.database.execute(
            "CREATE INDEX IF NOT EXISTS idx_user_vectorstores_user_id ON user_vectorstores(user_id)"
        )

    async def create_conversation(self, conversation_id: str, user_id: str, title: str | None = None) -> dict:
        """Create a new conversation."""
        await self.database.execute(
            "INSERT INTO conversations (id, user_id, title) VALUES (:id, :user_id, :title)",
            {"id": conversation_id, "user_id": user_id, "title": title},
        )
        return {"id": conversation_id, "user_id": user_id, "title": title}

    async def get_conversation(self, conversation_id: str, user_id: str) -> dict | None:
        """Get a conversation by ID, ensuring it belongs to the user."""
        row = await self.database.fetch_one(
            "SELECT * FROM conversations WHERE id = :id AND user_id = :user_id",
            {"id": conversation_id, "user_id": user_id},
        )
        return dict(row._mapping) if row else None

    async def list_conversations(self, user_id: str, limit: int = 50) -> list[dict]:
        """List conversations for a user, most recent first."""
        rows = await self.database.fetch_all(
            "SELECT * FROM conversations WHERE user_id = :user_id ORDER BY updated_at DESC LIMIT :limit",
            {"user_id": user_id, "limit": limit},
        )
        return [dict(row._mapping) for row in rows]

    async def update_conversation_title(self, conversation_id: str, user_id: str, title: str):
        """Update conversation title."""
        await self.database.execute(
            "UPDATE conversations SET title = :title, updated_at = :updated_at WHERE id = :id AND user_id = :user_id",
            {"title": title, "updated_at": datetime.now(UTC), "id": conversation_id, "user_id": user_id},
        )

    async def touch_conversation(self, conversation_id: str):
        """Update the updated_at timestamp."""
        await self.database.execute(
            "UPDATE conversations SET updated_at = :updated_at WHERE id = :id",
            {"updated_at": datetime.now(UTC), "id": conversation_id},
        )

    async def delete_conversation(self, conversation_id: str, user_id: str):
        """Delete a conversation (checkpointer data is left orphaned)."""
        await self.database.execute(
            "DELETE FROM conversations WHERE id = :id AND user_id = :user_id",
            {"id": conversation_id, "user_id": user_id},
        )

    # --- User Agent Configs ---

    async def get_user_agent_configs(self, user_id: str) -> list[dict]:
        """Get all agent config overrides for a user."""
        rows = await self.database.fetch_all(
            "SELECT agent_id, config_json FROM user_agent_configs WHERE user_id = :user_id",
            {"user_id": user_id},
        )
        return [{"agent_id": row._mapping["agent_id"], "config_json": row._mapping["config_json"]} for row in rows]

    async def get_user_agent_config(self, user_id: str, agent_id: str) -> dict | None:
        """Get a single agent config override for a user."""
        row = await self.database.fetch_one(
            "SELECT agent_id, config_json FROM user_agent_configs WHERE user_id = :user_id AND agent_id = :agent_id",
            {"user_id": user_id, "agent_id": agent_id},
        )
        if not row:
            return None
        return {"agent_id": row._mapping["agent_id"], "config_json": row._mapping["config_json"]}

    async def save_user_agent_config(self, user_id: str, agent_id: str, config: dict):
        """Save (insert or update) an agent config override."""
        config_json = json.dumps(config)
        await self.database.execute(
            """INSERT INTO user_agent_configs (user_id, agent_id, config_json, updated_at)
               VALUES (:user_id, :agent_id, :config_json, :updated_at)
               ON CONFLICT (user_id, agent_id) DO UPDATE SET
                   config_json = :config_json, updated_at = :updated_at""",
            {
                "user_id": user_id,
                "agent_id": agent_id,
                "config_json": config_json,
                "updated_at": datetime.now(UTC),
            },
        )

    async def delete_user_agent_config(self, user_id: str, agent_id: str):
        """Delete a single agent config override."""
        await self.database.execute(
            "DELETE FROM user_agent_configs WHERE user_id = :user_id AND agent_id = :agent_id",
            {"user_id": user_id, "agent_id": agent_id},
        )

    async def delete_all_user_agent_configs(self, user_id: str):
        """Delete all agent config overrides for a user (reset to defaults)."""
        await self.database.execute(
            "DELETE FROM user_agent_configs WHERE user_id = :user_id",
            {"user_id": user_id},
        )

    # --- Vector Stores ---

    async def create_vectorstore(
        self, id: str, user_id: str, collection_name: str, display_name: str, description: str = ""
    ) -> dict:
        """Register a new vector store."""
        await self.database.execute(
            """INSERT INTO user_vectorstores (id, user_id, collection_name, display_name, description)
               VALUES (:id, :user_id, :collection_name, :display_name, :description)""",
            {
                "id": id,
                "user_id": user_id,
                "collection_name": collection_name,
                "display_name": display_name,
                "description": description,
            },
        )
        return {
            "id": id,
            "user_id": user_id,
            "collection_name": collection_name,
            "display_name": display_name,
            "description": description,
            "document_count": 0,
        }

    async def list_vectorstores(self, user_id: str) -> list[dict]:
        """List all vector stores for a user."""
        rows = await self.database.fetch_all(
            "SELECT * FROM user_vectorstores WHERE user_id = :user_id ORDER BY created_at DESC",
            {"user_id": user_id},
        )
        return [dict(row._mapping) for row in rows]

    async def get_vectorstore(self, id: str, user_id: str) -> dict | None:
        """Get a vector store by ID, with ownership check."""
        row = await self.database.fetch_one(
            "SELECT * FROM user_vectorstores WHERE id = :id AND user_id = :user_id",
            {"id": id, "user_id": user_id},
        )
        return dict(row._mapping) if row else None

    async def get_vectorstore_by_id(self, id: str) -> dict | None:
        """Get a vector store by ID (no ownership check, for tool resolution)."""
        row = await self.database.fetch_one(
            "SELECT * FROM user_vectorstores WHERE id = :id",
            {"id": id},
        )
        return dict(row._mapping) if row else None

    async def update_vectorstore_doc_count(self, id: str, count: int):
        """Update the document count for a vector store."""
        await self.database.execute(
            "UPDATE user_vectorstores SET document_count = :count WHERE id = :id",
            {"count": count, "id": id},
        )

    async def delete_vectorstore(self, id: str, user_id: str):
        """Delete a vector store record."""
        await self.database.execute(
            "DELETE FROM user_vectorstores WHERE id = :id AND user_id = :user_id",
            {"id": id, "user_id": user_id},
        )
