# 🚀 Hackathon Automation Pipeline v2

> End-to-End Autonomous Multi-Agent System for Hackathon Operations

An AI-powered pipeline that takes a raw problem statement and autonomously produces a **deployed application** + **investor-grade pitch deck** through 4 phases — research, architecture, development, and delivery.

## v2 Improvements

| Feature | v1 | v2 |
|---------|:---:|:---:|
| Cost Tracking | ❌ | ✅ Immutable per-call tracking + budget guards |
| Retry Logic | ❌ | ✅ Exponential backoff (3 attempts) |
| Code Cleanup | ❌ | ✅ De-Sloppify pass (ECC pattern) |
| Security Scan | ❌ | ✅ OWASP-based security agent |
| Code Parser | Regex | ✅ State-machine parser |
| Review Depth | 8 files × 2KB | ✅ 30 files × 8KB (Gemini 1M) |
| Task Execution | Sequential | ✅ DAG-layer parallelism |
| Web Search | DuckDuckGo only | ✅ Exa + Firecrawl (optional) |
| Budget Control | ❌ | ✅ `--budget` flag + env var |

## Architecture

```
┌──────────────────────────────────────────────────────────────────────┐
│                     LangGraph Orchestrator v2                        │
│                   (Deterministic State Machine)                      │
├──────────────────────────────────────────────────────────────────────┤
│                                                                      │
│  Phase 0          Phase 1          Phase 2                           │
│  ┌──────────┐    ┌──────────┐    ┌──────────┐    ┌──────────┐      │
│  │Clarify   │ ─▶ │Research  │─▶  │Architect │─▶  │Planner   │      │
│  │Claude S4 │    │Gemini 2.5│    │Claude S4 │    │Claude S4 │      │
│  └──────────┘    │Flash     │    └──────────┘    └──────────┘      │
│  Interactive     └──────────┘                                       │
│                                                                      │
│  Phase 3 (v2: 5 agents)                                              │
│  ┌──────────┐   ┌──────────┐   ┌──────────┐   ┌──────────┐        │
│  │Coder     │─▶ │DeSlopify │─▶ │Reviewer  │─▶ │Security  │        │
│  │Claude S4 │   │Haiku 3.5 │   │Gemini 2.5│   │Claude S4 │        │
│  └──────────┘   │ NEW      │   │Pro       │   │ NEW      │        │
│       ▲         └──────────┘   └────┬─────┘   └────┬─────┘        │
│       └── retry (max 3) ───────────┘               │               │
│                                              ┌──────┴─────┐        │
│                                              │Deployer    │        │
│                                              │Haiku 3.5   │        │
│                                              └────────────┘        │
│  Phase 4                                                             │
│  ┌──────────┐    ┌──────────┐                                       │
│  │Pitch     │──▶ │Present   │──▶ 📦 Deployed App + Pitch Deck      │
│  │Claude S4 │    │Kimi k2   │                                       │
│  └──────────┘    │+ Gamma   │                                       │
│                  └──────────┘                                       │
│                                                                      │
│  💰 Cost Tracker (runs across ALL phases)                            │
└──────────────────────────────────────────────────────────────────────┘
```

## Agent → Model Routing

| Agent | Provider | Model | Phase | Purpose |
|-------|----------|-------|:---:|---------|
| Clarification | Anthropic | Claude Sonnet 4 | 0 | User intent analysis |
| Research | Google | Gemini 2.5 Flash | 1 | Web research synthesis |
| Knowledge Base | Google | text-embedding-004 | 1 | RAG embeddings |
| Architect | Anthropic | Claude Sonnet 4 | 2 | System design & PRD |
| Planner | Anthropic | Claude Sonnet 4 | 2 | Task decomposition |
| Coder | Anthropic | Claude Sonnet 4 | 3 | Code generation |
| **De-Sloppify** | Anthropic | Claude Haiku 3.5 | 3.5 | **Code cleanup** |
| Reviewer | Google | Gemini 2.5 Pro | 3 | 6-phase verification |
| **Security** | Anthropic | Claude Sonnet 4 | 3.5 | **OWASP security scan** |
| Deployer | Anthropic | Claude Haiku 3.5 | 3 | Railway deployment |
| Pitch | Anthropic | Claude Sonnet 4 | 4 | Narrative writing |
| Presentation | Moonshot | Kimi k2 | 4 | Gamma API integration |

## Quick Start

### 1. Clone & Install

```bash
cd automation
pip install -r requirements.txt
```

### 2. Configure API Keys

```bash
cp .env.example .env
# Edit .env with your API keys
```

