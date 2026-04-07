"""Langfuse observability integration.

Disabled (every accessor returns None) when LANGFUSE_PUBLIC_KEY is not set.
"""

import hashlib
import logging
import os
import secrets

logger = logging.getLogger(__name__)

# In-memory cache for per-user prompt registrations.
# Key: (username, agent_id). Value: (sha256(content), "<name>@v<N>", prompt_obj).
# Purpose: avoid hitting the Langfuse API on every chat invocation when the
# user's prompts haven't changed since the last sync. The cached prompt object
# is the TextPromptClient returned by the SDK; binding it as metadata on a
# langchain model creates the structured per-generation link in the trace UI.
_prompt_sync_cache: dict[tuple[str, str], tuple[str, str, object]] = {}


def langfuse_enabled() -> bool:
    """True iff a Langfuse public key is configured."""
    return bool(os.environ.get("LANGFUSE_PUBLIC_KEY"))


def new_trace_id() -> str:
    """Generate an OpenTelemetry-compatible 32-char hex trace id."""
    return secrets.token_hex(16)


def make_langfuse_handler(
    trace_id: str | None = None,
    prompt_objects: dict[str, object] | None = None,
):
    """Build a fresh Langfuse LangChain CallbackHandler bound to a trace id.

    A new handler is constructed per call so that the trace id can be set via
    `trace_context` (the only way to inject a custom trace id in the v4 SDK).
    Returns None when Langfuse is disabled or fails to initialize.

    `prompt_objects` is an optional `agent_id -> TextPromptClient` map used to
    structurally link each LLM generation to the prompt version that produced
    it. We can't bind these via `Runnable.with_config()` because langgraph's
    executor drops Runnable-bound metadata at node boundaries — only the run
    config metadata propagates. Instead, we wrap `on_chain_start`: every
    chain_start fired by langgraph carries `langgraph_node` in its metadata,
    so we look that up against `prompt_objects` and inject `langfuse_prompt`
    before delegating to the underlying handler. The handler then registers
    the prompt for that run, and the child LLM generation walks up the run
    chain in `_prompt_to_parent_run_map` and renders a clickable link.
    """
    if not langfuse_enabled():
        return None
    try:
        from langfuse.langchain import CallbackHandler

        if trace_id:
            handler = CallbackHandler(trace_context={"trace_id": trace_id})
        else:
            handler = CallbackHandler()

        if prompt_objects:
            # We do prompt linking by intercepting on_chain_start and injecting
            # `langfuse_prompt` into the metadata when `langgraph_node` matches
            # one of our agent ids. The Langfuse handler then stores it in
            # `_prompt_to_parent_run_map`, and the child LLM generation walks
            # up the parent run chain to find it and render a clickable link.
            #
            # The obvious approach -- wrapping each compiled agent with
            # `Runnable.with_config(metadata={"langfuse_prompt": ...})` --
            # does NOT work here. Langgraph's Pregel executor builds chain
            # runs from its own internal state and only propagates the *run
            # config* metadata (the dict passed to `graph.astream(config=...)`).
            # Metadata bound to inner Runnables via `with_config` is silently
            # dropped at node boundaries, so the chain_start events never see
            # `langfuse_prompt` and the handler never registers anything.
            # Verified empirically with a chain_start logger: every event
            # carried only the run-config metadata, never the bound metadata.
            #
            # We can rely on `langgraph_node` being present because langgraph
            # adds it to the metadata of every node it executes. The chain
            # name happens to match the agent id (because we name workers via
            # `create_agent(..., name=wc.id)` and the supervisor node is
            # `supervisor`), so the lookup against `prompt_objects` is direct.
            _orig_on_chain_start = handler.on_chain_start

            def _on_chain_start_with_prompt_injection(
                serialized, inputs, *, run_id, parent_run_id=None, tags=None, metadata=None, **kwargs
            ):
                node = (metadata or {}).get("langgraph_node")
                if node and node in prompt_objects:
                    # Copy so we don't mutate the dict langgraph reuses across nodes.
                    metadata = dict(metadata or {})
                    metadata["langfuse_prompt"] = prompt_objects[node]
                return _orig_on_chain_start(
                    serialized,
                    inputs,
                    run_id=run_id,
                    parent_run_id=parent_run_id,
                    tags=tags,
                    metadata=metadata,
                    **kwargs,
                )

            handler.on_chain_start = _on_chain_start_with_prompt_injection
        return handler
    except Exception as e:
        logger.warning("Failed to construct Langfuse handler: %s", e)
        return None


def get_langfuse_client():
    """Return the Langfuse SDK client, or None when disabled."""
    if not langfuse_enabled():
        return None
    try:
        from langfuse import get_client

        return get_client()
    except Exception as e:
        logger.warning("Failed to get Langfuse client: %s", e)
        return None


