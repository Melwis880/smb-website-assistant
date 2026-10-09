# Case study: a website assistant that answers from your own content and books leads

*Demo client: "Brightsmile Dental", a fictional dental clinic. All business data is invented.
This build has not been deployed at a real business. Nothing below is a client result.*

## 1. What problem does it solve?

A small clinic's front desk answers the same handful of questions all day by phone and email:
*How much is a check-up? Are you open Saturday? Do you bill my insurer? Do you do Invisalign?*
Each answer takes a few minutes, interrupts patients standing at the desk, and stops at closing
time. Visitors who browse the website in the evening either wait until the next day or leave.

A generic chatbot doesn't fix this. It makes up prices, answers questions about the weather, and
can be talked into "confirming" a free treatment. And a clinic can't afford to store contact
details without consent.

So the job is narrow: **answer correctly from the clinic's own content or say "I don't know" and
hand off; turn booking intent into a clean, consented lead the front desk can call back.**

## 2. How does it work?

1. **Content in Markdown.** Services, prices, hours, FAQ and policies live in five Markdown files
   the owner can edit. Each `##` section is one retrievable unit.
2. **Retrieval first.** For every question, a keyword search (BM25, pure Python) finds the most
   relevant sections. If nothing relevant is found, the assistant hands off to a human
   *without calling the AI model at all*.
3. **Grounded answer.** Claude (Haiku 4.5 by default) gets only those sections and strict rules:
   answer from this context or reply `NO_ANSWER`, which becomes a polite handoff.
4. **Booking flow as code, not AI.** When a visitor wants an appointment, a small state machine
   asks for name, email/phone and preferred time, validates each, and asks an explicit yes/no
   consent question. Only "yes" writes a row to `leads.csv` (and optionally triggers an n8n
   workflow that adds it to Google Sheets and emails the front desk).
5. **Layered injection defence.** Input filter, visitor text marked as data, a canary in the
   system prompt to catch leaks, and consent that no prompt can bypass.
6. **One script tag** embeds the chat widget on any website.

Architecture diagram and details: [README.md](README.md). Decisions and dead ends: [BUILD_LOG.md](BUILD_LOG.md).

## 3. What is the concrete result in a real business?

**There is no real-business result yet**: this is a demo build. What *can* be stated:

**Measured on demo data** (`python scripts/eval.py`, mock mode, 52 hand-written questions; the
set was written by the builder after the retriever, so it is a smoke test, not a blind benchmark):

- 25 of 30 in-scope questions answered from the expected section. 4 ended in a human handoff
  (no answer). **1 was answered from the wrong section**: "My tooth broke on Sunday night, what
  should I do?" got the Sunday opening hours instead of the out-of-hours emergency advice.
- 9 of 10 off-topic questions handed off without an answer. The miss ("weather this weekend")
  matched the Saturday opening hours.
- 8 of 8 prompt-injection attempts refused before reaching the model; 10 more in the test suite.
- 4 of 4 booking phrasings started the lead flow; no lead is ever stored without an explicit "yes"
  (enforced by tests).
- 92 automated tests pass in CI without an API key.

**Before / after**

| | Before (today) | After (with assistant) |
|---|---|---|
| Answer to a routine question (price, hours, insurance) | Phone/email during opening hours; minutes to next working day (**estimate**, assumes a typical small-clinic front desk) | Immediate, 24/7, with the source section shown (demo: retrieval + routing < 1 ms per turn in mock mode; real-model latency **not measured**) |
| Off-topic or unknown questions | Handled by staff | Handed off with phone/email instead of guessed (demo: 9/10 off-topic handed off) |
| Booking request outside opening hours | Voicemail or lost visitor (**estimate**) | Validated, consented lead in `leads.csv` / Google Sheet within seconds (demo flow tested) |
| Data quality of booking requests | Free-text emails, missing phone numbers (**estimate**) | Name + valid email/phone + preferred time + consent timestamp, every time (enforced in code) |
| Model cost per answered question | n/a | ≈ USD 0.001 (**estimate**: ~400 input tokens measured from the demo prompts by a 4-characters-per-token rule, +150 tokens of history, ~60 output tokens, at Haiku 4.5 list price of $1 / $5 per million tokens; off-topic questions cost $0 because the model isn't called) |
| Monthly model cost at 1,000 questions | n/a | ≈ USD 1 (**estimate**, same assumptions) |

**What a real pilot should measure** (2-4 weeks): share of chats answered without handoff; share
of answers the front desk marks as wrong (target: zero wrong prices); leads captured outside
opening hours; phone/email volume on routine questions compared with the weeks before. Those
are the numbers a clinic owner can act on, and none of them can be honestly claimed from a demo.
