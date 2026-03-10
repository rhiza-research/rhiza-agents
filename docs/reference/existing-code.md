# Existing Patterns Reference

This document describes patterns from the existing sheerwater-chat codebase that are reused in rhiza-agents infrastructure (Docker Compose, Helm, CI/CD). The application code patterns (auth, database, routing) are NOT reused — rhiza-agents uses Deep Agents + LangGraph Server + deep-agents-ui instead of custom FastAPI.

---

## Docker Compose Pattern

**Source**: `/Users/tristan/Devel/rhiza/sheerwater-chat/docker-compose.yml`

### Keycloak Internal vs Public URL

```yaml
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

## Keycloak Realm Configuration

**Source**: `/Users/tristan/Devel/rhiza/sheerwater-chat/keycloak/realm.json`

### Realm JSON Structure

The realm JSON is auto-imported by Keycloak on startup via `--import-realm`. It defines:
- Realm name and settings
- OIDC clients (clientId, secret, redirect URIs)
- Dev users for local development

The `rhiza-agents` client is configured in `keycloak/realm.json` with redirect URIs pointing to deep-agents-ui on port 3000.

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
- **Deploy branch**: `git push -f origin HEAD:deploy` — ArgoCD watches this branch
- **Tag update**: `sed -i 's/tag: .*/tag: <sha>/' chart/values.yaml`

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
    deployment.yaml    # Deployment spec
    service.yaml       # ClusterIP service
    ingress.yaml       # Conditional ingress (nginx, cert-manager)
    pvc.yaml           # PersistentVolumeClaim
```

### Key Points

- **Secrets** injected via `envFrom` referencing a K8s Secret (created by Terraform, not the chart)
- **Non-secret env vars** set from `values.yaml`
- **Image tag** updated by CI/CD pipeline via `sed`
