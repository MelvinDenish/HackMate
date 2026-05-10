"""
╔══════════════════════════════════════════════════════════════╗
║  CONTEXT UTILS — Context Compaction & Prefetch               ║
║                                                              ║
║  Prevents context window bloat by:                           ║
║  1. Compacting large state fields into summaries             ║
║  2. Pre-reading dependency files before task dispatch        ║
║  3. Truncating stale review context                          ║
║                                                              ║
║  Research: "Context Engineering" (Martin Fowler 2025)         ║
║  "Avoid token landfills. Use rolling summaries."             ║
╚══════════════════════════════════════════════════════════════╝
"""

import logging
import re
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


def compact_src_tree(src_tree: str, max_lines: int = 40) -> str:
    """Compact a source tree to top-level structure only.
    If tree has >max_lines, collapse deep nesting to keep it scannable."""
    lines = src_tree.strip().split("\n")
    if len(lines) <= max_lines:
        return src_tree

    compacted = []
    for line in lines:
        # Count indent level (2 spaces = 1 level)
        stripped = line.lstrip()
        indent = len(line) - len(stripped)
        depth = indent // 2

        if depth <= 1:  # Top-level files and first-level directories
            compacted.append(line)
        elif "📁" in line:  # Always show directory markers
            compacted.append(line)

    if len(compacted) < len(lines):
        compacted.append(f"  ... ({len(lines) - len(compacted)} more files)")

    result = "\n".join(compacted)
    logger.debug(
        f"[Context] Compacted src tree: {len(lines)} → {len(compacted)} lines"
    )
    return result


def compact_review_notes(notes: str, max_chars: int = 2000) -> str:
    """Extract only actionable fix instructions from verbose review notes.
    Strips the full analysis down to the VERDICT + FIX_INSTRUCTIONS."""
    if len(notes) <= max_chars:
        return notes

    # Try to extract just the essential sections
    sections = []

    # Extract VERDICT line
    verdict_match = re.search(r'VERDICT:.*', notes, re.IGNORECASE)
    if verdict_match:
        sections.append(verdict_match.group(0))

    # Extract VERIFICATION block
    verif_match = re.search(
        r'VERIFICATION:.*?(?=\n\n|\nFIX|\nNOTES|\Z)',
        notes, re.IGNORECASE | re.DOTALL
    )
    if verif_match:
        sections.append(verif_match.group(0)[:500])

    # Extract FIX_INSTRUCTIONS
    fix_match = re.search(
        r'FIX.?INSTRUCTIONS?:.*',
        notes, re.IGNORECASE | re.DOTALL
    )
    if fix_match:
        sections.append(fix_match.group(0)[:1200])

    if sections:
        result = "\n\n".join(sections)
        logger.debug(
            f"[Context] Compacted review notes: {len(notes)} → {len(result)} chars"
        )
        return result

    # Fallback: just truncate
    return notes[:max_chars] + "\n... (truncated)"


def compact_prd(prd_content: str, max_chars: int = 6000) -> str:
    """Compact PRD to essential sections for downstream agents.
    Prioritizes: features, tech stack, API endpoints, DB schema.
    Drops: verbose descriptions, rationale, appendices."""
    if len(prd_content) <= max_chars:
        return prd_content

    # Extract high-priority sections
    priority_patterns = [
        r'(?:^#+\s*(?:\d+\.\s*)?(?:Feature|Core|Key|MVP|Requirement).*?)(?=^#|\Z)',
        r'(?:^#+\s*(?:\d+\.\s*)?(?:Tech|Stack|Architecture).*?)(?=^#|\Z)',
        r'(?:^#+\s*(?:\d+\.\s*)?(?:API|Endpoint|Route).*?)(?=^#|\Z)',
        r'(?:^#+\s*(?:\d+\.\s*)?(?:Data|Database|Schema|Model).*?)(?=^#|\Z)',
        r'(?:^#+\s*(?:\d+\.\s*)?(?:UI|Screen|Page|View).*?)(?=^#|\Z)',
    ]

    extracted = []
    for pattern in priority_patterns:
        matches = re.findall(pattern, prd_content, re.MULTILINE | re.DOTALL | re.IGNORECASE)
        for m in matches:
            if m.strip():
                extracted.append(m.strip()[:1200])

    if extracted:
        result = "\n\n".join(extracted)
        if len(result) > max_chars:
            result = result[:max_chars]
        logger.debug(f"[Context] Compacted PRD: {len(prd_content)} → {len(result)} chars")
        return result

    # Fallback: first max_chars characters
    return prd_content[:max_chars]


def prefetch_dependency_files(
    tasks: list[dict],
    workspace_src_dir: Path,
) -> dict[str, str]:
    """Pre-read dependency file contents so they're instantly available
    when the coder processes tasks. Reads files created by dependency tasks.

    Returns: dict mapping relative_path → file_content
    """
    cache = {}
    for task in tasks:
        for dep_id in task.get("dependencies", []):
            # Find the dependency task
            dep_task = next(
                (t for t in tasks if t.get("id") == dep_id),
                None
            )
            if dep_task and dep_task.get("status") == "completed":
                for file_path in dep_task.get("files_created", []):
                    if file_path in cache:
                        continue
                    try:
                        fp = Path(file_path)
                        if not fp.is_absolute():
                            fp = workspace_src_dir / fp
                        if fp.exists() and fp.stat().st_size < 50_000:
                            cache[file_path] = fp.read_text(encoding="utf-8")
                    except Exception:
                        continue

    if cache:
        logger.info(
            f"[Context] Pre-fetched {len(cache)} dependency files "
            f"({sum(len(v) for v in cache.values()):,} chars)"
        )
    return cache
