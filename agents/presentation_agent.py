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
    Fallback: generate a self-contained Reveal.js HTML presentation
    when Gamma API key is not available.
    Opens in any browser as a real slide deck.
    """
    logger.info("[Presentation] Generating Reveal.js HTML fallback deck")

    title = pitch_data.get('title', 'Hackathon Pitch')
    subtitle = pitch_data.get('subtitle', '')
    slides = pitch_data.get("slides", [])

    # Build individual slide HTML
    slides_html = []

    # Title slide
    slides_html.append(f"""
      <section data-background-gradient="linear-gradient(135deg, #667eea 0%, #764ba2 100%)">
        <h1 style="font-size:2.5em;text-shadow:2px 2px 10px rgba(0,0,0,0.3)">{title}</h1>
        <p style="font-size:1.3em;opacity:0.9">{subtitle}</p>
      </section>""")

    # Content slides
    for slide in slides:
        headline = slide.get('headline', slide.get('title', ''))
        body = slide.get('body', '').replace('\n', '<br>')
        bullets = slide.get('bullets', [])

        bullet_html = ""
        if bullets:
            items = "".join(f"<li>{b}</li>" for b in bullets)
            bullet_html = f'<ul style="text-align:left;font-size:0.85em">{items}</ul>'

        # Alternate gradient backgrounds for visual variety
        slide_num = slide.get('slide_number', 1)
        gradients = [
            "linear-gradient(135deg, #0f0c29, #302b63, #24243e)",
            "linear-gradient(135deg, #1a1a2e, #16213e, #0f3460)",
            "linear-gradient(135deg, #0d1117, #161b22, #21262d)",
            "linear-gradient(135deg, #1e1e2e, #2d2d44, #1e1e2e)",
        ]
        bg = gradients[(slide_num - 1) % len(gradients)]

        slides_html.append(f"""
      <section data-background="{bg}">
        <h2 style="color:#7c3aed;margin-bottom:0.5em">{headline}</h2>
        <p style="font-size:0.9em;line-height:1.6">{body}</p>
        {bullet_html}
      </section>""")

    all_slides = "\n".join(slides_html)

    # Inject screenshot slides if available
    screenshots_dir = workspace.output_dir / "screenshots"
    if screenshots_dir.exists():
        import base64
        screenshot_files = sorted(screenshots_dir.glob("*.png"))[:3]
        if screenshot_files:
            screenshot_imgs = ""
            for ss in screenshot_files:
                try:
                    b64 = base64.b64encode(ss.read_bytes()).decode()
                    screenshot_imgs += (
                        f'<img src="data:image/png;base64,{b64}" '
                        f'style="max-width:80%;border-radius:12px;box-shadow:0 8px 32px rgba(0,0,0,0.4);margin:10px auto;display:block">'
                    )
                except Exception:
                    continue

            if screenshot_imgs:
                demo_slide = f"""
      <section data-background="linear-gradient(135deg, #0f0c29, #302b63, #24243e)">
        <h2 style="color:#7c3aed;margin-bottom:0.5em">🖥️ Live Demo</h2>
        {screenshot_imgs}
      </section>"""
                # Insert after the 3rd content slide (Solution slide)
                slide_list = all_slides.split("</section>")
                insert_pos = min(3, len(slide_list) - 1)
                slide_list.insert(insert_pos, demo_slide.rstrip("</section>"))
                all_slides = "</section>".join(slide_list)
                logger.info(f"[Presentation] Injected {len(screenshot_files)} screenshots into deck")

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{title}</title>
  <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/reveal.js@5.1.0/dist/reveal.css">
  <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/reveal.js@5.1.0/dist/theme/night.css">
  <style>
    .reveal h1, .reveal h2, .reveal h3 {{ font-family: 'Inter', 'Segoe UI', sans-serif; }}
    .reveal p, .reveal li {{ font-family: 'Inter', 'Segoe UI', sans-serif; }}
    .reveal ul {{ list-style: none; padding-left: 0; }}
    .reveal ul li::before {{ content: "→ "; color: #7c3aed; font-weight: bold; }}
    .reveal ul li {{ margin-bottom: 0.5em; }}
    .reveal section {{ text-align: left; padding: 40px; }}
  </style>
</head>
<body>
  <div class="reveal">
    <div class="slides">
{all_slides}
    </div>
  </div>
  <script src="https://cdn.jsdelivr.net/npm/reveal.js@5.1.0/dist/reveal.js"></script>
  <script>
    Reveal.initialize({{
      hash: true,
      transition: 'slide',
      backgroundTransition: 'fade',
      controls: true,
      progress: true,
    }});
  </script>
</body>
</html>"""

    html_path = workspace.write_file("output", "pitch_deck.html", html)

    # Also save markdown version for reference
    slides_md = [f"---\ntitle: {title}\n---\n"]
    for slide in slides:
        slide_content = f"# {slide.get('title', 'Slide')}\n\n"
        slide_content += f"## {slide.get('headline', '')}\n\n"
        slide_content += slide.get("body", "") + "\n\n"
        if slide.get("bullets"):
            for b in slide["bullets"]:
                slide_content += f"- {b}\n"
        slides_md.append(slide_content)
    md_path = workspace.write_file("output", "pitch_deck.md", "\n---\n\n".join(slides_md))

    return {
        "deck_url": "",
        "local_path": str(html_path),
        "status": "completed (Reveal.js HTML)",
    }
