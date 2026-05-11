"""
╔══════════════════════════════════════════════════════════════╗
║  FILE PARSER — Shared State-Machine Code Block Parser        ║
║                                                              ║
║  Used by: Coder Agent, De-Sloppify Agent, Deployer Agent     ║
║                                                              ║
║  Extracts ```file:path/to/file``` blocks from LLM output     ║
║  using a state-machine approach that handles:                ║
║  - Nested code blocks inside file content                    ║
║  - Whitespace variations in the opening fence                ║
║  - Language tags before the file: prefix                     ║
║  - Fallback regex for non-standard formats                   ║
╚══════════════════════════════════════════════════════════════╝
"""

import re
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


def parse_file_blocks(output: str) -> list[tuple[str, str]]:
    """
    State-machine parser for ```file:path/to/file``` blocks.

    More robust than regex — handles nested code blocks, edge cases,
    and whitespace variations correctly.

    Args:
        output: Raw LLM output text

    Returns:
        List of (filepath, content) tuples
    """
    parsed = []
    lines = output.split("\n")
    i = 0

    while i < len(lines):
        line = lines[i]

        # Detect file block start: ```file:path or ```lang file:path
        filepath = _match_file_fence(line)

        if filepath:
            # Accumulate content until closing ```
            i += 1
            content_lines = []
            nest_depth = 0
            hit_new_file = False

            while i < len(lines):
                stripped = lines[i].strip()

                # FIX: If this line starts a NEW file block, the current block
                # is implicitly closed (LLM sometimes omits closing ```)
                if _match_file_fence(lines[i]):
                    hit_new_file = True
                    break

                # Track nested code fences (``` inside file content)
                if stripped.startswith("```"):
                    if nest_depth > 0 and stripped == "```":
                        nest_depth -= 1
                    elif stripped != "```":
                        nest_depth += 1
                    content_lines.append(lines[i])
                elif stripped == "```" and nest_depth == 0:
                    # This is our closing fence
                    break
                else:
                    content_lines.append(lines[i])
                i += 1

            content = "\n".join(content_lines).strip()

            if filepath and content:
                # Normalize path: strip leading src/
                if filepath.startswith("src/"):
                    filepath = filepath[4:]
                parsed.append((filepath, content))

            # If we broke because of a new file, DON'T increment i —
            # the outer loop needs to process that line as the next file block
            if hit_new_file:
                continue

        i += 1

    # Fallback: try regex patterns if state machine found nothing
    if not parsed:
        parsed = _regex_fallback_parse(output)

    return parsed


def _match_file_fence(line: str) -> Optional[str]:
    """Match a file-block opening fence line and extract the filepath."""
    stripped = line.strip()

    # Pattern 1: ```file:path/to/file.ext
    match = re.match(r'^```file:(.+)$', stripped)
    if match:
        return match.group(1).strip()

    # Pattern 2: ``` file:path/to/file.ext (with space)
    match = re.match(r'^```\s+file:(.+)$', stripped)
    if match:
        return match.group(1).strip()

    # Pattern 3: ```language file:path/to/file.ext
    match = re.match(r'^```\w+\s+file:(.+)$', stripped)
    if match:
        return match.group(1).strip()

    # Pattern 4: ```language:file:path (rare but happens)
    match = re.match(r'^```\w+:file:(.+)$', stripped)
    if match:
        return match.group(1).strip()

    return None


def _regex_fallback_parse(output: str) -> list[tuple[str, str]]:
    """Fallback regex parser for non-standard output formats."""
    parsed = []

    # Try: ```language\n# file: path/to/file\ncontent```
    pattern = r'```\w*\n(?://|#)\s*(?:file:\s*)?(.+?)\n(.*?)```'
    matches = re.findall(pattern, output, re.DOTALL)

    if not matches:
        # Try: `path/to/file.ext`\n```language\ncontent```
        pattern2 = r'`([^`]+\.\w+)`\s*\n```\w*\n(.*?)```'
        matches = re.findall(pattern2, output, re.DOTALL)

    for filepath, content in matches:
        filepath = filepath.strip()
        content = content.strip()
        if filepath.startswith("src/"):
            filepath = filepath[4:]
        if filepath and content:
            parsed.append((filepath, content))

    return parsed


def write_parsed_files(
    parsed: list[tuple[str, str]],
    write_fn,
    label: str = "Parser",
) -> list[str]:
    """
    Write parsed file blocks to disk using a write function.

    Args:
        parsed: List of (filepath, content) tuples from parse_file_blocks()
        write_fn: Callable(filepath, content) -> Path (e.g. workspace.write_source_file)
        label: Log label (e.g. "Coder", "De-Sloppify")

    Returns:
        List of absolute file paths created
    """
    created = []
    for filepath, content in parsed:
        try:
            full_path = write_fn(filepath, content)
            created.append(str(full_path))
            logger.info(f"[{label}] Written: {filepath}")
        except Exception as e:
            logger.error(f"[{label}] Failed to write {filepath}: {e}")
    return created
