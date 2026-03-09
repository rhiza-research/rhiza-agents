"""Environment-based configuration."""

import os
from dataclasses import dataclass


@dataclass
class Config:
    """Application configuration loaded from environment variables."""

    # Keycloak OIDC
    keycloak_url: str
    keycloak_public_url: str
    keycloak_realm: str
    keycloak_client_id: str
    keycloak_client_secret: str

    # MCP server
    mcp_server_url: str

    # Anthropic API
    anthropic_api_key: str

    # App settings
    secret_key: str
    database_url: str
    checkpoint_db_path: str
    base_url: str

    @classmethod
    def from_env(cls) -> "Config":
        """Load configuration from environment variables."""
        keycloak_url = os.environ["KEYCLOAK_URL"]
        return cls(
            keycloak_url=keycloak_url,
            keycloak_public_url=os.environ.get("KEYCLOAK_PUBLIC_URL", keycloak_url),
            keycloak_realm=os.environ["KEYCLOAK_REALM"],
            keycloak_client_id=os.environ["KEYCLOAK_CLIENT_ID"],
            keycloak_client_secret=os.environ["KEYCLOAK_CLIENT_SECRET"],
            mcp_server_url=os.environ.get("MCP_SERVER_URL", "http://localhost:8000/sse"),
            anthropic_api_key=os.environ["ANTHROPIC_API_KEY"],
            secret_key=os.environ["SECRET_KEY"],
            database_url=os.environ.get("DATABASE_URL", "sqlite:///./rhiza_agents.db"),
            checkpoint_db_path=os.environ.get("CHECKPOINT_DB_PATH", "./checkpoints.db"),
            base_url=os.environ.get("BASE_URL", "http://localhost:8080"),
        )
