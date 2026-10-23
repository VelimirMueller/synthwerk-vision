#!/usr/bin/env bash
# Build the small MobileNet image and check that /readyz answers 200 (model loaded).
# The build stage of ci.yml runs it; it also runs locally with Docker.
#
# Usage: scripts/docker-smoke.sh   (exit 0 = /readyz answered 200 within 30 s)
set -euo pipefail
cd "$(dirname "$0")/.."

commit=${GITHUB_SHA:-$(git rev-parse HEAD)}
docker build --build-arg BACKEND=mobilenet --build-arg COMMIT="$commit" -t synthwerk-vision:smoke .

name="vision-smoke-$$"
trap 'docker rm -f "$name" >/dev/null 2>&1 || true' EXIT
docker run -d --name "$name" -p 127.0.0.1::8000 synthwerk-vision:smoke >/dev/null
port=$(docker port "$name" 8000/tcp | head -n1 | sed 's/.*://')

for _ in $(seq 1 30); do
  if curl -sf "http://127.0.0.1:$port/readyz"; then
    echo
    echo "ok: /readyz answered 200"
    exit 0
  fi
  sleep 1
done
docker logs "$name"
echo "::error::/readyz did not answer 200 within 30 s"
exit 1
