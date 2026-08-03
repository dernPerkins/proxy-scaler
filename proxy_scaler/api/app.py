"""FastAPI application replacing the Streamlit app.py entrypoint."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from proxy_scaler.api.routers import cards, generation, images, misc, pdf, projects

app = FastAPI(title="proxy-scaler API")

# The Tauri webview (tauri://localhost in a packaged build,
# http://localhost:<vite-port> in `cargo tauri dev`) and, in Remote mode,
# an arbitrary LAN/Tailscale host all need to reach this — Streamlit's
# same-origin browser-navigation model never needed CORS handling at all.
# Permissive because this is a locally-run/LAN app, not a public service.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(misc.router)
app.include_router(projects.router)
app.include_router(cards.router)
app.include_router(generation.router)
app.include_router(pdf.router)
app.include_router(images.router)
