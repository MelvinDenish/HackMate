"""
╔══════════════════════════════════════════════════════════════╗
║  TEST WRITER AGENT — TDD Verification Loop                   ║
║                                                              ║
║  🟣 LLM: Claude Haiku 3.5 (fast, cheap)                     ║
║                                                              ║
║  Generates test files from PRD acceptance criteria.          ║
║  Tests are written BEFORE review — catches bugs that the     ║
║  reviewer would miss with static analysis alone.             ║
║                                                              ║
║  SWE-bench 2026: TDD gives 15-30% performance boost.        ║
║                                                              ║
║  Outputs: pytest files (Python) or vitest/jest files (JS)    ║
╚══════════════════════════════════════════════════════════════╝
"""

import logging
from pathlib import Path
from typing import Optional

from langchain_core.messages import SystemMessage, HumanMessage

from config import PipelineConfig, APIKeys
from pipeline.cost_tracker import CostTracker

logger = logging.getLogger(__name__)

TEST_WRITER_SYSTEM = """You are a senior QA engineer writing automated tests for a hackathon project.

RULES:
1. Write ONLY test files — no implementation code
2. For JavaScript/TypeScript projects: use vitest (import { describe, it, expect } from 'vitest')
3. For Python projects: use pytest
4. Test the CRITICAL paths from the acceptance criteria
5. Keep tests simple and fast — no complex mocking
6. Each test file should be self-contained
7. Include at least: smoke test, API endpoint test, data validation test
8. Use the ```file:path``` format for each test file

FOCUS ON:
- Does the app start without errors?
- Do API endpoints return correct status codes?
- Is the data model valid?
- Do key user flows work?

DO NOT test styling, animations, or visual layout."""

TEST_WRITER_PROMPT = """Generate test files for this project.

## PRD Acceptance Criteria
{acceptance_criteria}

## Tech Stack
{tech_stack}

## Source Files Created
{source_files}

## Source Tree
{src_tree}

Write comprehensive but fast tests. Use ```file:path``` format for each test file.
For JS/TS projects, put tests in `__tests__/` or alongside source with `.test.` suffix.
For Python projects, put tests in `tests/` directory."""


def generate_tests(
    workspace,
    config: PipelineConfig,
    tasks: list[dict],
    prd_content: str,
    src_tree: str,
    cost_tracker: Optional[CostTracker] = None,
) -> list[str]:
    """Generate test files from PRD acceptance criteria.

    Returns list of test file paths created.
    """
    from agents.llm_factory import create_llm, invoke_with_retry

    # Extract acceptance criteria from tasks
    criteria = []
    source_files = []
    for task in tasks:
        for ac in task.get("acceptance_criteria", []):
            criteria.append(f"- {ac}")
        for f in task.get("files_created", []):
            source_files.append(f)

    if not criteria:
        # Extract from PRD directly
        import re
        for match in re.finditer(r'[-•]\s*(.+)', prd_content[:5000]):
            criteria.append(f"- {match.group(1)}")

    if not criteria:
        logger.info("[TestWriter] No acceptance criteria found, skipping test generation")
        return []

    # Detect tech stack
    tech_stack = _detect_test_framework(workspace.src_dir)

    context = TEST_WRITER_PROMPT.format(
        acceptance_criteria="\n".join(criteria[:30]),
        tech_stack=tech_stack,
        source_files="\n".join(f"- {f}" for f in source_files[:20]),
        src_tree=src_tree[:3000],
    )

    spec = config.get_model("deslopify")  # Use cheap Haiku model
    llm = create_llm(spec, config.keys)

    messages = [
        SystemMessage(content=TEST_WRITER_SYSTEM),
        HumanMessage(content=context),
    ]

    response = invoke_with_retry(
        llm, messages,
        spec=spec,
        agent_name="test_writer",
        phase="testing",
        cost_tracker=cost_tracker,
    )

    # Parse and write test files
    from utils.file_parser import parse_file_blocks, write_parsed_files
    parsed = parse_file_blocks(response.content)
    test_files = write_parsed_files(parsed, workspace.write_source_file, label="TestWriter")

    logger.info(f"[TestWriter] Generated {len(test_files)} test files")
    return test_files


def _detect_test_framework(src_dir: Path) -> str:
    """Detect which test framework to use."""
    pkg_json = src_dir / "package.json"
    if pkg_json.exists():
        content = pkg_json.read_text(encoding="utf-8")
        if "vitest" in content:
            return "vitest (JavaScript/TypeScript)"
        elif "jest" in content:
            return "jest (JavaScript/TypeScript)"
        return "vitest (JavaScript/TypeScript — recommended for new projects)"

    if (src_dir / "requirements.txt").exists():
        return "pytest (Python)"

    return "vitest or pytest (auto-detect from source files)"


def run_tests(workspace_src_dir: Path, timeout: int = 30) -> dict:
    """Run test suite and return results.

    Returns dict with:
        - passed: bool
        - output: str (stdout+stderr)
        - test_count: int
        - fail_count: int
    """
    import subprocess

    result = {
        "passed": False,
        "output": "",
        "test_count": 0,
        "fail_count": 0,
        "framework": "none",
    }

    # Try Node.js tests first
    pkg_json = workspace_src_dir / "package.json"
    if pkg_json.exists():
        try:
            proc = subprocess.run(
                ["npm", "test", "--", "--reporter=verbose"],
                cwd=str(workspace_src_dir),
                capture_output=True,
                text=True,
                timeout=timeout,
                shell=True,
            )
            result["output"] = (proc.stdout + "\n" + proc.stderr)[-3000:]
            result["framework"] = "npm test"
            result["passed"] = proc.returncode == 0

            # Count tests from output
            import re
            pass_match = re.search(r'(\d+)\s*(?:pass|passed)', result["output"], re.IGNORECASE)
            fail_match = re.search(r'(\d+)\s*(?:fail|failed)', result["output"], re.IGNORECASE)
            if pass_match:
                result["test_count"] += int(pass_match.group(1))
            if fail_match:
                result["fail_count"] = int(fail_match.group(1))
                result["test_count"] += result["fail_count"]

            return result
        except subprocess.TimeoutExpired:
            result["output"] = "Test execution timed out"
            return result
        except Exception as e:
            result["output"] = f"Failed to run tests: {e}"

    # Try Python tests
    tests_dir = workspace_src_dir / "tests"
    if tests_dir.exists() or list(workspace_src_dir.glob("test_*.py")):
        try:
            proc = subprocess.run(
                ["python", "-m", "pytest", "-v", "--tb=short", str(workspace_src_dir)],
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            result["output"] = (proc.stdout + "\n" + proc.stderr)[-3000:]
            result["framework"] = "pytest"
            result["passed"] = proc.returncode == 0

            import re
            summary = re.search(r'(\d+) passed', result["output"])
            failed = re.search(r'(\d+) failed', result["output"])
            if summary:
                result["test_count"] = int(summary.group(1))
            if failed:
                result["fail_count"] = int(failed.group(1))
                result["test_count"] += result["fail_count"]

            return result
        except Exception as e:
            result["output"] = f"Failed to run pytest: {e}"

    result["output"] = "No test framework detected"
    return result
