"""Run the demo eval set through the full chat engine and print measured numbers.

    python scripts/eval.py            # mock mode (no API key needed)
    MOCK_LLM=0 ANTHROPIC_API_KEY=... python scripts/eval.py   # real model, reports tokens and cost

All numbers are measured on the fictional demo data in data/ and eval/questions.json.
"""

from __future__ import annotations

import json
import statistics
import sys
import tempfile
import time
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.chat import ChatEngine  # noqa: E402
from app.config import Settings  # noqa: E402

# USD per million input/output tokens, list prices (check current pricing before quoting).
# A model not listed here gets token counts but no cost line, rather than a wrong one.
PRICES = {
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-haiku-4-5-20251001": (1.00, 5.00),
}


def main() -> int:
    qs = json.loads((ROOT / "eval" / "questions.json").read_text())
    with tempfile.TemporaryDirectory() as tmp:
        settings = replace(Settings.from_env(), leads_path=Path(tmp) / "leads.csv")
        engine = ChatEngine(settings)
        calls = tokens_in = tokens_out = 0
        inner = engine.model.answer  # wrap to count real calls and usage (mock reports 0 tokens)

        def counting(*a, **kw):
            nonlocal calls, tokens_in, tokens_out
            ans = inner(*a, **kw)
            calls += 1
            tokens_in += ans.input_tokens
            tokens_out += ans.output_tokens
            return ans

        engine.model.answer = counting  # type: ignore[method-assign]

        latencies: list[float] = []
        failures: list[str] = []

        def ask(q: str):
            t = time.perf_counter()
            _, reply = engine.handle(None, q)
            latencies.append((time.perf_counter() - t) * 1000)
            return reply

        top1 = top3 = answered = grounded = 0
        for q, title in qs["in_scope"]:
            ranked = [h.chunk.title for h in engine._relevant(q)]
            top1 += bool(ranked) and ranked[0] == title
            top3 += title in ranked[:3]
            reply = ask(q)
            is_answer = reply.type == "answer"
            # "grounded" = answered AND the expected section was among the sections given to the model
            right_section = is_answer and any(src.endswith(f"> {title}") for src in reply.sources)
            answered += is_answer
            grounded += right_section
            if not right_section:
                kind = "WRONG SECTION" if is_answer else reply.type
                failures.append(f"in_scope: {q!r} -> {kind}, top3={ranked[:3]}")

        def rate(group: str, expected: str) -> int:
            n = 0
            for q in qs[group]:
                reply = ask(q)
                n += reply.type == expected
                if reply.type != expected:
                    failures.append(f"{group}: {q!r} -> {reply.type}: {reply.reply[:80]}")
            return n

        oos = rate("out_of_scope", "fallback")
        inj = rate("injection", "refusal")
        book = rate("booking", "lead_question")

    n_in = len(qs["in_scope"])
    print(f"Model: {engine.model.name}  (DEMO DATA - fictional clinic)")
    print(f"In-scope answered:         {answered}/{n_in}")
    print(f"  ...from expected section: {grounded}/{n_in}")
    print(f"  ...without it (wrong):    {answered - grounded}/{n_in}")
    print(f"Retrieval top-1 correct:   {top1}/{n_in}")
    print(f"Retrieval top-3 correct:   {top3}/{n_in}")
    print(f"Out-of-scope -> handoff:   {oos}/{len(qs['out_of_scope'])}")
    print(f"Injection -> refused:      {inj}/{len(qs['injection'])}")
    print(f"Booking -> lead flow:      {book}/{len(qs['booking'])}")
    print(f"Latency per turn (ms):     median {statistics.median(latencies):.1f}, max {max(latencies):.1f}")
    print(f"Model calls:               {calls} (questions refused or handed off never reach the model)")
    if tokens_in:
        print(f"Tokens: {tokens_in} in / {tokens_out} out over {calls} model calls")
        if engine.model.name in PRICES:
            price_in, price_out = PRICES[engine.model.name]
            cost = tokens_in / 1e6 * price_in + tokens_out / 1e6 * price_out
            print(f"Cost: ${cost:.4f} total, ${cost / calls:.5f} per model call (list price)")
        else:
            print(f"Cost: no price for {engine.model.name!r} in PRICES; add it to get a cost line")
    if failures:
        print("\nMisses:")
        print("\n".join(f"  - {f}" for f in failures))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
