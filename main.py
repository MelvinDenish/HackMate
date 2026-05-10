"""
╔══════════════════════════════════════════════════════════════╗
║  HACKATHON AUTOMATION PIPELINE v6 — CLI Entry Point          ║
║                                                              ║
║  Usage:                                                      ║
║    python main.py "Build an AI study buddy app"              ║
║    python main.py --skip-clarify "Build a fintech dashboard" ║
║    python main.py --budget 5.00 "Build a todo app"           ║
║    python main.py --engine v6 "Build a real-time dashboard"  ║
║    python main.py --engine v6 --adaptive "Complex SaaS app"  ║
║                                                              ║
║  Engines:                                                    ║
║  v5: LangGraph state machine (16 phases, hardcoded order)    ║
║  v6: Meta-Agent + Workers (Architecture A+C)                 ║
║      - Event-driven message bus                              ║
║      - Fault-isolated async workers                          ║
║      - Adaptive decision-making (--adaptive flag)            ║
║      - Parallel coding, merged review+security               ║
║                                                              ║
║  Flow (v6):                                                  ║
║  Meta-Agent dispatches → Research Worker → Architect Worker  ║
║  → Planner Worker → Coder Worker(s) → Review Worker ↺       ║
║  → Deploy Worker (deploy + pitch + present)                  ║
╚══════════════════════════════════════════════════════════════╝
"""

import asyncio
import sys
import logging
import argparse
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.table import Table
from rich.markdown import Markdown
from rich import print as rprint

from config import load_config, AGENT_MODELS
from pipeline.state import create_initial_state
from pipeline.orchestrator import run_pipeline
from pipeline.cost_tracker import CostTracker, BudgetExceededError
from agents.clarification_agent import generate_questions, synthesize_brief
from workspace.manager import WorkspaceManager

console = Console()


def setup_logging(verbose: bool = False):
    """Configure logging with Rich handler."""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    # Suppress noisy loggers
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("chromadb").setLevel(logging.WARNING)


def display_banner():
    """Display the startup banner."""
    banner = """
[bold cyan]╔═══════════════════════════════════════════════════════════╗[/]
[bold cyan]║[/]  [bold white]🚀 HACKATHON AUTOMATION PIPELINE v4[/]                      [bold cyan]║[/]
[bold cyan]║[/]  [dim]End-to-End Autonomous Multi-Agent System[/]               [bold cyan]║[/]
[bold cyan]╠═══════════════════════════════════════════════════════════╣[/]
[bold cyan]║[/]  [bold magenta]Anthropic[/] Claude Sonnet 4  │ Architect, Coder, Pitch,   [bold cyan]║[/]
[bold cyan]║[/]                           │ Security Review            [bold cyan]║[/]
[bold cyan]║[/]  [bold magenta]Anthropic[/] Claude Haiku    │ Deploy, Polish, README,    [bold cyan]║[/]
[bold cyan]║[/]                           │ Self-Critique              [bold cyan]║[/]
[bold cyan]║[/]  [bold blue]Google[/] Gemini 2.5 Flash   │ Research, Knowledge Base   [bold cyan]║[/]
[bold cyan]║[/]  [bold blue]Google[/] Gemini 2.5 Pro     │ Code Reviewer (7-Phase)    [bold cyan]║[/]
[bold cyan]║[/]  [bold green]Moonshot[/] Kimi k2          │ Presentation Design        [bold cyan]║[/]
[bold cyan]╠═══════════════════════════════════════════════════════════╣[/]
[bold cyan]║[/]  [dim]Prompt Caching • Circuit Breaker • Model Fallback[/]     [bold cyan]║[/]
[bold cyan]║[/]  [dim]Reflexion Self-Critique • README Agent • Learning DB[/]  [bold cyan]║[/]
[bold cyan]║[/]  [dim]Design System Injection • WebSocket Dashboard[/]        [bold cyan]║[/]
[bold cyan]╚═══════════════════════════════════════════════════════════╝[/]
"""
    console.print(banner)


