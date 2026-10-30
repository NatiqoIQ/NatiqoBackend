# Load tests

Two files, one purpose: know where Natiqo breaks **before** a learner finds out.

| File | What it does |
|---|---|
| `k6/natiqo-mixed.js` | Mixed workload: browse (60%), lesson flow (30%), progress (10%). |
| `../scripts/mint_loadtest_tokens.py` | Mints one account per virtual user, so per-user limits are measured honestly. |

## 1. Prepare an isolated environment

Use staging (or a disposable copy of production). **Never run this against production.**

```bash
# Accounts (deterministic, rerunnable). Output holds live sessions: mode 0600, gitignored.
uv run python -m scripts.mint_loadtest_tokens \
  --count 5000 \
  --ttl-seconds 28800 \
  --concurrency 8 \
  --output loadtest/tokens.json
```

If the environment has no catalogue yet:

```bash
uv run python -m scripts.seed_content --yes
```

## 2. Install k6

```bash
# macOS
brew install k6
# Debian/Ubuntu
sudo gpg -k && \
  sudo gpg --no-default-keyring --keyring /usr/share/keyrings/k6-archive-keyring.gpg \
    --keyserver hkp://keyserver.ubuntu.com:80 --recv-keys C5AD17C747E3415A3642D57D77C6C491D6AC1D69 && \
  echo "deb [signed-by=/usr/share/keyrings/k6-archive-keyring.gpg] https://dl.k6.io/deb stable main" \
    | sudo tee /etc/apt/sources.list.d/k6.list && \
  sudo apt-get update && sudo apt-get install k6
```

## 3. Run

```bash
# Smoke (one shared dev account; not a capacity test)
k6 run loadtest/k6/natiqo-mixed.js -e BASE_URL=http://127.0.0.1:8100 -e DURATION=1m

# Capacity (~2,000 HTTP requests/s: 1,400 reads + 600 writes).
# LEARN_RPS counts sittings, not requests: one sitting is ~12 HTTP calls.
k6 run loadtest/k6/natiqo-mixed.js \
  -e BASE_URL=https://staging.example.com \
  -e TOKENS_FILE=$PWD/loadtest/tokens.json \
  -e BROWSE_RPS=400 -e LEARN_RPS=50 -e PROGRESS_RPS=66 \
  -e DURATION=30m
```

Pass criteria are in the script's `thresholds` and in
[`docs/system-design-scaling-plan-ar.md`](../docs/system-design-scaling-plan-ar.md) §10.

## 4. What to watch while it runs

- API: RPS per scenario, p95/p99, 5xx, 429, CPU, pod count.
- Database: pool in-use/waiters, connections, slow queries, CPU, replica lag.
- Redis: ops, latency, hit ratio, evictions (**must be zero on the queue instance**).
- Queue: lag, pending, DLQ.

## 5. Failure drills

Run these deliberately; a load test that never fails teaches nothing.

| Drill | Expected behaviour |
|---|---|
| Kill one API pod mid-run | No 5xx; in-flight requests complete or retry |
| Fail over Redis | Cache misses rise, requests still succeed; queue drains after recovery |
| Stop a read replica | Tolerant reads fall back to the primary; no 5xx |
| AI provider sleeps 20s | AI endpoints fall back fast; the rest of the API is unaffected |
| `pg_sleep(10)` in a hot query | Statement timeout fires; pool does not saturate |

## 6. Safety

- `tokens.json` is a credential store. It is gitignored; do not paste it into issues.
- The minter refuses `APP_ENV=production` unless `LOADTEST_MINT_ALLOW_PRODUCTION=1`
  is explicitly set.
- Dev-login must stay disabled in production (the app already refuses to boot with it).
