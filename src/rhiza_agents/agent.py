"""Deep agent definition for rhiza-agents."""

import asyncio

from deepagents import create_deep_agent
from langchain.agents.middleware import ModelRetryMiddleware
from langchain.agents.middleware.tool_call_limit import ToolCallLimitMiddleware
from langchain_anthropic import ChatAnthropic

from rhiza_agents.tools.mcp import get_mcp_tools
from rhiza_agents.tools.sandbox import execute_python_code, is_sandbox_available

SYSTEM_PROMPT = """\
You are a weather and climate data analyst. You help users explore, evaluate, \
and compare weather forecast models using the Sheerwater benchmarking platform.

You have access to tools that let you:
- List available forecast models, metrics, and ground truth datasets
- Run evaluation metrics comparing forecasts against ground truth
- Compare multiple models side-by-side
- Generate comparison charts
- Get links to Grafana dashboards for deeper exploration

When a user asks about model performance:
1. Clarify the region, variable, and time period if not specified
2. Use the appropriate tools to gather data
3. Explain results clearly, including what the metrics mean

For long-running queries, warn the user about expected wait times and track \
job progress.

Metric guidance:
- MAE/RMSE: Lower is better — measures overall accuracy
- Bias: Closer to 0 is better — shows systematic over/under-prediction
- ACC: Higher is better (-1 to 1) — anomaly correlation skill
- SEEPS: Lower is better — designed specifically for precipitation
- Heidke/ETS: Higher is better — categorical skill scores"""

CODE_RUNNER_PROMPT = """\
You are a Python code execution assistant. You write and execute Python code \
for data analysis, computation, and visualization.

When asked to run code:
1. Write clean, readable Python code
2. Execute it using the execute_python_code tool
3. Explain the results

You can build on previous code executions within the same conversation — \
the sandbox state (variables, installed packages) persists across calls."""

model = ChatAnthropic(model="claude-sonnet-4-20250514")
tools = asyncio.run(get_mcp_tools())

# Build subagents list — code_runner only available when Daytona API key is set
subagents = []
if is_sandbox_available():
    subagents.append(
        {
            "name": "code_runner",
            "description": "Executes Python code in a sandboxed environment for data analysis and computation.",
            "system_prompt": CODE_RUNNER_PROMPT,
            "tools": [execute_python_code],
        }
    )

graph = create_deep_agent(
    model=model,
    tools=tools,
    system_prompt=SYSTEM_PROMPT,
    subagents=subagents or None,
    middleware=[
        ModelRetryMiddleware(max_retries=3),
        ToolCallLimitMiddleware(run_limit=50),
    ],
)
