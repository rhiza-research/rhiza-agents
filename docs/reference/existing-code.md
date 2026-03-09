# Existing Code Patterns Reference

This document describes patterns from the existing sheerwater-chat codebase that should be reused in rhiza-agents. Each section references the source file and provides the exact pattern to follow.

---

## Auth Pattern

**Source**: `/Users/tristan/Devel/rhiza/sheerwater-chat/src/sheerwater_chat/auth.py`

Uses authlib OAuth with Starlette integration for Keycloak OIDC authentication.

### Dual URL Strategy

Keycloak requires two separate URLs because in Docker/K8s the backend reaches Keycloak via an internal URL, while the browser reaches it via a public URL:

- **`KEYCLOAK_URL`** (internal): Used by the backend for token exchange, userinfo, JWKS. Example: `http://keycloak:8080`
- **`KEYCLOAK_PUBLIC_URL`** (browser-facing): Used for the OAuth authorize redirect. Example: `http://localhost:8180`

### OAuth Client Registration

```python
from authlib.integrations.starlette_client import OAuth

def create_oauth(config: Config) -> OAuth:
    oauth = OAuth()

    internal_base = f"{config.keycloak_url}/realms/{config.keycloak_realm}/protocol/openid-connect"
    public_base = f"{config.keycloak_public_url}/realms/{config.keycloak_realm}/protocol/openid-connect"

    oauth.register(
        name="keycloak",
        client_id=config.keycloak_client_id,
        client_secret=config.keycloak_client_secret,
        # Browser-accessed endpoints use public URL
        authorize_url=f"{public_base}/auth",
        # Backend-accessed endpoints use internal URL
        access_token_url=f"{internal_base}/token",
        userinfo_endpoint=f"{internal_base}/userinfo",
        jwks_uri=f"{internal_base}/certs",
        client_kwargs={"scope": "openid email profile", "code_challenge_method": "S256"},
    )
    return oauth
```

Key details:
- PKCE is enabled with `code_challenge_method: "S256"`
- No `server_metadata_url` -- endpoints are configured explicitly to support the dual-URL pattern
- Scopes: `openid email profile`

### Helper Functions

```python
def get_user_from_session(request: Request) -> dict | None:
    """Get full user info dict from session."""
    return request.session.get("user")

def get_user_id(request: Request) -> str | None:
    """Get user ID (Keycloak 'sub' claim) from session."""
    user = get_user_from_session(request)
    return user.get("sub") if user else None

def get_user_email(request: Request) -> str | None:
    """Get user email from session."""
    user = get_user_from_session(request)
    return user.get("email") if user else None

def get_user_name(request: Request) -> str | None:
    """Get display name, falling back through name -> preferred_username -> email."""
    user = get_user_from_session(request)
    if user:
        return user.get("name") or user.get("preferred_username") or user.get("email")
    return None
```

### Session Middleware

```python
from starlette.middleware.sessions import SessionMiddleware

app.add_middleware(SessionMiddleware, secret_key=config.secret_key)
```

The `SECRET_KEY` env var provides the session signing key.

### Auth Routes

Three routes handle the OAuth flow:

```python
@app.get("/login")
async def login(request: Request):
    """Redirect to Keycloak login page."""
    redirect_uri = f"{config.base_url}/callback"
    return await oauth.keycloak.authorize_redirect(request, redirect_uri)

@app.get("/callback")
async def callback(request: Request):
    """Handle Keycloak callback -- exchange code for token, store user in session."""
    token = await oauth.keycloak.authorize_access_token(request)
    user_info = token.get("userinfo")
    if user_info:
        request.session["user"] = dict(user_info)
    return RedirectResponse(url="/")

@app.get("/logout")
async def logout(request: Request):
    """Clear session."""
    request.session.clear()
    return RedirectResponse(url="/")
```

### Auth Dependency

```python
def require_auth(request: Request):
    """FastAPI dependency that requires authentication."""
    user = get_user_from_session(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user

# Usage:
@app.get("/api/data")
async def get_data(user: dict = Depends(require_auth)):
    pass
```

