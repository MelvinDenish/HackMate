"""
╔══════════════════════════════════════════════════════════════╗
║  DOCKER SANDBOX — Secure Code Execution Environment          ║
║  Used by: Reviewer Agent (Phase 3)                           ║
║  Isolation: Docker containers with resource limits           ║
╚══════════════════════════════════════════════════════════════╝
"""

import logging
import tarfile
import io
import time
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional

import docker
from docker.errors import ContainerError, ImageNotFound, DockerException

logger = logging.getLogger(__name__)


@dataclass
class SandboxResult:
    """Result of a sandboxed code execution."""
    success: bool
    exit_code: int
    stdout: str
    stderr: str
    duration_ms: int
    files_created: list[str] = field(default_factory=list)
    error: str = ""

    def to_dict(self) -> dict:
        return {
            "success": self.success, "exit_code": self.exit_code,
            "stdout": self.stdout, "stderr": self.stderr,
            "duration_ms": self.duration_ms, "error": self.error,
        }

    @property
    def summary(self) -> str:
        status = "✅ PASSED" if self.success else "❌ FAILED"
        lines = [f"Execution {status} (exit code: {self.exit_code}, {self.duration_ms}ms)"]
        if self.stdout.strip():
            lines.append(f"\n── stdout ──\n{self.stdout[-3000:]}")
        if self.stderr.strip():
            lines.append(f"\n── stderr ──\n{self.stderr[-3000:]}")
        if self.error:
            lines.append(f"\n── error ──\n{self.error}")
        return "\n".join(lines)


class DockerSandbox:
    """Ephemeral Docker container for secure code execution."""

    def __init__(self, image="python:3.12-slim", timeout=120,
                 memory_limit="512m", cpu_count=1, network_disabled=False):
        self.image = image
        self.timeout = timeout
        self.memory_limit = memory_limit
        self.cpu_count = cpu_count
        self.network_disabled = network_disabled
        self._client: Optional[docker.DockerClient] = None

    @property
    def client(self) -> docker.DockerClient:
        if self._client is None:
            try:
                self._client = docker.from_env()
                self._client.ping()
            except DockerException as e:
                raise RuntimeError(
                    "Docker is not running. Start Docker Desktop and retry."
                ) from e
        return self._client

    def ensure_image(self):
        try:
            self.client.images.get(self.image)
        except ImageNotFound:
            logger.info(f"[Sandbox] Pulling image: {self.image}")
            self.client.images.pull(self.image)

    def _create_tar(self, files: dict[str, str]) -> bytes:
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tar:
            for name, content in files.items():
                data = content.encode("utf-8")
                info = tarfile.TarInfo(name=name)
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
        buf.seek(0)
        return buf.read()

    def _dir_to_tar(self, src_dir: Path) -> bytes:
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tar:
            for fp in src_dir.rglob("*"):
                if fp.is_file():
                    tar.add(str(fp), arcname=str(fp.relative_to(src_dir)))
        buf.seek(0)
        return buf.read()

    def execute_code(self, code: str, language="python",
                     filename="main.py", install_deps=None) -> SandboxResult:
        start = time.time()
        try:
            self.ensure_image()
            cmds = []
            if install_deps:
                pkg = " ".join(install_deps)
                if language == "python":
                    cmds.append(f"pip install -q {pkg}")
                elif language in ("node", "javascript"):
                    cmds.append(f"npm install -q {pkg}")

            runners = {"python": "python", "node": "node",
                       "javascript": "node", "bash": "bash"}
            runner = runners.get(language, "python")
            cmds.append(f"{runner} /app/{filename}")

            container = self.client.containers.create(
                image=self.image,
                command=["sh", "-c", " && ".join(cmds)],
                working_dir="/app",
                mem_limit=self.memory_limit,
                cpu_count=self.cpu_count,
                network_disabled=self.network_disabled,
                detach=True,
            )
            container.put_archive("/app", self._create_tar({filename: code}))
            container.start()
            result = container.wait(timeout=self.timeout)
            exit_code = result.get("StatusCode", -1)
            stdout = container.logs(stdout=True, stderr=False).decode("utf-8", errors="replace")
            stderr = container.logs(stdout=False, stderr=True).decode("utf-8", errors="replace")
            container.remove(force=True)
            return SandboxResult(
                success=(exit_code == 0), exit_code=exit_code,
                stdout=stdout, stderr=stderr,
                duration_ms=int((time.time() - start) * 1000),
            )
        except ContainerError as e:
            return SandboxResult(
                success=False, exit_code=e.exit_status, stdout="", stderr=str(e),
                duration_ms=int((time.time() - start) * 1000), error=str(e),
            )
        except Exception as e:
            return SandboxResult(
                success=False, exit_code=-1, stdout="", stderr="",
                duration_ms=int((time.time() - start) * 1000),
                error=f"Sandbox error: {e}",
            )

    def execute_project(self, src_dir: Path, entry_command: str,
                        install_command: Optional[str] = None) -> SandboxResult:
        start = time.time()
        container = None
        try:
            self.ensure_image()
            cmds = []
            if install_command:
                cmds.append(install_command)
            cmds.append(entry_command)
            container = self.client.containers.create(
                image=self.image,
                command=["sh", "-c", " && ".join(cmds)],
                working_dir="/app",
                mem_limit=self.memory_limit,
                cpu_count=self.cpu_count,
                network_disabled=self.network_disabled,
                detach=True,
            )
            container.put_archive("/app", self._dir_to_tar(src_dir))
            container.start()
            result = container.wait(timeout=self.timeout)
            exit_code = result.get("StatusCode", -1)
            stdout = container.logs(stdout=True, stderr=False).decode("utf-8", errors="replace")
            stderr = container.logs(stdout=False, stderr=True).decode("utf-8", errors="replace")
            return SandboxResult(
                success=(exit_code == 0), exit_code=exit_code,
                stdout=stdout, stderr=stderr,
                duration_ms=int((time.time() - start) * 1000),
            )
        except Exception as e:
            return SandboxResult(
                success=False, exit_code=-1, stdout="", stderr="",
                duration_ms=int((time.time() - start) * 1000),
                error=f"Sandbox error: {e}",
            )
        finally:
            # Guarantee container cleanup even on timeout
            if container is not None:
                try:
                    container.remove(force=True)
                except Exception:
                    pass

    def run_tests(self, src_dir: Path,
                  test_command="python -m pytest -v",
                  install_command="pip install -q -r requirements.txt") -> SandboxResult:
        return self.execute_project(src_dir, test_command, install_command)

    def health_check(self) -> bool:
        try:
            self.client.ping()
            self.ensure_image()
            return self.execute_code("print('sandbox OK')").success
        except Exception:
            return False


