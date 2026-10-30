"""Specialist flows the router can hand the caller to.

Each flow is a package exposing NAME and build_agent(current_date, model) -> Agent
(mode="chat"). To add one (e.g. fibre): copy flows/roaming/ to flows/fibre/
(agent.py, prompt.py with {{COMMON}}, tools.py via call_gateway, mock_backend.py)
and add it to FLOWS below. The router picks it up automatically.
"""

from . import roaming

FLOWS = [roaming]