---

## FastAPI App Pattern

**Source**: `/Users/tristan/Devel/rhiza/sheerwater-chat/src/sheerwater_chat/main.py`

### Lifespan Handler

Uses `@asynccontextmanager` lifespan for initialization and cleanup:

```python
from contextlib import asynccontextmanager

# Global state
config: Config = None
db: Database = None
mcp_client: McpClient = None
oauth = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    global config, db, mcp_client, oauth

    config = Config.from_env()
    db = Database(config.database_url)
    mcp_client = McpClient(config.mcp_server_url)
    oauth = create_oauth(config)

    await db.connect()

    async with mcp_client.connection():
        yield

    await db.disconnect()

app = FastAPI(title="App Name", lifespan=lifespan)
```

Key points:
- Global state is initialized in the lifespan, not at module level
- Database connection is opened at startup, closed on shutdown
- MCP client uses an async context manager that stays open for the app's lifetime

### Templates and Static Files

```python
from pathlib import Path
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

BASE_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=BASE_DIR / "templates")
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
```

### Route Organization

Routes are split between:
- **Page routes** (return HTML via `Jinja2Templates`): `GET /`, `GET /c/{id}`
- **API routes** (return JSON): `POST /api/chat`, `GET /api/conversations`, etc.
- **Auth routes**: `GET /login`, `GET /callback`, `GET /logout`

### Running the App

```python
def run():
    import uvicorn
    config = Config.from_env()
    app.add_middleware(SessionMiddleware, secret_key=config.secret_key)
    uvicorn.run(app, host="0.0.0.0", port=8080)
```

Note: `SessionMiddleware` is added in `run()`, not at module level, because it needs the config.

---

## Database Pattern

**Source**: `/Users/tristan/Devel/rhiza/sheerwater-chat/src/sheerwater_chat/database.py`

### Library

Uses the `databases` library (async, supports SQLite and Postgres):

```python
from databases import Database as DatabaseConnection
```

Install as `databases[aiosqlite]` for SQLite support, or `databases[asyncpg]` for Postgres.

### Database URL Format

| Backend | URL Format |
|---------|------------|
| SQLite | `sqlite:///./path/to/db.db` |
| PostgreSQL | `postgresql://user:pass@host/dbname` |

Set via `DATABASE_URL` environment variable.

### Schema Initialization

Schema is created with `CREATE TABLE IF NOT EXISTS` in an `_init_db()` method -- no ORM, no migration framework:

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
```

### Query Patterns

All queries use raw SQL with named parameters:

```python
# Insert
await self.database.execute(
    "INSERT INTO conversations (id, user_id, title) VALUES (:id, :user_id, :title)",
    {"id": conversation_id, "user_id": user_id, "title": title},
)

# Fetch one row
row = await self.database.fetch_one(
    "SELECT * FROM conversations WHERE id = :id AND user_id = :user_id",
    {"id": conversation_id, "user_id": user_id},
)
result = dict(row._mapping) if row else None

# Fetch all rows
rows = await self.database.fetch_all(
    "SELECT * FROM conversations WHERE user_id = :user_id ORDER BY updated_at DESC LIMIT :limit",
    {"user_id": user_id, "limit": limit},
)
results = [dict(row._mapping) for row in rows]

