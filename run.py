"""Start the platform:  python run.py  ->  http://localhost:8000  (API docs at /docs)"""
import logging
import os

import uvicorn

from onboarding.api import create_app

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
app = create_app()

if __name__ == "__main__":
    uvicorn.run(app, host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORT", "8000")), log_level="warning")
