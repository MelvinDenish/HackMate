"""
╔══════════════════════════════════════════════════════════════╗
║  DE-SLOPPIFY AGENT — Post-Coding Cleanup Pass                ║
║                                                              ║
║  🟣 LLM: Anthropic Claude Haiku 3.5 (fast + cheap)          ║
║  Why: Cleanup is pattern-matching, not deep reasoning.       ║
║       Haiku is 4x cheaper and fast enough for this task.     ║
║                                                              ║
║  Pattern: De-Sloppify (from ECC autonomous-loops skill)      ║
║  "Two focused agents outperform one constrained agent."      ║
║                                                              ║
║  Runs AFTER the Coder and BEFORE the Reviewer.               ║
║  Removes code slop without reducing quality.                 ║
╚══════════════════════════════════════════════════════════════╝
"""

import logging
import re
from pathlib import Path

from langchain_core.messages import SystemMessage, HumanMessage

from agents.llm_factory import create_llm, invoke_with_retry
from config import PipelineConfig
from pipeline.cost_tracker import CostTracker
from workspace.manager import WorkspaceManager

logger = logging.getLogger(__name__)

DESLOPIFY_SYSTEM_PROMPT = """You are a Code Polish Agent in an autonomous hackathon pipeline. Your job is to remove slop from generated code AND add small quality touches that make the app feel professional — WITHOUT changing core functionality.

## What to REMOVE
1. **Tests that test the language/framework, not business logic**
   - Tests like `it('should be a function', () => expect(typeof fn).toBe('function'))`
   - Tests that verify TypeScript generics work
   - Tests that verify framework lifecycle methods exist
2. **Overly defensive runtime checks the type system already handles**
   - `if (typeof x !== 'string') throw` when x is typed as string
   - Null checks after non-nullable assignments
3. **Debug leftovers**
   - `console.log()`, `print()`, `System.out.println()` debug statements
   - Commented-out code blocks (not explanatory comments)
   - `TODO` and `FIXME` comments added by the generator
4. **Unused imports and variables**
5. **Redundant error re-throws** that add no context

## What to ADD (Quick Polish Wins)
1. **HTML files**: Add `<meta name="viewport" content="width=device-width, initial-scale=1.0">` if missing
2. **HTML files**: Add `<title>` tag with app name if missing or generic
3. **CSS files**: Add `*, *::before, *::after { box-sizing: border-box; }` if missing
4. **CSS files**: Add `cursor: pointer` to all button/a[role=button] rules if missing
5. **JS/TS files**: Wrap `console.log` in `if (process.env.NODE_ENV !== 'production')` guard
6. **Python files**: Add `if __name__ == "__main__":` guard to entry point files if missing
7. **All files**: Ensure consistent line endings and trailing newline

## What to KEEP (DO NOT REMOVE)
- All actual business logic
- Meaningful error handling
- User-facing log messages
- Explanatory comments
- Type annotations
- Real unit/integration tests

## Output Format
For EACH file you modify, output:

```file:path/to/filename.ext
[complete cleaned file contents]
```

If a file needs NO changes, do NOT include it in output.
Only include files that were actually modified."""


def run_deslopify(
    workspace: WorkspaceManager,
    config: PipelineConfig,
    cost_tracker: CostTracker = None,
) -> dict:
    """
    Run the De-Sloppify cleanup pass on generated source code.

    1. Read all source files
    2. Send to Haiku for cleanup
    3. Overwrite files with cleaned versions

    Returns:
        Dict with files_cleaned count and details
    """
    logger.info("[De-Sloppify] Starting cleanup pass")

    src_dir = workspace.src_dir
    if not src_dir.exists() or not list(src_dir.iterdir()):
        logger.info("[De-Sloppify] No source files to clean")
        return {"files_cleaned": 0, "skipped": True}

    # Read all source files
    source_files = _read_all_sources(src_dir)
    if not source_files:
        return {"files_cleaned": 0, "skipped": True}

    spec = config.get_model("deslopify")
    llm = create_llm(spec, config.keys)

    # Batch files into groups of 5 for better cleanup quality
    file_items = list(source_files.items())
    batch_size = 5
    total_cleaned = 0

    for batch_start in range(0, len(file_items), batch_size):
        batch = file_items[batch_start:batch_start + batch_size]
        batch_num = (batch_start // batch_size) + 1
        total_batches = (len(file_items) + batch_size - 1) // batch_size

        logger.info(
            f"[De-Sloppify] Batch {batch_num}/{total_batches}: "
            f"{len(batch)} files"
        )

        files_context = ""
        for rel_path, content in batch:
            files_context += f"\n### {rel_path}\n```\n{content}\n```\n\n"

        prompt = (
            f"## Source Code to Clean\n"
            f"Review the following {len(batch)} files and apply cleanup rules.\n"
            f"{files_context}"
        )

        messages = [
            SystemMessage(content=DESLOPIFY_SYSTEM_PROMPT),
            HumanMessage(content=prompt),
        ]

        response = invoke_with_retry(
            llm, messages,
            spec=spec,
            agent_name="deslopify",
            phase="coding",
            cost_tracker=cost_tracker,
        )

        # Parse cleaned files and overwrite
        cleaned_count = _apply_cleaned_files(response.content, workspace)
        total_cleaned += cleaned_count

    workspace.log_event(
        "deslopify",
        f"Cleanup complete: {total_cleaned} files cleaned",
        f"Total source files: {len(source_files)} ({total_batches} batches)",
    )

    logger.info(f"[De-Sloppify] Cleaned {total_cleaned}/{len(source_files)} files")
    return {"files_cleaned": total_cleaned, "total_files": len(source_files)}


def _read_all_sources(
    src_dir: Path, max_chars_per_file: int = 10000
) -> dict[str, str]:
    """Read all source files from the src directory."""
    files = {}
    priority_exts = {".py", ".js", ".ts", ".jsx", ".tsx", ".json", ".yaml", ".yml"}

    for fp in sorted(src_dir.rglob("*")):
        if not fp.is_file():
            continue
        if fp.suffix not in priority_exts:
            continue
        # Skip node_modules, __pycache__, etc.
        if any(part.startswith(".") or part == "node_modules" or part == "__pycache__"
               for part in fp.parts):
            continue
        try:
            content = fp.read_text(encoding="utf-8")[:max_chars_per_file]
            rel_path = str(fp.relative_to(src_dir))
            files[rel_path] = content
        except Exception:
            continue

    return files


def _apply_cleaned_files(output: str, workspace: WorkspaceManager) -> int:
    """Parse ```file:path``` blocks and overwrite source files using the shared
    state-machine parser (handles nested fences correctly)."""
    from utils.file_parser import parse_file_blocks, write_parsed_files

    parsed = parse_file_blocks(output)
    paths = write_parsed_files(parsed, workspace.write_source_file, label="De-Sloppify")
    return len(paths)

