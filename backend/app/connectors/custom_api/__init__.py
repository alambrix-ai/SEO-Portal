"""Custom API / Webhook connector package."""
from app.connectors.custom_api.connector import CONNECTOR_CLASS, CustomApiConnector

__all__ = ["CONNECTOR_CLASS", "CustomApiConnector"]
