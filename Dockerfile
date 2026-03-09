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

# Install dependencies (include sandbox and vectorstore extras)
RUN uv sync --frozen --no-dev --extra sandbox --extra vectorstore

# Set git SHA and build timestamp as environment variables
ENV GIT_SHA=${GIT_SHA}
ENV BUILD_TIMESTAMP=${BUILD_TIMESTAMP}

# Run the app
CMD ["uv", "run", "rhiza-agents"]
