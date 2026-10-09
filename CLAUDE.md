# Case Study 1: Website Visitor Assistant for an SME

## Goal
Build a small, production-shaped AI assistant that an SME can embed on its website. It answers
visitor questions from the business's own content and captures qualified leads. This repo is a
public portfolio case study for an AI automation consultant, so clarity and honesty matter more
than feature count.

## Demo scenario
A fictional dental clinic, "Brightsmile Dental" (all data invented, clearly labeled as demo).
Visitors ask about services, prices, opening hours, insurance, and booking. Today the front desk
answers the same questions by phone and email.

## MVP scope
In scope:
- `data/` with demo business content (services, FAQ, prices, hours, policies) as Markdown.
- Simple retrieval over that content (keyword or small embedding index; keep it dependency-light).
- Chat backend in Python (FastAPI) calling the Claude API. Default model `claude-haiku-4-5-20251001`,
  configurable via env var.
- Answers only from the business content. If unsure, it says so and offers a human handoff.
- Lead capture: when a visitor shows booking intent, ask for name + contact + preferred time,
  validate, and save to `leads.csv` (or SQLite). Ask for consent before saving contact data.
- A minimal embeddable chat widget (single JS file + HTML demo page).
- `MOCK_LLM=1` mode with canned responses so tests and the demo run without an API key.

Out of scope: real calendar integration, auth, multi-tenant, payments, fancy UI frameworks.

## Constraints
- Never commit secrets. Read `ANTHROPIC_API_KEY` from env; ship `.env.example` only.
- Prompt-injection hygiene: user text never overrides the system rules; test a few attack strings.
- Keep it runnable with `docker compose up` and with plain `pip install -r requirements.txt`.

## Deliverables
1. Working code + tests (pytest) covering retrieval, refusal on out-of-scope questions, lead
   capture validation, consent, and injection attempts. Tests must pass in mock mode.
2. GitHub Actions CI running the tests.
3. `README.md` in English: what it does, architecture diagram (Mermaid), quickstart, config,
   limitations.
4. `CASE_STUDY.md` in English answering exactly three questions:
   - What problem does it solve?
   - How does it work?
   - What is the concrete result in a real business?
   Include a before/after table. Numbers measured on demo data must say so; baseline numbers that
   are estimates must be labeled "estimate" with the assumption stated. Never invent client results.
5. `n8n/` folder (optional, nice to have): an exported n8n workflow JSON that forwards new leads
   to email/Sheets, with a note on how to import it.

## Build log for content (required)
Keep `BUILD_LOG.md` updated as you work, so the member can turn the build into posts later:
- Key decisions and why (including options you rejected).
- Problems hit and how you solved them (real errors, short snippets).
- Measured numbers from demo runs (time, cost, accuracy), labeled as demo data.
- 3-5 "post hooks": one-sentence lessons or surprising findings worth sharing.
- Suggested screenshots/GIFs to capture (exact command or page to show).
Commit it with the code; keep it factual, no invented results.

## Definition of done
Tests green in CI, `docker compose up` serves the demo page, README and CASE_STUDY complete,
BUILD_LOG.md filled, no secrets in history, open a PR with a summary of what was built and what is left.
