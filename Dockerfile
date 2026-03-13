FROM python:3.12-slim

WORKDIR /app

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Copy project files
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
COPY langgraph.json ./

# Install dependencies (server extra includes langgraph-api + uvicorn)
RUN uv sync --frozen --no-dev --extra server --extra api --extra sandbox

# Configure the in-memory LangGraph runtime (no postgres/redis/license needed)
ENV LANGSERVE_GRAPHS='{"agent": "./src/rhiza_agents/agent.py:graph"}' \
    LANGSMITH_LANGGRAPH_API_VARIANT=local_dev \
    MIGRATIONS_PATH=__inmem \
    DATABASE_URI=:memory: \
    REDIS_URI=fake \
    N_JOBS_PER_WORKER=10 \
    LANGGRAPH_RUNTIME_EDITION=inmem

EXPOSE 8123

# Run the LangGraph API server directly via uvicorn
CMD ["uv", "run", "uvicorn", "langgraph_api.server:app", "--host", "0.0.0.0", "--port", "8123"]
