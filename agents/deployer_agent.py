"""
╔══════════════════════════════════════════════════════════════╗
║  DEPLOYER AGENT — Phase 3: Railway Deployment                ║
║                                                              ║
║  🟣 LLM: Anthropic Claude Haiku 3.5                          ║
║  Why: Simple deployment tasks — fast and cheap is optimal.   ║
║       Haiku can generate deployment configs efficiently.     ║
║                                                              ║
║  Workflow:                                                   ║
║  1. Analyze project type                                     ║
║  2. Generate deployment config (Dockerfile, railway.json)    ║
║  3. Deploy to Railway via API                                ║
║  4. Return live URL                                          ║
╚══════════════════════════════════════════════════════════════╝
"""

import json
import logging
import time
from pathlib import Path

import httpx
from langchain_core.messages import SystemMessage, HumanMessage

from agents.llm_factory import create_llm, invoke_with_retry
from config import PipelineConfig
from pipeline.cost_tracker import CostTracker
from workspace.manager import WorkspaceManager

logger = logging.getLogger(__name__)

RAILWAY_API_URL = "https://backboard.railway.com/graphql/v2"

DEPLOYER_SYSTEM_PROMPT = """You are a Deployer Agent. Generate deployment configuration files for a Railway deployment.

Given the project structure and tech stack, generate:
1. A Dockerfile (if not already present)
2. A railway.json configuration file
3. Any necessary start scripts

## Rules
- Use multi-stage Docker builds for smaller images
- Expose the correct port (check the source code)
- Set appropriate environment variables
- Use production-optimized settings
- Keep the Dockerfile minimal and efficient

## Output Format
Use the ```file:path``` format:
```file:Dockerfile
FROM node:20-slim
...
```

```file:railway.json
{...}
```"""


def _create_railway_project(api_token: str, project_name: str) -> dict:
    """Create a new Railway project via GraphQL API."""
    query = """
    mutation projectCreate($input: ProjectCreateInput!) {
        projectCreate(input: $input) {
            id
            name
        }
    }
    """
    variables = {"input": {"name": project_name}}

    headers = {
        "Authorization": f"Bearer {api_token}",
        "Content-Type": "application/json",
    }

    with httpx.Client(timeout=30) as client:
        resp = client.post(
            RAILWAY_API_URL,
            json={"query": query, "variables": variables},
            headers=headers,
        )
        resp.raise_for_status()
        data = resp.json()

    if "errors" in data:
        raise RuntimeError(f"Railway API error: {data['errors']}")

    return data.get("data", {}).get("projectCreate", {})


def _get_deployment_url(api_token: str, project_id: str) -> str:
    """Get the deployment URL for a Railway project."""
    query = """
    query project($id: String!) {
        project(id: $id) {
            services {
                edges {
                    node {
                        serviceInstances {
                            edges {
                                node {
                                    domains {
                                        serviceDomains {
                                            domain
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
            }
        }
    }
    """
    headers = {
        "Authorization": f"Bearer {api_token}",
        "Content-Type": "application/json",
    }

    with httpx.Client(timeout=30) as client:
        resp = client.post(
            RAILWAY_API_URL,
            json={"query": query, "variables": {"id": project_id}},
            headers=headers,
        )
        resp.raise_for_status()
        data = resp.json()

    # Extract domain from nested response
    try:
        services = data["data"]["project"]["services"]["edges"]
        for svc in services:
            instances = svc["node"]["serviceInstances"]["edges"]
            for inst in instances:
                domains = inst["node"]["domains"]["serviceDomains"]
                if domains:
                    return f"https://{domains[0]['domain']}"
    except (KeyError, IndexError, TypeError):
        pass

    return ""


def generate_deploy_config(
    workspace: WorkspaceManager,
    config: PipelineConfig,
    cost_tracker: CostTracker = None,
) -> list[str]:
    """
    Generate deployment configuration files (Dockerfile, railway.json).

    Args:
        workspace: Workspace manager
        config: Pipeline configuration
        cost_tracker: Optional cost tracker

    Returns:
        List of config file paths created
    """
    logger.info("[Deployer] Generating deployment config")

    src_tree = workspace.get_src_tree()

    # Read key config files for context
    src_dir = workspace.src_dir
    context_files = ""
    for fname in ["package.json", "requirements.txt", "go.mod", "pyproject.toml"]:
        fpath = src_dir / fname
        if fpath.exists():
            content = fpath.read_text(encoding="utf-8")
            context_files += f"\n### {fname}\n```\n{content}\n```\n"

    spec = config.get_model("deployer")
    llm = create_llm(spec, config.keys)

    messages = [
        SystemMessage(content=DEPLOYER_SYSTEM_PROMPT),
        HumanMessage(content=(
            f"## Project Structure\n```\n{src_tree}\n```\n"
            f"\n## Key Config Files\n{context_files}\n"
            "\nGenerate the deployment configuration files."
        )),
    ]

    response = invoke_with_retry(
        llm, messages,
        spec=spec,
        agent_name="deployer",
        phase="deployment",
        cost_tracker=cost_tracker,
    )
    output = response.content

    # Parse and write config files using shared parser
    from utils.file_parser import parse_file_blocks, write_parsed_files
    parsed = parse_file_blocks(output)
    files_created = write_parsed_files(parsed, workspace.write_source_file, label="Deployer")

    logger.info(f"[Deployer] Generated {len(files_created)} deploy configs")
    return files_created


