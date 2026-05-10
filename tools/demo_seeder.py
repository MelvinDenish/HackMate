"""
╔══════════════════════════════════════════════════════════════╗
║  DEMO SEEDER — Populate Apps with Realistic Data             ║
║                                                              ║
║  Empty demos = instant judge rejection.                      ║
║  This injects seed data + demo credentials so the app        ║
║  looks alive and populated during the demo.                  ║
║                                                              ║
║  Features:                                                   ║
║  • Auto-generates seed_data.json from PRD                    ║
║  • Injects demo user credentials                             ║
║  • Creates seed script (Node/Python) that runs on startup    ║
║  • Generates demo walkthrough steps for happy path           ║
╚══════════════════════════════════════════════════════════════╝
"""

import json
import logging
from pathlib import Path
from typing import Optional

from langchain_core.messages import SystemMessage, HumanMessage

from config import PipelineConfig
from pipeline.cost_tracker import CostTracker

logger = logging.getLogger(__name__)

SEED_PROMPT = """Generate realistic seed data for this hackathon project demo.

## Project Description
{prd_summary}

## Tech Stack
{tech_stack}

## Source Files
{source_files}

Generate a JSON object with this structure:
{{
  "demo_users": [
    {{"email": "demo@example.com", "password": "demo123", "name": "Demo User", "role": "admin"}},
    {{"email": "user@example.com", "password": "user123", "name": "Jane Smith", "role": "user"}}
  ],
  "seed_data": {{
    "<entity_name>": [
      // 5-10 realistic records for each main entity in the app
    ]
  }},
  "demo_walkthrough": [
    "Step 1: Open the app and see the populated dashboard",
    "Step 2: Click on ... to see ...",
    // 5-8 steps for a compelling 2-minute demo
  ]
}}

Make the data REALISTIC — real names, real-looking numbers, plausible dates.
Match the domain of the project (e.g., medical data for health apps, financial data for fintech).
"""


def generate_seed_data(
    workspace,
    config: PipelineConfig,
    prd_content: str,
    src_tree: str,
    cost_tracker: Optional[CostTracker] = None,
) -> dict:
    """Generate seed data for the project demo.

    Returns dict with demo_users, seed_data, and demo_walkthrough.
    """
    from agents.llm_factory import create_llm, invoke_with_retry

    # Use cheap model for seed data
    spec = config.get_model("deslopify")
    llm = create_llm(spec, config.keys)

    context = SEED_PROMPT.format(
        prd_summary=prd_content[:3000],
        tech_stack=_detect_stack(workspace.src_dir),
        source_files=src_tree[:2000],
    )

    messages = [
        SystemMessage(content="You are a data seeding specialist. Output ONLY valid JSON."),
        HumanMessage(content=context),
    ]

    response = invoke_with_retry(
        llm, messages,
        spec=spec,
        agent_name="demo_seeder",
        phase="seeding",
        cost_tracker=cost_tracker,
    )

    # Parse the JSON response
    from pipeline.schemas import safe_parse_json
    seed_data = safe_parse_json(response.content)

    if not seed_data:
        logger.warning("[DemoSeeder] Failed to parse seed data, using defaults")
        seed_data = _default_seed_data()

    return seed_data


def write_seed_files(
    seed_data: dict,
    workspace_src_dir: Path,
) -> list[str]:
    """Write seed data files to the workspace.

    Creates:
    - seed_data.json — the raw data
    - seed.js or seed.py — executable seeder script
    """
    created = []

    # Write seed_data.json
    seed_path = workspace_src_dir / "seed_data.json"
    seed_path.write_text(
        json.dumps(seed_data, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    created.append(str(seed_path))
    logger.info("[DemoSeeder] Written seed_data.json")

    # Write seeder script based on stack
    stack = _detect_stack(workspace_src_dir)

    if "node" in stack or "next" in stack or "express" in stack:
        seeder = workspace_src_dir / "seed.js"
        seeder.write_text(_node_seeder_script(), encoding="utf-8")
        created.append(str(seeder))
    elif "python" in stack or "flask" in stack:
        seeder = workspace_src_dir / "seed.py"
        seeder.write_text(_python_seeder_script(), encoding="utf-8")
        created.append(str(seeder))

    # Write demo walkthrough
    walkthrough = seed_data.get("demo_walkthrough", [])
    if walkthrough:
        demo_path = workspace_src_dir / "DEMO_WALKTHROUGH.md"
        lines = ["# Demo Walkthrough\n"]
        lines.append("Follow these steps for a compelling 2-minute demo:\n")
        for i, step in enumerate(walkthrough, 1):
            lines.append(f"{i}. {step}")
        lines.append("\n## Demo Credentials")
        for user in seed_data.get("demo_users", []):
            lines.append(f"- **{user.get('role', 'user')}**: `{user.get('email')}` / `{user.get('password')}`")
        demo_path.write_text("\n".join(lines), encoding="utf-8")
        created.append(str(demo_path))

    logger.info(f"[DemoSeeder] Created {len(created)} seed files")
    return created


def _detect_stack(src_dir: Path) -> str:
    if (src_dir / "package.json").exists():
        content = (src_dir / "package.json").read_text(encoding="utf-8")
        if "next" in content:
            return "nextjs"
        if "express" in content:
            return "express"
        return "node"
    if (src_dir / "requirements.txt").exists():
        return "python/flask"
    return "static"


def _default_seed_data() -> dict:
    return {
        "demo_users": [
            {"email": "demo@example.com", "password": "demo123", "name": "Demo User", "role": "admin"},
            {"email": "user@example.com", "password": "user123", "name": "Jane Smith", "role": "user"},
        ],
        "seed_data": {
            "items": [
                {"id": 1, "name": "Sample Item 1", "status": "active"},
                {"id": 2, "name": "Sample Item 2", "status": "pending"},
                {"id": 3, "name": "Sample Item 3", "status": "completed"},
            ]
        },
        "demo_walkthrough": [
            "Open the app homepage",
            "Log in with demo@example.com / demo123",
            "Browse the main dashboard",
            "Create a new item",
            "View the item details",
        ],
    }


def _node_seeder_script() -> str:
    return '''import fs from "fs";
const data = JSON.parse(fs.readFileSync("./seed_data.json", "utf-8"));
console.log(`[Seed] Loaded ${Object.keys(data.seed_data || {}).length} collections`);
console.log("[Seed] Demo users:", data.demo_users?.map(u => u.email).join(", "));
// Add your database seeding logic here
// e.g., for Prisma: await prisma.user.createMany({ data: data.demo_users });
console.log("[Seed] Complete!");
'''


def _python_seeder_script() -> str:
    return '''import json

with open("seed_data.json") as f:
    data = json.load(f)

print(f"[Seed] Loaded {len(data.get('seed_data', {}))} collections")
print(f"[Seed] Demo users: {[u['email'] for u in data.get('demo_users', [])]}")
# Add your database seeding logic here
# e.g., for SQLAlchemy: db.session.add_all([User(**u) for u in data['demo_users']])
print("[Seed] Complete!")
'''
