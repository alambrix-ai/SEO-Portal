"""Webhooks / Zapier connector package."""
from app.connectors.zapier_webhooks.connector import CONNECTOR_CLASS, ZapierWebhooksConnector

__all__ = ["CONNECTOR_CLASS", "ZapierWebhooksConnector"]
