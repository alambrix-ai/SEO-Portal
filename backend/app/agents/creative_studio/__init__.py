"""Creative Studio agent package (on demand; started by Creative Studio jobs).

Importing the package also registers the creative hooks — re-render after an
edit, regenerate with reviewer feedback, and restore a text version — which
the registry's discovery does at startup.
"""
from app.agents.creative_studio.agent import AGENT, NAME, SLUG, CreativeStudioAgent
from app.services.creative import hooks as _hooks  # noqa: F401 - registers the creative hooks

__all__ = ["AGENT", "NAME", "SLUG", "CreativeStudioAgent"]