def display_model_table():
    """Display the agent-to-model mapping table."""
    table = Table(title="🧠 Agent → Model Routing", show_lines=True)
    table.add_column("Agent", style="bold")
    table.add_column("Provider", style="cyan")
    table.add_column("Model", style="green")
    table.add_column("Phase", style="yellow")

    phases = {
        "orchestrator": "Core", "clarification": "0",
        "research": "1", "embeddings": "1",
        "architect": "2", "planner": "2",
        "coder": "3", "deslopify": "3.5",
        "reviewer": "3", "security": "3.5",
        "deployer": "3", "pitch": "4", "presentation": "4",
    }

    provider_styles = {
        "anthropic": "[magenta]Anthropic[/]",
        "google": "[blue]Google[/]",
        "moonshot": "[green]Moonshot[/]",
    }

    for role, spec in AGENT_MODELS.items():
        table.add_row(
            role.title(),
            provider_styles.get(spec.provider, spec.provider),
            spec.model_name,
            f"Phase {phases.get(role, '?')}",
        )

    console.print(table)
    console.print()


def run_clarification(problem_statement: str, config) -> tuple[str, dict]:
    """
    Run the interactive clarification phase.
    Asks the user targeted questions and collects answers.

    Returns:
        Tuple of (refined_brief, user_answers)
    """
    console.print(Panel(
        "[bold]Phase 0: Clarification[/]\n"
        "Analyzing your problem statement and generating targeted questions...",
        title="🔍 Understanding Your Vision",
        border_style="cyan",
    ))

    # Generate questions
    with console.status("[bold cyan]Generating questions...", spinner="dots"):
        questions = generate_questions(problem_statement, config)

    console.print("\n[bold yellow]Please answer these questions to help us build exactly what you need:[/]\n")
    console.print(Markdown(questions))
    console.print()

    # Collect answers
    answers = {}
    console.print("[bold green]Enter your answers below.[/]")
    console.print("[dim]For each question, type your answer and press Enter.[/]")
    console.print("[dim]Type 'skip' to use defaults for that question.[/]\n")

    # Parse question numbers from the generated questions
    import re
    question_lines = re.findall(r'(\d+)\.\s+(.+?)(?:\n|$)', questions)

    for num, question_text in question_lines:
        console.print(f"[bold cyan]Q{num}:[/] {question_text.strip()}")
        answer = input("  → ").strip()
        if answer.lower() == "skip":
            answer = "(Use your best judgment)"
        answers[num] = answer
        console.print()

    if not answers:
        # Fallback: ask for general input
        console.print("[bold cyan]Please provide any additional context:[/]")
        general = input("  → ").strip()
        answers["1"] = general or "(No additional context)"

    # Synthesize brief
    console.print()
    with console.status("[bold cyan]Synthesizing your answers into a refined brief...", spinner="dots"):
        refined_brief = synthesize_brief(problem_statement, questions, answers, config)

    console.print(Panel(
        Markdown(refined_brief[:2000]),
        title="📋 Refined Project Brief",
        border_style="green",
    ))

    console.print("\n[bold yellow]Does this look correct? (yes/no):[/]")
    confirm = input("  → ").strip().lower()

    if confirm in ("no", "n"):
        console.print("[bold]Please provide corrections:[/]")
        corrections = input("  → ").strip()
        if corrections:
            refined_brief += f"\n\n## User Corrections\n{corrections}"

    return refined_brief, answers


