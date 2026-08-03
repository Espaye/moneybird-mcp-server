"""Entry point inside the .mcpb bundle.

The build script (scripts/build_mcpb.py) ships this file as server/main.py next
to server/lib/, which holds the moneybird_mcp package and all its dependencies
pip-installed for one platform. Claude Desktop launches it with the system
Python; everything it imports must come from that lib directory.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))

from moneybird_mcp.server import main

main(["--transport", "stdio"])
