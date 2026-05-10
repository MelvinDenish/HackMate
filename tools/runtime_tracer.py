"""
╔══════════════════════════════════════════════════════════════╗
║  RUNTIME TRACER — Dynamic Error Capture                      ║
║                                                              ║
║  Executes the generated app and captures real runtime errors. ║
║  Static analysis misses import errors, port conflicts, and   ║
║  missing environment variables. This catches them.           ║
║                                                              ║
║  SWE-bench leaders use runtime tracing for 2x bug detection. ║
╚══════════════════════════════════════════════════════════════╝
"""

import logging
import subprocess
import time
from pathlib import Path

logger = logging.getLogger(__name__)


def trace_runtime(workspace_src_dir: Path, timeout: int = 15) -> dict:
    """Start the app, capture stdout/stderr for `timeout` seconds, then stop.

    Returns:
        dict with keys:
        - started: bool — did the app start without immediate crash?
        - stdout: str — captured stdout
        - stderr: str — captured stderr
        - errors: list[str] — extracted error messages
        - port: int — detected port (if any)
    """
    result = {
        "started": False,
        "stdout": "",
        "stderr": "",
        "errors": [],
        "port": None,
    }

    # Determine start command
    cmd = _detect_start_command(workspace_src_dir)
    if not cmd:
        result["errors"].append("Could not detect start command")
        return result

    logger.info(f"[Runtime] Starting: {' '.join(cmd)}")

    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(workspace_src_dir),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            shell=True,
        )

        # Wait for startup
        time.sleep(min(timeout, 8))

        # Check if still running
        retcode = proc.poll()
        if retcode is not None and retcode != 0:
            # Crashed during startup
            result["stdout"] = (proc.stdout.read() or "")[-2000:]
            result["stderr"] = (proc.stderr.read() or "")[-2000:]
            result["errors"] = _extract_errors(result["stderr"] + result["stdout"])
            logger.warning(f"[Runtime] App crashed on startup (exit={retcode})")
        else:
            result["started"] = True
            # Read available output
            try:
                proc.terminate()
                out, err = proc.communicate(timeout=5)
                result["stdout"] = (out or "")[-2000:]
                result["stderr"] = (err or "")[-2000:]
            except subprocess.TimeoutExpired:
                proc.kill()
                result["stdout"] = ""
                result["stderr"] = ""

            result["errors"] = _extract_errors(result["stderr"])
            # Detect port
            import re
            port_match = re.search(r'(?:port|PORT)\s*[=:]\s*(\d{4,5})', result["stdout"])
            if port_match:
                result["port"] = int(port_match.group(1))

            logger.info(f"[Runtime] App started successfully (port={result['port']})")

    except FileNotFoundError as e:
        result["errors"].append(f"Command not found: {e}")
    except Exception as e:
        result["errors"].append(f"Runtime trace failed: {str(e)[:300]}")
    finally:
        # Ensure process is killed
        try:
            proc.kill()
        except Exception:
            pass

    return result


def format_runtime_facts(trace: dict) -> str:
    """Format runtime trace results for injection into coder/reviewer context."""
    if trace["started"] and not trace["errors"]:
        return "✅ Runtime trace: App starts successfully, no errors detected."

    lines = []
    if not trace["started"]:
        lines.append("❌ RUNTIME FAILURE: App crashed during startup")

    if trace["errors"]:
        lines.append(f"🐛 {len(trace['errors'])} runtime errors detected:")
        for err in trace["errors"][:10]:
            lines.append(f"  • {err}")

    if trace["stderr"]:
        lines.append(f"\n📋 stderr (last 500 chars):\n{trace['stderr'][-500:]}")

    return "\n".join(lines)


def _detect_start_command(src_dir: Path) -> list[str] | None:
    """Auto-detect the start command for the project."""
    pkg = src_dir / "package.json"
    if pkg.exists():
        content = pkg.read_text(encoding="utf-8")
        if '"dev"' in content:
            return ["npm", "run", "dev"]
        elif '"start"' in content:
            return ["npm", "start"]

    if (src_dir / "run.py").exists():
        return ["python", "run.py"]
    if (src_dir / "app.py").exists():
        return ["python", "app.py"]
    if (src_dir / "manage.py").exists():
        return ["python", "manage.py", "runserver"]

    # Static site — use python http.server
    if (src_dir / "index.html").exists():
        return ["python", "-m", "http.server", "8080"]

    return None


def _extract_errors(text: str) -> list[str]:
    """Extract meaningful error messages from stdout/stderr."""
    import re
    errors = []
    seen = set()

    patterns = [
        r'Error:\s*(.+)',
        r'ERROR\s*[:\-]\s*(.+)',
        r'(?:Traceback|Exception|TypeError|SyntaxError|ImportError|ModuleNotFoundError).*?:\s*(.+)',
        r'ENOENT.*?\'(.+?)\'',
        r'Cannot find module\s+\'(.+?)\'',
        r'EADDRINUSE.*?(\d{4,5})',
    ]

    for pattern in patterns:
        for match in re.finditer(pattern, text, re.IGNORECASE):
            msg = match.group(1).strip()[:200]
            if msg and msg not in seen:
                errors.append(msg)
                seen.add(msg)

    return errors[:15]
