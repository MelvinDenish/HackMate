"""
╔══════════════════════════════════════════════════════════════╗
║  RESEARCH AGENT — Phase 1: Market Research & Ideation        ║
║                                                              ║
║  🔵 LLM: Google Gemini 2.5 Flash                             ║
║  Why: Fast, cheap, excellent at synthesizing web search      ║
║       results. Handles high-volume research efficiently.     ║
║                                                              ║
║  Purpose:                                                    ║
║  Conducts exhaustive market research using DuckDuckGo and    ║
║  URL scraping. Produces a structured Markdown "Dossier"      ║
║  saved to /workspace/briefs/ for downstream agents.          ║
║                                                              ║
║  Outputs:                                                    ║
║  • Market Dossier (Markdown) → /workspace/briefs/dossier.md  ║
║  • Knowledge base entries → ChromaDB                         ║
╚══════════════════════════════════════════════════════════════╝
"""

import json
import logging
from datetime import datetime, timezone

from langchain_core.messages import SystemMessage, HumanMessage

from agents.llm_factory import create_llm, invoke_with_retry
from config import PipelineConfig
from tools.web_search import research_topic, search_web
from workspace.manager import WorkspaceManager

logger = logging.getLogger(__name__)

RESEARCH_SYSTEM_PROMPT = """You are the Research Agent in an autonomous hackathon pipeline. Your role is to produce a comprehensive Market Dossier that grounds the entire project in reality.

## Your Mission
Using the provided web search results and page content, synthesize a thorough research dossier covering:

1. **Problem Validation**: Is this a real problem? Who suffers from it? How big is the pain?
2. **Market Landscape**: Market size, growth trends, key players, funding activity
3. **Competitive Analysis**: Existing solutions, their strengths/weaknesses, gaps in the market
4. **Technology Landscape**: Relevant technologies, APIs, frameworks commonly used
5. **User Insights**: Target user behaviors, preferences, pain points
6. **Opportunity Assessment**: Where is the whitespace? What angle is underserved?
7. **Recommended Technical Approach**: Based on research, what tech stack and architecture makes sense?
8. **Key Risks**: What could go wrong? Technical/market/regulatory risks?

## Critical Rules
- ONLY use information from the provided search results and fetched pages
- CITE sources with URLs for every claim
- If data is insufficient, say so — NEVER fabricate statistics
- Be specific with numbers: market sizes, user counts, growth rates
- Focus on actionable insights that will impact the hackathon project

## Output Format
Write as a structured Markdown document with clear headers, bullet points, and source citations.
Every factual claim MUST have a [Source](url) citation."""

QUERY_GEN_PROMPT = """Given the following project brief, generate 6-8 specific web search queries that will help research:
1. The market opportunity and size
2. Existing competitors and solutions
3. Target audience demographics and pain points
4. Relevant technology stacks and APIs
5. Recent trends and innovations in this space
6. Potential business models

Project Brief:
{brief}

Return ONLY a JSON array of search query strings. No explanation."""


def generate_search_queries(
    brief: str, config: PipelineConfig, cost_tracker=None,
) -> list[str]:
    """Use the LLM to generate targeted search queries from the brief."""
    spec = config.get_model("research")
    llm = create_llm(spec, config.keys)

    messages = [
        HumanMessage(content=QUERY_GEN_PROMPT.format(brief=brief))
    ]

    response = invoke_with_retry(
        llm, messages,
        spec=spec,
        agent_name="research",
        phase="research",
        cost_tracker=cost_tracker,
    )
    content = response.content.strip()

    # Parse JSON array from response
    try:
        # Handle markdown code blocks
        if "```" in content:
            content = content.split("```")[1]
            if content.startswith("json"):
                content = content[4:]
        queries = json.loads(content)
        if isinstance(queries, list):
            return queries[:8]
    except (json.JSONDecodeError, IndexError):
        logger.warning("[Research] Failed to parse query JSON, using defaults")

    # Fallback: generate basic queries from the brief
    return [
        brief,
        f"{brief} market size 2025 2026",
        f"{brief} competitors",
        f"{brief} technology stack",
    ]


