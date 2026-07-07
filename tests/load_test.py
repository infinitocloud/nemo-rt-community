#!/usr/bin/env python3
"""
Load test for S2S pipeline.

Tests concurrent WebSocket connections sending text directly to LLM+TTS
(bypasses VAD+STT) to measure how the pipeline scales under load.

Usage:
    python load_test.py --url wss://your-host.example.com/ws --users 5
    python load_test.py --url ws://localhost:8000/ws --users 10 --rounds 3
"""

import argparse
import asyncio
import json
import statistics
import time

import websockets
_KEY = ""


async def run_session(url, user_id, text, results, barrier):
    """Single user session: connect, wait for all ready, send text, measure timing."""
    try:
        async with websockets.connect(url, ping_interval=20, ping_timeout=20, extra_headers=({"Authorization": "Bearer "+_KEY} if _KEY else None)) as ws:
            # Wait for all users to be connected before firing
            await barrier.wait()

            t0 = time.time()
            await ws.send(json.dumps({"type": "chat", "text": text}))

            ttfa = None
            total = None
            sentences = []

            async for raw in ws:
                msg = json.loads(raw)
                msg_type = msg.get("type")

                if msg_type == "audio":
                    if ttfa is None:
                        ttfa = time.time() - t0
                    sentences.append(msg.get("sentence", ""))

                elif msg_type == "done":
                    total = time.time() - t0
                    # Server also reports its own timing
                    server_ttfa = msg.get("ttfa")
                    server_total = msg.get("total")
                    break

                elif msg_type == "error":
                    print(f"  [User {user_id}] ERROR: {msg.get('message')}")
                    break

            results.append({
                "user": user_id,
                "ttfa": round(ttfa, 3) if ttfa else None,
                "total": round(total, 3) if total else None,
                "server_ttfa": server_ttfa,
                "server_total": server_total,
                "sentences": len(sentences),
            })

    except Exception as e:
        results.append({
            "user": user_id,
            "ttfa": None,
            "total": None,
            "error": str(e),
        })


async def run_round(url, num_users, text, round_num):
    """Run one round of concurrent users."""
    print(f"\n{'='*60}")
    print(f"Round {round_num}: {num_users} concurrent users")
    print(f"{'='*60}")

    results = []
    barrier = asyncio.Barrier(num_users)

    tasks = [
        run_session(url, i + 1, text, results, barrier)
        for i in range(num_users)
    ]

    await asyncio.gather(*tasks)

    # Sort by user id
    results.sort(key=lambda r: r["user"])

    # Print individual results
    for r in results:
        if r.get("error"):
            print(f"  User {r['user']:2d}: ERROR - {r['error']}")
        else:
            print(f"  User {r['user']:2d}: TTFA={r['ttfa']:.3f}s  Total={r['total']:.3f}s  "
                  f"(server: TTFA={r['server_ttfa']}s Total={r['server_total']}s)  "
                  f"sentences={r['sentences']}")

    # Stats
    ttfas = [r["ttfa"] for r in results if r.get("ttfa") is not None]
    totals = [r["total"] for r in results if r.get("total") is not None]
    errors = sum(1 for r in results if r.get("error"))

    if ttfas:
        print(f"\n  Summary ({len(ttfas)} ok, {errors} errors):")
        print(f"    TTFA:  min={min(ttfas):.3f}s  avg={statistics.mean(ttfas):.3f}s  "
              f"max={max(ttfas):.3f}s  p95={sorted(ttfas)[int(len(ttfas)*0.95)]:.3f}s")
        print(f"    Total: min={min(totals):.3f}s  avg={statistics.mean(totals):.3f}s  "
              f"max={max(totals):.3f}s  p95={sorted(totals)[int(len(totals)*0.95)]:.3f}s")

    return {
        "users": num_users,
        "ttfa_avg": round(statistics.mean(ttfas), 3) if ttfas else None,
        "ttfa_max": round(max(ttfas), 3) if ttfas else None,
        "total_avg": round(statistics.mean(totals), 3) if totals else None,
        "total_max": round(max(totals), 3) if totals else None,
        "errors": errors,
    }


async def main():
    parser = argparse.ArgumentParser(description="S2S Pipeline Load Test")
    parser.add_argument("--url", default="ws://localhost:8000/ws",
                        help="WebSocket URL")
    parser.add_argument("--users", type=int, default=5,
                        help="Max concurrent users to test")
    parser.add_argument("--text", default="hola cuál es tu nombre",
                        help="Text to send to LLM")
    parser.add_argument("--rounds", type=int, default=1,
                        help="Rounds per user count")
    parser.add_argument("--ramp", action="store_true",
                        help="Ramp up: test 1, 2, 5, 10, ... up to --users")
    parser.add_argument("--key", default="",
                        help="API key for authentication")
    args = parser.parse_args()

    global _KEY
    _KEY = args.key
    ws_url = args.url

    print(f"S2S Load Test")
    print(f"URL: {args.url}")
    print(f"Text: '{args.text}'")

    if args.ramp:
        # Ramp up: 1, 2, 5, 10, 15, 20, 30, ...
        levels = []
        for n in [1, 2, 5, 10, 15, 20, 30, 50, 75, 100]:
            if n <= args.users:
                levels.append(n)
        if args.users not in levels:
            levels.append(args.users)
    else:
        levels = [args.users]

    all_results = []
    for num_users in levels:
        for r in range(args.rounds):
            result = await run_round(ws_url, num_users, args.text, f"{num_users}u-r{r+1}")
            all_results.append(result)
            # Brief pause between rounds to let GPU settle
            if num_users < levels[-1] or r < args.rounds - 1:
                await asyncio.sleep(2)

    # Final summary table
    print(f"\n{'='*60}")
    print(f"FINAL SUMMARY")
    print(f"{'='*60}")
    print(f"{'Users':>6} {'TTFA avg':>10} {'TTFA max':>10} {'Total avg':>11} {'Total max':>11} {'Errors':>7}")
    print(f"{'-'*6} {'-'*10} {'-'*10} {'-'*11} {'-'*11} {'-'*7}")
    for r in all_results:
        ttfa_a = f"{r['ttfa_avg']:.3f}s" if r['ttfa_avg'] else "N/A"
        ttfa_m = f"{r['ttfa_max']:.3f}s" if r['ttfa_max'] else "N/A"
        tot_a = f"{r['total_avg']:.3f}s" if r['total_avg'] else "N/A"
        tot_m = f"{r['total_max']:.3f}s" if r['total_max'] else "N/A"
        print(f"{r['users']:>6} {ttfa_a:>10} {ttfa_m:>10} {tot_a:>11} {tot_m:>11} {r['errors']:>7}")


if __name__ == "__main__":
    asyncio.run(main())
