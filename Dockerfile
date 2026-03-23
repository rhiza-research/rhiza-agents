FROM node:23-alpine AS frontend

WORKDIR /build
COPY frontend/package.json frontend/package-lock.json frontend/
COPY frontend/tsconfig.json frontend/
COPY frontend/src frontend/src
# Create output directory where esbuild expects it
RUN mkdir -p src/rhiza_agents/static
WORKDIR /build/frontend
RUN npm ci && npm run build


FROM python:3.12-slim

# Accept git SHA and build timestamp as build arguments
ARG GIT_SHA=unknown
ARG BUILD_TIMESTAMP=unknown

WORKDIR /app

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Copy project files
COPY pyproject.toml uv.lock README.md ./
COPY src ./src

# Copy frontend build output
COPY --from=frontend /build/src/rhiza_agents/static/ src/rhiza_agents/static/

# Install dependencies (include sandbox and vectorstore extras)
RUN uv sync --frozen --no-dev --extra sandbox --extra vectorstore

# Set git SHA and build timestamp as environment variables
ENV GIT_SHA=${GIT_SHA}
ENV BUILD_TIMESTAMP=${BUILD_TIMESTAMP}

# Run the app
CMD ["uv", "run", "rhiza-agents"]
