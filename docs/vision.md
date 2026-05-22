# Platform Cortex — Vision & Value

## 🧠 The Problem

**AI agents are only as good as the context they receive — and today, that context is broken.**

Organizations investing in AI-powered development agents (code assistants, multi-agent systems, autonomous coding workflows) hit a wall: the architectural context these agents rely on is **static, stale, and scattered**. Static context packs, Confluence pages, and hand-maintained architecture docs get outdated the moment code is merged. Dependencies shift, endpoints change, modules are added, Kafka topics evolve — but the documentation stays frozen in time.

The result? Agents hallucinate. They suggest code that calls deprecated endpoints, miss inter-service dependencies, misunderstand module boundaries, and produce changes that break contracts they never knew existed. **Developers lose trust, adoption stalls, and what was promised as an "agentic solution" becomes just another unreliable autocomplete.** Without a living, accurate understanding of the platform, AI agents cannot make informed architectural decisions — and the entire promise of multi-agent software engineering collapses.

---

## 🎯 The Principle: Trust Only the Sources of Truth

To solve this, you can't rely on second-hand documentation — you have to go straight to the origin. We believe there are **three true sources of architectural truth**:

1. **📦 The Code** — The repository itself: dependencies, modules, endpoints, configurations, contracts, permissions. This is the canonical definition of what the system *is*.

2. **🌐 The Live APIs** — The deployed Swagger/OpenAPI specs that represent what the system *exposes right now* in production.

3. **📊 Real Production Usage** — Observability data (e.g., Datadog) that reveals what the system *actually does* — real traffic patterns, which endpoints are hit, which are dead, actual latency and error profiles.

With **Platform Cortex**, we are fully tackling **Source #1 (the code)** with deep, deterministic extraction across Android, iOS, and Spring Boot repositories — and we have a **partial implementation of Source #2** by extracting Swagger/OpenAPI contract references and endpoint metadata. Source #3 (production observability) represents the next frontier.

**The key insight: anything that isn't derived directly from these sources will eventually lie to your agents.**

---

## 💡 The Solution: Platform Cortex

**Platform Cortex is a living architectural knowledge graph that automatically extracts, aggregates, and serves real-time platform context to AI agents.**

### Architecture, Not Code

An important distinction: **Cortex is not a code indexer.** It doesn't store lines of code, syntax trees, or raw source files. Instead, it operates at a **layer above the code** — extracting the *architectural signal* that matters for decision-making:

- What modules exist and how they depend on each other
- What endpoints a service exposes
- What Kafka topics it produces and consumes
- What SDK versions it targets
- What permissions it requires
- What DTOs define its API contracts
- How services communicate across the platform

Think of it as the difference between reading a city's blueprint and walking every street. Agents don't need every line of code to make sound architectural decisions — they need to understand the *structure, boundaries, and connections* of the system. Cortex provides exactly that: **a high-fidelity architectural map, derived from code but abstracted to the level where agents can reason about the platform as a whole.**

### How It Works

It operates as a three-stage pipeline:

1. **Extract** — Deterministic parsers (no LLMs) scan Android, iOS, and Spring Boot repositories, pulling structured architectural metadata directly from source code: dependencies, API endpoints, Kafka topics, module graphs, SDK versions, database schemas, permissions, DTO contracts, inter-service HTTP calls, and more.

2. **Aggregate** — Extracted manifests are merged into a unified platform graph that maps every service, its capabilities, and how services communicate with each other (HTTP calls, Kafka event flows).

3. **Serve** — An MCP (Model Context Protocol) server exposes this graph to any AI agent through 4 purpose-built tools: discover relevant services by task description, list endpoints, get deep service context, and inspect API contracts. It runs on Cloud Run in production and can be connected to any MCP-compatible agent.

```
Source Repos (Android, iOS, Spring Boot)
    │
    ▼  deterministic parsing — no LLMs
Extract (per repo, parallel)
    │
    ▼
Aggregate → Unified Platform Graph
    │
    ▼
MCP Server (4 tools, Cloud Run)
    │
    ▼
AI Agents — always up-to-date architectural context
```

---

## ✅ Key Benefits

- **Always up-to-date**: Cortex reads the source code directly — every pipeline run reflects the real state of the codebase, not a wiki page someone forgot to update six months ago.

- **Architectural-level abstraction**: Agents get the structural understanding they need — modules, dependencies, endpoints, communication patterns — without drowning in implementation details. The right level of context for the right decisions.

- **Deterministic and trustworthy**: No LLM-based extraction. The parsers are rule-based and produce consistent, verifiable results. What agents see is exactly what the code says.

- **Multi-platform, cross-repo awareness**: Covers Android (Kotlin/Java), iOS (Swift), and Spring Boot backends in a single unified graph. Agents can understand how a mobile app talks to a backend service, what Kafka topics connect them, and what DTOs they share.

- **Agent-native delivery (MCP)**: Context is served through the Model Context Protocol standard — the same protocol used by Claude, Kilo, and the emerging multi-agent ecosystem. Any MCP-compatible agent can connect and query.

- **Fail-soft and incremental**: One repo failing extraction never blocks the rest. The graph is always available, even if partial.

- **The missing infrastructure for real agentic AI**: Without reliable, current architectural context, agents are guessing. Cortex is the foundational layer that makes multi-agent systems practical — it turns agents from "sometimes useful" to "architecturally aware and trustworthy."

---

## 🔑 The Elevator Pitch

> *"Static docs die the moment you merge. Cortex keeps your platform's architectural truth alive — extracted straight from code, elevated to the architectural level, and served to AI agents in real time via MCP. It's the difference between agents that guess and agents that know."*
