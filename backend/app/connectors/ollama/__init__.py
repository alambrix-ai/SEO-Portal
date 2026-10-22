"""Ollama (self-hosted model server) connector package."""
from app.connectors.ollama.connector import CONNECTOR_CLASS, OllamaConnector

__all__ = ["CONNECTOR_CLASS", "OllamaConnector"]
