"""
╔══════════════════════════════════════════════════════════════╗
║  WORKSPACE MANAGER — Shared Intelligence Workspace           ║
║                                                              ║
║  Implements the Model Context Protocol (MCP) pattern from    ║
║  the research paper. Agents communicate through FILE PATHS,  ║
║  not by stuffing content into prompts.                       ║
║                                                              ║
║  Directory Layout:                                           ║
║  workspace/                                                  ║
║  ├── briefs/     ← Phase 1: Research dossiers                ║
║  ├── specs/      ← Phase 2: PRD documents                    ║
║  ├── tasks/      ← Phase 2: JSON task queues                 ║
║  ├── src/        ← Phase 3: Generated source code            ║
║  └── output/     ← Phase 4: Pitch decks & deliverables       ║
╚══════════════════════════════════════════════════════════════╝
"""

import json
import logging
from pathlib import Path
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


class WorkspaceManager:
    """
    Manages the shared file system that all agents read from
    and write to. Implements 'Delegation by Reference' — agents
    receive file paths instead of raw content, preserving their
    context windows for actual reasoning.
    """

    def __init__(self, root: Path):
        self.root = Path(root)
        self._src_tree_cache: str = ""
        self._tree_dirty: bool = True
        self._ensure_structure()

    def _ensure_structure(self) -> None:
        """Create all workspace subdirectories."""
        dirs = ["briefs", "specs", "tasks", "src", "output", "logs"]
        for d in dirs:
            (self.root / d).mkdir(parents=True, exist_ok=True)
        logger.info(f"Workspace initialized at: {self.root.resolve()}")

    # ── Directory Properties ──────────────────────────────────

    @property
    def briefs_dir(self) -> Path:
        return self.root / "briefs"

    @property
    def specs_dir(self) -> Path:
        return self.root / "specs"

    @property
    def tasks_dir(self) -> Path:
        return self.root / "tasks"

    @property
    def src_dir(self) -> Path:
        return self.root / "src"

    @property
    def output_dir(self) -> Path:
        return self.root / "output"

    @property
    def logs_dir(self) -> Path:
        return self.root / "logs"

    # ── File Operations ───────────────────────────────────────

    def write_file(self, subdir: str, filename: str, content: str) -> Path:
        """
        Write content to a file in the workspace.

        Args:
            subdir: Subdirectory name (briefs, specs, tasks, src, output)
            filename: File name to create
            content: File content to write

        Returns:
            Absolute path to the written file
        """
        dir_path = self.root / subdir
        dir_path.mkdir(parents=True, exist_ok=True)
        file_path = dir_path / filename
        file_path.write_text(content, encoding="utf-8")
        logger.info(f"[Workspace] Wrote: {file_path.relative_to(self.root)}")
        return file_path

    def write_json(self, subdir: str, filename: str, data: dict | list) -> Path:
        """Write JSON data to a file in the workspace."""
        content = json.dumps(data, indent=2, ensure_ascii=False)
        return self.write_file(subdir, filename, content)

    def read_file(self, file_path: str | Path) -> str:
        """
        Read content from a workspace file.

        Args:
            file_path: Absolute or relative path to the file

        Returns:
            File content as string
        """
        path = Path(file_path)
        if not path.is_absolute():
            path = self.root / path
        if not path.exists():
            raise FileNotFoundError(f"Workspace file not found: {path}")
        content = path.read_text(encoding="utf-8")
        logger.info(f"[Workspace] Read: {path.relative_to(self.root)}")
        return content

    def read_json(self, file_path: str | Path) -> dict | list:
        """Read and parse a JSON file from the workspace."""
        content = self.read_file(file_path)
        return json.loads(content)

    def write_source_file(self, relative_path: str, content: str) -> Path:
        """
        Write a source code file, creating subdirectories as needed.

        Args:
            relative_path: Path relative to src/ (e.g. "backend/server.js")
            content: Source code content

        Returns:
            Absolute path to the written file
        """
        file_path = self.src_dir / relative_path
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content, encoding="utf-8")
        self._tree_dirty = True  # Invalidate cached tree
        logger.info(f"[Workspace] Source: {relative_path}")
        return file_path

    def list_files(self, subdir: str, pattern: str = "*") -> list[Path]:
        """List all files matching pattern in a subdirectory."""
        dir_path = self.root / subdir
        if not dir_path.exists():
            return []
        return sorted(dir_path.rglob(pattern))

    def get_src_tree(self) -> str:
        """
        Generate a text representation of the src/ directory tree.
        Shows proper directory hierarchy with tree characters.
        Uses incremental caching — only rebuilds when files change.
        """
        # Return cache if still valid
        if not self._tree_dirty and self._src_tree_cache:
            return self._src_tree_cache

        src = self.src_dir
        if not src.exists():
            return "(empty)"

        # Collect all paths
        all_paths = sorted(
            p for p in src.rglob("*")
            if not any(part.startswith(".") or part in ("node_modules", "__pycache__")
                       for part in p.relative_to(src).parts)
        )

        if not all_paths:
            return "(empty)"

        lines = []
        seen_dirs: set[str] = set()

        for path in all_paths:
            rel = path.relative_to(src)
            parts = rel.parts

            # Render parent directories that haven't been shown yet
            for depth in range(len(parts) - 1):
                dir_key = "/".join(parts[:depth + 1])
                if dir_key not in seen_dirs:
                    seen_dirs.add(dir_key)
                    indent = "  " * depth
                    lines.append(f"{indent}📁 {parts[depth]}/")

            # Render the file (or leaf directory)
            if path.is_file():
                indent = "  " * (len(parts) - 1)
                size = path.stat().st_size
                lines.append(f"{indent}├── {parts[-1]} ({size:,}B)")
            elif path.is_dir():
                dir_key = "/".join(parts)
                if dir_key not in seen_dirs:
                    seen_dirs.add(dir_key)
                    indent = "  " * (len(parts) - 1)
                    lines.append(f"{indent}📁 {parts[-1]}/")

        result = "\n".join(lines) if lines else "(empty)"
        # Cache the result
        self._src_tree_cache = result
        self._tree_dirty = False
        return result

    def log_event(self, phase: str, event: str, details: str = "") -> None:
        """Log a pipeline event to the workspace logs (both flat text and JSONL)."""
        timestamp = datetime.now(timezone.utc).isoformat()

        # Flat text log (human readable)
        log_line = f"[{timestamp}] [{phase.upper()}] {event}"
        if details:
            log_line += f"\n  {details}"
        log_line += "\n"

        log_file = self.logs_dir / "pipeline.log"
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(log_line)

        # JSONL structured log (machine parseable for dashboards/analytics)
        jsonl_record = {
            "ts": timestamp,
            "phase": phase,
            "event": event,
            "details": details,
        }
        jsonl_file = self.logs_dir / "pipeline_events.jsonl"
        with open(jsonl_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(jsonl_record, ensure_ascii=False) + "\n")

    def get_context_for_agent(self, agent_role: str) -> dict[str, str]:
        """
        Build a minimal context bundle for an agent, containing
        only the file paths it needs. This implements the
        'Summary Handoff' pattern — agents get paths, not content.

        Args:
            agent_role: The agent's role (research, architect, coder, etc.)

        Returns:
            Dict of context_key -> file_path
        """
        context: dict[str, str] = {}

        # Each agent gets exactly the files it needs
        if agent_role == "research":
            # Research agent needs: nothing (starts from scratch)
            pass

        elif agent_role == "architect":
            # Architect needs: research dossier
            dossiers = self.list_files("briefs", "*.md")
            if dossiers:
                context["dossier"] = str(dossiers[-1])

        elif agent_role in ("planner", "planning"):
            # Planner needs: PRD
            prds = self.list_files("specs", "prd_*.md")
            if prds:
                context["prd"] = str(prds[-1])

        elif agent_role == "coder":
            # Coder needs: PRD + task queue + existing src tree
            prds = self.list_files("specs", "prd_*.md")
            tasks = self.list_files("tasks", "*.json")
            if prds:
                context["prd"] = str(prds[-1])
            if tasks:
                context["task_queue"] = str(tasks[-1])
            context["src_tree"] = self.get_src_tree()

        elif agent_role == "reviewer":
            # Reviewer needs: PRD + src directory
            prds = self.list_files("specs", "prd_*.md")
            if prds:
                context["prd"] = str(prds[-1])
            context["src_dir"] = str(self.src_dir)
            context["src_tree"] = self.get_src_tree()

        elif agent_role == "deployer":
            context["src_dir"] = str(self.src_dir)

        elif agent_role == "pitch":
            # Pitch agent needs: dossier + PRD
            dossiers = self.list_files("briefs", "*.md")
            prds = self.list_files("specs", "prd_*.md")
            if dossiers:
                context["dossier"] = str(dossiers[-1])
            if prds:
                context["prd"] = str(prds[-1])

        elif agent_role == "presentation":
            # Presentation agent needs: pitch content
            pitches = self.list_files("output", "pitch_*.json")
            if pitches:
                context["pitch_content"] = str(pitches[-1])

        return context
