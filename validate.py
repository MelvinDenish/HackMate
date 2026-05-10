"""Validate syntax of all pipeline files."""
import ast
import sys
from pathlib import Path

files = [
    "config.py",
    "pipeline/cost_tracker.py",
    "pipeline/state.py",
    "pipeline/orchestrator.py",
    "pipeline/schemas.py",
    "pipeline/learning_db.py",
    "pipeline/progress_server.py",
    "pipeline/context_utils.py",
    "agents/llm_factory.py",
    "agents/deslopify_agent.py",
    "agents/security_agent.py",
    "agents/coder_agent.py",
    "agents/reviewer_agent.py",
    "agents/research_agent.py",
    "agents/architect_agent.py",
    "agents/planner_agent.py",
    "agents/pitch_agent.py",
    "agents/presentation_agent.py",
    "agents/clarification_agent.py",
    "agents/knowledge_base.py",
    "agents/deployer_agent.py",
    "agents/readme_agent.py",
    "tools/exa_search.py",
    "tools/sandbox.py",
    "tools/web_search.py",
    "tools/presentation.py",
    "tools/dev_server.py",
    "tools/screenshots.py",
    "design_systems/__init__.py",
    "utils/file_parser.py",
    "workspace/manager.py",
    "main.py",
]

errors = 0
for f in files:
    try:
        source = Path(f).read_text(encoding="utf-8")
        ast.parse(source)
        print(f"  [PASS] {f}")
    except SyntaxError as e:
        print(f"  [FAIL] {f}: Line {e.lineno}: {e.msg}")
        errors += 1
    except FileNotFoundError:
        print(f"  [SKIP] {f}: NOT FOUND")

print(f"\n{'='*40}")
if errors == 0:
    print("All files passed syntax validation!")
else:
    print(f"{errors} file(s) have syntax errors.")
    sys.exit(1)
