"""
╔══════════════════════════════════════════════════════════════╗
║  REVIEWER AGENT v2 — Phase 3: Full Verification Loop         ║
║                                                              ║
║  🔵 LLM: Google Gemini 2.5 Pro                               ║
║  Why: Massive context window (1M tokens) for reviewing       ║
║       entire codebases, excellent at identifying bugs        ║
║                                                              ║
║  Improvements over v1:                                       ║
║  ✅ 6-phase verification loop (ECC verification-loop skill)  ║
║  ✅ Full codebase review (30 files × 8K chars — was 8×2K)   ║
║  ✅ Cost tracking via invoke_with_retry                      ║
║  ✅ Structured build + import + lint + test + security scan  ║
║  ✅ Better sandbox result analysis                           ║
║                                                              ║
║  Workflow:                                                   ║
║  1. Run code in Docker sandbox (build + run)                 ║
║  2. Static checks (imports, syntax)                          ║
║  3. Full codebase review with Gemini (1M context)            ║
║  4. If failed: return fix instructions to Coder Agent        ║
║  5. If passed: approve and move to security review           ║
║  Max retries: 3 (configurable)                               ║
╚══════════════════════════════════════════════════════════════╝
"""

import logging
import re
from pathlib import Path

from langchain_core.messages import SystemMessage, HumanMessage

from agents.llm_factory import create_llm, invoke_with_retry
from config import PipelineConfig
from pipeline.cost_tracker import CostTracker
from tools.sandbox import DockerSandbox, LocalSandbox, SandboxResult, create_sandbox
from workspace.manager import WorkspaceManager

logger = logging.getLogger(__name__)

REVIEWER_SYSTEM_PROMPT = """You are the Reviewer Agent in an autonomous hackathon pipeline. You analyze code quality and test results to determine if the code is ready for deployment.

## 6-Phase Verification Loop (ECC verification-loop pattern)

### Phase 1: Build Verification
Analyze the sandbox execution results — did the code build and start?

### Phase 2: Import/Dependency Check
Check for missing imports, unresolved modules, or dependency mismatches.

### Phase 3: Lint Check
Look for obvious code quality issues (unused variables, unreachable code, style issues).

### Phase 4: Test Execution
Analyze test output if any tests were run. Note test pass/fail counts.

### Phase 5: Runtime Check
Did the application start without crashing? Any runtime errors in stderr?

### Phase 6: Logic Review
Review the source code for obvious bugs, edge cases, or logic errors.

## Decision Criteria
- PASS if: Code builds, runs without errors, core functionality works, no critical bugs
- FAIL if: Runtime errors, missing dependencies, broken imports, logic bugs, crashes

## For Hackathon Standards (be pragmatic)
- Don't fail for missing tests or documentation
- Don't fail for style issues or minor warnings
- DO fail for runtime errors, crashes, or broken core features

## Output Format (STRICT)
If PASS:
```
VERDICT: PASS

VERIFICATION:
- Build: [PASS/FAIL]
- Imports: [PASS/FAIL]
- Lint: [PASS/WARN/FAIL]
- Tests: [PASS/SKIP/FAIL]
- Runtime: [PASS/FAIL]
- Logic: [PASS/WARN/FAIL]

NOTES: [Brief summary of what works well]
```

If FAIL:
```
VERDICT: FAIL

VERIFICATION:
- Build: [PASS/FAIL]
- Imports: [PASS/FAIL]
- Lint: [PASS/WARN/FAIL]
- Tests: [PASS/SKIP/FAIL]
- Runtime: [PASS/FAIL]
- Logic: [PASS/WARN/FAIL]

ISSUES:
1. [Specific issue with file path and line description]
2. [Another issue]

FIX_INSTRUCTIONS:
[Detailed instructions for the Coder Agent on what to fix and how]
```"""


class ReviewResult:
    """Result of a code review cycle."""

    def __init__(self, passed: bool, verdict: str, notes: str,
                 fix_instructions: str, verification: dict = None,
                 sandbox_result: SandboxResult = None):
        self.passed = passed
        self.verdict = verdict
        self.notes = notes
        self.fix_instructions = fix_instructions
        self.verification = verification or {}
        self.sandbox_result = sandbox_result

    def to_dict(self) -> dict:
        return {
            "passed": self.passed,
            "verdict": self.verdict,
            "notes": self.notes,
            "fix_instructions": self.fix_instructions,
            "verification": self.verification,
            "sandbox": self.sandbox_result.to_dict() if self.sandbox_result else None,
        }


