# Phase 4: Production Deployment

## Goal

Deploy rhiza-agents to GKE via ArgoCD with full CI/CD. Two services: the LangGraph Server (with agent code + PostgreSQL + Redis) and deep-agents-ui (Next.js). LangSmith tracing is enabled for observability.

## Prerequisites

Phase 3 must be complete:
- Deep agent with MCP tools + Daytona sandbox + middleware
- deep-agents-ui fork with NextAuth.js + Keycloak auth
- Full stack running reliably in local Docker Compose

Also required (external, not in this repo):
- GKE cluster with ArgoCD running (managed in infrastructure repo)
- Keycloak instance with OIDC client configured for rhiza-agents
- DNS records for production hostnames
- GitHub repository secrets configured

## What You're Building

1. GitHub Actions workflow to build and push Docker images (agent + UI)
2. Helm charts for both services
3. LangSmith tracing configuration

## What You're NOT Building

- No Terraform changes in this repo (documented for infra team)
- No custom monitoring/alerting (LangSmith tracing is sufficient initially)
- No auto-scaling (single replica to start)

## Implementation Details

### CI/CD — GitHub Actions

Follow the sheerwater-chat pattern:
- Trigger on push to `main`
- Build multi-arch Docker images (amd64 + arm64)
- Push to GHCR
- Update Helm chart values with the new image tag (commit SHA)
- Force-push to `deploy` branch
- ArgoCD watches `deploy` branch and syncs

Two images to build:
1. **Agent image**: Python package with agent code, served by LangGraph Server
2. **UI image**: Next.js app (deep-agents-ui fork)

### Helm Charts

Two charts (or one chart with subcharts):

**LangGraph Server deployment:**
- The agent container running LangGraph Server
- PostgreSQL (for checkpointing)
- Redis (for task queue)
- PVC for any persistent storage
- Environment variables for: ANTHROPIC_API_KEY, DAYTONA_API_KEY, MCP_SERVER_URL, LANGSMITH config

**deep-agents-ui deployment:**
- Next.js container
- Environment variables for: LangGraph Server URL, Keycloak OIDC config, NEXTAUTH_SECRET
- No PVC needed (stateless)

Both use ClusterIP services behind nginx ingress with TLS.

### LangSmith Tracing

LangGraph and LangChain automatically trace when these env vars are set:

| Env Var | Purpose |
|---------|---------|
| `LANGCHAIN_TRACING_V2` | Set to "true" to enable |
| `LANGCHAIN_API_KEY` | LangSmith API key |
| `LANGCHAIN_PROJECT` | Project name in LangSmith dashboard |

No code changes needed — just set the env vars on the LangGraph Server deployment.

### Infrastructure Repo Changes (Documentation Only)

Document what the infra team needs to create in `infrastructure/terraform/20-gke-cluster/`:

1. Kubernetes namespace: `rhiza-agents`
2. Kubernetes secret with: ANTHROPIC_API_KEY, DAYTONA_API_KEY, KEYCLOAK_CLIENT_SECRET, NEXTAUTH_SECRET, LANGCHAIN_API_KEY
3. ArgoCD Application pointing to this repo's `deploy` branch, `chart/` path
4. DNS records for production hostnames

### Keycloak Production Client

The production Keycloak OIDC client (`rhiza-agents`) needs:
- Valid redirect URIs updated for the production hostname
- Client secret stored in the K8s secret

## Acceptance Criteria

1. Push to `main` → GitHub Actions builds both images successfully
2. Images pushed to GHCR with commit SHA tags
3. `deploy` branch updated with new image tags
4. ArgoCD syncs and pods start healthy in GKE
5. App accessible at production URL with valid TLS
6. Keycloak login works in production
7. Chat works end-to-end: MCP tools, sandbox, streaming
8. LangSmith shows traces for conversations
9. `helm template` renders without errors

## What NOT to Do

- Do not modify the infrastructure repo from this repo
- Do not run Terraform commands
- Do not implement PostgreSQL migration tooling — LangGraph Server handles its own schema
- Do not set up monitoring beyond LangSmith — that comes later
- Do not configure auto-scaling — single replica to start
- Do not manually build or push Docker images — always use CI/CD
