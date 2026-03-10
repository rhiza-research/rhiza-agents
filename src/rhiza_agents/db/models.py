"""Pydantic models for rhiza-agents."""

from pydantic import BaseModel, Field


class AgentConfig(BaseModel):
    """Configuration for an agent in the multi-agent graph."""

    id: str
    name: str
    type: str  # "supervisor" or "worker"
    system_prompt: str
    model: str = "claude-sonnet-4-20250514"
    tools: list[str] = Field(default_factory=list)
    vectorstore_ids: list[str] = Field(default_factory=list)
    enabled: bool = True
