# Phase 7: Production Deployment

## Goal

Deploy rhiza-agents to GKE via ArgoCD with full CI/CD. GitHub Actions builds and pushes the container image, updates the Helm chart on a deploy branch, and ArgoCD syncs it to the cluster. LangSmith tracing is enabled for observability. The app is accessible at a production URL with TLS.

## Prerequisites

Phase 6 must be complete and working:
- Full multi-agent system with streaming, MCP tools, sandbox, vector stores, config editor
- The app runs reliably in local Docker Compose

Also required (external, not in this repo):
- GKE cluster with ArgoCD running (managed in the infrastructure repo)
- Keycloak instance with an OIDC client configured for rhiza-agents
- DNS record for the production hostname
- GitHub repository secrets configured

## Files to Create

```
.github/workflows/build.yml
```

## Files to Modify

```
src/rhiza_agents/config.py
chart/Chart.yaml
chart/values.yaml
chart/templates/deployment.yaml
chart/templates/service.yaml
chart/templates/ingress.yaml
chart/templates/pvc.yaml
chart/templates/_helpers.tpl
```

Note: The `chart/` directory may already have skeleton files. If not, create them.

## Key APIs & Packages

```python
# LangSmith tracing (automatic with env vars -- no code changes needed)
# langgraph traces automatically when LANGCHAIN_TRACING_V2=true

# Postgres checkpointer (for production migration)
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
```

Add `langgraph-checkpoint-postgres` and `psycopg[binary]` to `pyproject.toml` dependencies (or as an optional dependency group).

## Implementation Details

### `.github/workflows/build.yml` -- CI/CD Pipeline

Follow the pattern from sheerwater-chat's build workflow:

```yaml
name: Build and Push Docker Image

on:
  push:
    branches:
      - main

permissions:
  contents: write
  packages: write

jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - name: Checkout
        uses: actions/checkout@v4

      - name: Set up Docker Buildx
        uses: docker/setup-buildx-action@v3

      - name: Log in to GHCR
        uses: docker/login-action@v3
        with:
          registry: ghcr.io
          username: ${{ github.actor }}
          password: ${{ secrets.GITHUB_TOKEN }}

      - name: Set up QEMU
        uses: docker/setup-qemu-action@v3

      - name: Get build timestamp
        id: timestamp
        run: echo "timestamp=$(date -u +'%Y-%m-%d')" >> $GITHUB_OUTPUT

      - name: Build and push
        uses: docker/build-push-action@v6
        with:
          context: .
          platforms: linux/amd64,linux/arm64
          push: true
          tags: |
            ghcr.io/${{ github.repository }}:${{ github.sha }}
            ghcr.io/${{ github.repository }}:latest
          build-args: |
            GIT_SHA=${{ github.sha }}
            BUILD_TIMESTAMP=${{ steps.timestamp.outputs.timestamp }}
          cache-from: type=gha
          cache-to: type=gha,mode=max

      - name: Update chart and push to deploy branch
        run: |
          sed -i 's/tag: .*/tag: ${{ github.sha }}/' chart/values.yaml
          git config user.name "github-actions[bot]"
          git config user.email "github-actions[bot]@users.noreply.github.com"
          git add chart/values.yaml
          git commit -m "Deploy ${{ github.sha }}"
          git push -f origin HEAD:deploy
```

This workflow:
1. Triggers on push to main
2. Builds a multi-arch Docker image (amd64 + arm64)
3. Pushes to GHCR with both SHA tag and `latest` tag
4. Updates `chart/values.yaml` with the new image tag
5. Force-pushes to the `deploy` branch
6. ArgoCD watches the `deploy` branch and syncs automatically

### Helm Chart Files

#### `chart/Chart.yaml`

```yaml
apiVersion: v2
name: rhiza-agents
description: Multi-agent chat platform built on LangGraph

type: application

version: 0.1.0
appVersion: "0.1.0"
```

#### `chart/templates/_helpers.tpl`

Standard Helm helpers for name/labels/selectors. Follow the sheerwater-chat pattern:

```
{{- define "rhiza-agents.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "rhiza-agents.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- printf "%s" $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}

{{- define "rhiza-agents.labels" -}}
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version }}
app.kubernetes.io/name: {{ include "rhiza-agents.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{- define "rhiza-agents.selectorLabels" -}}
app.kubernetes.io/name: {{ include "rhiza-agents.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}
```

#### `chart/templates/deployment.yaml`

Follow the sheerwater-chat deployment pattern:

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: {{ include "rhiza-agents.fullname" . }}
  labels:
    {{- include "rhiza-agents.labels" . | nindent 4 }}
