"""Adapters (bindings): one per kind of agent. Each starts the agent as the test account and uses its stops."""
from __future__ import annotations

NAMES = ('pretend', 'mcp', 'n8n', 'langgraph', 'temporal', 'claude-code', 'openclaw')


def get(name: str):
    """The adapter called `name`. Raises KeyError for an unknown name."""
    if name == 'pretend':
        from .pretend import PretendAdapter
        return PretendAdapter()
    if name == 'mcp':
        from .mcp import MCPAdapter
        return MCPAdapter()
    if name == 'n8n':
        from .n8n import N8nAdapter
        return N8nAdapter()
    if name == 'langgraph':
        from .langgraph_dev import LangGraphAdapter
        return LangGraphAdapter()
    if name == 'temporal':
        from .temporal_dev import TemporalAdapter
        return TemporalAdapter()
    if name == 'claude-code':
        from .claude_code import ClaudeCodeAdapter
        return ClaudeCodeAdapter()
    if name == 'openclaw':
        from .openclaw import OpenClawAdapter
        return OpenClawAdapter()
    raise KeyError(f'unknown adapter {name!r}; known: {", ".join(NAMES)}')