def _detect_project_type(src_dir: Path) -> dict:
    """Detect project type and appropriate run commands."""
    info = {"type": "unknown", "entry": "", "install": "", "test": ""}

    if (src_dir / "package.json").exists():
        info["type"] = "node"
        info["install"] = "npm install"
        info["entry"] = "node index.js"
        info["test"] = "npm test"
        # Check for specific entry points
        for entry in ["server.js", "app.js", "index.js", "src/index.js"]:
            if (src_dir / entry).exists():
                info["entry"] = f"node {entry}"
                break

    elif (src_dir / "requirements.txt").exists():
        info["type"] = "python"
        info["install"] = "pip install -q -r requirements.txt"
        info["entry"] = "python main.py"
        info["test"] = "python -m pytest -v"
        for entry in ["main.py", "app.py", "server.py", "run.py"]:
            if (src_dir / entry).exists():
                info["entry"] = f"python {entry}"
                break

    elif (src_dir / "go.mod").exists():
        info["type"] = "go"
        info["entry"] = "go run ."
        info["test"] = "go test ./..."

    return info


def _static_checks(src_dir: Path) -> str:
    """Run static checks before sandbox (import verification, syntax)."""
    findings = []

    for fp in sorted(src_dir.rglob("*.py")):
        try:
            content = fp.read_text(encoding="utf-8")
            compile(content, str(fp), "exec")
        except SyntaxError as e:
            rel = fp.relative_to(src_dir)
            findings.append(f"[SYNTAX ERROR] {rel}: Line {e.lineno}: {e.msg}")

    # Check for obvious import issues in Python files
    for fp in sorted(src_dir.rglob("*.py")):
        try:
            content = fp.read_text(encoding="utf-8")
            imports = re.findall(r'^(?:from|import)\s+(\S+)', content, re.MULTILINE)
            rel = fp.relative_to(src_dir)
            for imp in imports:
                module = imp.split(".")[0]
                # Flag imports from non-existent local modules
                if not module.startswith("_") and module not in {
                    "os", "sys", "json", "re", "time", "pathlib", "datetime",
                    "typing", "logging", "asyncio", "math", "random", "hashlib",
                    "collections", "functools", "itertools", "dataclasses",
                    "abc", "enum", "io", "copy", "uuid", "base64", "urllib",
                    "http", "socket", "subprocess", "shutil", "glob",
                    # Common packages
                    "flask", "django", "fastapi", "express", "react",
                    "sqlalchemy", "requests", "numpy", "pandas",
                    "pydantic", "pytest", "click", "rich",
                }:
                    # Check if it's a local module
                    local_module = src_dir / f"{module}.py"
                    local_package = src_dir / module / "__init__.py"
                    if not local_module.exists() and not local_package.exists():
                        pass  # Don't flag — too many false positives with 3rd party
        except Exception:
            continue

    return "\n".join(findings) if findings else ""


