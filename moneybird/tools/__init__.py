"""MCP tool surface, split by domain.

Importing this package registers every tool on the shared ``mcp`` instance and
the guidance layer (playbook resource + scenario prompts). All public tool
functions are re-exported here so ``from moneybird import tools`` keeps working.
"""
from __future__ import annotations

from ._registry import mcp, SERVER_INSTRUCTIONS
from ._context import get_client  # re-exported for back-compat
from .core import *  # noqa: F401,F403
from .contacts import *  # noqa: F401,F403
from .reference import *  # noqa: F401,F403
from .purchases import *  # noqa: F401,F403
from .reports import *  # noqa: F401,F403
from .sales import *  # noqa: F401,F403
from .sales_batches import *  # noqa: F401,F403
from .ledger import *  # noqa: F401,F403
from .payments import *  # noqa: F401,F403
from .bank import *  # noqa: F401,F403

# Register the guidance layer (playbook resource + scenario prompts) last, so the
# mcp instance and all tools already exist; guidance.py imports nothing from here.
from ..guidance import register_guidance

register_guidance(mcp)
