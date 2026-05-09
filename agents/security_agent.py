"""
╔══════════════════════════════════════════════════════════════╗
║  SECURITY AGENT — Pre-Deployment Security Review             ║
║                                                              ║
║  🟣 LLM: Anthropic Claude Sonnet 4                           ║
║  Why: Security requires deep reasoning and pattern           ║
║       recognition — Claude is best at this.                  ║
║                                                              ║
║  Pattern: security-review (from ECC skill)                   ║
║                                                              ║
║  Runs AFTER the Reviewer passes and BEFORE Deployment.       ║
║  Checks for OWASP Top 10 vulnerabilities and common          ║
║  security anti-patterns.                                     ║
╚══════════════════════════════════════════════════════════════╝
"""

import re
import logging
from pathlib import Path
from dataclasses import dataclass

from langchain_core.messages import SystemMessage, HumanMessage

from agents.llm_factory import create_llm, invoke_with_retry
from config import PipelineConfig
from pipeline.cost_tracker import CostTracker
from workspace.manager import WorkspaceManager

logger = logging.getLogger(__name__)

SECURITY_SYSTEM_PROMPT = """You are a Security Review Agent in an autonomous hackathon pipeline. You perform a pre-deployment security audit.

## Your Checklist (from OWASP Top 10 + ECC security-review skill)

### 1. Secrets Management
- Hardcoded API keys, tokens, passwords in source code
- Secrets not loaded from environment variables
- .env files not in .gitignore

### 2. Injection Prevention
- SQL injection: string concatenation in database queries
- Command injection: unsanitized input in os.system() / exec() / subprocess
- Path traversal: user input in file paths without sanitization

### 3. Authentication & Authorization
- Missing auth checks on sensitive endpoints
- Tokens stored in localStorage (should be httpOnly cookies)
- Missing rate limiting on login/signup endpoints

### 4. Input Validation
- User input used without validation
- Missing file upload restrictions (size, type)
- No schema validation on API inputs

### 5. Data Exposure
- Sensitive data in error messages or logs
- Stack traces returned to users
- Passwords or secrets in log output
- Debug endpoints left enabled

### 6. Dependency Security
- Known vulnerable dependencies
- Missing lock files
- Outdated packages with CVEs

## Output Format (STRICT)

```
SECURITY VERDICT: [PASS|WARN|FAIL]

CRITICAL_ISSUES: [count]
WARNING_ISSUES: [count]

FINDINGS:
1. [CRITICAL|WARNING|INFO] [File: path/to/file] [Description of issue]
   FIX: [Specific fix instruction]

2. ...

RECOMMENDATIONS:
- [General security recommendations for deployment]
```

Rules:
- FAIL if any CRITICAL issues found (hardcoded secrets, SQL injection, command injection)
- WARN if only WARNING issues found (missing rate limiting, no input validation)
- PASS if no critical or warning issues
- For hackathon context, be pragmatic but NEVER pass hardcoded secrets"""


@dataclass
class SecurityResult:
    """Result of a security review."""
    verdict: str  # "PASS", "WARN", "FAIL"
    critical_count: int
    warning_count: int
    findings: str
    recommendations: str
    passed: bool

    def to_dict(self) -> dict:
        return {
            "verdict": self.verdict,
            "critical_count": self.critical_count,
            "warning_count": self.warning_count,
            "findings": self.findings,
            "recommendations": self.recommendations,
            "passed": self.passed,
        }


