# 🚀 Netaro: Founding Engineer III - Systems Challenge

> **The Netaro Standard:** We are bypassing SWIFT to move eight figures of heavy-industry capital globally. We do not patch legacy banking; we replace it. The crypto mechanics are easy. The hard part is atomic settlement, double-entry accounting, and ensuring that a network timeout doesn’t result in a $500,000 reconciliation error.
> 

## 📌 Context

We don’t care if you have the exact syntax of a library memorized. We care how you architect deterministic state, how you handle failure, and how you leverage AI to move at velocity without compromising correctness. You will be building the core execution loop of Netaro’s settlement API in **Python (FastAPI)** and **PostgreSQL**.

## ⏱️ The Rules of Engagement

### 1. The AI Recording (Mandatory)

You are encouraged to use Claude, Gemini, Cursor, or Copilot for this assignment. In fact, we expect it.

- **Requirement:** You must record your entire screen/session (via Loom, OBS, or similar) while you work on this. We don't want a narrated presentation at the end; we want to watch your raw workflow. We want to see how you prompt, how you structure your context, and—most importantly—how you push back when the AI inevitably hallucinates a race condition or a bad schema design.

### 2. Strict Time Constraint: 4 Hours Maximum

We deeply respect your time. Do not spend your entire weekend on this. The screen recording of your AI-assisted coding session **must not exceed 4 hours**.

- We are testing your ability to prioritize under pressure. If you are using AI tools effectively, 4 hours is enough time to generate the boilerplate and architect the core settlement loop.
- **If you run out of time:** Stop coding. Submit what you have. We are evaluating your architectural choices and database locking strategy, not your ability to write infinite lines of code. Use your ADR to explain what you prioritized and how you would have finished it.

## 🏗️ The Problem: Atomic FX Routing & Double-Entry Ledger

You will receive a high-throughput stream of concurrent API requests from Enterprise CFOs attempting to settle invoices (e.g., *Pay $100,000 USD to a vendor in PHP*).

To execute a payout, your system must:

1. **Find the optimal routing path** across a dynamic liquidity graph.
2. **Execute a double-entry ledger transaction** (locking funds).
3. **Call a mock 3rd-party Payout API** to execute the local fiat wire.
4. **Commit or rollback** the ledger based on the network response.

### 🔴 Constraint 1: The Liquidity Graph (Algorithmic Rigor)

You will have a simulated in-memory stream of FX rates from 3 different Liquidity Providers (LPs).

- Rates fluctuate every 50ms.
- The graph of currencies includes USD, USDC, EUR, PHP, and AED.
- **The Task:** When a request hits, your system must calculate the cheapest path from USD to the target currency (e.g., `USD -> USDC -> PHP` vs. `USD -> EUR -> PHP`) in real-time ($O(V+E)$ or better).

### 🔴 Constraint 2: The Double-Entry Ledger (Systems Rigor)

You must design a relational database schema (using SQLAlchemy/Postgres) for a double-entry ledger.

- You must manage an Omnibus USD account, an Omnibus USDC account, and the customer's virtual balance.
- **The Task:** The system will be bombarded with 1,000 concurrent settlement requests. You must guarantee that race conditions do not result in negative balances or phantom reads. *(Hint: We want to see your command of isolation levels, `SELECT FOR UPDATE`, and idempotency keys).*

### 🔴 Constraint 3: Byzantine Failures (Fault Tolerance)

You will build a mock external `Payout_API` function.

- 70% of the time, it returns `200 OK`.
- 15% of the time, it returns `503 Service Unavailable`.
- 15% of the time, it **times out after 5 seconds** (you don’t know if the money moved or not).
- **The Task:** Implement a deterministic state machine (Saga pattern or distributed lock) to handle these failures. If a timeout occurs, your system cannot just drop the row—it must transition to a verifiable `PENDING_RECONCILIATION` state.

## 📦 Deliverables

Please submit the following via email upon completion:

1. **The Codebase:** A GitHub repo link containing your Python/FastAPI/Postgres implementation. It must be cleanly runnable locally via `docker-compose up`.
2. **The Tests:** A load-test script hitting your API with 1,000 concurrent requests to prove your database locks and routing algorithms hold up under pressure without dropping a cent.
3. **The Session Recording:** A URL to the unedited screen recording of your build process with AI (Loom/YouTube/Google Drive).
4. **The ADR (Architecture Decision Record):** A 1-page markdown file explaining:
    - Why you chose your specific database locking strategy.
    - How you designed the graph routing algorithm.
    - Where the system would break if we scaled it to 10,000 requests per second, and how you would re-architect it.
    - *If you ran out of time:* What you skipped and why.

## 🧠 How We Evaluate You

- **Correctness Over Feature Completeness:** A flawless locking mechanism with missing endpoints is better than a fully built API that drops database rows.
- **AI Leverage:** Did you use Cursor/Copilot to blast through boilerplate, leaving you time to deeply think through the isolation levels and algorithms?
- **High-Agency Ownership:** We want builders who anticipate edge cases before they hit production. Show us how you handle the unknown.