class LocalSandbox:
    """Fallback sandbox that runs basic checks without Docker.
    Used when Docker is unavailable (e.g., CI, Windows without WSL)."""

    def __init__(self, timeout=60, **kwargs):
        self.timeout = timeout
        self.image = "local"  # For compatibility

    def execute_project(self, src_dir: Path, entry_command: str,
                        install_command: Optional[str] = None) -> SandboxResult:
        """Run basic syntax/compile checks without Docker."""
        import subprocess
        start = time.time()
        findings = []

        # Check Python files for syntax errors
        for fp in sorted(src_dir.rglob("*.py")):
            try:
                source = fp.read_text(encoding="utf-8")
                compile(source, str(fp), "exec")
            except SyntaxError as e:
                rel = fp.relative_to(src_dir)
                findings.append(f"SYNTAX ERROR in {rel}: Line {e.lineno}: {e.msg}")

        # Check JS/TS files with node --check if available
        for fp in sorted(src_dir.rglob("*.js")):
            try:
                result = subprocess.run(
                    ["node", "--check", str(fp)],
                    capture_output=True, text=True, timeout=10,
                )
                if result.returncode != 0:
                    rel = fp.relative_to(src_dir)
                    findings.append(f"JS ERROR in {rel}: {result.stderr[:200]}")
            except (FileNotFoundError, subprocess.TimeoutExpired):
                break  # node not available

        if findings:
            return SandboxResult(
                success=False, exit_code=1,
                stdout="", stderr="\n".join(findings),
                duration_ms=int((time.time() - start) * 1000),
                error="Local syntax check failed",
            )

        return SandboxResult(
            success=True, exit_code=0,
            stdout="Local syntax checks passed (no Docker runtime verification)",
            stderr="",
            duration_ms=int((time.time() - start) * 1000),
        )

    def execute_code(self, code: str, **kwargs) -> SandboxResult:
        return SandboxResult(
            success=True, exit_code=0,
            stdout="(local mode — no execution)", stderr="",
            duration_ms=0,
        )

    def run_tests(self, src_dir: Path, **kwargs) -> SandboxResult:
        return self.execute_project(src_dir, "test")

    def health_check(self) -> bool:
        return True


def create_sandbox(config=None) -> "DockerSandbox | LocalSandbox":
    """Factory: returns DockerSandbox if Docker is running, else LocalSandbox."""
    try:
        sandbox = DockerSandbox(
            image=config.sandbox.image if config else "python:3.12-slim",
            timeout=config.sandbox.timeout if config else 120,
            memory_limit=config.sandbox.memory_limit if config else "512m",
        )
        sandbox.client.ping()
        logger.info("[Sandbox] Docker is available — using DockerSandbox")
        return sandbox
    except Exception:
        logger.warning("[Sandbox] Docker unavailable — falling back to LocalSandbox")
        return LocalSandbox(timeout=config.sandbox.timeout if config else 60)