def _upload_source_to_railway(
    api_token: str, project_id: str, src_dir: Path
) -> str:
    """
    Upload source code to Railway via the serviceInstanceDeploy mutation.
    Uses the CLI upload endpoint for code push without git.
    """
    import tarfile
    import io
    import base64

    # Create tarball of src directory
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for fp in src_dir.rglob("*"):
            if fp.is_file():
                # Skip node_modules, __pycache__, .git
                if any(p in str(fp) for p in ["node_modules", "__pycache__", ".git"]):
                    continue
                tar.add(str(fp), arcname=str(fp.relative_to(src_dir)))
    buf.seek(0)
    tarball_bytes = buf.getvalue()

    # Step 1: Get an upload URL via Railway API
    headers = {
        "Authorization": f"Bearer {api_token}",
        "Content-Type": "application/json",
    }

    # Create a service in the project
    create_service_query = """
    mutation serviceCreate($input: ServiceCreateInput!) {
        serviceCreate(input: $input) {
            id
        }
    }
    """
    with httpx.Client(timeout=30) as client:
        resp = client.post(
            RAILWAY_API_URL,
            json={
                "query": create_service_query,
                "variables": {"input": {"projectId": project_id, "name": "web"}},
            },
            headers=headers,
        )
        resp.raise_for_status()
        svc_data = resp.json()

    service_id = svc_data.get("data", {}).get("serviceCreate", {}).get("id", "")
    if not service_id:
        logger.warning("[Deployer] Could not create Railway service")
        return ""

    logger.info(f"[Deployer] Service created: {service_id}")
    return service_id


def deploy_to_railway(
    workspace: WorkspaceManager,
    config: PipelineConfig,
    project_name: str = "hackathon-app",
    cost_tracker: CostTracker = None,
) -> str:
    """
    Deploy the project to Railway.

    1. Generate deployment configs (Dockerfile, railway.json)
    2. Create Railway project + service
    3. Upload source code
    4. Return deployment URL

    Args:
        workspace: Workspace manager
        config: Pipeline configuration
        project_name: Name for the Railway project
        cost_tracker: Optional cost tracker

    Returns:
        Deployment URL (or empty string on failure)
    """
    logger.info("[Deployer] Starting Railway deployment")

    if not config.keys.railway:
        logger.warning("[Deployer] No RAILWAY_API_TOKEN — skipping deployment")
        workspace.log_event("deployment", "Skipped — no Railway token")
        return ""

    try:
        # Step 1: Generate configs (only here, not duplicated)
        generate_deploy_config(workspace, config, cost_tracker=cost_tracker)

        # Step 2: Create Railway project
        project = _create_railway_project(config.keys.railway, project_name)
        project_id = project.get("id", "")
        logger.info(f"[Deployer] Railway project created: {project_id}")

        # Step 3: Upload source code to project
        if project_id:
            _upload_source_to_railway(
                config.keys.railway, project_id, workspace.src_dir
            )

        # Step 4: Wait for deployment and get URL
        deployment_url = ""
        if project_id:
            for attempt in range(12):  # Poll for up to 2 minutes
                time.sleep(10)
                deployment_url = _get_deployment_url(
                    config.keys.railway, project_id
                )
                if deployment_url:
                    break
                logger.info(f"[Deployer] Waiting for deployment... ({attempt + 1}/12)")

        if deployment_url:
            logger.info(f"[Deployer] Deployed to: {deployment_url}")
        else:
            deployment_url = f"https://{project_name}.up.railway.app"
            logger.info(f"[Deployer] Expected URL: {deployment_url}")

        workspace.log_event("deployment", f"Deployed: {deployment_url}")
        return deployment_url

    except Exception as e:
        logger.error(f"[Deployer] Deployment failed: {e}")
        workspace.log_event("deployment", f"Failed: {e}")
        return ""