def run_research(
    refined_brief: str,
    workspace: WorkspaceManager,
    config: PipelineConfig,
    cost_tracker=None,
) -> str:
    """
    Execute the full research pipeline.

    1. Generate targeted search queries from the brief
    2. Execute web searches
    3. Fetch top result pages
    4. Synthesize into a structured Dossier
    5. Save to workspace

    Args:
        refined_brief: The Refined Brief from Phase 0
        workspace: Workspace manager for file I/O
        config: Pipeline configuration

    Returns:
        Path to the saved dossier file
    """
    logger.info("[Research] Starting market research phase")

    # Step 1: Generate search queries
    queries = generate_search_queries(refined_brief, config, cost_tracker=cost_tracker)
    logger.info(f"[Research] Generated {len(queries)} search queries")

    # Step 2: Execute research (Exa first if available, then DuckDuckGo)
    all_search_results = []
    all_fetched_pages = []

    # Try Exa AI search (higher quality, if API key available)
    exa_used = False
    if config.keys.exa:
        try:
            from tools.exa_search import ExaSearch
            exa = ExaSearch(api_key=config.keys.exa)
            for query in queries:
                exa_results = exa.search(query, num_results=5)
                all_search_results.extend(exa_results)
            exa_used = True
            logger.info(f"[Research] Exa returned {len(all_search_results)} results")

            # Use Firecrawl for deep page scraping if available
            if config.keys.firecrawl:
                from tools.exa_search import FirecrawlScraper
                scraper = FirecrawlScraper(api_key=config.keys.firecrawl)
                top_urls = [r["url"] for r in all_search_results[:5] if r.get("url")]
                for url in top_urls:
                    page = scraper.scrape(url)
                    if page.get("success"):
                        all_fetched_pages.append(page)
                logger.info(f"[Research] Firecrawl scraped {len(all_fetched_pages)} pages")
        except Exception as e:
            logger.warning(f"[Research] Exa/Firecrawl failed, falling back to DuckDuckGo: {e}")
            exa_used = False

    # Fallback: DuckDuckGo (always works, no API key needed)
    if not exa_used:
        for query in queries:
            data = research_topic(
                topic=query,
                search_queries=[query],
                max_results_per_query=5,
                fetch_top_n=2,
            )
            all_search_results.extend(data["search_results"])
            all_fetched_pages.extend(data["fetched_pages"])

    # Deduplicate by URL
    seen = set()
    unique_results = []
    for r in all_search_results:
        url = r.get("url", "")
        if url and url not in seen:
            seen.add(url)
            unique_results.append(r)

    logger.info(
        f"[Research] Gathered {len(unique_results)} unique results, "
        f"fetched {len(all_fetched_pages)} pages"
        f" (source: {'Exa' if exa_used else 'DuckDuckGo'})"
    )

    # Step 3: Format research data for the LLM
    research_context = "## Web Search Results\n\n"
    for i, r in enumerate(unique_results[:20], 1):
        research_context += (
            f"### Result {i}\n"
            f"- **Title**: {r['title']}\n"
            f"- **URL**: {r['url']}\n"
            f"- **Snippet**: {r['snippet']}\n\n"
        )

    research_context += "\n## Fetched Page Contents\n\n"
    for i, page in enumerate(all_fetched_pages[:8], 1):
        research_context += (
            f"### Page {i}: {page['title']}\n"
            f"- **URL**: {page['url']}\n"
            f"- **Content**:\n{page['content'][:3000]}\n\n"
        )

    # Step 4: Synthesize with LLM
    spec = config.get_model("research")
    llm = create_llm(spec, config.keys)

    messages = [
        SystemMessage(content=RESEARCH_SYSTEM_PROMPT),
        HumanMessage(content=(
            f"## Project Brief\n{refined_brief}\n\n"
            f"## Research Data\n{research_context}\n\n"
            "Synthesize this into a comprehensive Market Dossier. "
            "Cite every claim with source URLs from the data above."
        )),
    ]

    response = invoke_with_retry(
        llm, messages,
        spec=spec,
        agent_name="research",
        phase="research",
        cost_tracker=cost_tracker,
    )
    dossier_content = response.content

    # Add metadata header
    timestamp = datetime.now(timezone.utc).isoformat()
    header = (
        f"# Market Research Dossier\n\n"
        f"> Generated: {timestamp}\n"
        f"> Sources analyzed: {len(unique_results)}\n"
        f"> Pages fetched: {len(all_fetched_pages)}\n"
        f"> Search queries: {len(queries)}\n\n---\n\n"
    )

    full_dossier = header + dossier_content

    # Step 5: Save to workspace
    dossier_path = workspace.write_file("briefs", "dossier.md", full_dossier)
    workspace.log_event("research", "Dossier generated", str(dossier_path))

    # Also save raw research data for reference
    raw_data = {
        "queries": queries,
        "results_count": len(unique_results),
        "pages_fetched": len(all_fetched_pages),
        "results": unique_results[:20],
        "timestamp": timestamp,
    }
    workspace.write_json("briefs", "research_raw.json", raw_data)

    logger.info(f"[Research] Dossier saved to: {dossier_path}")
    return str(dossier_path)