# Upsert
await self.database.execute("""
    INSERT INTO settings (key, value) VALUES (:key, :value)
    ON CONFLICT(key) DO UPDATE SET value = :value
""", {"key": key, "value": value})
```

### Row to Dict Conversion

Rows are converted to dicts via `dict(row._mapping)`. This works with both SQLite (aiosqlite) and Postgres (asyncpg) backends.

---

## Docker Compose Pattern

**Source**: `/Users/tristan/Devel/rhiza/sheerwater-chat/docker-compose.yml`

### Service Layout

Three services:

1. **keycloak** (port 8180): Identity provider
   - Image: `quay.io/keycloak/keycloak:25.0`
   - Command: `start-dev --import-realm`
   - Mounts `realm.json` for auto-import
   - Internal port 8080 mapped to host 8180

2. **sheerwater-mcp** (port 8000): MCP server
   - Image: `ghcr.io/rhiza-research/sheerwater/mcp:latest`
   - Builds from `../sheerwater` with `mcp/Dockerfile`
   - Mounts GCP credentials for data access

3. **sheerwater-chat** (port 8080): The web application
   - Builds from current directory
   - Source mounted read-only for hot reload: `./src:/app/src:ro`
   - Depends on keycloak and sheerwater-mcp

### Keycloak Internal vs Public URL

```yaml
sheerwater-chat:
  environment:
    KEYCLOAK_URL: http://keycloak:8080           # Internal (container-to-container)
    KEYCLOAK_PUBLIC_URL: http://localhost:8180     # Public (browser access)
```

### Environment Variables from Host

Secrets are passed through from the host shell using `${VAR}` syntax:

```yaml
environment:
  ANTHROPIC_API_KEY: ${ANTHROPIC_API_KEY}
```

---

## Helm Chart Pattern

**Source**: `/Users/tristan/Devel/rhiza/sheerwater-chat/chart/`

### Chart Structure

```
chart/
  Chart.yaml           # Chart metadata (apiVersion: v2, type: application)
  values.yaml          # Default values
  templates/
    _helpers.tpl       # Template helpers (fullname, labels, selectorLabels)
    deployment.yaml    # Deployment with Recreate strategy
    service.yaml       # ClusterIP service
    ingress.yaml       # Conditional ingress (nginx, cert-manager)
    pvc.yaml           # PersistentVolumeClaim for /data
```

### Deployment Key Points

- **Strategy: Recreate** -- required for SQLite (no concurrent writers)
- **Single replica** (`replicas: 1`)
- **PVC mounted at `/data`** for persistent storage
- **DATABASE_URL** hardcoded to PVC path: `sqlite:////data/app.db`
- **Secrets** injected via `envFrom` referencing a K8s Secret (created by Terraform, not the chart)
- **Non-secret env vars** set from `values.yaml`

### values.yaml Structure

```yaml
image:
  repository: ghcr.io/rhiza-research/sheerwater-chat
  tag: latest
  pullPolicy: Always

service:
  type: ClusterIP
  port: 8080

resources:
  limits:
    cpu: 500m
    memory: 512Mi
  requests:
    cpu: 100m
    memory: 256Mi

ingress:
  enabled: false
  className: ""
  annotations: {}
  hosts: []
  tls: []

persistence:
  size: 1Gi
  storageClassName: standard-rwo

# K8s secret name (created by Terraform)
secretName: sheerwater-chat-secrets

# Non-secret env vars
env:
  KEYCLOAK_URL: ""
  KEYCLOAK_PUBLIC_URL: ""
  KEYCLOAK_REALM: ""
  KEYCLOAK_CLIENT_ID: ""
  MCP_SERVER_URL: ""
  BASE_URL: ""
```

### PVC

```yaml
apiVersion: v1
kind: PersistentVolumeClaim
spec:
  accessModes: [ReadWriteOnce]
  resources:
    requests:
      storage: 1Gi           # From values.persistence.size
  storageClassName: standard-rwo  # From values.persistence.storageClassName
```

### Ingress

Conditional on `ingress.enabled`. Supports:
- `ingressClassName` (nginx)
- TLS with cert-manager
- Multiple hosts/paths

---

## CI/CD Pattern

**Source**: `/Users/tristan/Devel/rhiza/sheerwater/.github/workflows/mcp-build.yml`

### Workflow Structure

1. **Trigger**: Push to `main` branch
2. **Build job** (matrix: amd64 + arm64):
   - Checkout code
   - Login to GHCR
   - Build Docker image for target platform
   - Push by digest (not by tag)
   - Upload digest as artifact
3. **Merge job** (depends on build):
   - Download all digests
   - Create multi-arch manifest with tags: `<sha>` and `latest`
   - Update `chart/values.yaml` with new SHA tag
   - Force-push to `deploy` branch

