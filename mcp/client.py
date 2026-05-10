"""
╔══════════════════════════════════════════════════════════════╗
║  WORKSPACE TOOL SERVER — Agent Tool Registry                  ║
║                                                              ║
║  Provides two components:                                    ║
║  1. MCPRegistry: Reads .mcp.json for external tool config    ║
║  2. MCPWorkspaceServer: In-process tool server exposing:     ║
║     read_file, list_files, get_source_tree, run_command      ║
║                                                              ║
║  NOTE: MCPWorkspaceServer is NOT a real MCP JSON-RPC server. ║
║  It's a simplified in-process tool interface used by agents.  ║
║  For full MCP compliance, use the official MCP SDK.           ║
╚══════════════════════════════════════════════════════════════╝
"""

import json
import logging
import subprocess
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class MCPRegistry:
    """Registry of available MCP servers from .mcp.json."""

    def __init__(self, config_path: Optional[Path] = None):
        self._servers: dict[str, dict] = {}
        self._config_path = config_path

        if config_path and config_path.exists():
            self._load_config(config_path)

    def _load_config(self, path: Path):
        """Load MCP server definitions from .mcp.json."""
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            self._servers = data.get("mcpServers", {})
            logger.info(
                f"[MCP] Loaded {len(self._servers)} servers: "
                f"{', '.join(self._servers.keys())}"
            )
        except Exception as e:
            logger.warning(f"[MCP] Failed to load config: {e}")

    @property
    def available_servers(self) -> list[str]:
        """List available MCP server names."""
        return list(self._servers.keys())

    def get_server_info(self, name: str) -> Optional[dict]:
        """Get server configuration by name."""
        return self._servers.get(name)

    def has_server(self, name: str) -> bool:
        """Check if a server is registered."""
        return name in self._servers

    def format_for_agent(self) -> str:
        """Format available MCP tools for agent context injection."""
        if not self._servers:
            return "No MCP servers configured."

        lines = ["## Available MCP Tools"]
        for name, config in self._servers.items():
            server_type = config.get("type", "stdio")
            if server_type == "http":
                lines.append(f"- **{name}** (HTTP): {config.get('url', 'N/A')}")
            else:
                cmd = config.get("command", "")
                args = " ".join(config.get("args", []))
                lines.append(f"- **{name}** (stdio): {cmd} {args}")

        return "\n".join(lines)


class MCPWorkspaceServer:
    """Lightweight MCP-like tool server that exposes workspace operations.

    This provides a standardized interface for agents to interact with
    the workspace — reading files, listing directories, running commands.

    Note: This is a simplified in-process version. For full MCP compliance,
    use the official MCP SDK with JSON-RPC transport.
    """

    def __init__(self, workspace_src_dir: Path):
        self.src_dir = workspace_src_dir
        self._tools = {
            "read_file": self._read_file,
            "list_files": self._list_files,
            "get_source_tree": self._get_source_tree,
            "run_command": self._run_command,
            "file_exists": self._file_exists,
        }

    @property
    def available_tools(self) -> list[str]:
        return list(self._tools.keys())

    def call_tool(self, tool_name: str, **kwargs) -> str:
        """Call a tool by name with arguments."""
        if tool_name not in self._tools:
            return f"Unknown tool: {tool_name}. Available: {self.available_tools}"
        try:
            return self._tools[tool_name](**kwargs)
        except Exception as e:
            return f"Tool error ({tool_name}): {e}"

    def _read_file(self, path: str) -> str:
        """Read a file from the workspace."""
        target = self.src_dir / path
        if not target.exists():
            return f"File not found: {path}"
        if target.stat().st_size > 100_000:
            return f"File too large: {path} ({target.stat().st_size} bytes)"
        return target.read_text(encoding="utf-8", errors="replace")

    def _list_files(self, directory: str = ".") -> str:
        """List files in a workspace directory."""
        target = self.src_dir / directory
        if not target.is_dir():
            return f"Not a directory: {directory}"

        files = []
        for p in sorted(target.iterdir()):
            if p.name.startswith("."):
                continue
            prefix = "📁" if p.is_dir() else "📄"
            files.append(f"{prefix} {p.name}")

        return "\n".join(files) if files else "(empty directory)"

    def _get_source_tree(self, max_depth: int = 3) -> str:
        """Get the full source tree as text."""
        lines = []
        self._walk_tree(self.src_dir, "", max_depth, lines)
        return "\n".join(lines[:200])  # Cap at 200 lines

    def _walk_tree(self, path: Path, prefix: str, depth: int, lines: list):
        """Recursively build source tree."""
        if depth <= 0:
            return
        skip = {".git", "node_modules", "__pycache__", ".next", ".venv", "venv"}
        try:
            entries = sorted(path.iterdir(), key=lambda p: (not p.is_dir(), p.name))
            for entry in entries:
                if entry.name in skip:
                    continue
                lines.append(f"{prefix}{entry.name}")
                if entry.is_dir():
                    self._walk_tree(entry, prefix + "  ", depth - 1, lines)
        except PermissionError:
            pass

    def _run_command(self, command: str) -> str:
        """Run a shell command in the workspace directory.
        Limited to safe read-only commands."""
        # Safety: only allow read-only commands
        safe_prefixes = ["ls", "dir", "cat", "type", "find", "grep", "npm list", "pip list"]
        cmd_lower = command.lower().strip()

        if not any(cmd_lower.startswith(p) for p in safe_prefixes):
            return f"Blocked: only read-only commands allowed. Got: {command}"

        try:
            result = subprocess.run(
                command, shell=True, cwd=str(self.src_dir),
                capture_output=True, text=True, timeout=10,
            )
            output = (result.stdout + result.stderr)[:3000]
            return output if output else "(no output)"
        except subprocess.TimeoutExpired:
            return "Command timed out (10s limit)"
        except Exception as e:
            return f"Command failed: {e}"

    def _file_exists(self, path: str) -> str:
        """Check if a file exists."""
        target = self.src_dir / path
        return "true" if target.exists() else "false"


def create_mcp_registry(project_root: Path) -> MCPRegistry:
    """Create an MCP registry from the project's .mcp.json."""
    config_path = project_root / ".mcp.json"
    return MCPRegistry(config_path)


def create_workspace_server(workspace_src_dir: Path) -> MCPWorkspaceServer:
    """Create a workspace MCP server for agent tool use."""
    return MCPWorkspaceServer(workspace_src_dir)