Required keys:
- `ANTHROPIC_API_KEY` — [Get from Anthropic Console](https://console.anthropic.com/)
- `GOOGLE_API_KEY` — [Get from Google AI Studio](https://aistudio.google.com/apikey)
- `MOONSHOT_API_KEY` — [Get from Moonshot Platform](https://platform.moonshot.cn/)
- `GAMMA_API_KEY` — [Get from Gamma Developers](https://developers.gamma.app/)
- `RAILWAY_API_TOKEN` — [Get from Railway](https://railway.app/account/tokens)

Optional keys (enhanced research):
- `EXA_API_KEY` — [Exa AI search](https://exa.ai)
- `FIRECRAWL_API_KEY` — [Firecrawl deep scraping](https://firecrawl.dev)

### 3. Ensure Docker is Running

```bash
docker --version  # Verify Docker is installed
docker pull python:3.12-slim  # Pre-pull the sandbox image
```

### 4. Run the Pipeline

```bash
# Interactive mode (recommended)
python main.py "Build an AI-powered study buddy for college students"

# Skip clarification questions
python main.py --skip-clarify "Build a real-time crypto dashboard"

# Set budget limit
python main.py --budget 5.00 "Build a todo app with Flask"

# View agent-model routing
python main.py --show-models

# Verbose logging
python main.py -v "Build a task management app"
```

## Pipeline Phases (v2)

### Phase 0: Clarification (Interactive)
- Analyzes your problem statement
- Asks 6-8 targeted questions about audience, tech stack, scope
- Synthesizes answers into a Refined Brief
- **Never assumes — always asks**

### Phase 1: Research (Autonomous)
- Searches web via DuckDuckGo (+ Exa/Firecrawl if API keys set)
- Fetches and analyzes competitor pages
- Stores findings in ChromaDB (RAG)
- Produces a cited Market Dossier

### Phase 2: Architecture (Autonomous)
- Generates comprehensive PRD from dossier
- Defines tech stack, DB schemas, API contracts
- Decomposes into dependency-aware task queue (DAG)
- Follows Mise en Place methodology

### Phase 3: Development (Autonomous — v2 Enhanced)
- **Coder**: Executes tasks via stateless coding agents (DAG-layer parallel)
- **De-Sloppify** *(NEW)*: Cleanup pass removes code slop without changing logic
- **Reviewer**: 6-phase verification loop (Build→Import→Lint→Test→Runtime→Logic)
- Self-correction loop (up to 3 retries)
- **Security Review** *(NEW)*: OWASP-based scan for secrets, injection, XSS
- Deploys to Railway

### Phase 4: Delivery (Autonomous)
- Generates 7-slide pitch narrative (investor-materials quality gate)
- Renders via Gamma API
- Exports as PPTX/PDF
- Downloads to workspace

## v2 Features in Detail

### 💰 Cost Tracking
Every LLM API call is tracked with immutable `CostRecord` objects. Budget guards stop execution before overspending.

```bash
# Set budget via CLI
python main.py --budget 5.00 "Build an app"

# Or via environment variable
PIPELINE_BUDGET_LIMIT=5.00 python main.py "Build an app"
```

Output includes a full cost report:
- Total cost, input/output/cached tokens
- Cost breakdown by phase, provider, and agent
- Saved to `workspace/logs/cost_report.json`

### 🧹 De-Sloppify Pass (ECC Pattern)
Separate cleanup agent (Claude Haiku 3.5) runs after coding:
- Removes tests that test the language, not logic
- Removes `console.log`/`print()` debug statements
- Removes commented-out code
- Removes unused imports

### 🔒 Security Review (OWASP)
Pre-deployment security agent (Claude Sonnet 4):
- Static regex scan for hardcoded secrets
- LLM-based deep review for injection, XSS, auth issues
- Verdict: PASS / WARN / FAIL

### ⚡ Retry Logic
All LLM calls use exponential backoff:
- 3 attempts with 1s, 2s, 4s delays
- Retries only on transient errors (rate limit, timeout, 5xx)
- Fails fast on auth errors, bad requests

## Project Structure

```
automation/
├── main.py                      # CLI entry point (v2: cost dashboard)
├── config.py                    # Multi-provider model routing
├── validate.py                  # Syntax validation script
├── requirements.txt             # Dependencies
├── .env.example                 # API key template
├── .mcp.json                    # MCP server configuration
│
├── pipeline/
│   ├── orchestrator.py          # LangGraph state machine (v2)
│   ├── state.py                 # Shared state definitions (v2)
│   └── cost_tracker.py          # NEW: Immutable cost tracking
│
├── agents/
│   ├── llm_factory.py           # v2: Retry + cost tracking
│   ├── clarification_agent.py   # Phase 0 (Claude Sonnet 4)
│   ├── research_agent.py        # Phase 1 (Gemini 2.5 Flash)
│   ├── knowledge_base.py        # Phase 1 (Gemini Embeddings)
│   ├── architect_agent.py       # Phase 2 (Claude Sonnet 4)
│   ├── planner_agent.py         # Phase 2 (Claude Sonnet 4)
│   ├── coder_agent.py           # v2: State-machine parser + DAG
│   ├── deslopify_agent.py       # NEW: Code cleanup (Haiku 3.5)
│   ├── reviewer_agent.py        # v2: 6-phase verification loop
│   ├── security_agent.py        # NEW: OWASP security scan
│   ├── deployer_agent.py        # Phase 3 (Claude Haiku 3.5)
│   ├── pitch_agent.py           # Phase 4 (Claude Sonnet 4)
│   └── presentation_agent.py    # Phase 4 (Kimi k2)
│
├── tools/
│   ├── web_search.py            # DuckDuckGo + URL fetch
│   ├── exa_search.py            # NEW: Exa AI search + Firecrawl
│   ├── sandbox.py               # Docker execution
│   └── presentation.py          # Gamma API client
│
└── workspace/
    └── manager.py               # Shared file system (MCP)
```

## Design Principles

1. **Plan First (Mise en Place)** — Deliberate preparation before any code generation
2. **Delegation by Reference** — Agents pass file paths, not content, preserving context windows
3. **Hierarchical Manager-Worker** — Orchestrator delegates, workers execute statelessly
4. **Deterministic Flow** — LangGraph enforces strict phase transitions with validation
5. **Self-Correction** — Review-Code loop with max 3 retries prevents infinite loops
6. **Multi-Provider** — Best model for each task, not one model for everything
7. **Cost Awareness** *(NEW)* — Every API call tracked, budget enforced
8. **De-Sloppify** *(NEW)* — Two focused agents outperform one constrained agent
9. **Security First** *(NEW)* — Pre-deployment OWASP scan, never ship hardcoded secrets

## License

MIT