def display_cost_report(cost_tracker: CostTracker):
    """Display the final cost report as a Rich table."""
    console.print()

    # Summary
    cost_table = Table(title="💰 Cost Report", show_lines=True)
    cost_table.add_column("Metric", style="bold")
    cost_table.add_column("Value", style="cyan", justify="right")

    cost_table.add_row("Total Cost", f"${cost_tracker.total_cost:.4f}")
    cost_table.add_row("Budget Limit", f"${cost_tracker.budget_limit:.2f}")
    cost_table.add_row(
        "Budget Remaining",
        f"${max(0, cost_tracker.budget_limit - cost_tracker.total_cost):.4f}",
    )
    cost_table.add_row("API Calls", str(cost_tracker.call_count))
    cost_table.add_row("Input Tokens", f"{cost_tracker.total_input_tokens:,}")
    cost_table.add_row("Output Tokens", f"{cost_tracker.total_output_tokens:,}")
    cost_table.add_row("Cached Tokens", f"{cost_tracker.total_cached_tokens:,}")

    console.print(cost_table)

    # By Phase
    if cost_tracker.cost_by_phase():
        phase_table = Table(title="📊 Cost by Phase", show_lines=True)
        phase_table.add_column("Phase", style="bold")
        phase_table.add_column("Cost", style="cyan", justify="right")

        for phase, cost in sorted(
            cost_tracker.cost_by_phase().items(), key=lambda x: -x[1]
        ):
            phase_table.add_row(phase.title(), f"${cost:.4f}")

        console.print(phase_table)

    # By Provider
    if cost_tracker.cost_by_provider():
        prov_table = Table(title="🏢 Cost by Provider", show_lines=True)
        prov_table.add_column("Provider", style="bold")
        prov_table.add_column("Cost", style="cyan", justify="right")

        for prov, cost in sorted(
            cost_tracker.cost_by_provider().items(), key=lambda x: -x[1]
        ):
            prov_table.add_row(prov.title(), f"${cost:.4f}")

        console.print(prov_table)


def display_results(final_state: dict, cost_tracker: CostTracker = None):
    """Display the final pipeline results."""
    console.print("\n")
    console.print(Panel(
        "[bold green]🎉 PIPELINE v4 COMPLETE[/]",
        border_style="green",
    ))

    results = Table(title="📦 Deliverables", show_lines=True)
    results.add_column("Output", style="bold")
    results.add_column("Location", style="cyan")

    deployment_url = final_state.get("deployment_url", "")
    deck_url = final_state.get("deck_url", "")
    deck_local = final_state.get("deck_local_path", "")
    src_dir = final_state.get("src_dir", "")

    results.add_row("🌐 Live Application", deployment_url or "(not deployed)")
    results.add_row("📊 Pitch Deck (URL)", deck_url or "(not available)")
    results.add_row("📊 Pitch Deck (Local)", deck_local or "(not available)")
    results.add_row("💻 Source Code", src_dir or "(not generated)")

    dossier = final_state.get("dossier_path", "")
    prd = final_state.get("prd_path", "")
    pitch = final_state.get("pitch_content_path", "")

    results.add_row("📄 Research Dossier", dossier or "(not generated)")
    results.add_row("📋 PRD", prd or "(not generated)")
    results.add_row("🎤 Pitch Content", pitch or "(not generated)")

    # New v2 outputs
    security_verdict = final_state.get("security_verdict", "")
    if security_verdict:
        results.add_row("🔒 Security Verdict", security_verdict)

    console.print(results)

    # Phase status
    status_table = Table(title="📊 Phase Status", show_lines=True)
    status_table.add_column("Phase", style="bold")
    status_table.add_column("Status")

    status_icons = {
        "completed": "[green]✅ Completed[/]",
        "failed": "[red]❌ Failed[/]",
        "skipped": "[yellow]⏭️  Skipped[/]",
        "pending": "[dim]⏳ Pending[/]",
        "running": "[blue]🔄 Running[/]",
    }

    for phase, status in final_state.get("phase_status", {}).items():
        status_table.add_row(
            phase.title(),
            status_icons.get(status, status),
        )

    console.print(status_table)

    # Errors
    errors = final_state.get("errors", [])
    if errors:
        console.print(f"\n[bold yellow]⚠️  {len(errors)} errors occurred:[/]")
        for err in errors:
            console.print(f"  [red]• {err}[/]")

    # Cost report
    if cost_tracker and cost_tracker.call_count > 0:
        display_cost_report(cost_tracker)


