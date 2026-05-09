"""
╔══════════════════════════════════════════════════════════════╗
║  PITCH AGENT — Phase 4: Narrative Construction               ║
║                                                              ║
║  🟣 LLM: Anthropic Claude Sonnet 4                           ║
║  Why: Best at persuasive, structured narrative writing.      ║
║       Produces VC-quality pitch content.                     ║
║                                                              ║
║  Inputs:  Dossier + PRD + deployment URL                     ║
║  Outputs: Structured pitch content (JSON) → /workspace/output║
║                                                              ║
║  Follows the 7-Slide Framework from the research paper:      ║
║  1. Attention Grabber  5. Go-to-Market                       ║
║  2. Problem Statement  6. Financial Projections              ║
║  3. Solution & Edge    7. The Ask                            ║
║  4. Market Sizing                                            ║
╚══════════════════════════════════════════════════════════════╝
"""

import json
import logging
from datetime import datetime, timezone

from langchain_core.messages import SystemMessage, HumanMessage

from agents.llm_factory import create_llm, invoke_with_retry
from config import PipelineConfig
from workspace.manager import WorkspaceManager

logger = logging.getLogger(__name__)

PITCH_SYSTEM_PROMPT = """You are the Pitch Agent in an autonomous hackathon pipeline. You are a hybrid venture capital strategist and master storyteller. Your job is to create a competition-winning pitch narrative.

## Your Mandate
Transform the technical project into a compelling story that will WOW hackathon judges. You must think like a VC evaluator, assessing:
- Strategic framing and narrative hook
- Value proposition clarity
- Innovation edge over existing solutions
- Market relevance and timing
- Execution realism and demo quality

## The 7-Slide Framework (REQUIRED)
Generate content for exactly 7 slides:

### Slide 1: The Hook (Attention Grabber)
- A shocking statistic, provocative question, or compelling scenario
- Must grab attention in the first 3 seconds
- Connect emotionally to the problem

### Slide 2: The Problem
- Clear articulation of the user pain point
- Use specific personas and scenarios
- Quantify the cost of the problem (time, money, frustration)

### Slide 3: The Solution & Differentiator
- What we built and HOW it solves the problem
- Live demo preview / key screenshots description
- What makes this different from existing solutions (competitive edge)

### Slide 4: Market Opportunity
- TAM / SAM / SOM with cited numbers from research
- Growth trends and timing ("why now?")
- Target segment definition

### Slide 5: Go-to-Market Strategy
- User acquisition channels
- Growth flywheel / viral mechanics
- Partnership opportunities

### Slide 6: Business Model & Projections
- Revenue model (freemium, SaaS, marketplace, etc.)
- Unit economics (even rough estimates)
- 12-month projection

### Slide 7: The Ask
- What do we need? (Prize money allocation, mentorship, partnerships)
- 90-day roadmap after the hackathon
- Team strengths and commitment
- Closing hook / call to action

## Output Format (STRICT JSON)
Return a JSON object with this structure:
```json
{
  "title": "Presentation Title",
  "subtitle": "One-line tagline",
  "slides": [
    {
      "slide_number": 1,
      "title": "Slide Title",
      "type": "hook|problem|solution|market|gtm|business|ask",
      "headline": "Bold headline for the slide",
      "body": "Main content (2-4 paragraphs)",
      "bullets": ["Key point 1", "Key point 2"],
      "speaker_notes": "What the presenter should say",
      "visual_suggestion": "Description of ideal visual/image for this slide"
    }
  ],
  "metadata": {
    "target_duration": "5 minutes",
    "tone": "confident, data-driven, inspiring"
  }
}
```

Be specific, data-driven, and compelling. Every claim should reference the research dossier."""


