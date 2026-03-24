"""FastAPI application creation, lifespan, and router registration."""

import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from starlette.middleware.sessions import SessionMiddleware

from .agents.registry import get_default_configs_by_id
from .agents.tools.mcp import create_mcp_client
from .agents.tools.sandbox import cleanup_idle_sandboxes
from .auth import create_oauth
from .config import Config
from .db.sqlite import Database
from .logging_config import setup_logging

logger = logging.getLogger(__name__)


async def _load_system_skills(db):
    """Scan bundled skills/ directory and upsert each into the database."""
    from .agents.tools.skills import parse_skill_md

    skills_dir = Path(__file__).parent / "skills"
    if not skills_dir.is_dir():
        return

    count = 0
    for skill_path in sorted(skills_dir.iterdir()):
        if not skill_path.is_dir():
            continue
        skill_md_path = skill_path / "SKILL.md"
        if not skill_md_path.exists():
            continue
        skill_md = skill_md_path.read_text()
        try:
            parsed = parse_skill_md(skill_md)
        except ValueError as e:
            logger.warning("Skipping invalid system skill %s: %s", skill_path.name, e)
            continue

        # Load companion files
        scripts_json = _load_companion_dir(skill_path / "scripts")
        references_json = _load_companion_dir(skill_path / "references")
        assets_json = _load_companion_dir(skill_path / "assets")

        await db.upsert_system_skill(
            skill_id=f"system-{parsed.name}",
            name=parsed.name,
            description=parsed.description,
            skill_md=skill_md,
            scripts_json=scripts_json,
            references_json=references_json,
            assets_json=assets_json,
        )
        count += 1

    if count:
        logger.info("Loaded %d system skills", count)


def _load_companion_dir(directory: Path) -> str | None:
    """Load all files in a companion directory as a JSON dict of filename -> content."""
    import json

    if not directory.is_dir():
        return None
    contents = {}
    for f in sorted(directory.iterdir()):
        if f.is_file():
            contents[f.name] = f.read_text()
    return json.dumps(contents) if contents else None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan handler — initialize and clean up shared resources."""
    config = Config.from_env()
    setup_logging(config.log_level, config.chat_event_logging)

    db = Database(config.database_url)
    oauth = create_oauth(config)

    await db.connect()
    logger.info("Connected to database")

    # Store on app.state for dependency injection
    app.state.config = config
    app.state.db = db
    app.state.oauth = oauth
    app.state.mcp_tools = []
    app.state.mcp_tools_by_server = {}
    app.state.vectorstore_manager = None
    app.state.static_version = str(int(time.time()))

    # Seed system MCP server into database and load tools
    if config.mcp_server_url:
        await db.upsert_system_mcp_server("sheerwater", "Sheerwater", config.mcp_server_url, "sse")
        mcp_client = create_mcp_client({"sheerwater": {"url": config.mcp_server_url, "transport": "sse"}})
        for attempt in range(10):
            try:
                app.state.mcp_tools = await mcp_client.get_tools()
                break
            except Exception:
                if attempt == 9:
                    raise
                logger.info("MCP server not ready, retrying in %ds...", attempt + 1)
                await asyncio.sleep(attempt + 1)
        app.state.mcp_tools_by_server["sheerwater"] = list(app.state.mcp_tools)
        logger.info("Loaded %d MCP tools from sheerwater", len(app.state.mcp_tools))

    # Initialize vector store manager
    if config.chroma_persist_dir:
        from .vectorstore.manager import VectorStoreManager

        app.state.vectorstore_manager = VectorStoreManager(config.chroma_persist_dir)
        logger.info("Initialized VectorStoreManager at %s", config.chroma_persist_dir)

    # Load system skills from bundled directory
    await _load_system_skills(db)

    # Build initial agent name mappings for logging
    configs_by_id = get_default_configs_by_id()
    app.state.agent_names = {agent_id: c.name for agent_id, c in configs_by_id.items()}
    app.state.tool_to_agent = {}
    for agent_id, c in configs_by_id.items():
        for tool_id in c.tools:
            if tool_id.startswith("mcp:"):
                for t in app.state.mcp_tools:
                    app.state.tool_to_agent[t.name] = agent_id
        if "sandbox:daytona" in c.tools:
            app.state.tool_to_agent["execute_python_code"] = agent_id
            app.state.tool_to_agent["write_file"] = agent_id
            app.state.tool_to_agent["run_file"] = agent_id

    async def _sandbox_cleanup_loop():
        while True:
            await asyncio.sleep(60)
            await cleanup_idle_sandboxes()

    async with AsyncSqliteSaver.from_conn_string(config.checkpoint_db_path) as cp:
        app.state.checkpointer = cp
        logger.info("Supervisor graph ready (built on first request)")

        cleanup_task = asyncio.create_task(_sandbox_cleanup_loop())
        yield
        cleanup_task.cancel()

    await db.disconnect()


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""
    app = FastAPI(title="Rhiza Agents", lifespan=lifespan)
    app.add_middleware(SessionMiddleware, secret_key=os.environ.get("SECRET_KEY", "dev-secret-key"))

    # Templates and static files
    base_dir = Path(__file__).parent
    app.state.templates = Jinja2Templates(directory=base_dir / "templates")
    app.mount("/static", StaticFiles(directory=base_dir / "static"), name="static")

    # Register route modules
    from .routes import agents, chat, conversations, mcp_servers, pages, settings, skills, vectorstores

    app.include_router(pages.router)
    app.include_router(chat.router)
    app.include_router(agents.router)
    app.include_router(mcp_servers.router)
    app.include_router(skills.router)
    app.include_router(vectorstores.router)
    app.include_router(conversations.router)
    app.include_router(settings.router)

    return app


app = create_app()


def run():
    """Entry point for `uv run rhiza-agents`."""
    import uvicorn

    uvicorn.run(
        "rhiza_agents.app:app",
        host="0.0.0.0",
        port=8080,
        reload=True,
        reload_dirs=["src"],
    )
