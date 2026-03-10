# Database & Storage Reference

## Storage Architecture

rhiza-agents does **not** manage its own database. LangGraph Server handles all persistent storage:

| Component | Storage | Managed By |
|-----------|---------|------------|
| Graph state & message history | PostgreSQL | LangGraph Server (checkpointing) |
| Task queue | Redis | LangGraph Server |
| Thread metadata | PostgreSQL | LangGraph Server |

### Key Points

- No custom database tables, no ORM, no migration framework
- LangGraph Server manages its own PostgreSQL schema automatically
- Thread management (create, list, delete) is done through the LangGraph Server REST API
- The `thread_id` passed in `config["configurable"]["thread_id"]` links conversations to their persisted state

### Docker Compose

PostgreSQL and Redis are separate services in Docker Compose:

```yaml
postgres:
  image: postgres:16
  environment:
    POSTGRES_USER: langgraph
    POSTGRES_PASSWORD: langgraph
    POSTGRES_DB: langgraph

redis:
  image: redis:7
```

The LangGraph Server connects via `LANGGRAPH_POSTGRES_URI` and `REDIS_URL` environment variables.

### Production (GKE)

In production, PostgreSQL and Redis run as separate services in the Helm chart. The connection strings are provided via environment variables or Kubernetes secrets.
