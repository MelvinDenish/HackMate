"""
╔══════════════════════════════════════════════════════════════╗
║  DEMO STORYBOARD — Auto-Generate Demo Walkthroughs           ║
║                                                              ║
║  Generates markdown storyboards and interactive HTML          ║
║  slideshows from screenshots + demo step descriptions.       ║
║                                                              ║
║  Output formats:                                             ║
║  - DEMO_STORYBOARD.md: Markdown with embedded screenshots    ║
║  - demo_walkthrough.html: Interactive HTML slideshow          ║
║                                                              ║
║  Covers 15% of hackathon judging rubric (Presentation).      ║
╚══════════════════════════════════════════════════════════════╝
"""

import logging
import shutil
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


def create_demo_storyboard(
    screenshots_dir: Path,
    demo_walkthrough: list[str],
    output_dir: Path,
) -> Optional[Path]:
    """Create a demo storyboard from screenshots + walkthrough steps.

    Generates a markdown storyboard with embedded screenshots and
    step-by-step annotations that can be used in presentations.

    Args:
        screenshots_dir: Directory containing captured screenshots
        demo_walkthrough: List of demo step descriptions
        output_dir: Where to save the storyboard

    Returns:
        Path to the generated storyboard markdown file
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    # Find all screenshots
    screenshots = sorted(
        screenshots_dir.glob("*.png"),
        key=lambda p: p.stat().st_mtime if p.exists() else 0,
    )

    if not screenshots:
        # Try WebP format
        screenshots = sorted(screenshots_dir.glob("*.webp"))

    if not screenshots:
        logger.warning("[Video] No screenshots found for storyboard")
        return None

    # Copy screenshots to output dir
    storyboard_imgs = output_dir / "storyboard_images"
    storyboard_imgs.mkdir(exist_ok=True)

    for i, ss in enumerate(screenshots):
        dest = storyboard_imgs / f"step_{i+1}{ss.suffix}"
        shutil.copy2(ss, dest)

    # Generate storyboard markdown
    storyboard_path = output_dir / "DEMO_STORYBOARD.md"
    lines = [
        "# 🎬 Demo Storyboard\n",
        "Follow this storyboard for a compelling demo presentation.\n",
        "---\n",
    ]

    for i, step in enumerate(demo_walkthrough):
        lines.append(f"## Step {i+1}: {step}\n")

        # Match screenshot to step (if available)
        if i < len(screenshots):
            img_name = f"step_{i+1}{screenshots[i].suffix}"
            lines.append(f"![Step {i+1}](storyboard_images/{img_name})\n")
        else:
            lines.append("*(screenshot not available)*\n")

        lines.append("---\n")

    # Add any extra screenshots not covered by steps
    if len(screenshots) > len(demo_walkthrough):
        lines.append("## Additional Screenshots\n")
        for i in range(len(demo_walkthrough), len(screenshots)):
            img_name = f"step_{i+1}{screenshots[i].suffix}"
            lines.append(f"![Extra {i+1}](storyboard_images/{img_name})\n")

    storyboard_path.write_text("\n".join(lines), encoding="utf-8")
    logger.info(
        f"[Video] Storyboard created: {len(demo_walkthrough)} steps, "
        f"{len(screenshots)} screenshots"
    )

    return storyboard_path


def generate_demo_html(
    demo_walkthrough: list[str],
    screenshots_dir: Path,
    output_path: Path,
) -> Optional[Path]:
    """Generate an interactive HTML demo page with auto-play slideshow.

    This creates a standalone HTML file that judges can open in a browser
    to see the demo walkthrough with auto-advancing slides.
    """
    screenshots = sorted(screenshots_dir.glob("*.png")) + sorted(screenshots_dir.glob("*.webp"))

    if not screenshots and not demo_walkthrough:
        return None

    # Build image data URIs or relative paths
    slides_html = []
    for i, step in enumerate(demo_walkthrough):
        img_tag = ""
        if i < len(screenshots):
            img_name = screenshots[i].name
            img_tag = f'<img src="screenshots/{img_name}" alt="Step {i+1}" class="slide-img">'

        slides_html.append(f"""
        <div class="slide" id="slide-{i}">
            <div class="step-number">Step {i+1}</div>
            <h2>{step}</h2>
            {img_tag}
        </div>""")

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Demo Walkthrough</title>
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{ font-family: 'Inter', system-ui, sans-serif; background: #0a0a0f; color: #e4e4e7; }}
  .container {{ max-width: 900px; margin: 0 auto; padding: 2rem; }}
  h1 {{ text-align: center; font-size: 2rem; margin-bottom: 2rem;
       background: linear-gradient(135deg, #7c3aed, #3b82f6);
       -webkit-background-clip: text; -webkit-text-fill-color: transparent; }}
  .slide {{ display: none; text-align: center; animation: fadeIn 0.5s; }}
  .slide.active {{ display: block; }}
  .slide h2 {{ font-size: 1.3rem; margin: 1rem 0; color: #a78bfa; }}
  .slide-img {{ max-width: 100%; border-radius: 12px; border: 1px solid #27272a;
                box-shadow: 0 8px 32px rgba(0,0,0,0.4); margin: 1rem 0; }}
  .step-number {{ font-size: 0.9rem; color: #6b7280; text-transform: uppercase;
                  letter-spacing: 2px; }}
  .controls {{ text-align: center; margin-top: 2rem; }}
  .controls button {{ background: #7c3aed; color: white; border: none; padding: 0.7rem 1.5rem;
                      border-radius: 8px; cursor: pointer; font-size: 1rem; margin: 0 0.5rem; }}
  .controls button:hover {{ background: #6d28d9; }}
  .progress {{ text-align: center; margin-top: 1rem; color: #6b7280; }}
  @keyframes fadeIn {{ from {{ opacity: 0; transform: translateY(10px); }}
                       to {{ opacity: 1; transform: translateY(0); }} }}
</style>
</head>
<body>
<div class="container">
  <h1>🎬 Demo Walkthrough</h1>
  {"".join(slides_html)}
  <div class="controls">
    <button onclick="prev()">← Previous</button>
    <button onclick="next()">Next →</button>
    <button onclick="toggleAuto()" id="autoBtn">▶ Auto Play</button>
  </div>
  <div class="progress" id="progress"></div>
</div>
<script>
  let current = 0;
  const slides = document.querySelectorAll('.slide');
  let autoPlay = null;
  function show(n) {{
    slides.forEach(s => s.classList.remove('active'));
    current = (n + slides.length) % slides.length;
    slides[current].classList.add('active');
    document.getElementById('progress').textContent = `${{current+1}} / ${{slides.length}}`;
  }}
  function next() {{ show(current + 1); }}
  function prev() {{ show(current - 1); }}
  function toggleAuto() {{
    if (autoPlay) {{ clearInterval(autoPlay); autoPlay = null;
      document.getElementById('autoBtn').textContent = '▶ Auto Play';
    }} else {{ autoPlay = setInterval(next, 3000);
      document.getElementById('autoBtn').textContent = '⏸ Pause';
    }}
  }}
  show(0);
</script>
</body>
</html>"""

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(html, encoding="utf-8")
    logger.info(f"[Video] Interactive demo HTML: {output_path}")
    return output_path
