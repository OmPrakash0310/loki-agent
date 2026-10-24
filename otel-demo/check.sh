#!/usr/bin/env bash
# Checks each link in the lab: demo metrics -> our Prometheus, demo logs -> Grafana Loki, Loki up.
ok()   { printf '  OK    %s\n' "$*"; }
bad()  { printf '  FAIL  %s\n' "$*"; FAILED=1; }
FAILED=0

echo "Loki agent"
curl -fsS localhost:8088/health >/dev/null && ok "answers on :8088" || bad "not answering on :8088 (docker compose -f otel-demo/docker-compose.yml logs loki-agent)"

echo "Metrics (demo -> our Prometheus on :9091)"
n=$(curl -fsS 'localhost:9091/api/v1/label/service_name/values' | python3 -c 'import sys,json; print(len(json.load(sys.stdin)["data"]))' 2>/dev/null || echo 0)
[[ "$n" -gt 0 ]] && ok "$n services have span metrics" \
  || bad "no span metrics yet; open localhost:9091/targets. If the target is up but empty, the metric names differ: look at the demo Prometheus's names and update metrics.json and alerts.yml"

echo "Logs (demo -> Grafana Loki on :3100)"
svc=$(curl -fsS 'localhost:3100/loki/api/v1/label/service_name/values' | python3 -c 'import sys,json; print(", ".join(json.load(sys.stdin).get("data") or []))' 2>/dev/null)
[[ -n "$svc" ]] && ok "logs from: $svc" \
  || bad "no logs yet; see: docker compose -f otel-demo/docker-compose.yml logs promtail"

echo "Alerts"
curl -fsS localhost:9091/api/v1/rules >/dev/null && ok "alert rules loaded" || bad "Prometheus rules not loaded"

exit $FAILED
