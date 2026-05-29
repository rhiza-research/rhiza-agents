"""Slack Socket Mode connector that drives the single-agent graph.

Each Slack channel is bound to one configured rhiza-agents user (via
``config.slack_channel_user_map``); that user's skills, MCP servers, and
knowledge bases define the channel's experience. A top-level @-mention opens a
thread-scoped conversation whose id is the Slack thread root ``ts`` — which is
also the LangGraph checkpointer ``thread_id`` and the reply ``thread_ts``, so
one key is the conversation boundary, the context scope, and the reply target.

After opening, the bot owns the thread: it continues on plain replies (no
re-mention needed) until ``THREAD_IDLE_SECONDS`` of inactivity, derived from the
conversation's ``updated_at`` (refreshed each turn via ``touch_conversation`` —
no separate registry). A re-mention in an aged-out thread resumes the same
conversation with full prior context (the checkpointer replays history).

Over Slack the agent is skills-only (no ``execute_python_code``) and HITL
interrupts are auto-approved. Those two are coupled: curated, installed skills
are the trust boundary that makes unattended execution acceptable.
"""

import asyncio
import logging
import os
from datetime import UTC, datetime

from langchain_core.messages import HumanMessage, ToolMessage
from langgraph.types import Command

from .agents.graph import get_or_build_agent_graph
from .agents.tools.files import fetch_file_content
from .config import Config
from .db.sqlite import Database
from .deps import effective_agent_configs, mcp_tools_for_user, skill_tools_for_user
from .logging_config import chat_event_logger
from .messages import extract_content_blocks, extract_content_blocks_from_token

logger = logging.getLogger(__name__)

# Sliding idle window: a thread stops being watched after this much inactivity.
THREAD_IDLE_SECONDS = 24 * 60 * 60

# Bound the HITL auto-approve loop so a tool that interrupts on every round
# cannot spin forever (runaway model spend).
MAX_AUTO_RESUMES = 25

_IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".webp")

# Slack's chat.postMessage text field upper bound; keep a safety margin.
_MAX_SLACK_TEXT = 38000

# Cap images uploaded per turn so a skill emitting many files can't spam a thread.
MAX_IMAGES_PER_TURN = 10


def _conversation_is_active(conversation: dict) -> bool:
    """True if the conversation's last activity is within the idle window."""
    ts = conversation.get("updated_at")
    if ts is None:
        return True  # just created; no activity recorded yet
    if isinstance(ts, datetime):
        dt = ts
    elif isinstance(ts, (int, float)):
        dt = datetime.fromtimestamp(ts, tz=UTC)
    elif isinstance(ts, str):
        try:
            dt = datetime.fromisoformat(ts)
        except ValueError:
            return False  # corrupt timestamp -> fail closed (don't auto-run on a bad value)
    else:
        return False  # unknown type -> fail closed
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return (datetime.now(UTC) - dt).total_seconds() < THREAD_IDLE_SECONDS