def run_pitch(
    dossier_path: str,
    prd_path: str,
    deployment_url: str,
    workspace: WorkspaceManager,
    config: PipelineConfig,
    cost_tracker=None,
) -> tuple[str, list[dict]]:
    """
    Generate the pitch deck content.

    Args:
        dossier_path: Path to research dossier
        prd_path: Path to PRD
        deployment_url: Live application URL
        workspace: Workspace manager
        config: Pipeline configuration

    Returns:
        Tuple of (pitch_content_path, slides_list)
    """
    logger.info("[Pitch] Generating pitch narrative")

    # Read source documents (fail-forward: proceed with whatever is available)
    dossier = ""
    try:
        if dossier_path:
            dossier = workspace.read_file(dossier_path)
    except Exception as e:
        logger.warning(f"[Pitch] Dossier unavailable ({e}), proceeding without")

    prd = ""
    try:
        if prd_path:
            prd = workspace.read_file(prd_path)
    except Exception as e:
        logger.warning(f"[Pitch] PRD unavailable ({e}), proceeding without")

    # Query Knowledge Base for market data (TAM, competitors, trends)
    kb_context = ""
    try:
        from agents.knowledge_base import KnowledgeBase
        kb = KnowledgeBase(config)
        kb_context = kb.get_context_for_prompt(
            "market size TAM SAM competitors business model revenue",
            n_results=5,
            max_chars=3000,
        )
        if kb_context and "No relevant context" not in kb_context:
            logger.info(f"[Pitch] KB returned market context ({len(kb_context)} chars)")
        else:
            kb_context = ""
    except Exception as e:
        logger.warning(f"[Pitch] KB query failed ({e}), continuing without")

    spec = config.get_model("pitch")
    llm = create_llm(spec, config.keys)

    # Build rich context with ALL available information
    prompt_parts = []
    if dossier:
        prompt_parts.append(f"## Market Research Dossier\n\n{dossier}\n\n")
    if kb_context:
        prompt_parts.append(f"## Additional Market Data (from Knowledge Base)\n\n{kb_context}\n\n")
    if prd:
        prompt_parts.append(f"## Product Requirements Document\n\n{prd}\n\n")
    prompt_parts.append(
        f"## Live Demo URL\n{deployment_url or '(deployment pending)'}\n\n"
        "Create the 7-slide pitch deck content. Return ONLY valid JSON."
    )

    messages = [
        SystemMessage(content=PITCH_SYSTEM_PROMPT),
        HumanMessage(content="".join(prompt_parts)),
    ]

    response = invoke_with_retry(
        llm, messages,
        spec=spec,
        agent_name="pitch",
        phase="pitch",
        cost_tracker=cost_tracker,
    )
    content = response.content.strip()

    # Parse JSON
    try:
        if "```" in content:
            parts = content.split("```")
            for part in parts[1:]:
                cleaned = part.strip()
                if cleaned.startswith("json"):
                    cleaned = cleaned[4:].strip()
                try:
                    pitch_data = json.loads(cleaned)
                    break
                except json.JSONDecodeError:
                    continue
            else:
                pitch_data = json.loads(content)
        else:
            pitch_data = json.loads(content)
    except json.JSONDecodeError:
        logger.warning("[Pitch] Failed to parse pitch JSON, wrapping as-is")
        pitch_data = {
            "title": "Hackathon Pitch",
            "subtitle": "",
            "slides": [],
            "raw_content": content,
        }

    # Add metadata
    pitch_data["generated_at"] = datetime.now(timezone.utc).isoformat()
    pitch_data["pitch_model"] = "Claude Sonnet 4 (Anthropic)"
    if deployment_url:
        pitch_data["demo_url"] = deployment_url

    # Save to workspace
    pitch_path = workspace.write_json("output", "pitch_content.json", pitch_data)

    # Also save as readable markdown
    md_content = _pitch_to_markdown(pitch_data)
    workspace.write_file("output", "pitch_narrative.md", md_content)

    slides = pitch_data.get("slides", [])
    workspace.log_event("pitch", f"Generated {len(slides)} slides", str(pitch_path))
    logger.info(f"[Pitch] Saved {len(slides)} slides to: {pitch_path}")

    return str(pitch_path), slides


def _pitch_to_markdown(pitch_data: dict) -> str:
    """Convert pitch JSON to readable markdown."""
    lines = [
        f"# {pitch_data.get('title', 'Pitch Deck')}",
        f"*{pitch_data.get('subtitle', '')}*\n",
        "---\n",
    ]

    for slide in pitch_data.get("slides", []):
        lines.append(f"## Slide {slide.get('slide_number', '?')}: {slide.get('title', '')}")
        lines.append(f"**{slide.get('headline', '')}**\n")
        lines.append(slide.get("body", ""))
        if slide.get("bullets"):
            lines.append("")
            for bullet in slide["bullets"]:
                lines.append(f"- {bullet}")
        if slide.get("speaker_notes"):
            lines.append(f"\n> 🎤 *Speaker Notes: {slide['speaker_notes']}*")
        lines.append("\n---\n")

    return "\n".join(lines)