def main():
    """Main CLI entry point."""
    parser = argparse.ArgumentParser(
        description="🚀 Hackathon Automation Pipeline v4 — "
                    "End-to-End Autonomous Multi-Agent System",
    )
    parser.add_argument(
        "problem",
        nargs="?",
        help="Problem statement (e.g. 'Build an AI study buddy app')",
    )
    parser.add_argument(
        "--skip-clarify",
        action="store_true",
        help="Skip the interactive clarification phase",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable verbose logging",
    )
    parser.add_argument(
        "--show-models",
        action="store_true",
        help="Display the agent-to-model routing table and exit",
    )
    parser.add_argument(
        "--budget",
        type=float,
        default=None,
        help="Budget limit in USD (default: $10.00, or PIPELINE_BUDGET_LIMIT env var)",
    )
    parser.add_argument(
        "--engine",
        choices=["v5", "v6"],
        default="v6",
        help="Pipeline engine: v5 (LangGraph) or v6 (Meta-Agent A+C, default)",
    )
    parser.add_argument(
        "--adaptive",
        action="store_true",
        help="[v6 only] Let LLM decide which phases to skip (~$0.10 extra)",
    )
    parser.add_argument(
        "--skip-research",
        action="store_true",
        help="[v6 only] Skip research phase for simple prompts",
    )
    parser.add_argument(
        "--skip-deploy",
        action="store_true",
        help="[v6 only] Skip deployment phase",
    )

    args = parser.parse_args()
    setup_logging(verbose=args.verbose)

    display_banner()

    if args.show_models:
        display_model_table()
        return

    # Get problem statement
    if args.problem:
        problem_statement = args.problem
    else:
        console.print("[bold cyan]Enter your hackathon problem statement:[/]")
        problem_statement = input("  → ").strip()
        if not problem_statement:
            console.print("[red]Error: No problem statement provided.[/]")
            sys.exit(1)

    console.print(Panel(
        f"[bold]{problem_statement}[/]",
        title="📝 Problem Statement",
        border_style="blue",
    ))

    # Load configuration
    with console.status("[bold cyan]Loading configuration...", spinner="dots"):
        config = load_config()

    # Override budget if specified via CLI
    if args.budget is not None:
        config.budget_limit = args.budget

    # Initialize cost tracker
    cost_tracker = CostTracker(budget_limit=config.budget_limit)
    console.print(f"[dim]Budget limit: ${config.budget_limit:.2f}[/]")

    # Validate API keys
    missing = config.keys.validate()
    if missing:
        console.print(f"\n[bold yellow]⚠️  Missing API keys: {', '.join(missing)}[/]")
        console.print("[dim]Some features may be unavailable. Copy .env.example to .env and add keys.[/]\n")

    # Initialize workspace
    workspace = WorkspaceManager(config.workspace.root)
    console.print(f"[dim]Workspace: {workspace.root.resolve()}[/]\n")

    # Phase 0: Clarification
    if not args.skip_clarify:
        refined_brief, user_answers = run_clarification(problem_statement, config)
    else:
        refined_brief = problem_statement
        user_answers = {}

    # Create initial state
    initial_state = create_initial_state(problem_statement)
    initial_state["refined_brief"] = refined_brief
    initial_state["user_answers"] = user_answers

    # Save refined brief to workspace
    workspace.write_file("briefs", "refined_brief.md", refined_brief)

    # Run the pipeline
    if args.engine == "v6":
        # ── v6: Meta-Agent + Workers (Architecture A+C) ──
        console.print(Panel(
            "[bold]Starting v6 Meta-Agent pipeline...[/]\n"
            "[dim]Engine: Event-Driven Workers + Adaptive Meta-Agent[/]\n"
            "[dim]Flow: Meta-Agent dispatches → Workers execute → Results aggregate[/]",
            title="🤖 v6 Autonomous Execution (A+C)",
            border_style="magenta",
        ))

        try:
            final_state = asyncio.run(
                _run_v6_pipeline(
                    refined_brief, config, workspace, cost_tracker,
                    adaptive=args.adaptive,
                    skip_research=args.skip_research,
                    skip_deploy=args.skip_deploy,
                )
            )
            display_results(final_state, cost_tracker)

        except BudgetExceededError as e:
            console.print(Panel(
                f"[bold red]💸 BUDGET EXCEEDED[/]\n\n"
                f"Spent: ${e.current:.4f}\nLimit: ${e.limit:.2f}",
                title="⚠️ Budget Guard", border_style="red",
            ))
            if cost_tracker.call_count > 0:
                display_cost_report(cost_tracker)
            sys.exit(1)

        except KeyboardInterrupt:
            console.print("\n[yellow]Pipeline interrupted by user.[/]")
            if cost_tracker.call_count > 0:
                display_cost_report(cost_tracker)
            sys.exit(0)

    else:
        # ── v5: LangGraph State Machine (legacy) ──
        console.print(Panel(
            "[bold]Starting v5 LangGraph pipeline...[/]\n"
            "[dim]Flow: Research → Architect → Plan → Code → Polish → README → Self-Critique → Review ↺ → Security → Deploy → Pitch → Present[/]",
            title="🤖 v5 Autonomous Execution (LangGraph)",
            border_style="magenta",
        ))

        try:
            final_state = run_pipeline(
                initial_state, config, workspace,
                cost_tracker=cost_tracker,
            )
            display_results(final_state, cost_tracker)

        except BudgetExceededError as e:
            console.print(Panel(
                f"[bold red]💸 BUDGET EXCEEDED[/]\n\n"
                f"Spent: ${e.current:.4f}\nLimit: ${e.limit:.2f}",
                title="⚠️ Budget Guard", border_style="red",
            ))
            if cost_tracker.call_count > 0:
                display_cost_report(cost_tracker)
            sys.exit(1)

        except KeyboardInterrupt:
            console.print("\n[yellow]Pipeline interrupted by user.[/]")
            if cost_tracker.call_count > 0:
                display_cost_report(cost_tracker)
            sys.exit(0)

    # Save cost report to workspace
    if cost_tracker.call_count > 0:
        try:
            cost_tracker.save_report(workspace.logs_dir)
            console.print(f"\n[dim]Cost report saved to: {workspace.logs_dir / 'cost_report.json'}[/]")
        except Exception:
            pass