def run_security_review(
    workspace: WorkspaceManager,
    config: PipelineConfig,
    cost_tracker: CostTracker = None,
) -> SecurityResult:
    """
    Run security review on generated source code.

    1. Static analysis (regex-based secret detection)
    2. LLM-based deep review (Claude Sonnet 4)
    3. Combined verdict

    Returns:
        SecurityResult with verdict and findings
    """
    logger.info("[Security] Starting pre-deployment security review")

    src_dir = workspace.src_dir
    if not src_dir.exists() or not list(src_dir.iterdir()):
        return SecurityResult(
            verdict="PASS", critical_count=0, warning_count=0,
            findings="No source files to review",
            recommendations="", passed=True,
        )

    # Step 1: Static regex scan for secrets
    static_findings = _static_secret_scan(src_dir)

    # Step 2: Read source files for LLM review
    source_context = _read_sources_for_review(src_dir)

    # Step 3: LLM deep review
    spec = config.get_model("security")
    llm = create_llm(spec, config.keys)

    prompt = (
        f"## Static Analysis Pre-Findings\n"
        f"{static_findings if static_findings else 'No secrets detected by static scan.'}\n\n"
        f"## Source Code to Review\n{source_context}\n\n"
        f"Perform a thorough security review. Be specific about file paths and line numbers."
    )

    messages = [
        SystemMessage(content=SECURITY_SYSTEM_PROMPT),
        HumanMessage(content=prompt),
    ]

    response = invoke_with_retry(
        llm, messages,
        spec=spec,
        agent_name="security",
        phase="security",
        cost_tracker=cost_tracker,
    )
    analysis = response.content

    # Parse verdict
    verdict = "PASS"
    if "SECURITY VERDICT: FAIL" in analysis.upper():
        verdict = "FAIL"
    elif "SECURITY VERDICT: WARN" in analysis.upper():
        verdict = "WARN"

    # Count issues
    critical_count = analysis.upper().count("[CRITICAL]")
    warning_count = analysis.upper().count("[WARNING]")

    # If static scan found secrets, force FAIL
    if static_findings:
        verdict = "FAIL"
        critical_count = max(critical_count, 1)

    result = SecurityResult(
        verdict=verdict,
        critical_count=critical_count,
        warning_count=warning_count,
        findings=analysis,
        recommendations=_extract_recommendations(analysis),
        passed=(verdict != "FAIL"),
    )

    workspace.log_event(
        "security",
        f"Security verdict: {result.verdict} "
        f"(critical: {result.critical_count}, warnings: {result.warning_count})",
        result.findings[:500],
    )

    logger.info(
        f"[Security] Verdict: {result.verdict} "
        f"({result.critical_count} critical, {result.warning_count} warnings)"
    )
    return result


def _static_secret_scan(src_dir: Path) -> str:
    """Regex-based static scan for hardcoded secrets."""
    findings = []

    secret_patterns = [
        (r'["\']sk-[a-zA-Z0-9]{20,}["\']', "OpenAI/Anthropic API key"),
        (r'["\']ghp_[a-zA-Z0-9]{36}["\']', "GitHub personal access token"),
        (r'["\']gho_[a-zA-Z0-9]{36}["\']', "GitHub OAuth token"),
        (r'["\']AKIA[A-Z0-9]{16}["\']', "AWS access key"),
        (r'password\s*=\s*["\'][^"\']+["\']', "Hardcoded password"),
        (r'api[_-]?key\s*=\s*["\'][a-zA-Z0-9]{10,}["\']', "Hardcoded API key"),
        (r'secret\s*=\s*["\'][a-zA-Z0-9]{10,}["\']', "Hardcoded secret"),
        (r'Bearer\s+[a-zA-Z0-9\-._~+/]+=*', "Hardcoded bearer token"),
    ]

    code_exts = {".py", ".js", ".ts", ".jsx", ".tsx", ".json", ".yaml", ".yml", ".env"}

    for fp in src_dir.rglob("*"):
        if not fp.is_file() or fp.suffix not in code_exts:
            continue
        if "node_modules" in str(fp) or "__pycache__" in str(fp):
            continue

        try:
            content = fp.read_text(encoding="utf-8")
            for pattern, desc in secret_patterns:
                matches = re.findall(pattern, content, re.IGNORECASE)
                if matches:
                    rel = fp.relative_to(src_dir)
                    findings.append(
                        f"[CRITICAL] {rel}: {desc} detected ({len(matches)} occurrence(s))"
                    )
        except Exception:
            continue

    return "\n".join(findings) if findings else ""


def _read_sources_for_review(src_dir: Path, max_chars: int = 8000) -> str:
    """Read source files for security review context."""
    samples = []
    priority_exts = {".py", ".js", ".ts", ".jsx", ".tsx", ".json", ".yaml", ".yml"}

    for fp in sorted(src_dir.rglob("*")):
        if not fp.is_file() or fp.suffix not in priority_exts:
            continue
        if "node_modules" in str(fp) or "__pycache__" in str(fp):
            continue
        try:
            content = fp.read_text(encoding="utf-8")[:max_chars]
            rel = fp.relative_to(src_dir)
            samples.append(f"### {rel}\n```\n{content}\n```")
        except Exception:
            continue

    return "\n\n".join(samples[:30]) if samples else "(No readable source files)"


def _extract_recommendations(analysis: str) -> str:
    """Extract recommendations section from analysis."""
    if "RECOMMENDATIONS:" in analysis:
        return analysis.split("RECOMMENDATIONS:")[-1].strip()
    return ""
