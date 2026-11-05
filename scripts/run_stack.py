"""Arranque sencillo: solo se necesita el servidor web (incluye monitores)."""
import os
import subprocess
import sys
from pathlib import Path

root = Path(__file__).resolve().parent.parent
subprocess.run([sys.executable, '-m', 'uvicorn', 'control_plane.api:app',
                '--host', '127.0.0.1', '--port', os.getenv('DEPLOYMENT_PORT', '8000')], cwd=root)