spec:
  replicas: 1
  strategy:
    type: Recreate  # SQLite does not support concurrent writers
  selector:
    matchLabels:
      {{- include "rhiza-agents.selectorLabels" . | nindent 6 }}
  template:
    metadata:
      labels:
        {{- include "rhiza-agents.selectorLabels" . | nindent 8 }}
    spec:
      {{- with .Values.imagePullSecrets }}
      imagePullSecrets:
        {{- toYaml . | nindent 8 }}
      {{- end }}
      containers:
        - name: {{ .Chart.Name }}
          image: "{{ .Values.image.repository }}:{{ .Values.image.tag | default .Chart.AppVersion }}"
          imagePullPolicy: {{ .Values.image.pullPolicy }}
          ports:
            - name: http
              containerPort: {{ .Values.service.port }}
              protocol: TCP
          env:
            - name: DATABASE_URL
              value: "sqlite:////data/rhiza_agents.db"
            - name: CHECKPOINT_DB_PATH
              value: "/data/checkpoints.db"
            - name: CHROMA_PERSIST_DIR
              value: "/data/chroma"
            {{- range $key, $value := .Values.env }}
            {{- if $value }}
            - name: {{ $key }}
              value: {{ $value | quote }}
            {{- end }}
            {{- end }}
          envFrom:
            - secretRef:
                name: {{ .Values.secretName }}
          volumeMounts:
            - name: data
              mountPath: /data
          {{- with .Values.resources }}
          resources:
            {{- toYaml . | nindent 12 }}
          {{- end }}
          livenessProbe:
            httpGet:
              path: /login
              port: http
            initialDelaySeconds: 10
            periodSeconds: 30
          readinessProbe:
            httpGet:
              path: /login
              port: http
            initialDelaySeconds: 5
            periodSeconds: 10
      volumes:
        - name: data
          persistentVolumeClaim:
            claimName: {{ include "rhiza-agents.fullname" . }}-pvc
```

#### `chart/templates/service.yaml`

```yaml
apiVersion: v1
kind: Service
metadata:
  name: {{ include "rhiza-agents.fullname" . }}
  labels:
    {{- include "rhiza-agents.labels" . | nindent 4 }}
spec:
  type: {{ .Values.service.type }}
  ports:
    - port: {{ .Values.service.port }}
      targetPort: http
      protocol: TCP
      name: http
  selector:
    {{- include "rhiza-agents.selectorLabels" . | nindent 4 }}
```

#### `chart/templates/ingress.yaml`

```yaml
{{- if .Values.ingress.enabled }}
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: {{ include "rhiza-agents.fullname" . }}
  labels:
    {{- include "rhiza-agents.labels" . | nindent 4 }}
  {{- with .Values.ingress.annotations }}
  annotations:
    {{- toYaml . | nindent 4 }}
  {{- end }}
spec:
  {{- if .Values.ingress.className }}
  ingressClassName: {{ .Values.ingress.className }}
  {{- end }}
  {{- if .Values.ingress.tls }}
  tls:
    {{- range .Values.ingress.tls }}
    - hosts:
        {{- range .hosts }}
        - {{ . | quote }}
        {{- end }}
      secretName: {{ .secretName }}
    {{- end }}
  {{- end }}
  rules:
    {{- range .Values.ingress.hosts }}
    - host: {{ .host | quote }}
      http:
        paths:
          {{- range .paths }}
          - path: {{ .path }}
            pathType: {{ .pathType }}
            backend:
              service:
                name: {{ include "rhiza-agents.fullname" $ }}
                port:
                  number: {{ $.Values.service.port }}
          {{- end }}
    {{- end }}
{{- end }}
```

#### `chart/templates/pvc.yaml`

```yaml
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: {{ include "rhiza-agents.fullname" . }}-pvc
  labels:
    {{- include "rhiza-agents.labels" . | nindent 4 }}
spec:
  accessModes:
    - ReadWriteOnce
  storageClassName: {{ .Values.persistence.storageClassName }}
  resources:
    requests:
      storage: {{ .Values.persistence.size }}
```

#### `chart/values.yaml`

```yaml
# Container image configuration
image:
  repository: ghcr.io/rhiza-research/rhiza-agents
  tag: latest
  pullPolicy: Always

imagePullSecrets: []

# Service configuration
service:
  type: ClusterIP
  port: 8080

# Resource configuration
resources:
  limits:
    cpu: 1000m
    memory: 2Gi
  requests:
    cpu: 500m
    memory: 1Gi

