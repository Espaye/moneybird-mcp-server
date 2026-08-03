"""MCP tool surface, split by domain.

Importing this package registers every tool on the shared ``mcp`` instance and
the guidance layer (playbook resource + scenario prompts). All public tool
functions are re-exported here so ``from moneybird_mcp import tools`` keeps working.
"""
from __future__ import annotations

# Register the guidance layer (playbook resource + scenario prompts) last, so the
# mcp instance and all tools already exist; guidance.py imports nothing from here.
from ..guidance import register_guidance
from ._context import get_client as get_client  # explicit back-compat re-export
from ._registry import SERVER_INSTRUCTIONS as SERVER_INSTRUCTIONS
from ._registry import mcp
from .approvals import *  # noqa: F401,F403
from .bank import *  # noqa: F401,F403
from .contacts import *  # noqa: F401,F403
from .core import *  # noqa: F401,F403
from .ledger import *  # noqa: F401,F403
from .payments import *  # noqa: F401,F403
from .purchases import *  # noqa: F401,F403
from .reference import *  # noqa: F401,F403
from .reports import *  # noqa: F401,F403
from .sales import *  # noqa: F401,F403
from .sales_batches import *  # noqa: F401,F403
from .workflows import *  # noqa: F401,F403

register_guidance(mcp)

# Direct Python imports retain the complete catalogue by default. The runnable
# server sets MONEYBIRD_TOOL_DISCOVERY before importing this package and defaults
# to the compact search mode.
from ..tool_discovery import configure_tool_discovery

configure_tool_discovery(mcp)
