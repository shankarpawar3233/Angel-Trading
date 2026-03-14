from __future__ import annotations

import os

import uvicorn

from api.api_server import app  # noqa: F401
from config.settings import settings


if __name__ == "__main__":
    port = int(os.environ.get("PORT", settings.port))
    uvicorn.run("api.api_server:app", host="0.0.0.0", port=port, reload=False)

