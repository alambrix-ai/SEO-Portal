"""Agent fleet.

Each agent lives in its own package named after its slug. The registry in
``app.agents.base.registry`` discovers them at import time, so adding an agent
is adding a folder.
"""