# Ingress configuration
ingress:
  enabled: false
  className: ""
  annotations: {}
  hosts: []
  tls: []

# Persistent volume for SQLite + ChromaDB
persistence:
  size: 5Gi
  storageClassName: standard-rwo

# Name of the Kubernetes secret containing sensitive env vars.
# This secret is created by Terraform, NOT by this chart.
# Keys: KEYCLOAK_CLIENT_SECRET, ANTHROPIC_API_KEY, DAYTONA_API_KEY, SECRET_KEY,
#        LANGSMITH_API_KEY (optional)
secretName: rhiza-agents-secrets

# Non-secret environment variables
env:
  KEYCLOAK_URL: ""
  KEYCLOAK_PUBLIC_URL: ""
  KEYCLOAK_REALM: ""
  KEYCLOAK_CLIENT_ID: ""
  MCP_SERVER_URL: ""
  BASE_URL: ""
  LANGCHAIN_TRACING_V2: "true"
  LANGCHAIN_PROJECT: "rhiza-agents"
  # DATABASE_URL, CHECKPOINT_DB_PATH, CHROMA_PERSIST_DIR are set in the deployment template
```

### Modifications to `config.py` -- LangSmith Environment Variables

LangSmith integration requires no code changes -- LangGraph and LangChain automatically detect these environment variables:

| Env Var | Purpose |
|---------|---------|
| `LANGCHAIN_TRACING_V2` | Set to "true" to enable tracing |
| `LANGCHAIN_API_KEY` | LangSmith API key (in the K8s secret as `LANGSMITH_API_KEY`) |
| `LANGCHAIN_PROJECT` | Project name in LangSmith dashboard |

However, the K8s secret key `LANGSMITH_API_KEY` needs to map to env var `LANGCHAIN_API_KEY`. Handle this in one of two ways:

**Option A:** Name the secret key `LANGCHAIN_API_KEY` in Terraform (preferred).

**Option B:** Add an env var mapping in the deployment template:
```yaml
- name: LANGCHAIN_API_KEY
  valueFrom:
    secretKeyRef:
      name: {{ .Values.secretName }}
      key: LANGSMITH_API_KEY
      optional: true
```

Go with Option A for simplicity -- just ensure the Terraform secret uses the key name `LANGCHAIN_API_KEY`.

Add `LANGSMITH_API_KEY` to `config.py` as an optional field (empty string default) for documentation purposes, but it doesn't need to be used directly in code:

```python
# LangSmith (optional -- tracing is automatic when env vars are set)
langsmith_api_key: str = ""  # LANGCHAIN_API_KEY env var
langsmith_project: str = ""  # LANGCHAIN_PROJECT env var
```

### Postgres Migration (Future, Documented Here)

For production with multiple replicas, SQLite must be replaced with PostgreSQL for both the app database and the LangGraph checkpointer. This is not implemented in this phase but is documented here for the follow-up.

**App database migration:**
- Change `DATABASE_URL` from `sqlite:///...` to `postgresql://...`
- The `databases` package supports both backends -- the `databases[asyncpg]` extra provides PostgreSQL support
- Add `databases[asyncpg]` to pyproject.toml
- SQL syntax differences: check for any SQLite-specific SQL (e.g., `AUTOINCREMENT` vs `SERIAL`)

**Checkpointer migration:**
- Replace `AsyncSqliteSaver` with `AsyncPostgresSaver` from `langgraph-checkpoint-postgres`
- In the lifespan, set up the checkpointer:
  ```python
  async with AsyncPostgresSaver.from_conn_string(postgres_url) as checkpointer:
      await checkpointer.setup()  # Creates tables if they don't exist
      # ... use checkpointer
  ```
- Add env var `CHECKPOINT_DB_URL` (PostgreSQL connection string) as an alternative to `CHECKPOINT_DB_PATH`
- Logic: if `CHECKPOINT_DB_URL` is set, use `AsyncPostgresSaver`; otherwise use `AsyncSqliteSaver`

**Helm chart changes for Postgres:**
- Remove the PVC (no longer needed for SQLite -- ChromaDB still needs it)
- Actually, ChromaDB still needs the PVC. Keep PVC, just adjust the deployment to not mount SQLite/checkpoint paths if using Postgres.
- Add Postgres connection env vars to the deployment

This migration is NOT implemented in this phase. Deploy with SQLite first (single replica, Recreate strategy).

### Infrastructure Repo Changes (Documentation Only)

