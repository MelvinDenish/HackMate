"""
╔══════════════════════════════════════════════════════════════╗
║  README AGENT — Phase 3.6: Professional README + Seed Data   ║
║                                                              ║
║  🟣 LLM: Anthropic Claude Haiku 3.5 (fast + cheap)          ║
║                                                              ║
║  Guarantees every build has:                                 ║
║  1. Professional README.md with badges, features, quickstart ║
║  2. Realistic seed/demo data (JSON, SQL, or fixtures)        ║
║  3. .env.example with all required environment variables     ║
║                                                              ║
║  Why this matters: README is the #1 thing hackathon judges   ║
║  look at. A missing or sparse README = instant credibility   ║
║  loss. This agent guarantees professional presentation.      ║
╚══════════════════════════════════════════════════════════════╝
"""

import logging
from pathlib import Path

from langchain_core.messages import SystemMessage, HumanMessage

from agents.llm_factory import create_llm, invoke_with_retry
from config import PipelineConfig
from pipeline.cost_tracker import CostTracker
from workspace.manager import WorkspaceManager

logger = logging.getLogger(__name__)

README_SYSTEM_PROMPT = """You are the README Agent in a hackathon pipeline. Your job is to generate a PROFESSIONAL, judge-ready README.md and seed data for the generated project.

## README Requirements (MUST include ALL)

### Structure
1. **Project Title** with emoji + one-line tagline
2. **Badges row** (Tech stack badges using shields.io markdown)
3. **Screenshot/Demo section** (placeholder: `![Demo](screenshots/demo.png)`)
4. **Features** — bullet list of key features with emoji icons
5. **Quick Start** — step-by-step setup instructions
6. **Tech Stack** — table with technology, purpose, and why chosen
7. **Architecture** — brief description of system design
8. **API Endpoints** (if backend exists) — table with method, route, description
9. **Environment Variables** — table with variable name, description, required/optional
10. **Contributing** + **License** sections

### Style Rules
- Use professional markdown formatting
- Include code blocks for commands
- Use tables for structured data
- Keep it scannable (short paragraphs, lots of headers)
- Sound confident and professional, not generic

## Seed Data Requirements
Generate realistic demo data that makes the app feel ALIVE on first launch.
- 5-10 realistic records (not "test user 1", "test user 2")
- Use real-sounding names, emails, descriptions
- Include varied data (different statuses, dates, amounts)
- Format as JSON array that can be loaded on startup

## Output Format
Respond with two file blocks:

```file:README.md
(full README content)
```

```file:seed_data.json
(JSON array of realistic demo records)
```
"""


def run_readme_agent(
    workspace: WorkspaceManager,
    config: PipelineConfig,
    cost_tracker: CostTracker = None,
    prd_path: str = "",
) -> list[str]:
    """Generate professional README.md and seed data.

    Args:
        workspace: Workspace manager
        config: Pipeline configuration
        cost_tracker: Optional cost tracker
        prd_path: Path to the PRD for context

    Returns:
        List of created file paths
    """
    logger.info("[README] Generating professional README + seed data")

    # Gather context
    src_tree = workspace.get_src_tree()
    prd_content = ""
    if prd_path:
        try:
            prd_content = workspace.read_file(prd_path)[:6000]
        except Exception:
            pass

    # Check what already exists
    existing_readme = ""
    readme_path = workspace.src_dir / "README.md"
    if readme_path.exists():
        existing_readme = readme_path.read_text(encoding="utf-8")
        if len(existing_readme) > 500:
            logger.info("[README] README already exists and looks adequate — enhancing")

    # Detect tech stack from files
    tech_hints = _detect_tech_stack(workspace.src_dir)

    context = (
        f"## Project Structure\n```\n{src_tree}\n```\n\n"
        f"## PRD Summary\n{prd_content[:4000]}\n\n"
        f"## Detected Technologies\n{tech_hints}\n\n"
    )
    if existing_readme:
        context += f"## Current README (improve this)\n{existing_readme[:3000]}\n\n"

    spec = config.get_model("deslopify")  # Use Haiku — fast and cheap
    llm = create_llm(spec, config.keys)

    messages = [
        SystemMessage(content=README_SYSTEM_PROMPT),
        HumanMessage(content=context),
    ]

    response = invoke_with_retry(
        llm, messages,
        spec=spec,
        agent_name="readme",
        phase="readme",
        cost_tracker=cost_tracker,
    )

    # Parse and write files
    from utils.file_parser import parse_file_blocks, write_parsed_files
    parsed = parse_file_blocks(response.content)
    files_created = write_parsed_files(parsed, workspace.write_source_file, label="README")

    logger.info(f"[README] Generated {len(files_created)} files")
    return files_created


def _detect_tech_stack(src_dir: Path) -> str:
    """Detect technologies from source files."""
    hints = []

    if (src_dir / "package.json").exists():
        try:
            import json
            pkg = json.loads((src_dir / "package.json").read_text(encoding="utf-8"))
            deps = list(pkg.get("dependencies", {}).keys())
            if deps:
                hints.append(f"Node.js packages: {', '.join(deps[:15])}")
        except Exception:
            hints.append("Node.js project (package.json found)")

    if (src_dir / "requirements.txt").exists():
        try:
            reqs = (src_dir / "requirements.txt").read_text(encoding="utf-8")
            hints.append(f"Python packages:\n{reqs[:500]}")
        except Exception:
            hints.append("Python project (requirements.txt found)")

    if (src_dir / "manage.py").exists():
        hints.append("Django framework detected")

    py_files = list(src_dir.rglob("*.py"))
    if py_files:
        for f in py_files[:5]:
            try:
                content = f.read_text(encoding="utf-8")[:500]
                if "fastapi" in content.lower():
                    hints.append("FastAPI framework detected")
                if "flask" in content.lower():
                    hints.append("Flask framework detected")
            except Exception:
                pass

    html_files = list(src_dir.rglob("*.html"))
    if html_files:
        hints.append(f"{len(html_files)} HTML files found")

    css_files = list(src_dir.rglob("*.css"))
    if css_files:
        hints.append(f"{len(css_files)} CSS files found")

    js_files = list(src_dir.rglob("*.js")) + list(src_dir.rglob("*.ts"))
    if js_files:
        hints.append(f"{len(js_files)} JS/TS files found")

    return "\n".join(hints) if hints else "Unable to detect tech stack"
