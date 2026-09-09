"""Email / SMTP connector package."""
from app.connectors.smtp_email.connector import CONNECTOR_CLASS, SmtpEmailConnector

__all__ = ["CONNECTOR_CLASS", "SmtpEmailConnector"]
