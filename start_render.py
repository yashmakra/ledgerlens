"""Render entrypoint: keep the API and extraction worker in one web service."""
import os
import subprocess
import sys

worker = subprocess.Popen([sys.executable, "-m", "app.worker"])
api = subprocess.Popen([
    sys.executable, "-m", "uvicorn", "app.main:app",
    "--host", "0.0.0.0", "--port", os.getenv("PORT", "8000"),
])
try:
    raise SystemExit(api.wait())
finally:
    if api.poll() is None:
        api.terminate()
    if worker.poll() is None:
        worker.terminate()
        try:
            worker.wait(timeout=10)
        except subprocess.TimeoutExpired:
            worker.kill()
