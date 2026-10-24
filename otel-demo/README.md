# OTel Demo lab: a realistic system for Loki to investigate

The [OpenTelemetry Demo](https://github.com/open-telemetry/opentelemetry-demo) is an online
store of about 15 services with built-in failure switches. This folder runs Loki next to it,
so you can break the store on purpose and check whether Loki finds the real cause.

```
OTel Demo ──span metrics──► demo Prometheus ──► loki-prometheus (alert rules) ──► loki-alertmanager ─┐
    └──container output── promtail ─────────► grafana-loki                                         ▼
                                                     └──────────────────────────────────────► loki-agent :8088
```

## Steps

1. **Create a server.** DigitalOcean droplet, Ubuntu, 16 GB RAM (or 8 GB with `--minimal`).
   It's billed by the hour while it exists: **destroy it when you're done** (powering off still bills).
2. **Log in and get the code.**
   ```bash
   ssh root@<droplet-ip>
   git clone https://github.com/OmPrakash0310/loki-agent.git && cd loki-agent
   echo "ANTHROPIC_API_KEY=sk-ant-..." > .env
   ```
3. **Run setup** (about 10 minutes the first time):
   ```bash
   ./otel-demo/setup.sh            # or: ./otel-demo/setup.sh --minimal
   ```
4. **Wait 5 minutes, then check the wiring:**
   ```bash
   ./otel-demo/check.sh
   ```
   All lines should say OK. If one says FAIL, it tells you where to look.
5. **Break something:**
   ```bash
   python3 otel-demo/break.py list
   python3 otel-demo/break.py paymentFailure
   ```
6. **Wait 2-3 minutes, then score Loki's answer:**
   ```bash
   python3 otel-demo/break.py score
   ```
7. **Fix the store, wait about 5 minutes, then try the next failure:**
   ```bash
   python3 otel-demo/break.py reset
   ```
8. **Destroy the droplet** when you're finished.

## Run the whole eval unattended

`eval.py` does steps 5 to 7 for many failures in a row: it waits until the store has
recovered, breaks it, waits for Loki's report, scores it and resets, then prints an
accuracy table (also saved to `otel-demo/eval-results.md`).

```bash
tmux new -s eval
python3 otel-demo/eval.py                 # 6 failures x 3 deploy histories, once each (about 4-5 hours)
```

Reattach after a disconnect with `tmux attach -t eval`. If it stops, run it again with
`--resume` to skip what's done. `--failures a,b`, `--modes fresh,none` and `--runs 2` narrow
or repeat it; `--table` only prints the table.

Each failure is tried with three deploy histories (`break.py --deploy` does the same by hand):
- **fresh:** the matching bad deploy went out a minute before.
- **none:** no matching deploy, only innocent ones. Loki must name the service and blame no deploy.
- **old:** the matching deploy went out hours ago. Blaming it is wrong.

It costs the Claude usage of one investigation per run (a few cents each).

## Known gaps

- Latency comes from span metrics in **milliseconds**, but Loki labels p95 as seconds.
  Read Loki's "p95 latency 1200" as 1200 ms until this is fixed.
- The metric names in `metrics.json` and `alerts.yml` follow the demo's span metrics
  (`traces_span_metrics_*`). If `check.sh` finds no metrics, check the names in the
  demo's Prometheus (`docker exec prometheus wget -qO- localhost:9090/api/v1/label/__name__/values`)
  and update both files.
- `score` only checks whether the report names the right service and deploy. Read the
  report to judge whether the reasoning is right.