### Key Details

- **Registry**: GHCR (`ghcr.io`)
- **Auth**: `GITHUB_TOKEN` (automatic)
- **Multi-arch**: Separate runners for amd64 and arm64, digests merged into manifest
- **Deploy branch**: `git push -f origin HEAD:deploy` -- ArgoCD watches this branch
- **Tag update**: `sed -i 's/tag: .*/tag: <sha>/' chart/values.yaml`

### For rhiza-agents

The same pattern applies:
- Build on push to main
- Push to `ghcr.io/rhiza-research/rhiza-agents:<sha>` and `:latest`
- Update chart values with new tag
- Force-push to deploy branch
- ArgoCD picks up the change

---

## Config Pattern

**Source**: `/Users/tristan/Devel/rhiza/sheerwater-chat/src/sheerwater_chat/config.py`

### Simple Dataclass with from_env()

```python
import os
from dataclasses import dataclass

@dataclass
class Config:
    """Application configuration loaded from environment variables."""

    # Required fields (no default -- os.environ[] raises KeyError if missing)
    keycloak_url: str
    keycloak_realm: str
    keycloak_client_id: str
    keycloak_client_secret: str
    anthropic_api_key: str
    secret_key: str

    # Optional fields with defaults (os.environ.get() with fallback)
    keycloak_public_url: str
    mcp_server_url: str
    database_url: str
    base_url: str

    @classmethod
    def from_env(cls) -> "Config":
        keycloak_url = os.environ["KEYCLOAK_URL"]
        return cls(
            keycloak_url=keycloak_url,
            keycloak_public_url=os.environ.get("KEYCLOAK_PUBLIC_URL", keycloak_url),
            keycloak_realm=os.environ["KEYCLOAK_REALM"],
            keycloak_client_id=os.environ["KEYCLOAK_CLIENT_ID"],
            keycloak_client_secret=os.environ["KEYCLOAK_CLIENT_SECRET"],
            mcp_server_url=os.environ.get("MCP_SERVER_URL", "http://localhost:8000"),
            anthropic_api_key=os.environ["ANTHROPIC_API_KEY"],
            secret_key=os.environ["SECRET_KEY"],
            database_url=os.environ.get("DATABASE_URL", "sqlite:///./sheerwater_chat.db"),
            base_url=os.environ.get("BASE_URL", "http://localhost:8080"),
        )
```

Key points:
- Required vars use `os.environ["KEY"]` (raises `KeyError` if missing)
- Optional vars use `os.environ.get("KEY", default)`
- `KEYCLOAK_PUBLIC_URL` defaults to `KEYCLOAK_URL` if not set (same URL for both)
- No validation beyond presence -- keep it simple

---

## Keycloak Realm Configuration

**Source**: `/Users/tristan/Devel/rhiza/sheerwater-chat/keycloak/realm.json`

### Realm JSON Structure

```json
{
  "realm": "sheerwater",
  "enabled": true,
  "sslRequired": "none",
  "registrationAllowed": true,
  "clients": [
    {
      "clientId": "sheerwater-chat",
      "enabled": true,
      "publicClient": false,
      "secret": "dev-secret",
      "redirectUris": ["http://localhost:8080/*"],
      "webOrigins": ["http://localhost:8080"],
      "standardFlowEnabled": true,
      "directAccessGrantsEnabled": true
    }
  ],
  "users": [
    {
      "username": "dev",
      "enabled": true,
      "email": "dev@localhost",
      "firstName": "Dev",
      "lastName": "User",
      "credentials": [
        {
          "type": "password",
          "value": "dev",
          "temporary": false
        }
      ]
    }
  ]
}
```

### Adapting for rhiza-agents

To create a realm for the new app:
1. Copy the realm JSON
2. Change `realm` name (e.g., `"rhiza-agents"`)
3. Update `clientId` and `secret`
4. Update `redirectUris` and `webOrigins` to match the new app's port
5. Keep the dev user for local development
6. Set `sslRequired: "none"` for local dev, Keycloak enforces SSL in production by default
