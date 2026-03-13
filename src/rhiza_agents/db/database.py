"""Database for user agent configs and MCP server configs.

Chat messages are NOT stored here — they live in the LangGraph checkpointer.
"""

import json
from datetime import UTC, datetime

from databases import Database as DatabaseConnection


class Database:
    """Async database for agent config and MCP server management."""

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
            CREATE TABLE IF NOT EXISTS user_agent_configs (
                user_id TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                config_json TEXT NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (user_id, agent_id)
            )
        """)

        await self.database.execute("""
            CREATE TABLE IF NOT EXISTS mcp_servers (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                url TEXT NOT NULL,
                transport TEXT NOT NULL DEFAULT 'sse',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

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

    # --- MCP Servers ---

    async def list_mcp_servers(self) -> list[dict]:
        """List all configured MCP servers."""
        rows = await self.database.fetch_all(
            "SELECT id, name, url, transport, created_at FROM mcp_servers ORDER BY created_at"
        )
        return [dict(row._mapping) for row in rows]

    async def get_mcp_server(self, server_id: str) -> dict | None:
        """Get a single MCP server config."""
        row = await self.database.fetch_one(
            "SELECT id, name, url, transport, created_at FROM mcp_servers WHERE id = :id",
            {"id": server_id},
        )
        return dict(row._mapping) if row else None

    async def save_mcp_server(self, server_id: str, name: str, url: str, transport: str = "sse"):
        """Save (insert or update) an MCP server config."""
        await self.database.execute(
            """INSERT INTO mcp_servers (id, name, url, transport, created_at)
               VALUES (:id, :name, :url, :transport, :created_at)
               ON CONFLICT (id) DO UPDATE SET
                   name = :name, url = :url, transport = :transport""",
            {
                "id": server_id,
                "name": name,
                "url": url,
                "transport": transport,
                "created_at": datetime.now(UTC),
            },
        )

    async def delete_mcp_server(self, server_id: str):
        """Delete an MCP server config."""
        await self.database.execute(
            "DELETE FROM mcp_servers WHERE id = :id",
            {"id": server_id},
        )
