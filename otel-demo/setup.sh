#!/usr/bin/env bash
# One-command setup for the OTel Demo lab on a fresh Ubuntu server.
#   ./otel-demo/setup.sh            full demo (needs 16 GB RAM)
#   ./otel-demo/setup.sh --minimal  smaller demo (8 GB RAM)
# Run it from the repo folder, with ANTHROPIC_API_KEY in .env.
set -euo pipefail

DEMO_VERSION=2.0.2
DEMO_DIR="${DEMO_DIR:-$HOME/opentelemetry-demo}"
LAB="$(cd "$(dirname "$0")" && pwd)"
REPO="$(dirname "$LAB")"
COMPOSE_FILE=docker-compose.yml
[[ "${1:-}" == "--minimal" ]] && COMPOSE_FILE=docker-compose.minimal.yml

step() { printf '\n==> %s\n' "$*"; }

step "1/6 Installing Docker, git and Python YAML (skipped if already there)"
command -v docker >/dev/null || curl -fsSL https://get.docker.com | sh
command -v git >/dev/null && python3 -c "import yaml" 2>/dev/null \
  || { sudo apt-get update -q && sudo apt-get install -yq git python3-yaml; }

step "2/6 Checking .env has an Anthropic key"
grep -q '^ANTHROPIC_API_KEY=.\+' "$REPO/.env" 2>/dev/null \
  || { echo "Put ANTHROPIC_API_KEY=... in $REPO/.env first."; exit 1; }

step "3/6 Downloading the OpenTelemetry Demo $DEMO_VERSION"
[[ -d "$DEMO_DIR" ]] || git clone --depth 1 --branch "$DEMO_VERSION" \
  https://github.com/open-telemetry/opentelemetry-demo.git "$DEMO_DIR"

# The demo's .env says DEMO_VERSION=latest: newer images that don't match these config files.
sed -i "s/^DEMO_VERSION=.*/DEMO_VERSION=$DEMO_VERSION/" "$DEMO_DIR/.env"

step "4/6 Sending the demo's logs to Grafana Loki too"
# Keep the demo's own log exporters and add ours. The extras file is merged over the main config.
python3 - "$DEMO_DIR/src/otel-collector" <<'PY'
import sys, yaml, pathlib
d = pathlib.Path(sys.argv[1])
base = yaml.safe_load((d / "otelcol-config.yml").read_text())
exporters = list(base["service"]["pipelines"]["logs"]["exporters"])
if "otlphttp/grafana-loki" not in exporters:
    exporters.append("otlphttp/grafana-loki")
extras = {
    # The demo's collector asks Docker for API 1.25; Docker 29+ only accepts 1.40 and up.
    "receivers": {"docker_stats": {"api_version": "1.44"}},
    "exporters": {"otlphttp/grafana-loki": {"endpoint": "http://grafana-loki:3100/otlp"}},
    "service": {"pipelines": {"logs": {"exporters": exporters}}},
}
(d / "otelcol-config-extras.yml").write_text(yaml.safe_dump(extras, sort_keys=False))
print("  logs go to:", ", ".join(exporters))
PY

step "5/6 Starting the demo store ($COMPOSE_FILE)"
(cd "$DEMO_DIR" && docker compose -f "$COMPOSE_FILE" up -d --force-recreate --remove-orphans)

step "6/6 Starting Loki next to it"
mkdir -p "$LAB/state"
[[ -f "$LAB/state/deploys.json" ]] || echo "[]" > "$LAB/state/deploys.json"
(cd "$LAB" && docker compose --env-file "$REPO/.env" up -d --build)

IP="$(curl -fsS -4 https://ifconfig.me 2>/dev/null || hostname -I | awk '{print $1}')"
cat <<EOF

Done. Give it 3-5 minutes to warm up, then:
  Store:            http://$IP:8080
  Failure switches: http://$IP:8080/feature
  Loki health:      curl localhost:8088/health
  Check the wiring: ./otel-demo/check.sh
  Break something:  python3 otel-demo/break.py paymentFailure
EOF
