"""
╔══════════════════════════════════════════════════════════════╗
║  SCREENSHOT CAPTURE — Headless Browser Screenshots           ║
║                                                              ║
║  Inspired by: Lovable.dev (auto-screenshots for pitch)       ║
║                                                              ║
║  Uses Playwright (headless) to capture screenshots of the    ║
║  deployed app. Screenshots feed into the pitch deck and      ║
║  README generation for professional presentation.            ║
║                                                              ║
║  Fallback: If Playwright is not installed, generates         ║
║  placeholder image descriptions instead.                     ║
╚══════════════════════════════════════════════════════════════╝
"""

import asyncio
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


async def capture_screenshots(
    url: str,
    output_dir: Path,
    pages: list[str] = None,
    viewport_width: int = 1280,
    viewport_height: int = 720,
    wait_ms: int = 3000,
) -> list[dict]:
    """Capture screenshots of a web application using headless Playwright.

    Args:
        url: Base URL of the application
        output_dir: Directory to save screenshots
        pages: List of paths to capture (e.g. ["/", "/dashboard", "/settings"])
        viewport_width: Browser viewport width
        viewport_height: Browser viewport height
        wait_ms: Milliseconds to wait after navigation before screenshot

    Returns:
        List of dicts: {"path": "/", "file": "screenshot_home.png", "full_path": "..."}
    """
    if pages is None:
        pages = ["/"]

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    try:
        from playwright.async_api import async_playwright
    except ImportError:
        logger.warning(
            "[Screenshots] Playwright not installed. "
            "Install with: pip install playwright && python -m playwright install chromium"
        )
        return _generate_placeholder_descriptions(url, pages, output_dir)

    screenshots = []

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(
                viewport={"width": viewport_width, "height": viewport_height},
                device_scale_factor=2,  # Retina quality
            )
            page = await context.new_page()

            for i, path in enumerate(pages):
                try:
                    full_url = f"{url.rstrip('/')}{path}"
                    slug = path.strip("/").replace("/", "_") or "home"
                    filename = f"screenshot_{slug}.png"
                    filepath = output_dir / filename

                    logger.info(f"[Screenshots] Capturing {full_url}")
                    await page.goto(full_url, wait_until="networkidle", timeout=15000)

                    # Wait for dynamic content to render
                    await page.wait_for_timeout(wait_ms)

                    await page.screenshot(path=str(filepath), full_page=False)

                    screenshots.append({
                        "path": path,
                        "file": filename,
                        "full_path": str(filepath),
                        "url": full_url,
                    })
                    logger.info(f"[Screenshots] Saved: {filename}")

                except Exception as e:
                    logger.warning(f"[Screenshots] Failed to capture {path}: {e}")

            await browser.close()

    except Exception as e:
        logger.error(f"[Screenshots] Browser error: {e}")
        return _generate_placeholder_descriptions(url, pages, output_dir)

    logger.info(f"[Screenshots] Captured {len(screenshots)} screenshots")
    return screenshots


def capture_screenshots_sync(
    url: str,
    output_dir: Path,
    pages: list[str] = None,
) -> list[dict]:
    """Synchronous wrapper for capture_screenshots."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            # Can't nest event loops — use thread
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as pool:
                future = pool.submit(
                    asyncio.run,
                    capture_screenshots(url, output_dir, pages)
                )
                return future.result(timeout=60)
        else:
            return asyncio.run(capture_screenshots(url, output_dir, pages))
    except RuntimeError:
        return asyncio.run(capture_screenshots(url, output_dir, pages))


def _generate_placeholder_descriptions(
    url: str, pages: list[str], output_dir: Path
) -> list[dict]:
    """Generate text descriptions when Playwright is not available.
    These descriptions can be used by the pitch agent to describe visuals."""
    descriptions = []
    for path in pages:
        slug = path.strip("/").replace("/", "_") or "home"
        desc_file = output_dir / f"screenshot_{slug}.txt"
        content = (
            f"Screenshot Placeholder: {url}{path}\n"
            f"Description: Screenshot of the application at route '{path}'.\n"
            f"Use this to describe the visual appearance in the pitch deck.\n"
        )
        desc_file.write_text(content, encoding="utf-8")
        descriptions.append({
            "path": path,
            "file": f"screenshot_{slug}.txt",
            "full_path": str(desc_file),
            "url": f"{url.rstrip('/')}{path}",
            "placeholder": True,
        })

    logger.info(f"[Screenshots] Generated {len(descriptions)} placeholder descriptions")
    return descriptions
