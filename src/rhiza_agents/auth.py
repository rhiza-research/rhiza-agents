"""Keycloak OIDC authentication."""

from authlib.integrations.starlette_client import OAuth
from starlette.requests import Request

from .config import Config


def create_oauth(config: Config) -> OAuth:
    """Create OAuth client configured for Keycloak.

    Uses separate URLs for browser redirects (public) vs backend calls (internal).
    This allows running in Docker where internal and external URLs differ.
    """
    oauth = OAuth()

    internal_base = f"{config.keycloak_url}/realms/{config.keycloak_realm}/protocol/openid-connect"
    public_base = f"{config.keycloak_public_url}/realms/{config.keycloak_realm}/protocol/openid-connect"

    oauth.register(
        name="keycloak",
        client_id=config.keycloak_client_id,
        client_secret=config.keycloak_client_secret,
        authorize_url=f"{public_base}/auth",
        access_token_url=f"{internal_base}/token",
        userinfo_endpoint=f"{internal_base}/userinfo",
        jwks_uri=f"{internal_base}/certs",
        client_kwargs={"scope": "openid email profile", "code_challenge_method": "S256"},
    )

    return oauth


def get_user_from_session(request: Request) -> dict | None:
    """Get user info from session."""
    return request.session.get("user")


def get_user_id(request: Request) -> str | None:
    """Get Keycloak sub claim from session."""
    user = get_user_from_session(request)
    if user:
        return user.get("sub")
    return None


def get_user_name(request: Request) -> str | None:
    """Get user display name from session."""
    user = get_user_from_session(request)
    if user:
        return user.get("name") or user.get("preferred_username") or user.get("email")
    return None