The following changes need to be made in the infrastructure repository (`infrastructure/terraform/20-gke-cluster/`). Do NOT make these changes in the rhiza-agents repo -- document them here for the infra team.

**New Terraform file: `rhiza-agents.tf`**

Should define:
1. Kubernetes namespace: `rhiza-agents`
2. Kubernetes secret: `rhiza-agents-secrets` with keys:
   - `KEYCLOAK_CLIENT_SECRET`
   - `ANTHROPIC_API_KEY`
   - `DAYTONA_API_KEY`
   - `SECRET_KEY`
   - `LANGCHAIN_API_KEY` (LangSmith)
3. ArgoCD Application:
   - `repoURL`: GitHub repo URL for rhiza-agents
   - `targetRevision`: `deploy` branch
   - `path`: `chart/`
   - `destination.server`: `https://kubernetes.default.svc`
   - `destination.namespace`: `rhiza-agents`
   - Helm values overrides for production env vars (KEYCLOAK_URL, MCP_SERVER_URL, etc.)
   - Auto-sync policy (optional -- can start with manual sync)
4. Keycloak OIDC client: `rhiza-agents` client in the shared Keycloak realm
5. DNS record: `agents.shared.rhizaresearch.org` pointing to the ingress load balancer IP

**Ingress values override in ArgoCD Application:**
```yaml
ingress:
  enabled: true
  className: nginx
  annotations:
    cert-manager.io/cluster-issuer: letsencrypt-prod
  hosts:
    - host: agents.shared.rhizaresearch.org
      paths:
        - path: /
          pathType: Prefix
  tls:
    - secretName: rhiza-agents-tls
      hosts:
        - agents.shared.rhizaresearch.org
```

### GitHub Repository Settings

Configure these repository secrets (via GitHub Settings > Secrets and Variables > Actions):
- `GITHUB_TOKEN` is automatic (no configuration needed)

The GHCR push uses `GITHUB_TOKEN` with `packages: write` permission, which is already declared in the workflow.

Ensure the repository visibility allows GHCR image pulls from the cluster. If the repo is private, you need imagePullSecrets in the Helm chart. If public, no pull secrets needed.

## Reference Files

| File | What to learn |
|------|---------------|
| `/Users/tristan/Devel/rhiza/rhiza-agents/docs/ARCHITECTURE.md` | Deployment section, technology stack |
| `/Users/tristan/Devel/rhiza/sheerwater-chat/.github/workflows/build.yml` | GitHub Actions workflow pattern |
| `/Users/tristan/Devel/rhiza/sheerwater-chat/chart/values.yaml` | Helm chart values pattern |
| `/Users/tristan/Devel/rhiza/sheerwater-chat/chart/Chart.yaml` | Chart metadata pattern |
| `/Users/tristan/Devel/rhiza/sheerwater-chat/chart/templates/deployment.yaml` | Deployment template pattern |
| `/Users/tristan/Devel/rhiza/sheerwater-chat/Dockerfile` | Dockerfile pattern |
| `/Users/tristan/Devel/rhiza/rhiza-agents/src/rhiza_agents/config.py` | Config to add LangSmith vars |

## Acceptance Criteria

1. Push a commit to `main` -- GitHub Actions builds the image successfully
2. Multi-arch image (amd64/arm64) is pushed to GHCR
3. `chart/values.yaml` on the `deploy` branch has the updated image tag (commit SHA)
4. ArgoCD detects the change and syncs (or manual sync works)
5. Pod starts up healthy in GKE (liveness/readiness probes pass)
6. App is accessible at the production hostname with valid TLS certificate
7. Login via Keycloak works with the production OIDC client
8. Chat works end-to-end: send message, get response with MCP tool calls
9. LangSmith shows traces for the conversation (check the LangSmith dashboard)
10. Helm chart can be deployed cleanly with `helm template` (no syntax errors)

## What NOT to Do

- **Do NOT modify the infrastructure repo** from this rhiza-agents repo. The Terraform changes (namespace, secret, ArgoCD app, DNS) are a separate PR to the infrastructure repo. This spec only documents what needs to exist there.
- **Do NOT run Terraform commands** from this repo.
- **Do NOT implement the Postgres migration** in this phase. Deploy with SQLite first (single replica). Postgres migration is a follow-up task once the initial deployment is validated.
- **Do NOT set up monitoring/alerting** -- LangSmith tracing is sufficient for initial observability. Prometheus/Grafana integration can come later.
- **Do NOT configure auto-scaling** -- single replica with Recreate strategy. Scaling requires Postgres first.
- **Do NOT manually build or push Docker images** -- always use the CI/CD pipeline.
