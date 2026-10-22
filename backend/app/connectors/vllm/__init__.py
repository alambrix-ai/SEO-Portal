"""vLLM (self-hosted model server) connector package."""
from app.connectors.vllm.connector import CONNECTOR_CLASS, VllmConnector

__all__ = ["CONNECTOR_CLASS", "VllmConnector"]
