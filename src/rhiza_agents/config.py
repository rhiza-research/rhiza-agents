"""Environment-based configuration."""

import json
import logging
import os
from dataclasses import dataclass

logger = logging.getLogger(__name__)


def _parse_channel_user_map(raw: str) -> dict[str, str]:
    """Parse SLACK_CHANNEL_USER_MAP (a JSON object of Slack channel id -> user id).

    Empty or invalid input yields an empty map, which makes the Slack
    connector match no channels (and logs a warning so the operator gets a
    signal rather than a silently dead bot). Only scalar (str/int) values are
    accepted; non-scalar values are skipped to avoid stringified garbage ids.
    """
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("SLACK_CHANNEL_USER_MAP is not valid JSON; ignoring (Slack will match no channels)")
        return {}
    if not isinstance(data, dict):
        logger.warning("SLACK_CHANNEL_USER_MAP must be a JSON object; ignoring")
        return {}
    result: dict[str, str] = {}
    for k, v in data.items():
        if isinstance(v, (str, int)) and not isinstance(v, bool):
            result[str(k)] = str(v)
        else:
            logger.warning("SLACK_CHANNEL_USER_MAP entry %r has a non-scalar value; skipping", k)
    return result


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

    # Daytona sandbox
    daytona_api_key: str
    daytona_api_url: str
    daytona_proxy_url: str

    # Vector store
    chroma_persist_dir: str

    # App settings
    secret_key: str
    database_url: str
    checkpoint_db_path: str
    base_url: str

    # Logging
    log_level: str
    chat_event_logging: str  # "false", "true", or "opt-in"

    # Langfuse observability (optional — disabled if keys missing)
    langfuse_public_key: str
    langfuse_secret_key: str
    langfuse_base_url: str

    # Credential encryption (feature is disabled if unset)
    credential_encryption_key: str

    # Slack connector (disabled if bot/app tokens unset)
    slack_bot_token: str
    slack_app_token: str
    slack_channel_user_map: dict[str, str]

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
            daytona_api_key=os.environ.get("DAYTONA_API_KEY", ""),
            daytona_api_url=os.environ.get("DAYTONA_API_URL", ""),
            daytona_proxy_url=os.environ.get("DAYTONA_PROXY_URL", ""),
            chroma_persist_dir=os.environ.get("CHROMA_PERSIST_DIR", "./chroma_data"),
            secret_key=os.environ["SECRET_KEY"],
            database_url=os.environ.get("DATABASE_URL", "sqlite:///./rhiza_agents.db"),
            checkpoint_db_path=os.environ.get("CHECKPOINT_DB_PATH", "./checkpoints.db"),
            base_url=os.environ.get("BASE_URL", "http://localhost:8080"),
            log_level=os.environ.get("LOG_LEVEL", "INFO"),
            chat_event_logging=os.environ.get("CHAT_EVENT_LOGGING", "false"),
            langfuse_public_key=os.environ.get("LANGFUSE_PUBLIC_KEY", ""),
            langfuse_secret_key=os.environ.get("LANGFUSE_SECRET_KEY", ""),
            langfuse_base_url=os.environ.get("LANGFUSE_BASE_URL", ""),
            credential_encryption_key=os.environ.get("CREDENTIAL_ENCRYPTION_KEY", ""),
            slack_bot_token=os.environ.get("SLACK_BOT_TOKEN", ""),
            slack_app_token=os.environ.get("SLACK_APP_TOKEN", ""),
            slack_channel_user_map=_parse_channel_user_map(os.environ.get("SLACK_CHANNEL_USER_MAP", "")),
        )
