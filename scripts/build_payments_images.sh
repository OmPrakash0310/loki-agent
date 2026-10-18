#!/bin/sh
# Build one payments-svc image per release, so the verifier can run the exact
# version a deploy shipped. v2.5.0 has the multi-currency bug; v2.4.0 and the
# v2.5.1 revert don't. Usage: scripts/build_payments_images.sh [image-repo]
set -e
REPO="${1:-loki-demo/payments-svc}"
cd "$(dirname "$0")/../services/payments-svc"
for v in v2.4.0 v2.5.0 v2.5.1; do
  docker build -q --build-arg PAYMENTS_VERSION="$v" -t "$REPO:$v" .
done