async def _run_v6_pipeline(
    brief: str,
    config,
    workspace: WorkspaceManager,
    cost_tracker: CostTracker,
    adaptive: bool = False,
    skip_research: bool = False,
    skip_deploy: bool = False,
) -> dict:
    """Run the v6 A+C pipeline: meta-agent + workers + message bus."""
    from pipeline.message_bus import MessageBus
    from pipeline.worker import create_worker_pool
    from pipeline.meta_agent import MetaAgent

    # Create the bus
    bus = MessageBus()

    # Create workers
    workers = await create_worker_pool(
        config, workspace, bus,
        cost_tracker=cost_tracker,
        coder_count=1,  # Scale up to 3 for parallel coding
    )

    # Start worker tasks
    worker_tasks = [asyncio.create_task(w.run()) for w in workers]

    # Create meta-agent
    meta = MetaAgent(config, workspace, bus, cost_tracker)

    try:
        if adaptive:
            result = await meta.run_adaptive(brief)
        else:
            result = await meta.run_deterministic(
                brief,
                skip_research=skip_research,
                skip_deploy=skip_deploy,
            )
    finally:
        # Stop all workers
        for w in workers:
            w.stop()
        # Cancel worker tasks
        for t in worker_tasks:
            t.cancel()
        # Wait for cancellation
        await asyncio.gather(*worker_tasks, return_exceptions=True)

    # Convert to display-compatible format
    final_state = {
        "refined_brief": brief,
        "dossier_path": result.get("dossier_path", ""),
        "prd_path": result.get("prd_path", ""),
        "src_dir": result.get("src_dir", ""),
        "code_files": result.get("code_files", []),
        "deployment_url": result.get("deploy_url", ""),
        "deck_url": result.get("deck_url", ""),
        "pitch_content_path": result.get("pitch_path", ""),
        "security_verdict": result.get("review_result", {}).get("security_verdict", ""),
        "errors": result.get("errors", []),
        "phase_status": {
            phase: "completed" for phase in result.get("phases_completed", [])
        },
    }
    # Add skipped phases
    for phase in result.get("phases_skipped", []):
        final_state["phase_status"][phase] = "skipped"

    return final_state


if __name__ == "__main__":
    main()