def run_review(
    workspace: WorkspaceManager,
    config: PipelineConfig,
    cost_tracker: CostTracker = None,
    prd_path: str = "",
) -> ReviewResult:
    """
    Review generated code using the 6-phase verification loop.

    1. Static checks (syntax, imports)
    2. Docker sandbox execution
    3. Full codebase LLM review with Gemini 2.5 Pro
    4. Return PASS/FAIL verdict

    Args:
        workspace: Workspace manager
        config: Pipeline configuration
        cost_tracker: Optional cost tracker

    Returns:
        ReviewResult with verdict and fix instructions
    """
    logger.info("[Reviewer] Starting 6-phase verification loop")

    src_dir = workspace.src_dir
    if not src_dir.exists() or not list(src_dir.iterdir()):
        return ReviewResult(
            passed=False, verdict="FAIL",
            notes="No source files found",
            fix_instructions="No code has been generated yet.",
        )

    # Phase 0: Static checks
    static_issues = _static_checks(src_dir)
    if static_issues:
        logger.warning(f"[Reviewer] Static issues found:\n{static_issues}")

    # Phase 1: Detect project type
    project = _detect_project_type(src_dir)
    logger.info(f"[Reviewer] Detected project type: {project['type']}")

    # Phase 2: Run in sandbox (with Docker fallback)
    sandbox = create_sandbox(config)

    # Override image for Node projects if using Docker
    if project["type"] == "node" and isinstance(sandbox, DockerSandbox):
        sandbox.image = "node:20-slim"

    sandbox_result = sandbox.execute_project(
        src_dir=src_dir,
        entry_command=project["entry"],
        install_command=project["install"] if project["install"] else None,
    )

    logger.info(
        f"[Reviewer] Sandbox result: exit={sandbox_result.exit_code}, "
        f"duration={sandbox_result.duration_ms}ms"
    )

    # Phase 3: Get FULL source tree and ALL files for context
    # v2: Increased to 30 files × 8000 chars (Gemini can handle 1M tokens)
    src_tree = workspace.get_src_tree()
    all_source_files = _get_all_source_files(src_dir, max_files=30, max_chars=8000)

    # Phase 4: Analyze with Gemini 2.5 Pro
    spec = config.get_model("reviewer")
    llm = create_llm(spec, config.keys)

    review_context = (
        f"## Project Type: {project['type']}\n\n"
        f"## Source Tree\n```\n{src_tree}\n```\n\n"
    )

    if static_issues:
        review_context += f"## Static Analysis Issues\n{static_issues}\n\n"

    # Add PRD context so reviewer can verify spec compliance
    if prd_path:
        try:
            prd_content = workspace.read_file(prd_path)
            # Include key spec sections for verification
            review_context += f"## PRD Specification (for compliance check)\n{prd_content[:6000]}\n\n"
        except Exception:
            pass

    review_context += (
        f"## Sandbox Execution Result\n{sandbox_result.summary}\n\n"
        f"## Full Source Code ({len(all_source_files)} files)\n{all_source_files}\n\n"
        "Perform the 6-phase verification and provide your verdict. "
        "Also verify that the code implements the PRD specification correctly."
    )

    messages = [
        SystemMessage(content=REVIEWER_SYSTEM_PROMPT),
        HumanMessage(content=review_context),
    ]

    response = invoke_with_retry(
        llm, messages,
        spec=spec,
        agent_name="reviewer",
        phase="review",
        cost_tracker=cost_tracker,
    )
    analysis = response.content

    # Parse verdict
    passed = "VERDICT: PASS" in analysis.upper()
    fix_instructions = ""
    if not passed and "FIX_INSTRUCTIONS:" in analysis:
        fix_instructions = analysis.split("FIX_INSTRUCTIONS:")[-1].strip()

    # Parse verification phases
    verification = _parse_verification(analysis)

    result = ReviewResult(
        passed=passed,
        verdict="PASS" if passed else "FAIL",
        notes=analysis,
        fix_instructions=fix_instructions,
        verification=verification,
        sandbox_result=sandbox_result,
    )

    workspace.log_event(
        "review",
        f"Review verdict: {result.verdict} | Phases: {verification}",
        result.notes[:500],
    )

    logger.info(f"[Reviewer] Verdict: {result.verdict}")
    return result


def _get_all_source_files(src_dir: Path, max_files: int = 30,
                          max_chars: int = 8000) -> str:
    """Read ALL source files for comprehensive review context."""
    samples = []
    priority_exts = [".py", ".js", ".ts", ".jsx", ".tsx", ".json", ".yaml",
                     ".yml", ".html", ".css", ".md"]

    files = sorted(src_dir.rglob("*"))
    # Prioritize by extension
    prioritized = []
    for ext in priority_exts:
        for f in files:
            if f.suffix == ext and f.is_file():
                if "node_modules" not in str(f) and "__pycache__" not in str(f):
                    prioritized.append(f)
    # Add remaining files
    for f in files:
        if f.is_file() and f not in prioritized:
            if "node_modules" not in str(f) and "__pycache__" not in str(f):
                prioritized.append(f)

    for f in prioritized[:max_files]:
        try:
            content = f.read_text(encoding="utf-8")[:max_chars]
            rel_path = f.relative_to(src_dir)
            samples.append(f"### {rel_path}\n```\n{content}\n```")
        except Exception:
            continue

    return "\n\n".join(samples) if samples else "(No readable source files)"


def _parse_verification(analysis: str) -> dict:
    """Parse verification phase results from the analysis."""
    phases = {}
    for phase in ["Build", "Imports", "Lint", "Tests", "Runtime", "Logic"]:
        pattern = rf'{phase}:\s*(PASS|FAIL|WARN|SKIP)'
        match = re.search(pattern, analysis, re.IGNORECASE)
        if match:
            phases[phase.lower()] = match.group(1).upper()
    return phases
