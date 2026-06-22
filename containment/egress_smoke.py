"""Live egress smoke test — run INSIDE the kaizen container, on the containment
network, to prove the egress posture end to end:

  A. the provider host is reachable THROUGH the allowlisting proxy,
  B. the agent's REAL metered path works and is PRICED:
     substrate.metering.make_metered_complete -> litellm -> proxy -> DeepSeek.
     Note: DeepSeek's `deepseek-chat` reports response model `deepseek-v4-flash`
     (a newer backing model litellm's map doesn't know); the metering prices it by
     the REQUESTED id via cost_per_token, so the circuit breaker does NOT trip.
  C. a NON-allowlisted host (example.com) is DENIED by the proxy.

PASS requires B and C. Run (from the repo root):

  docker compose --env-file .env -f containment/docker-compose.yml down -v
  Get-Content containment/egress_smoke.py -Raw | docker compose --env-file .env \
      -f containment/docker-compose.yml run -T --rm --no-deps --entrypoint python kaizen -
"""

import sys

sys.path.insert(0, "/lineage")  # substrate
sys.path.insert(0, "/lineage/agent/src")  # spine (for the normalized Completion)


def main() -> int:
    import httpx

    # A. Connectivity: reachable through the proxy at all?
    try:
        r = httpx.get("https://api.deepseek.com", timeout=15)
        print(f"[egress] A reachable: api.deepseek.com -> HTTP {r.status_code}")
    except Exception as e:  # noqa: BLE001
        print(f"[egress] A UNREACHABLE: {type(e).__name__}: {e}")

    # B. The REAL agent path: the substrate's metered complete, priced by the meter.
    deepseek_ok = False
    try:
        from substrate.metering import CostMeter, make_metered_complete

        meter = CostMeter()
        complete = make_metered_complete(meter, model="deepseek/deepseek-chat")
        out = complete(
            "deepseek/deepseek-chat",
            [{"role": "user", "content": "Reply with exactly one word: pong"}],
            None,
        )
        print(
            f"[egress] B metered DeepSeek: reply={(out.content or '').strip()!r} "
            f"calls={meter.calls} cost=${meter.total_usd:.6f} tokens={meter.total_tokens} "
            f"unpriceable={meter.had_unpriceable} models={list(meter.by_model)}"
        )
        deepseek_ok = (
            meter.calls == 1 and not meter.had_unpriceable and meter.total_usd > 0
        )
    except Exception as e:  # noqa: BLE001
        print(f"[egress] B FAILED: {type(e).__name__}: {e}")

    # C. A genuinely non-allowlisted host must be denied by the proxy.
    other_blocked = False
    try:
        r = httpx.get("https://example.com", timeout=15)
        print(f"[egress] C example.com NOT blocked (HTTP {r.status_code}) — TOO OPEN!")
    except Exception as e:  # noqa: BLE001
        print(f"[egress] C example.com blocked (good): {type(e).__name__}")
        other_blocked = True

    ok = deepseek_ok and other_blocked
    print(
        f"[egress] RESULT: deepseek_metered_and_priced={deepseek_ok} "
        f"other_blocked={other_blocked} -> {'PASS' if ok else 'FAIL'}"
    )
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
