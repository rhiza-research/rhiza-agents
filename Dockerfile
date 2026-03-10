FROM python:3.12-slim

# Accept git SHA and build timestamp as build arguments
ARG GIT_SHA=unknown
ARG BUILD_TIMESTAMP=unknown

WORKDIR /app

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Copy project files
COPY pyproject.toml uv.lock ./
COPY src ./src
COPY langgraph.json ./

# Install dependencies
RUN uv sync --frozen --no-dev --extra sandbox

# Set git SHA and build timestamp as environment variables
ENV GIT_SHA=${GIT_SHA}
ENV BUILD_TIMESTAMP=${BUILD_TIMESTAMP}

# LangGraph Server serves the agent graph
CMD ["uv", "run", "langgraph", "up", "--host", "0.0.0.0", "--port", "8123"]