def _hash_prompt(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _prompt_name(agent_id: str, username: str | None) -> str:
    """`agent/<id>` for shared defaults, `agent/<id>/<username>` for overrides.

    Langfuse renders `/`-separated names as folders in the Prompts tab, so
    overrides land under a per-user folder under each agent. We deliberately
    use the human-readable username (Keycloak `preferred_username`) here
    rather than the UUID `sub` so the Prompts tab is browsable.
    """
    if username:
        return f"agent/{agent_id}/{username}"
    return f"agent/{agent_id}"


def _sync_prompt(client, *, name: str, content: str, agent_type: str, is_override: bool) -> tuple[str, object] | None:
    """Ensure a Langfuse prompt with the given name and content exists.

    Returns `(ref, prompt_obj)` where `ref` is the `<name>@v<version>` string
    and `prompt_obj` is the SDK TextPromptClient (used for metadata binding
    on the langchain model). Returns None on failure. Idempotent: only
    creates a new version when the in-code text differs from the version
    currently labeled `production` on the server.
    """
    try:
        existing = client.get_prompt(name, label="production")
        existing_text = getattr(existing, "prompt", None)
        if isinstance(existing_text, str) and existing_text == content:
            return f"{name}@v{getattr(existing, 'version', -1)}", existing
    except Exception:
        # Prompt does not exist yet — fall through to create.
        pass

    try:
        new_prompt = client.create_prompt(
            name=name,
            prompt=content,
            type="text",
            labels=["production"],
            tags=["rhiza-agents", f"agent-type:{agent_type}"],
            commit_message=("Synced from user override" if is_override else "Synced from default configs"),
        )
        version = getattr(new_prompt, "version", -1)
        logger.info("[langfuse-prompts] registered %s (v%d)", name, version)
        return f"{name}@v{version}", new_prompt
    except Exception as e:
        logger.warning("[langfuse-prompts] failed to register %s: %s", name, e)
        return None


def register_default_prompts() -> None:
    """Mirror the default agent prompts into Langfuse at app startup.

    Ensures the Prompts tab in the Langfuse UI is populated even before any
    user has run a chat. User-customized prompts are registered lazily on
    chat invocation via `sync_user_prompts`, not here.
    """
    client = get_langfuse_client()
    if client is None:
        return

    from .agents.registry import get_default_configs

    for cfg in get_default_configs():
        if not getattr(cfg, "enabled", True):
            continue
        result = _sync_prompt(
            client,
            name=_prompt_name(cfg.id, None),
            content=cfg.system_prompt,
            agent_type=cfg.type,
            is_override=False,
        )
        if result:
            logger.info("[langfuse-prompts] %s -> %s", cfg.id, result[0])


def sync_user_prompts(username: str, effective_configs: list) -> tuple[dict[str, str], dict[str, object]]:
    """Ensure each effective agent prompt for `username` is registered.

    Called once per chat invocation before the graph is built. For each
    enabled agent in the user's effective config, decides whether the prompt
    matches the in-code default (registered as `agent/<id>`) or is a user
    override (registered under `agent/<id>/<username>`).

    Returns a tuple `(refs, prompt_objects)`:
        - `refs`: dict of `agent_id -> "<prompt_name>@v<version>"` for
          attaching as trace metadata so the trace records which prompt
          version actually ran.
        - `prompt_objects`: dict of `agent_id -> TextPromptClient` for
          binding as `langfuse_prompt` metadata on each agent's langchain
          model. The Langfuse callback handler reads this binding to attach
          a structured (clickable) prompt link to the corresponding
          generation span in the trace UI.

    The cache makes the steady state (user has not edited any prompt) zero
    Langfuse API calls per invocation: just N hash computations. When a user
    edits a prompt the next invocation detects the hash mismatch, syncs the
    new version, and updates the cache.
    """
    client = get_langfuse_client()
    if client is None:
        return {}, {}

    from .agents.registry import get_default_configs_by_id

    defaults_by_id = get_default_configs_by_id()
    refs: dict[str, str] = {}
    prompt_objects: dict[str, object] = {}

    for cfg in effective_configs:
        if not getattr(cfg, "enabled", True):
            continue
        agent_id = cfg.id
        prompt_text = cfg.system_prompt
        default = defaults_by_id.get(agent_id)
        is_override = default is None or default.system_prompt != prompt_text

        cache_key = (username, agent_id)
        content_hash = _hash_prompt(prompt_text)
        cached = _prompt_sync_cache.get(cache_key)
        if cached and cached[0] == content_hash:
            refs[agent_id] = cached[1]
            prompt_objects[agent_id] = cached[2]
            continue

        name = _prompt_name(agent_id, username if is_override else None)
        result = _sync_prompt(
            client,
            name=name,
            content=prompt_text,
            agent_type=cfg.type,
            is_override=is_override,
        )
        if result:
            ref, prompt_obj = result
            _prompt_sync_cache[cache_key] = (content_hash, ref, prompt_obj)
            refs[agent_id] = ref
            prompt_objects[agent_id] = prompt_obj

    return refs, prompt_objects
