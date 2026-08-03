"""FastAPI application replacing the Streamlit app.py entrypoint."""

from proxy_scaler.api.app import app

__all__ = ["app"]
