"""
╔══════════════════════════════════════════════════════════════╗
║  CLARIFICATION AGENT — Phase 0: Interactive Questioning      ║
║                                                              ║
║  🟣 LLM: Anthropic Claude Sonnet 4                           ║
║  Why: Best at understanding user intent, generating          ║
║       targeted questions, and synthesizing answers            ║
║                                                              ║
║  Purpose:                                                    ║
║  The pipeline NEVER assumes. Before any autonomous work,     ║
║  this agent asks the user targeted questions about their     ║
║  problem statement to build a comprehensive brief.           ║
║                                                              ║
║  Flow:                                                       ║
║  1. Receive raw problem statement                            ║
║  2. Analyze for ambiguity and missing context                ║
║  3. Generate 5-8 targeted clarifying questions               ║
║  4. Collect user answers                                     ║
║  5. Synthesize into a Refined Brief                          ║
╚══════════════════════════════════════════════════════════════╝
"""

import logging
from langchain_core.messages import SystemMessage, HumanMessage

from agents.llm_factory import create_llm, invoke_with_retry
from config import PipelineConfig
from pipeline.cost_tracker import CostTracker

logger = logging.getLogger(__name__)

CLARIFICATION_SYSTEM_PROMPT = """You are the Clarification Agent in an autonomous hackathon pipeline. Your role is to deeply analyze a user's problem statement and ask highly targeted questions to eliminate ambiguity BEFORE any work begins.

## Your Mandate
- NEVER assume anything about the project
- Ask questions that will materially impact architecture decisions
- Focus on questions whose answers will change what gets built
- Be specific, not generic — tailor questions to the actual problem statement

## Required Question Categories
You MUST cover these areas:

1. **Target Audience**: Who exactly will use this? Age, profession, tech literacy?
2. **Core Features (MVP)**: What are the 3-5 must-have features for a hackathon demo? What's explicitly OUT of scope?
3. **Tech Stack Preference**: Any preferred languages, frameworks, or platforms? (React, Vue, Flask, Next.js, mobile, etc.)
4. **Data & Integrations**: Does this need external APIs, databases, AI models, payment systems?
5. **Design Aesthetic**: Visual style preference? (Dark mode, minimal, colorful, corporate, playful?)
6. **Differentiator**: What makes this different from existing solutions? What's the "wow factor"?
7. **Demo Scenario**: What specific scenario should the live demo show to judges?
8. **Deployment Constraints**: Any specific hosting/platform requirements?

## Output Format
Generate your questions as a numbered list. Each question should be:
- Specific to the problem statement (not generic)
- Actionable (the answer directly impacts what gets built)
- Concise (one clear question, not compound questions)

Generate 6-8 questions maximum. Quality over quantity."""

SYNTHESIS_SYSTEM_PROMPT = """You are synthesizing user answers into a comprehensive Refined Brief for an autonomous hackathon pipeline. 

Given the original problem statement and the user's answers to clarifying questions, create a detailed brief that downstream agents (Research, Architect, Coder, Pitch) can use without any ambiguity.

## Output Format — Refined Brief

# Refined Project Brief

## Problem Statement
[Enriched version of the original problem, incorporating user context]

## Target Audience
[Specific user personas based on answers]

## Core Features (MVP Scope)
- Feature 1: [description]
- Feature 2: [description]
- ...

## Explicitly Out of Scope
- [Things NOT to build]

## Tech Stack
- Frontend: [specific framework]
- Backend: [specific framework]
- Database: [specific DB]
- AI/ML: [if applicable]
- Other: [any integrations]

## Design Direction
[Visual style, theme, aesthetic preferences]

## Competitive Edge / Differentiator
[What makes this unique — the "wow factor"]

## Demo Scenario
[Step-by-step of what the live demo should show]

## Success Criteria
[How judges will evaluate this]

Be precise and specific. Downstream agents will build exactly what this brief says."""


def generate_questions(
    problem_statement: str, config: PipelineConfig, cost_tracker: CostTracker = None,
) -> str:
    """
    Analyze the problem statement and generate targeted
    clarifying questions for the user.

    Args:
        problem_statement: Raw user input
        config: Pipeline configuration
        cost_tracker: Optional cost tracker

    Returns:
        Formatted string of numbered questions
    """
    logger.info("[Clarification] Generating questions for problem statement")

    spec = config.get_model("clarification")
    llm = create_llm(spec, config.keys)

    messages = [
        SystemMessage(content=CLARIFICATION_SYSTEM_PROMPT),
        HumanMessage(content=(
            f"## Problem Statement\n\n{problem_statement}\n\n"
            "Analyze this problem statement and generate targeted "
            "clarifying questions. Remember: ask questions whose "
            "answers will materially change what gets built."
        )),
    ]

    response = invoke_with_retry(
        llm, messages,
        spec=spec,
        agent_name="clarification",
        phase="clarification",
        cost_tracker=cost_tracker,
    )
    questions = response.content
    logger.info("[Clarification] Questions generated successfully")
    return questions


def synthesize_brief(
    problem_statement: str,
    questions: str,
    answers: dict[str, str],
    config: PipelineConfig,
    cost_tracker: CostTracker = None,
) -> str:
    """
    Synthesize the user's answers into a comprehensive Refined Brief.

    Args:
        problem_statement: Original problem statement
        questions: The questions that were asked
        answers: Dict mapping question number/text to user's answer
        config: Pipeline configuration
        cost_tracker: Optional cost tracker

    Returns:
        Markdown Refined Brief document
    """
    logger.info("[Clarification] Synthesizing answers into Refined Brief")

    spec = config.get_model("clarification")
    llm = create_llm(spec, config.keys)

    # Format Q&A pairs
    qa_text = ""
    for q_num, answer in answers.items():
        qa_text += f"**Q{q_num}**: {answer}\n\n"

    messages = [
        SystemMessage(content=SYNTHESIS_SYSTEM_PROMPT),
        HumanMessage(content=(
            f"## Original Problem Statement\n{problem_statement}\n\n"
            f"## Questions Asked\n{questions}\n\n"
            f"## User Answers\n{qa_text}\n\n"
            "Synthesize these into a comprehensive Refined Brief."
        )),
    ]

    response = invoke_with_retry(
        llm, messages,
        spec=spec,
        agent_name="clarification",
        phase="clarification",
        cost_tracker=cost_tracker,
    )
    brief = response.content
    logger.info("[Clarification] Refined Brief synthesized")
    return brief
