# Optimization audit

Audit date: 2026-10-09 · Commit audited: `a0a527e` · Scope: `app/`, `web/`, `scripts/eval.py`,
`Dockerfile`, `docker-compose.yml`, CI, config files.
Commit hashes in this file are the individual commits of
[pull request #1](https://github.com/Melwis880/smb-website-assistant/pull/1), which was squash-merged into `main`.

Report only: no code was changed. All numbers below were measured locally on the fictional demo
data in mock mode (`MOCK_LLM=1`, Python 3.13, `.venv`) unless they are marked **estimate**.
The real Claude API was not called.

**Baseline (measured):** `pytest` passes 149 tests in 9.5 s. `scripts/eval.py` takes **0.7 ms median**
and 2.2 ms max per turn end to end in mock mode. BM25 `search()` takes 0.19 ms per query and
`coverage()` takes 0.07 ms, over 26 chunks and a 301-term vocabulary.

---

## 1) Optimization Summary

**Health: good for the intended load.** The design already avoids the expensive mistakes:

- The LLM is called only after retrieval clears two thresholds.
- Injection attempts and bookings never reach the model.
- Every in-memory structure is bounded: sessions are capped at 5,000 with a 1-hour TTL, limiter keys
  at 10,000, and history at 12 turns.
- There is a daily LLM call budget.
- Local compute per turn is under 1 ms.

The real risks are not CPU or algorithmic. They come from **what happens when the upstream API is
slow** and from **CPU spent on hostile input before it is truncated**.

**Top 3 highest-impact improvements**
1. **Keep slow Claude calls from using up the server's 40-thread pool** (F1), and shorten the
   worst-case wait from about 60 s to about 20 s (F2). Today an Anthropic slowdown with about 40
   concurrent chats also stalls `/healthz`, the demo page and `widget.js`.
2. **Truncate visitor text before Unicode normalization** (F3). A 4,000-character crafted message
   costs **97 ms of CPU**, against 2 ms for a normal one, because NFKC expands it 18× before the cut
   to 1,000 characters.
3. **Log token usage per model call** (F4). Production records no tokens or cost today, so the
   real-model cost figure in the case study can't be measured once deployed.

**Biggest risk if nothing changes:** a slow or retrying Anthropic API (20 s timeout × 3 attempts)
holds every worker thread. `/healthz` then misses its 3 s Docker timeout. Under an orchestrator that
restarts unhealthy containers, the restart wipes all in-memory sessions, including visitors who are
halfway through the booking flow.

---

## 2) Findings (Prioritized)

### F1. Sync endpoints share one 40-thread pool, so slow LLM calls starve `/healthz` and static files
- **Category:** Concurrency / Reliability
- **Severity:** Medium. It becomes High during an upstream incident.
- **Impact:** availability, health-check stability, and latency of the demo page and `widget.js`.
- **Evidence:**
  - `chat()`, `healthz()`, `demo_page()` and `widget()` in `app/main.py:143-169` are all plain
    `def`, so FastAPI runs each one on AnyIO's default thread limiter. That limiter has
    **40 tokens** (measured: `current_default_thread_limiter().total_tokens == 40`).
  - Each chat holds a thread for the whole Claude call (`app/llm.py:75`).
  - Repro: with a model stubbed to sleep 3 s, here is how long `/healthz` took:

    | Concurrent chats | `/healthz` latency |
    |---|---|
    | 10 | 0.01 s |
    | 45 | **2.94 s** |

    The Dockerfile `HEALTHCHECK` times out at 3 s.
- **Why it's inefficient:** liveness and static assets queue behind unrelated network-bound work.
  With real worst-case calls of about 60 s (see F2), `/healthz` would wait up to a minute.
- **Recommended fix:**
  - (a) Make `healthz` `async def`. It does no I/O, so it never needs a thread.
  - (b) Make `chat` `async def` and run `engine.handle` with `anyio.to_thread.run_sync(...,
    limiter=CHAT_LIMITER)`, where `CHAT_LIMITER = anyio.CapacityLimiter(20)`. Chats can then never
    take more than half of the default pool, and the static files (`FileResponse` reads through the
    default pool) keep working.
- **Tradeoffs / Risks:** (b) caps concurrent chats at 20 per process. That is far above SME
  traffic: about 13 turns/s at about 1.5 s per Claude call (**estimate**, real latency not measured).
  Excess chats queue instead of failing.
- **Expected impact estimate:** in the repro above, `/healthz` drops back to about 0.01 s at any
  chat concurrency. Throughput is unchanged at normal load.
- **Removal Safety:** Safe. Behaviour and API are unchanged, and tests use `TestClient`, which
  handles async endpoints.
- **Reuse Scope:** service-wide (`app/main.py`).

### F2. Worst-case turn latency is about 60 s, and the widget never gives up
- **Category:** Reliability / Network
- **Severity:** Medium
- **Impact:** tail latency, thread hold time (feeds F1), and visitor experience.
- **Evidence:**
  - `anthropic.Anthropic(timeout=20.0, max_retries=2)` in `app/llm.py:59`. The SDK retries timeouts,
    429 and 5xx, so the wall clock can reach about 20 s × 3 plus backoff, which is ≈ 60–70 s.
  - `web/widget.js:126` calls `fetch` with no `AbortController`. The visitor sees "…" with Send
    disabled for the whole time.
- **Why it's inefficient:** a 1–3 sentence answer that hasn't arrived in about 10 s will not
  arrive usefully. Waiting longer only holds a thread and the visitor. The fallback reply (human
  handoff) is already a graceful way to fail.
- **Recommended fix:**
  - Set `timeout=anthropic.Timeout(10.0, connect=3.0)` and `max_retries=1`, which caps the wait at
    about 21 s.
  - In the widget, abort the request after 25 s and show the existing error text.
  - Optionally make both configurable (`CLAUDE_TIMEOUT_S`).
- **Tradeoffs / Risks:** more fallbacks during short Anthropic blips (one retry instead of two).
  Measure the fallback rate with the token/latency log from F4 before and after.
- **Expected impact estimate:** worst-case thread hold time drops by about 3× (≈60 s → ≈21 s).
  Typical latency is unchanged.
- **Removal Safety:** Likely Safe. `tests/test_claude_model.py` may assert the constructor arguments.
- **Reuse Scope:** `app/llm.py`, `web/widget.js`.

### F3. `clean_text` normalizes the full input before truncating: about 45× CPU amplification
- **Category:** CPU / Security-impacting inefficiency
- **Severity:** Medium
- **Impact:** CPU per request under hostile input, and fairness of the shared GIL.
- **Evidence:**
  - `app/guard.py:37-42` runs NFKC normalization, then a per-character Python loop calling
    `unicodedata.category()`, and only then slices to `max_chars`.
  - `ChatRequest.message` accepts up to **4,000** characters (`app/main.py:75`), while the engine
    keeps only **1,000** (`config.max_message_chars`). The widget also caps input at 1,000.
  - Measured:

    | Input (4,000 characters) | Length after NFKC | `clean_text` time |
    |---|---|---|
    | ASCII | 4,000 | 2.1 ms |
    | U+FDFA (`ﷺ`) | **72,000** | **96.6 ms** |

    The U+FDFA payload is 12 KB of UTF-8, under the 16 KB body limit.
- **Why it's inefficient:** up to 71,000 of those characters are processed only to be thrown
  away. Each request is rate-limited per IP (20/min), but distributed senders can push one core to
  100% at about 10 requests/s.
- **Recommended fix:** truncate twice, first at `max_chars` before normalization and again after
  it. Also set `ChatRequest.message` to `max_length=1000`, or derive it from settings, so the
  three limits agree.
- **Tradeoffs / Risks:**
  - Cutting before NFKC can split a combining sequence at the boundary. That is harmless, because
    the text is cut at the same place anyway.
  - Lowering the Pydantic limit turns silently truncated 1,001–4,000-character messages into HTTP
    422 responses. The widget never sends those, so this only affects direct API callers.
- **Expected impact estimate:** worst case drops from 97 ms to about 25 ms or less, because NFKC
  now only sees 1,000 characters. With `max_length=1000`, input stays bounded at 1,000 characters
  before any work is done.
- **Removal Safety:** Safe. `tests/test_injection.py` covers the cleaning semantics.
- **Reuse Scope:** `app/guard.py`, `app/main.py`.

### F4. No per-call token or cost logging in production
- **Category:** Cost (observability)
- **Severity:** Medium
- **Impact:** cost visibility, budget tuning, and the honesty of CASE_STUDY numbers.
- **Evidence:**
  - `Answer.input_tokens` and `Answer.output_tokens` are filled in by `ClaudeModel`
    (`app/llm.py:91`) but read only by `scripts/eval.py`.
  - The per-turn log line (`app/main.py:160`) records only `type` and `ms`.
  - `LLM_DAILY_CALL_BUDGET` counts calls, not tokens.
- **Why it's inefficient:** you can't tell whether the 1,000-call/day budget is about $1 or about
  $10, or spot growth in context or history size. The case study can't report measured real-model
  cost from production.
- **Recommended fix:** in `ClaudeModel.answer`, log one JSON line per call with `in`, `out`, `ms`
  and `model`. Never log text, so there is no personal data. Optionally add `usage.cache_*` fields
  if caching is ever enabled.
- **Tradeoffs / Risks:** one extra log line per LLM call. Negligible.
- **Expected impact estimate:** no runtime gain. It enables every cost decision below.
  Cost per call is **estimated** at about $0.0012 (≈800 input tokens × $1/M + ≈80 output × $5/M on
  Haiku 4.5). The token counts are assumptions from a ≈300-token system prompt, ≈170 tokens of
  context and ≤6 history turns, and must be confirmed with this log.
- **Removal Safety:** Safe.
- **Reuse Scope:** `app/llm.py`.

### F5. Lead webhook is fire-and-forget, with no retry
- **Category:** Reliability
- **Severity:** Medium (business impact: a lead the front desk never sees)
- **Impact:** lead delivery to n8n, email and Sheets.
- **Evidence:** `LeadStore._notify` (`app/leads.py:176-187`) makes a single POST with a 5 s timeout.
  On any failure it only calls `log.warning`. Nothing ever re-sends the lead.
- **Why it's inefficient:** n8n restarts, deploys and 502s are routine. The CSV stays correct, but
  the workflow that actually alerts staff silently misses that lead.
- **Recommended fix:**
  - Retry inside the existing background thread: up to 3 attempts with backoff of about 1 s, 4 s
    and 16 s plus jitter. Retry only on connection errors and 5xx, never on 4xx.
  - Log `error` (not `warning`) after the last attempt.
  - Longer-term option: mark the CSV row as `notified` and re-send unnotified rows from the purge loop.
- **Tradeoffs / Risks:** a retry can deliver a lead twice if n8n handled the request but the
  response was lost. Dedupe in n8n on `session_id` + `created_at`.
- **Expected impact estimate:** recovers transient webhook failures. The rate can't be quantified
  without production logs.
- **Removal Safety:** Safe.
- **Reuse Scope:** `app/leads.py`.

### F6. Config defaults are copied in four places, and the input limits disagree
- **Category:** Maintainability (Reuse Opportunity)
- **Severity:** Medium
- **Impact:** drift risk and bug surface.
- **Evidence:**
  - Each tunable default appears in `Settings` field defaults (`app/config.py:22-43`), again as
    string literals in `from_env()` (`:56-71`), in `docker-compose.yml:9-23` and in `.env.example`.
    For example, `MIN_RETRIEVAL_SCORE=2.0` appears 4 times and the model ID 4 times.
  - The message length limit is 4,000 in Pydantic, 1,000 in Settings and 1,000 in the widget.
- **Why it's inefficient:** changing a default in one place silently leaves the others stale.
  The Docker defaults would then differ from `pip install` runs, so measured behaviour would depend
  on how the app was started.
- **Recommended fix:**
  - In `from_env`, read each variable through a helper that falls back to
    `cls.__dataclass_fields__[name].default` when the variable is unset or empty.
  - In `docker-compose.yml`, pass variables through as `${VAR:-}` with no literal defaults.
  - Keep `.env.example` as documentation only.
  - Derive the Pydantic `max_length` from the same constant (see F3).
- **Tradeoffs / Risks:** compose loses its self-documenting defaults (`.env.example` keeps them).
  An empty string now means "default", which is already true for the string fields.
- **Expected impact estimate:** each default lives in 1 place instead of 4. No runtime change.
- **Removal Safety:** Likely Safe. Run the full test suite plus the CI docker smoke test.
- **Reuse Scope:** module (`app/config.py`) plus deploy files.

### F7. `widget.js` is served with no `Cache-Control`, no 304 and no compression
- **Category:** Network / Frontend
- **Severity:** Low (Medium if a client site has high page-view traffic)
- **Impact:** bandwidth and requests to the assistant server on every page view of the embedding site.
- **Evidence:** measured with `TestClient`:
  - `/widget.js` returns 200 with 7,312 bytes, `ETag` and `Last-Modified`, but **no
    `Cache-Control`**.
  - Repeating the request with `If-None-Match` still returns **200 with the full body** (no 304).
  - With `Accept-Encoding: gzip` there is no `Content-Encoding`.
  - The widget is loaded on every page of the client's site, not only when someone chats.
- **Why it's inefficient:** browsers fall back to heuristic caching. Every miss is a full download
  and occupies a thread from the default pool (see F1).
- **Recommended fix:**
  - Add `headers={"Cache-Control": "public, max-age=3600"}` to the widget `FileResponse`.
  - Optionally add `GZipMiddleware(minimum_size=1024)`.
  - For long-lived caching, version the URL (`/widget.js?v=<hash>`) and use `max-age=31536000, immutable`.
- **Tradeoffs / Risks:** a widget fix takes up to `max-age` to reach visitors. One hour is a safe default.
- **Expected impact estimate (estimate):** repeat page views make 0 requests within the hour.
  Gzip should shrink 7.3 KB to about 2.5 KB (typical ratio for minified-style JavaScript; not measured).
- **Removal Safety:** Safe.
- **Reuse Scope:** `app/main.py`.

### F8. Cost lever, not urgent: Claude Haiku 5.5 is listed at 10× lower per-token price
- **Category:** Cost
- **Severity:** Low. The absolute spend is small: the default budget caps it at about $1.2 per day
  (**estimate**, from F4).
- **Impact:** cost per answer.
- **Evidence:** the project brief ([CLAUDE.md](CLAUDE.md)) sets `claude-haiku-4-5-20251001` as the default ($1 / $5 per million
  input/output tokens). Claude Haiku 5.5 (`claude-haiku-5-5`) is listed at $0.10 / $0.50 for prompts
  of 100K tokens or fewer. The model is already switchable through `CLAUDE_MODEL`.
- **Why it matters:** list price per answer would be about 7–8× lower, even after Haiku 5.5's
  tokenizer counts the same text as about 30% more tokens.
- **Required before switching (not a drop-in change):**
  - On Haiku 5.5, thinking is **on by default** (adaptive, effort `medium`). Thinking tokens count
    against `max_tokens=400` (`app/config.py:41`), so the visible answer could be cut off and
    latency would rise.
  - Send `thinking={"type": "disabled"}`, or `output_config={"effort": "low"}`, when the model is
    Haiku 5.5.
  - Haiku 5.5 can return `stop_reason: "refusal"` with no fallback. The current code already treats
    "no text" as `NO_ANSWER`, which becomes the human handoff, so that case is safe.
- **Recommended fix:** keep Haiku 4.5 as the default, as the project brief requires. Run `scripts/eval.py`
  with a real key on both models and compare grounded answers, out-of-scope handling, injection
  results and cost. Switch only if quality holds.
- **Tradeoffs / Risks:** the quality difference on this task is unmeasured, and the eval set has
  only 30 in-scope questions.
- **Expected impact estimate (estimate):** about 85% lower LLM spend, which is small in dollars at
  SME volumes.
- **Removal Safety:** Needs Verification (real-model eval).
- **Reuse Scope:** `app/llm.py` (thinking parameter per model), config.

### F9. History keeps 12 turns but sends 6, and sends canned fallback text to the model
- **Category:** Memory / Cost
- **Severity:** Low
- **Impact:** memory per session and input tokens per call.
- **Evidence:**
  - `sess.history[-12:]` (`app/chat.py:138`), but `ClaudeModel` sends only `history[-6:]`
    (`app/llm.py:71`), and the follow-up logic reads only the last user turn.
  - `fallback` replies (about 60 tokens of canned "I'm not sure… type book") are stored as
    assistant turns (`app/chat.py:136`) and sent back to the model.
- **Why it's inefficient:** half of the stored history is never read. Worst case is about 16 KB
  per session × 5,000 sessions ≈ 80 MB (**estimate**: 1,000-character questions and 400-token
  answers). The canned text costs tokens and adds nothing for grounding.
- **Recommended fix:** trim to `[-6:]`. Store fallback turns as the user question only, or replace
  the reply with a short marker such as `(no answer found)`.
- **Tradeoffs / Risks:** check that follow-ups after a fallback still work. They use only the
  user turn, so they should.
- **Expected impact estimate:** about 50% less worst-case session memory and about 60 fewer input
  tokens per call after a fallback.
- **Removal Safety:** Safe (Dead Code: unused history depth).
- **Reuse Scope:** `app/chat.py`.

### F10. Retrieval repeats work on the follow-up path, and full scan is O(chunks × terms)
- **Category:** Algorithm
- **Severity:** Low. No action needed at the current content size.
- **Impact:** CPU per turn, measured in microseconds.
- **Evidence:**
  - A follow-up question that misses runs `coverage()` 3 times and `search()` twice
    (`app/chat.py:154-168`). About 0.5 ms in total, measured.
  - `Retriever.search` scores every chunk (`app/retrieval.py:166-179`). That costs 0.19 ms for
    26 chunks.
- **Why it's not worth fixing now:** it is under 0.1% of a real turn, which is dominated by the
  roughly 1 s or more Claude call (**estimate**).
- **When to act:** if the content grows past about 2,000 sections (multi-location clinics, full
  site crawl), build an inverted index (`term → [(chunk_idx, tf)]`) so only chunks containing the
  query terms are scored.
- **Removal Safety:** n/a
- **Reuse Scope:** `app/retrieval.py`.

### F11. Sessions and limiter keys are created for requests that don't need them
- **Category:** Memory
- **Severity:** Low
- **Impact:** session eviction churn.
- **Evidence:**
  - `ChatEngine.handle` calls `sessions.get()` before checking for empty text (`app/chat.py:127-130`),
    so blank messages without a `session_id` still allocate a session.
  - `SlidingWindowLimiter.allowed()` inserts an empty deque for unseen keys (`app/limits.py:22-28`),
    even though it is documented as "does not record anything".
- **Why it's inefficient:** both are bounded by LRU, but they speed up eviction of real entries:
  - Real sessions (a visitor halfway through booking) get pushed out of the 5,000-session cap.
  - Real lead counters get pushed out of the 10,000-key cap.
- **Recommended fix:**
  - Clean the text first. Return early without creating a session when there is no `session_id`
    and the text is empty.
  - In `allowed()`, read with `self._events.get(key)` and don't insert.
- **Removal Safety:** Safe.
- **Reuse Scope:** `app/chat.py`, `app/limits.py`.

### F12. `security_headers` uses `BaseHTTPMiddleware`
- **Category:** CPU / Reliability
- **Severity:** Low
- **Evidence:** `@app.middleware("http")` (`app/main.py:120`) wraps every response in Starlette's
  `BaseHTTPMiddleware`. That adds a task plus a stream copy per request, and has known caveats with
  streaming responses and contextvars.
- **Recommended fix:** rewrite it as a small pure-ASGI middleware (about 20 lines, like
  `BodySizeLimit`) that adds the headers in the `send` wrapper on `http.response.start`.
- **Tradeoffs / Risks:** slightly more code. The ordering with `BodySizeLimit` must stay the same:
  headers wrap the 413 response, and `tests/test_api.py` checks this.
- **Expected impact estimate:** small. Likely well under 1 ms per request; measure before bothering.
- **Removal Safety:** Likely Safe.
- **Reuse Scope:** `app/main.py`.

### F13. Measurement accuracy in `scripts/eval.py`
- **Category:** Cost (measurement)
- **Severity:** Low. It matters because CASE_STUDY numbers come from this script.
- **Evidence:**
  - `calls = n_in` (`scripts/eval.py:97`) prints "~30 model calls", but the actual number of calls
    differs: fallbacks skip the model, and follow-ups or retries do not.
  - `engine._relevant(q)` is called and then `ask(q)` retrieves again (`:59-62`), using a private
    method.
  - Prices are hard-coded for Haiku 4.5 only, even when `CLAUDE_MODEL` is changed (see F8).
- **Recommended fix:**
  - Count calls in the existing `counting()` wrapper.
  - Choose prices from a small `{model: (in, out)}` table and print "unknown price" otherwise.
  - Optionally expose a public `relevant()` method.
- **Removal Safety:** Safe.
- **Reuse Scope:** `scripts/eval.py`.

### F14. Docker image pulls dependencies production doesn't use (likely)
- **Category:** Build
- **Severity:** Low
- **Evidence:** `uvicorn[standard]` (`requirements.txt:2`) installs `watchfiles` (only used by
  `--reload`) and `websockets` (the app has no WebSocket routes), in addition to `uvloop` and
  `httptools`, which do help.
- **Recommended fix:** depend on `uvicorn` plus `uvloop` and `httptools` explicitly. Measure with
  `docker image ls` before and after.
- **Tradeoffs / Risks:** `watchfiles` is gone for local `--reload`; put it in `requirements-dev.txt`.
- **Expected impact estimate:** a few MB and less dependency surface. Not measured.
- **Removal Safety:** Needs Verification (image size and the CI smoke test).
- **Reuse Scope:** build.

### Code reuse and dead code check

| Item | Classification | Verdict |
|---|---|---|
| Duplicated defaults (F6) | Reuse Opportunity | Fix |
| `history[-12:]` vs `[-6:]` (F9) | Dead Code (unused depth) | Safe to trim |
| `LeadStore.read_all()` (`app/leads.py:226`) | Used only by `tests/test_limits.py:106` | Keep. It is 4 lines and a natural API for the owner. Likely Safe to move into tests if you want production code to have zero test-only paths |
| `MockModel.answer` `if not hits` branch | Unreachable from `ChatEngine` (it returns before calling the model) | Keep. It is part of the `AnswerModel` contract and covered by tests |
| `REQUIRED_DRAFT_KEYS` guard (`app/chat.py:225`) | Defensive branch, normally dead | Keep. It turns a broken state into a clear message instead of a 500 |
| Repeated `Reply(self._fallback(), "fallback")` (4×) | Minor duplication | Not worth abstracting |

No over-abstracted code was found. The module split (`guard`, `limits`, `leads`, `retrieval`, `llm`,
`chat`) matches real responsibilities.

---

## 3) Quick Wins (Do First)

| # | Change | Effort | Value |
|---|---|---|---|
| 1 | `async def healthz` (F1a) | 1 line | Health check never queues behind chats |
| 2 | Truncate before NFKC, and Pydantic `max_length=1000` (F3) | 3 lines | Removes 4× input waste and 18× expansion |
| 3 | Claude `timeout=10, max_retries=1`, plus a 25 s widget abort (F2) | ~10 lines | About 3× shorter worst-case hold time |
| 4 | Per-call token log line (F4) | ~5 lines | Real cost visibility |
| 5 | `Cache-Control` on `widget.js` (F7) | 1 line | Repeat page views make no request |
| 6 | Trim history to 6, and don't feed fallback text to the model (F9) | 2 lines | Less memory, fewer tokens |
| 7 | `allowed()` without insert, and no session for empty text (F11) | ~4 lines | Less LRU churn |

## 4) Deeper Optimizations (Do Next)

1. **Dedicated `CapacityLimiter` for chats (F1b)**, or later a move to `AsyncAnthropic`. The limiter
   is about 5 lines and keeps the engine synchronous. `AsyncAnthropic` would mean making
   `ChatEngine` async and swapping `threading.Lock` for `asyncio.Lock`. That is only worth it if the
   app ever needs hundreds of concurrent chats, which an SME widget does not.
2. **Webhook retry with backoff, and optionally a re-send of unnotified rows (F5).**
3. **Single source of truth for config (F6).**
4. **Real-model eval of Haiku 4.5 vs Haiku 5.5 with thinking off (F8, F13)**, before any change to
   the default model.
5. **Shared state (Redis or SQLite) for sessions and limits**, only if you need more than one
   worker or zero-downtime deploys. This is already listed in the README limitations. Not needed
   for SME traffic: one process handles about 13 turns/s (**estimate**, F1).

### Considered and rejected (not worth doing)

- **Prompt caching:** the system prompt is about 300 tokens. Haiku 4.5's minimum cacheable prefix
  is **4,096 tokens**, so a `cache_control` marker would silently do nothing. Revisit only if the
  prompt or fixed context grows past that.
- **Answer cache for repeated FAQ questions:** it would save about $0.001 per hit (**estimate**)
  and about 1 s of latency, but it brings cache invalidation on every content edit and conflicts
  with history-dependent answers. Low ROI at SME volumes.
- **Streaming replies to the widget:** answers are 1–3 sentences, so time-to-first-token savings
  are small compared with the work of adding SSE to the widget and the API.
- **Embeddings or a vector index:** rejected in BUILD_LOG for dependency weight. Nothing in this
  audit changes that. BM25 takes 0.19 ms.

---

## 5) Validation Plan

**Correctness (every change):**
```bash
MOCK_LLM=1 pytest -q            # baseline: 149 passed
MOCK_LLM=1 python scripts/eval.py   # baseline: 26/30 answered, 25/30 grounded, 9/10 OOS, 8/8 inj, 4/4 booking
```
Any change to these numbers is a regression unless it is intended.

**F1, thread starvation:** re-run the repro below. Target: `/healthz` under 0.05 s at 45 or more
concurrent slow chats.
```python
# model stub: time.sleep(3) inside answer(); rate_limit_per_minute=0; httpx.AsyncClient(ASGITransport(app))
# fire N concurrent POST /api/chat plus one GET /healthz after 0.3 s and time it
# baseline: N=10 -> 0.01 s, N=45 -> 2.94 s
```
Add it as a pytest test that asserts `/healthz` returns in under 0.5 s with 45 blocked chats.

**F2, timeouts:**
- Unit test that `ClaudeModel` builds the client with the new timeout and retries (the existing
  fake-client test pattern).
- Manually point `ANTHROPIC_BASE_URL` at a black-hole port and check that the visitor gets the
  fallback within about 21 s.

**F3, input cost:**
```bash
python -c "import timeit;from app import guard;t='ﷺ'*4000;print(timeit.timeit(lambda:guard.clean_text(t,1000),number=20)/20*1e3,'ms')"
# baseline 96.6 ms -> target ≤ 25 ms
```
Plus the existing `tests/test_injection.py`.

**F4, cost:** with a real key, run `scripts/eval.py` and check that the sum of the logged `in`/`out`
tokens matches the script's totals. This gives the first measured real-model cost per answer for
CASE_STUDY.

**F7, caching:** `curl -sI localhost:8000/widget.js | grep -i cache-control`. With gzip, also run
`curl -s -H 'Accept-Encoding: gzip' localhost:8000/widget.js | wc -c`.

**F14, image size:** `docker image ls` before and after, plus the CI docker smoke job.

**Metrics to compare before and after:**
- p50/p95 `ms` from the per-turn log line
- `/healthz` latency under load
- `fallback` share of replies (watch it after F2)
- tokens per call (F4)
- container RSS: `docker stats`

---

## 6) Optimized Code / Patch (suggested, not applied)

**F1, isolate chats from the default thread pool (`app/main.py`):**
```python
import anyio
...
    chat_limiter = anyio.CapacityLimiter(20)  # chats may use at most 20 of the 40 default threads

    @app.get("/healthz")
    async def healthz() -> dict:  # async: never waits for a worker thread
        return {"status": "ok"}

    @app.post("/api/chat", response_model=ChatResponse, responses={429: {}, 413: {}})
    async def chat(req: ChatRequest, request: Request):
        client_ip = request.client.host if request.client else "unknown"
        if not per_ip.hit(client_ip):
            return JSONResponse(...)  # unchanged
        started = time.perf_counter()
        session_id, reply = await anyio.to_thread.run_sync(
            engine.handle, req.session_id, req.message, client_ip, limiter=chat_limiter
        )
        ...  # unchanged
```
What changed: `healthz` no longer uses a thread, and chats run on their own 20-slot limiter, so
static files and health checks always have threads left.

**F2, bounded upstream wait (`app/llm.py`):**
```python
self.client = anthropic.Anthropic(
    api_key=api_key, timeout=anthropic.Timeout(10.0, connect=3.0), max_retries=1
)
```
In `web/widget.js`:
```js
var ctrl = typeof AbortController === "function" ? new AbortController() : null;
var timer = ctrl && setTimeout(function () { ctrl.abort(); }, 25000);
fetch(API + "/api/chat", { method: "POST", signal: ctrl && ctrl.signal, /* ... */ })
  /* ...existing chain... */
  .then(function () { clearTimeout(timer); send.disabled = false; input.focus(); });
```

**F3, truncate before normalizing (`app/guard.py`):**
```python
def clean_text(text: str, max_chars: int) -> str:
    """Normalise unicode, drop control characters, collapse whitespace, cap length."""
    text = unicodedata.normalize("NFKC", text[:max_chars])  # cut first: NFKC can expand 18x
    text = "".join(ch for ch in text if ch in "\n\t" or unicodedata.category(ch)[0] != "C")
    text = re.sub(r"\s+", " ", text).strip()
    return text[:max_chars]
```
In `app/main.py`: `message: str = Field(min_length=1, max_length=1000)`.

**F4, token log (`app/llm.py`, end of `ClaudeModel.answer`):**
```python
text = "".join(b.text for b in resp.content if b.type == "text").strip()
# Token counts only, never text: cost visibility without personal data.
log.info(json.dumps({"event": "llm", "model": self.name,
                     "in": resp.usage.input_tokens, "out": resp.usage.output_tokens}))
return Answer(text, resp.usage.input_tokens, resp.usage.output_tokens)
```

**F5, webhook retry (`app/leads.py`):**
```python
def _notify(self, lead: Lead) -> None:
    ...
    for attempt in range(3):
        try:
            _WEBHOOK_OPENER.open(req, timeout=5).close()
            return
        except urllib.error.HTTPError as exc:
            if exc.code < 500:  # 4xx: our request is wrong, retrying won't help
                break
        except Exception:
            pass
        time.sleep(4 ** attempt + random.random())  # ~1s, ~4s, ~16s
    log.error("Lead webhook failed after retries (lead is saved in CSV)")
```

**F6, one source of truth for defaults (`app/config.py`):**
```python
@classmethod
def from_env(cls) -> "Settings":
    def env(var: str, field_name: str, cast=str):
        raw = os.getenv(var, "").strip()
        return cast(raw) if raw else cls.__dataclass_fields__[field_name].default
    ...
    min_retrieval_score=env("MIN_RETRIEVAL_SCORE", "min_retrieval_score", float),
    rate_limit_per_minute=env("RATE_LIMIT_PER_MINUTE", "rate_limit_per_minute", int),
    ...
```
In `docker-compose.yml`: `MIN_RETRIEVAL_SCORE: ${MIN_RETRIEVAL_SCORE:-}`, with the same pattern
for every tunable.

**F7, widget caching (`app/main.py`):**
```python
return FileResponse(WEB_DIR / "widget.js", media_type="application/javascript",
                    headers={"Cache-Control": "public, max-age=3600"})
```

**F9 and F11, small trims:**
```python
# app/chat.py handle(): history depth matches what the model is sent
sess.history = sess.history[-6:]
# app/limits.py allowed(): read-only check
events = self._events.get(key)
return events is None or sum(1 for t in events if now - t < self.window) < self.limit
```

---

## Applied

Applied 2026-10-09 in [pull request #1](https://github.com/Melwis880/smb-website-assistant/pull/1), one commit per finding.
`MOCK_LLM=1 pytest -q` passed after every commit. The suite went from 149 to **158 tests**.
All numbers are measured on the fictional demo data in mock mode, on the same machine, before and
after, with the same script.

| Finding | Commit | What changed |
|---|---|---|
| F1 (a + b) | `19726f9` | `/healthz` is `async`. Chats run on a dedicated 20-thread `ThreadPoolExecutor` instead of the shared pool. New test: `tests/test_slow_llm.py` |
| F2 | `4390597` | Claude client `Timeout(10 s, connect=3 s)`, `max_retries=1`. The widget aborts after 25 s |
| F3 | `a6f5460` | `clean_text` cuts before and after NFKC. One `MAX_MESSAGE_CHARS = 1000` for API, engine and widget (longer messages get 422) |
| F4 | `f589f18` | One JSON log line per Claude call: model, input/output tokens, ms. No text |
| F5 | `0a52da4` | Webhook: 3 attempts on network errors and 5xx (waits ≈1 s, then ≈4 s, with jitter). 4xx and redirects are not retried. n8n README updated |
| F7 | `f148ba0` | `widget.js` sends `Cache-Control: public, max-age=3600` |
| F9 | `f977585` | Sessions keep 6 history turns (was 12). Fallback replies are stored as a short note, not the canned text |
| F11 | `8aee1ac` | Empty messages create no session. `SlidingWindowLimiter.allowed()` no longer inserts keys |
| F13 | `78a35f4` | `eval.py` counts real model calls and prices models from a table (no price, no cost line) |

### Measured before / after (demo data, mock mode)

| Measurement | Before (`a0a527e`) | After (`78a35f4`) |
|---|---|---|
| `/healthz` latency with 45 chats each blocked 3 s | **2.80 s** | **0.00–0.01 s** (3 runs) |
| `/widget.js` latency in the same scenario | 2.81 s | 0.00–0.01 s |
| Time until all 45 blocked chats finish | 6.1 s | **9.1 s** (see tradeoff below) |
| Time until 20 blocked chats finish | 3.1 s | 3.1 s |
| `clean_text`, 4,000 × U+FDFA (best of 5 × 20) | 32.9 ms | **7.4 ms** (−78%) |
| `clean_text`, 4,000 ASCII characters | 1.09 ms | 0.29 ms |
| Largest message the API accepts | 4,000 characters | 1,000 characters (now the same as the widget) |
| `widget.js` `Cache-Control` | none | `public, max-age=3600` |
| History turns stored per session | up to 12 | up to 6 |
| `eval.py` quality metrics (26/30, 25/30, 23/30, 25/30, 9/10, 8/8, 4/4) | baseline | **identical**, same misses |
| Model calls in an eval run | printed as "~30" (assumed) | 27 (counted) |
| Worst-case wait for Claude per turn | ≈60–70 s (3 × 20 s plus backoff) | ≈21 s (2 × 10 s plus backoff). Configured, not measured against the real API |

Measurement notes:
- The audit above quoted 96.6 ms for the U+FDFA case. That was one average including a cold start.
  The controlled best-of-5 baseline on the same machine is 32.9 ms, and the before/after comparison
  uses that method on both sides.
- The F1 test fails on the old code: `/healthz` doesn't answer within 0.5 s while 45 chats hang.
  It passes on the new code, and every queued chat is still answered.

**F1 tradeoff (measured):** with 45 chats blocked at once, the last one now finishes after 9.1 s
instead of 6.1 s, because chats run 20 at a time instead of 40. Up to 20 at once, nothing changes.
That trade is deliberate: the health check and static files stay responsive, and an SME widget
rarely has more than 20 chats waiting on Claude at the same moment (**estimate**).

### Deviations from the suggested patch
- **F1:** a `ThreadPoolExecutor` instead of `anyio.CapacityLimiter`. An AnyIO limiter binds to the
  event loop where it is first used, and Starlette's `TestClient` starts a new loop per request.
  An executor doesn't depend on any loop.
- **F5:** two waits (≈1 s, ≈4 s), not three. With 3 attempts there are only two gaps.
- **F7:** `Cache-Control` only. Gzip was not added: it would also wrap the JSON API and the 413
  path for about 5 KB saved on a file that browsers now cache.

### Deferred, and why
- **F6 (single source of truth for config):** it touches every setting, `docker-compose.yml` and
  `.env.example` at once. It deserves its own reviewed change with the CI Docker smoke test.
  Only the message length was unified here (part of F3).
- **F8 (Haiku 5.5):** the project brief fixes Haiku 4.5 as the default, and a switch needs a real-model eval
  with a key plus a thinking setting. Not possible in mock mode.
- **F10 (inverted index):** BM25 takes 0.19 ms over 26 sections. Only worth doing past about 2,000 sections.
- **F12 (pure-ASGI header middleware):** expected gain is well under 1 ms per request and not measured.
  The rewrite risks changing the header order on 413 responses for no proven benefit.
- **F14 (slimmer uvicorn extras):** image size wasn't measured, and the change needs the Docker smoke test.

### Still not measured
Real-model latency, token counts and cost. F4 now logs them per call, and `scripts/eval.py` reports
calls, tokens and cost when run with `MOCK_LLM=0 ANTHROPIC_API_KEY=…`.
