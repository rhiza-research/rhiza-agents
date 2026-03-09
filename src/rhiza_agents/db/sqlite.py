"""App database for conversation metadata.

Messages are NOT stored here -- they live in the LangGraph checkpointer.
"""

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
