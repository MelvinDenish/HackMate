"""
╔══════════════════════════════════════════════════════════════╗
║  DEV SERVER — Auto-Start Local Preview Server                ║
║                                                              ║
║  Inspired by: Bolt.new (instant preview)                     ║
║                                                              ║
║  After the first code layer completes, automatically starts  ║
║  a local dev server so the generated app can be previewed    ║
║  immediately — without waiting for full deployment.          ║
║                                                              ║
║  Supported stacks:                                           ║
║  - Node.js (npm run dev / npm start)                         ║
║  - Python (flask / uvicorn / python manage.py runserver)     ║
║  - Static HTML (python -m http.server)                       ║
╚══════════════════════════════════════════════════════════════╝
"""

import logging
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Track running dev server process
_dev_server_process: Optional[subprocess.Popen] = None


def detect_project_type(src_dir: Path) -> str:
    """Detect the project type from source files.

    Returns:
        Project type: 'node', 'python_flask', 'python_django',
                      'python_fastapi', 'static', or 'unknown'
    """
    if (src_dir / "package.json").exists():
        return "node"
    if (src_dir / "manage.py").exists():
        return "python_django"

    # Check Python files for framework imports
    for py_file in src_dir.rglob("*.py"):
        try:
            content = py_file.read_text(encoding="utf-8")
            if "from fastapi" in content or "import fastapi" in content:
                return "python_fastapi"
            if "from flask" in content or "import flask" in content:
                return "python_flask"
        except Exception:
            continue

    # Check for HTML files (static site)
    if list(src_dir.glob("*.html")) or list(src_dir.glob("**/*.html")):
        return "static"

    return "unknown"


def start_dev_server(src_dir: Path) -> Optional[str]:
    """Start a local development server for live preview.

    Args:
        src_dir: Path to the source code directory

    Returns:
        Local URL (e.g. http://localhost:3000) or None if unable to start
    """
    global _dev_server_process

    # Don't start if already running
    if _dev_server_process and _dev_server_process.poll() is None:
        logger.info("[DevServer] Already running, skipping")
        return None

    project_type = detect_project_type(src_dir)
    logger.info(f"[DevServer] Detected project type: {project_type}")

    try:
        if project_type == "node":
            # Check if node_modules exist, install if not
            if not (src_dir / "node_modules").exists():
                logger.info("[DevServer] Installing dependencies...")
                subprocess.run(
                    ["npm", "install"],
                    cwd=str(src_dir),
                    capture_output=True,
                    timeout=120,
                )

            # Prefer dev script, fallback to start
            pkg_json = src_dir / "package.json"
            import json
            try:
                pkg = json.loads(pkg_json.read_text(encoding="utf-8"))
                scripts = pkg.get("scripts", {})
                if "dev" in scripts:
                    cmd = ["npm", "run", "dev"]
                elif "start" in scripts:
                    cmd = ["npm", "start"]
                else:
                    cmd = ["npx", "serve", "-s", ".", "-p", "3000"]
            except Exception:
                cmd = ["npm", "start"]

            _dev_server_process = subprocess.Popen(
                cmd, cwd=str(src_dir),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            time.sleep(2)
            url = "http://localhost:3000"

        elif project_type == "python_django":
            _dev_server_process = subprocess.Popen(
                [sys.executable, "manage.py", "runserver", "0.0.0.0:8000"],
                cwd=str(src_dir),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            time.sleep(2)
            url = "http://localhost:8000"

        elif project_type == "python_fastapi":
            # Find the main file
            main_file = "main.py"
            for candidate in ["main.py", "app.py", "server.py"]:
                if (src_dir / candidate).exists():
                    main_file = candidate
                    break

            module = main_file.replace(".py", "")
            _dev_server_process = subprocess.Popen(
                [sys.executable, "-m", "uvicorn", f"{module}:app",
                 "--host", "0.0.0.0", "--port", "8000", "--reload"],
                cwd=str(src_dir),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            time.sleep(2)
            url = "http://localhost:8000"

        elif project_type == "python_flask":
            main_file = "app.py"
            for candidate in ["app.py", "main.py", "server.py"]:
                if (src_dir / candidate).exists():
                    main_file = candidate
                    break

            _dev_server_process = subprocess.Popen(
                [sys.executable, main_file],
                cwd=str(src_dir),
                env={**dict(__import__("os").environ), "FLASK_ENV": "development"},
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            time.sleep(2)
            url = "http://localhost:5000"

        elif project_type == "static":
            _dev_server_process = subprocess.Popen(
                [sys.executable, "-m", "http.server", "3000"],
                cwd=str(src_dir),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            time.sleep(1)
            url = "http://localhost:3000"

        else:
            logger.warning("[DevServer] Unknown project type — skipping dev server")
            return None

        # Verify the server started
        if _dev_server_process.poll() is not None:
            logger.warning("[DevServer] Server process exited immediately")
            return None

        logger.info(f"[DevServer] Started at {url} (pid: {_dev_server_process.pid})")
        return url

    except Exception as e:
        logger.warning(f"[DevServer] Failed to start: {e}")
        return None


def stop_dev_server() -> None:
    """Stop the running development server."""
    global _dev_server_process

    if _dev_server_process and _dev_server_process.poll() is None:
        logger.info(f"[DevServer] Stopping server (pid: {_dev_server_process.pid})")
        _dev_server_process.terminate()
        try:
            _dev_server_process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _dev_server_process.kill()
        _dev_server_process = None
    else:
        logger.debug("[DevServer] No running server to stop")


def is_running() -> bool:
    """Check if the dev server is currently running."""
    return _dev_server_process is not None and _dev_server_process.poll() is None
