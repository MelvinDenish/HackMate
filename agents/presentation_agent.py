"""
╔══════════════════════════════════════════════════════════════╗
║  PRESENTATION AGENT — Phase 4: Gamma API Rendering           ║
║                                                              ║
║  🟢 LLM: Moonshot Kimi k2                                    ║
║  Why: Multimodal understanding for visual content design,    ║
║       excellent at structuring visual layouts                ║
║                                                              ║
║  Workflow:                                                   ║
║  1. Read pitch content JSON                                  ║
║  2. Transform into Gamma API-optimized prompt                ║
║  3. Call Gamma API to generate + export presentation         ║
║  4. Download final PPTX/PDF to /workspace/output/            ║
╚══════════════════════════════════════════════════════════════╝
"""

import json
import logging

from langchain_core.messages import SystemMessage, HumanMessage

from agents.llm_factory import create_llm, invoke_with_retry
from config import PipelineConfig
from tools.presentation import GammaPresentation
from workspace.manager import WorkspaceManager

logger = logging.getLogger(__name__)

PRESENTATION_SYSTEM_PROMPT = """You are the Presentation Agent. Transform structured pitch content into a polished presentation prompt optimized for the Gamma API.

## Your Task
Given the pitch slides JSON, create a comprehensive presentation prompt that will produce a stunning, professional deck via Gamma.

## Requirements
- Incorporate ALL slide content from the pitch JSON
- Add visual direction: suggest color schemes, image types, layout styles
- Ensure the narrative flows smoothly between slides
- Include data visualizations where numbers are mentioned
- Make it visually dynamic — not boring bullet points

## Output Format
Return a JSON object:
```json
{
  "topic": "Presentation title",
  "num_cards": 8,
  "prompt": "Detailed prompt for Gamma API including all slide content, visual directions, and design preferences",
  "text_mode": "concise"
}
```

The prompt field should be a single, comprehensive string that tells Gamma exactly what to generate for each slide."""


def run_presentation(
    pitch_content_path: str,
    workspace: WorkspaceManager,
    config: PipelineConfig,
    cost_tracker=None,
) -> dict:
    """
    Generate the final presentation via Gamma API.

    1. Read pitch content
    2. Use Kimi k2 to optimize the content for Gamma
    3. Call Gamma API to generate the deck
    4. Export and download

    Args:
        pitch_content_path: Path to pitch content JSON
        workspace: Workspace manager
        config: Pipeline configuration

    Returns:
        Dict with: deck_url, local_path, status
    """
    logger.info("[Presentation] Starting deck generation")

    # Step 1: Read pitch content
    pitch_data = workspace.read_json(pitch_content_path)
    pitch_json_str = json.dumps(pitch_data, indent=2)

    # Step 2: Use Kimi k2 to create optimized Gamma prompt
    spec = config.get_model("presentation")
    llm = create_llm(spec, config.keys)

    messages = [
        SystemMessage(content=PRESENTATION_SYSTEM_PROMPT),
        HumanMessage(content=(
            f"## Pitch Content\n```json\n{pitch_json_str}\n```\n\n"
            "Transform this into an optimized Gamma API prompt. "
            "Return ONLY valid JSON."
        )),
    ]

    response = invoke_with_retry(
        llm, messages,
        spec=spec,
        agent_name="presentation",
        phase="presentation",
        cost_tracker=cost_tracker,
    )
    content = response.content.strip()

    # Parse Gamma parameters
    try:
        if "```" in content:
            parts = content.split("```")
            for part in parts[1:]:
                cleaned = part.strip()
                if cleaned.startswith("json"):
                    cleaned = cleaned[4:].strip()
                try:
                    gamma_params = json.loads(cleaned)
                    break
                except json.JSONDecodeError:
                    continue
            else:
                gamma_params = json.loads(content)
        else:
            gamma_params = json.loads(content)
    except json.JSONDecodeError:
        logger.warning("[Presentation] Failed to parse Kimi output, using defaults")
        gamma_params = {
            "topic": pitch_data.get("title", "Hackathon Pitch"),
            "num_cards": len(pitch_data.get("slides", [])) + 1,
            "prompt": pitch_json_str[:3000],
            "text_mode": "concise",
        }

    # Step 3: Call Gamma API
    if not config.keys.gamma:
        logger.warning("[Presentation] No GAMMA_API_KEY — generating markdown fallback")
        return _generate_markdown_fallback(pitch_data, workspace)

    gamma = GammaPresentation(api_key=config.keys.gamma)
    result = gamma.generate_full_deck(
        topic=gamma_params.get("topic", "Hackathon Pitch"),
        content_prompt=gamma_params.get("prompt", ""),
        num_cards=gamma_params.get("num_cards", 8),
        text_mode=gamma_params.get("text_mode", "concise"),
        export_format="pptx",
        save_dir=str(workspace.output_dir),
    )

    workspace.log_event(
        "presentation",
        f"Deck generated: {result.get('status', 'unknown')}",
        f"URL: {result.get('export_url', 'N/A')}"
    )

    logger.info(f"[Presentation] Result: {result.get('status')}")
    return result


def _generate_markdown_fallback(pitch_data: dict,
                                 workspace: WorkspaceManager) -> dict:
    """
    Fallback: generate a Markdown-based presentation file
    when Gamma API key is not available.
    """
    logger.info("[Presentation] Generating markdown fallback deck")

    slides_md = [f"---\ntitle: {pitch_data.get('title', 'Pitch')}\n---\n"]

    for slide in pitch_data.get("slides", []):
        slide_content = f"# {slide.get('title', 'Slide')}\n\n"
        slide_content += f"## {slide.get('headline', '')}\n\n"
        slide_content += slide.get("body", "") + "\n\n"

        if slide.get("bullets"):
            for b in slide["bullets"]:
                slide_content += f"- {b}\n"

        slides_md.append(slide_content)

    full_md = "\n---\n\n".join(slides_md)
    md_path = workspace.write_file("output", "pitch_deck.md", full_md)

    return {
        "deck_url": "",
        "local_path": str(md_path),
        "status": "completed (markdown fallback)",
    }
