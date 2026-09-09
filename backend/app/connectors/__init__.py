"""Connector catalogue.

Each integration lives in its own package named after its slug. The
registry in ``app.connectors.base.registry`` discovers them at import
time, so adding an integration is adding a folder.
"""
