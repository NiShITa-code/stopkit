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
        from .langgraph import LangGraphAdapter
        return LangGraphAdapter()
    if name == 'temporal':
        from .temporal import TemporalAdapter
        return TemporalAdapter()
    if name == 'claude-code':
        from .claude_code import ClaudeCodeAdapter
        return ClaudeCodeAdapter()
    if name == 'openclaw':
        from .openclaw import OpenClawAdapter
        return OpenClawAdapter()
    raise KeyError(f'unknown adapter {name!r}; known: {", ".join(NAMES)}')


def resolve(target: str):
    """The adapter for `target`: an agent's config file (a path ending in .toml), or a ready-made binding's name.
    Raises KitError, saying what is known, for anything else."""
    from ..system import KitError
    if target.endswith('.toml'):
        from ..agents.agent_config import load
        from ..agents.configured import ConfiguredAdapter
        return ConfiguredAdapter(load(target))
    try:
        return get(target)
    except KeyError:
        raise KitError(f'{target!r} is neither a config file (FILE.toml) nor a ready-made binding '
                       f'({", ".join(NAMES)})') from None