class SlackConnector:
    """Owns the Slack AsyncApp + Socket Mode handler and runs agent turns."""

    def __init__(
        self,
        *,
        config: Config,
        db: Database,
        checkpointer,
        system_mcp_tools: list,
        system_mcp_tools_by_server: dict[str, list],
        vectorstore_manager=None,
    ):
        # Imported here so the slack extra stays optional — this module is only
        # imported when Slack is configured.
        from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
        from slack_bolt.app.async_app import AsyncApp

        self.config = config
        self.db = db
        self.checkpointer = checkpointer
        self.system_mcp_tools = system_mcp_tools
        self.system_mcp_tools_by_server = system_mcp_tools_by_server
        self.vectorstore_manager = vectorstore_manager
        self.channel_user_map = config.slack_channel_user_map
        self.bot_user_id: str | None = None
        # Per-conversation locks serialize turns within a single thread.
        self._locks: dict[str, asyncio.Lock] = {}

        self.app = AsyncApp(token=config.slack_bot_token)
        self.handler = AsyncSocketModeHandler(self.app, config.slack_app_token)
        self.app.event("app_mention")(self._on_app_mention)
        self.app.event("message")(self._on_message)

    async def start(self):
        """Resolve the bot's own user id, then run the Socket Mode loop.

        Raises if the bot identity can't be resolved. Without ``bot_user_id``,
        mention dedup between the ``app_mention`` and ``message`` handlers is
        unreliable — a single mention would be processed by both, double-running
        the turn — so we refuse to run degraded.
        """
        auth = await self.app.client.auth_test()
        self.bot_user_id = auth.get("user_id")
        if not self.bot_user_id:
            raise RuntimeError("Slack auth_test returned no user_id; refusing to start the connector")
        logger.info("Slack connector starting (channels mapped: %d)", len(self.channel_user_map))
        await self.handler.start_async()

    async def stop(self):
        try:
            await self.handler.close_async()
        except Exception:
            logger.warning("Error closing Slack Socket Mode handler", exc_info=True)

    def _user_for_channel(self, channel: str) -> str | None:
        return self.channel_user_map.get(channel)

    def _strip_mention(self, text: str) -> str:
        if self.bot_user_id:
            text = text.replace(f"<@{self.bot_user_id}>", "")
        return text.strip()

    def _mentions_bot(self, text: str) -> bool:
        return bool(self.bot_user_id) and f"<@{self.bot_user_id}>" in text

    def _lock_for(self, conversation_id: str) -> asyncio.Lock:
        lock = self._locks.get(conversation_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[conversation_id] = lock
        return lock

    async def _on_app_mention(self, event: dict, client):
        """A top-level or in-thread @-mention: open or resume a thread conversation."""
        if event.get("bot_id") or event.get("subtype"):
            return  # ignore bot/system/edited events — no loops, no reprocessing
        channel = event.get("channel", "")
        user_id = self._user_for_channel(channel)
        if not user_id:
            logger.debug("Ignoring mention in unmapped channel %s", channel)
            return
        # The thread root is the conversation key: an existing thread_ts, else
        # this message's own ts (a new top-level thread).
        thread_root = event.get("thread_ts") or event.get("ts")
        if not thread_root:
            return
        text = self._strip_mention(event.get("text", ""))
        try:
            conversation = await self.db.get_conversation_by_id(thread_root)
            if conversation is None:
                try:
                    await self.db.create_conversation(thread_root, user_id)
                except Exception:
                    # A concurrent opening mention already created it — fine.
                    logger.debug("conversation %s created concurrently", thread_root)
        except Exception:
            logger.exception("Slack: DB error opening conversation %s", thread_root)
            return
        # Mention always (re)activates the thread, even if it had aged out.
        await self._run_turn(channel=channel, thread_root=thread_root, user_id=user_id, text=text, client=client)

    async def _on_message(self, event: dict, client):
        """A plain reply: continue only if it's in an owned, active thread."""
        if event.get("bot_id") or event.get("subtype"):
            return  # ignore bot/system messages — no loops
        channel = event.get("channel", "")
        user_id = self._user_for_channel(channel)
        if not user_id:
            return
        thread_root = event.get("thread_ts")
        if not thread_root:
            return  # top-level non-mention — ignore
        text = event.get("text", "")
        if self._mentions_bot(text):
            return  # the app_mention handler owns mentions
        try:
            conversation = await self.db.get_conversation_by_id(thread_root)
        except Exception:
            logger.exception("Slack: DB error looking up conversation %s", thread_root)
            return
        if conversation is None or not _conversation_is_active(conversation):
            return  # not an owned thread, or aged out — re-mention required to revive
        await self._run_turn(
            channel=channel, thread_root=thread_root, user_id=user_id, text=text.strip(), client=client
        )

    def _log(self, event: str, conversation_id: str, user_id: str, **data):
        # Auto-approve removes the human gate, so keep an audit trail of what ran.
        chat_event_logger.info(event, extra={"conversation_id": conversation_id, "user_id": user_id, **data})

    async def _build_graph(self, user_id: str):
        mcp_by_server, _ = await mcp_tools_for_user(self.db, self.system_mcp_tools_by_server, user_id)
        skills = await skill_tools_for_user(self.db, user_id)
        configs = await effective_agent_configs(self.db, user_id)
        return await get_or_build_agent_graph(
            configs,
            self.system_mcp_tools,
            self.checkpointer,
            self.vectorstore_manager,
            self.db,
            mcp_by_server,
            skills,
            user_id=user_id,
            skills_only=True,
        )

    async def _files_dict(self, graph, run_config) -> dict:
        return self._files_from_state(await graph.aget_state(run_config))

    @staticmethod
    def _files_from_state(state) -> dict:
        if state and getattr(state, "values", None):
            files = state.values.get("files", {})
            if isinstance(files, dict):
                return files
        return {}

    @staticmethod
    def _final_text_from_state(state) -> str:
        """Text of the last assistant message — the final answer, without the
        interim pre-tool narration the raw token stream would also include."""
        if not (state and getattr(state, "values", None)):
            return ""
        for msg in reversed(state.values.get("messages") or []):
            if getattr(msg, "type", "") == "ai":
                content = msg.content
                if isinstance(content, str):
                    return content.strip()
                if isinstance(content, list):
                    text, _ = extract_content_blocks(content)
                    return (text or "").strip()
        return ""

    @staticmethod
    def _new_image_paths(before: dict, after: dict) -> set[str]:
        """Image paths created or overwritten this turn, excluding the shared
        /data cache (only per-conversation /workspace output is delivered)."""
        out = set()
        for path, entry in after.items():
            if not path.lower().endswith(_IMAGE_SUFFIXES):
                continue
            if path.startswith("/data"):
                continue
            if before.get(path) != entry:  # new or overwritten
                out.add(path)
        return out

    async def _run_turn(self, *, channel: str, thread_root: str, user_id: str, text: str, client):
        conversation_id = thread_root
        self._log("slack_user_message", conversation_id, user_id, channel=channel, content=text[:500])
        if not text.strip():
            # Bare mention with no instruction — prompt rather than run on empty input.
            await client.chat_postMessage(
                channel=channel, thread_ts=thread_root, text="Hi! What would you like me to do?"
            )
            return
        run_config = {"configurable": {"thread_id": conversation_id}}

        # Serialize turns within one thread so concurrent Slack events don't
        # interleave writes to the same checkpointer thread_id.
        lock = self._lock_for(conversation_id)
        await lock.acquire()
        try:
            graph = await self._build_graph(user_id)
            files_before = await self._files_dict(graph, run_config)

            stream_input = {"messages": [HumanMessage(content=text)]}
            text_parts: list[str] = []
            for _resume_round in range(MAX_AUTO_RESUMES + 1):
                auto_resume = False
                # version="v2" makes astream yield dict chunks ({"type","ns","data"});
                # without it astream returns positional tuples and the
                # chunk["type"]/chunk["data"] access below fails. Mirrors chat.py.
                async for chunk in graph.astream(
                    stream_input,
                    config=run_config,
                    stream_mode=["messages", "updates"],
                    version="v2",
                    subgraphs=True,
                ):
                    ctype = chunk["type"]
                    if ctype == "messages":
                        token, metadata = chunk["data"]
                        if isinstance(token, ToolMessage):
                            continue
                        if metadata.get("langgraph_node", "") == "tools":
                            continue
                        piece, _reasoning = extract_content_blocks_from_token(token)
                        if piece:
                            text_parts.append(piece)
                    elif ctype == "updates":
                        update_data = chunk["data"]
                        if "__interrupt__" in update_data:
                            if chunk.get("ns"):
                                continue
                            # Skills-only path: auto-approve (no Slack approval modal).
                            stream_input = Command(resume={"decisions": [{"type": "approve"}]})
                            auto_resume = True
                            break
                if not auto_resume:
                    break
            else:
                logger.warning("Auto-resume cap (%d) reached for conversation %s", MAX_AUTO_RESUMES, conversation_id)

            final_state = await graph.aget_state(run_config)
            reply = self._final_text_from_state(final_state) or "".join(text_parts).strip()
            files_after = self._files_from_state(final_state)
            new_images = sorted(self._new_image_paths(files_before, files_after))

            await self._deliver(client, channel, thread_root, reply, conversation_id, new_images)
            await self.db.touch_conversation(conversation_id)
            self._log("slack_done", conversation_id, user_id, images=len(new_images))
        except Exception as e:
            logger.exception("Slack turn failed for conversation %s", conversation_id)
            self._log("slack_error", conversation_id, user_id, error=str(e))
            try:
                await client.chat_postMessage(
                    channel=channel,
                    thread_ts=thread_root,
                    text="Sorry — something went wrong handling that request.",
                )
            except Exception:
                logger.warning("Failed to post Slack error message", exc_info=True)
        finally:
            lock.release()

    async def _post(self, client, channel: str, thread_root: str, text: str):
        try:
            await client.chat_postMessage(channel=channel, thread_ts=thread_root, text=text)
        except Exception:
            logger.warning("Failed to post Slack message to thread %s", thread_root, exc_info=True)

    async def _deliver(self, client, channel: str, thread_root: str, reply: str, conversation_id: str, images):
        if reply:
            if len(reply) > _MAX_SLACK_TEXT:
                reply = reply[:_MAX_SLACK_TEXT] + "\n\n…(response truncated)"
            await self._post(client, channel, thread_root, reply)
        elif not images:
            await self._post(client, channel, thread_root, "(no response produced)")

        for path in images[:MAX_IMAGES_PER_TURN]:
            name = os.path.basename(path)
            try:
                content_bytes, _mtime = await fetch_file_content(conversation_id, path)
                await client.files_upload_v2(
                    channel=channel,
                    thread_ts=thread_root,
                    file=content_bytes,
                    filename=name,
                    title=name,
                )
            except Exception:
                logger.warning("Failed to upload generated image %s to Slack", path, exc_info=True)
                await self._post(client, channel, thread_root, f"(generated `{name}` but couldn't upload it)")

        if len(images) > MAX_IMAGES_PER_TURN:
            await self._post(
                client, channel, thread_root, f"(+{len(images) - MAX_IMAGES_PER_TURN} more images not shown)"
            )
