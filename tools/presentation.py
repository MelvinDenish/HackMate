"""
╔══════════════════════════════════════════════════════════════╗
║  GAMMA API — Presentation Rendering Engine                   ║
║  Used by: Presentation Agent (Phase 4)                       ║
║  Provider: Gamma (https://developers.gamma.app/)             ║
║                                                              ║
║  Workflow:                                                   ║
║  1. Generate content outline via Gamma API                   ║
║  2. Create presentation from generated content               ║
║  3. Poll for completion                                      ║
║  4. Export as PPTX/PDF                                       ║
╚══════════════════════════════════════════════════════════════╝
"""

import logging
import time
from typing import Optional

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

logger = logging.getLogger(__name__)

GAMMA_API_BASE = "https://api.gamma.app"


class GammaPresentation:
    """Client for the Gamma API to generate and export presentations."""

    def __init__(self, api_key: str):
        self.api_key = api_key
        self.headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=15))
    def generate_outline(self, topic: str, num_cards: int = 8,
                         outline_prompt: str = "") -> dict:
        """
        Step 1: Generate a content outline for the presentation.

        Args:
            topic: Main presentation topic/title
            num_cards: Number of slides to generate
            outline_prompt: Optional detailed instructions

        Returns:
            Dict with outline data including card_id for next step
        """
        logger.info(f"[Gamma] Generating outline for: {topic}")

        payload = {
            "topic": topic,
            "num_cards": num_cards,
        }
        if outline_prompt:
            payload["prompt"] = outline_prompt

        with httpx.Client(timeout=60) as client:
            resp = client.post(
                f"{GAMMA_API_BASE}/api/generate/outline",
                json=payload,
                headers=self.headers,
            )
            resp.raise_for_status()

        data = resp.json()
        logger.info(f"[Gamma] Outline generated with {num_cards} cards")
        return data

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=15))
    def create_presentation(self, outline_data: dict,
                            text_mode: str = "concise",
                            image_mode: str = "ai") -> dict:
        """
        Step 2: Create presentation from outline.

        Args:
            outline_data: Outline from generate_outline()
            text_mode: "concise" | "detailed" | "bullets"
            image_mode: "ai" | "web" | "none"

        Returns:
            Dict with presentation_id and status
        """
        logger.info("[Gamma] Creating presentation from outline...")

        payload = {
            "outline": outline_data,
            "text_mode": text_mode,
            "image_mode": image_mode,
        }

        with httpx.Client(timeout=90) as client:
            resp = client.post(
                f"{GAMMA_API_BASE}/api/generate/presentation",
                json=payload,
                headers=self.headers,
            )
            resp.raise_for_status()

        data = resp.json()
        logger.info(f"[Gamma] Presentation creation initiated")
        return data

    def poll_status(self, presentation_id: str,
                    max_wait: int = 300, poll_interval: int = 5) -> dict:
        """
        Poll until presentation generation completes.

        Args:
            presentation_id: ID from create_presentation()
            max_wait: Maximum seconds to wait
            poll_interval: Seconds between status checks

        Returns:
            Final presentation data with export URLs
        """
        logger.info(f"[Gamma] Polling status for: {presentation_id}")
        start = time.time()

        while (time.time() - start) < max_wait:
            try:
                with httpx.Client(timeout=30) as client:
                    resp = client.get(
                        f"{GAMMA_API_BASE}/api/presentations/{presentation_id}",
                        headers=self.headers,
                    )
                    resp.raise_for_status()
                    data = resp.json()

                status = data.get("status", "unknown")
                if status == "completed":
                    logger.info("[Gamma] Presentation ready!")
                    return data
                elif status == "failed":
                    logger.error(f"[Gamma] Generation failed: {data}")
                    return data

                logger.info(f"[Gamma] Status: {status}, waiting...")
                time.sleep(poll_interval)

            except Exception as e:
                logger.warning(f"[Gamma] Poll error: {e}")
                time.sleep(poll_interval)

        logger.error("[Gamma] Timed out waiting for presentation")
        return {"status": "timeout", "error": "Exceeded max wait time"}

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=10))
    def export_presentation(self, presentation_id: str,
                            format: str = "pptx") -> Optional[str]:
        """
        Export the presentation as PPTX or PDF.

        Args:
            presentation_id: ID of the completed presentation
            format: "pptx" or "pdf"

        Returns:
            Download URL for the exported file, or None on failure
        """
        logger.info(f"[Gamma] Exporting as {format}: {presentation_id}")

        with httpx.Client(timeout=60) as client:
            resp = client.post(
                f"{GAMMA_API_BASE}/api/presentations/{presentation_id}/export",
                json={"format": format},
                headers=self.headers,
            )
            resp.raise_for_status()
            data = resp.json()

        url = data.get("download_url", data.get("url"))
        if url:
            logger.info(f"[Gamma] Export URL: {url}")
        return url

    def download_file(self, url: str, save_path: str) -> str:
        """Download a file from URL and save locally."""
        logger.info(f"[Gamma] Downloading to: {save_path}")
        with httpx.Client(timeout=120, follow_redirects=True) as client:
            resp = client.get(url)
            resp.raise_for_status()
            with open(save_path, "wb") as f:
                f.write(resp.content)
        logger.info(f"[Gamma] Saved: {save_path}")
        return save_path

    def generate_full_deck(
        self,
        topic: str,
        content_prompt: str,
        num_cards: int = 8,
        text_mode: str = "concise",
        export_format: str = "pptx",
        save_dir: str = ".",
    ) -> dict:
        """
        End-to-end: generate outline → create deck → export.

        Args:
            topic: Presentation title
            content_prompt: Detailed prompt with slide content
            num_cards: Number of slides
            text_mode: "concise" | "detailed"
            export_format: "pptx" | "pdf"
            save_dir: Directory to save exported file

        Returns:
            Dict with: presentation_id, export_url, local_path, status
        """
        result = {
            "presentation_id": "",
            "export_url": "",
            "local_path": "",
            "status": "started",
        }

        try:
            # Step 1: Generate outline
            outline = self.generate_outline(topic, num_cards, content_prompt)
            result["status"] = "outline_generated"

            # Step 2: Create presentation
            pres_data = self.create_presentation(outline, text_mode=text_mode)
            pres_id = pres_data.get("id", pres_data.get("presentation_id", ""))
            result["presentation_id"] = pres_id
            result["status"] = "creating"

            if not pres_id:
                # Some API versions return the presentation directly
                pres_id = pres_data.get("data", {}).get("id", "")
                result["presentation_id"] = pres_id

            if pres_id:
                # Step 3: Poll for completion
                final = self.poll_status(pres_id)

                if final.get("status") == "completed":
                    # Step 4: Export
                    export_url = self.export_presentation(pres_id, export_format)
                    result["export_url"] = export_url or ""
                    result["status"] = "exported"

                    # Step 5: Download
                    if export_url:
                        filename = f"pitch_deck.{export_format}"
                        local_path = f"{save_dir}/{filename}"
                        self.download_file(export_url, local_path)
                        result["local_path"] = local_path
                        result["status"] = "completed"
                else:
                    result["status"] = f"failed: {final.get('status')}"
            else:
                # Inline generation (no async polling needed)
                result["status"] = "completed"
                result["export_url"] = pres_data.get("url", "")

        except Exception as e:
            logger.error(f"[Gamma] Full deck generation failed: {e}")
            result["status"] = f"error: {str(e)}"

        return result
