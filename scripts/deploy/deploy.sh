#!/usr/bin/env bash
# deploy-command of deploy.yml and promote.yml (fleet-workflows _deploy-run.yml): deploy $IMAGE
# by digest to the docker compose host of $ENVIRONMENT over SSH, then check that the running
# container has that digest (scripts/deploy/remote.sh, the blueprint host contract).
#
# _deploy-run.yml sets IMAGE (ghcr.io/<owner>/<repo>:<sha>), SHA and ENVIRONMENT, and exports the
# KEY=value lines of the DEPLOY_ENV secret of the GitHub environment:
#   DEPLOY_HOST             host name or IP
#   DEPLOY_USER             SSH user in the docker group
#   DEPLOY_SSH_KEY_B64      private key, base64 on one line (base64 -w0 < key)
#   DEPLOY_KNOWN_HOSTS_B64  known_hosts lines of the host, base64 on one line
#   DEPLOY_PORT             optional, default 22
#   DEPLOY_COMPOSE_DIR      optional, default /opt/synthwerk/vision; stg and prd need their own
#                           host or dir, else they replace dev
#
# Usage: scripts/deploy/deploy.sh   (run by the fleet workflow; exit 0 = deployed and verified)
set -euo pipefail

fail() {
  echo "::error title=deploy::$1"
  exit 1
}

for name in IMAGE ENVIRONMENT DEPLOY_HOST DEPLOY_USER DEPLOY_SSH_KEY_B64 DEPLOY_KNOWN_HOSTS_B64; do
  [[ -n "${!name:-}" ]] || fail "$name is not set (DEPLOY_ENV of the ${ENVIRONMENT:-unknown} environment)"
done
port=${DEPLOY_PORT:-22}
dir=${DEPLOY_COMPOSE_DIR:-/opt/synthwerk/vision}
service=vision

# Strict shapes: these values end up in the remote command line (same checks as the blueprint).
[[ "$IMAGE" =~ ^[a-z0-9][a-z0-9._/-]*:[0-9a-f]{40}$ ]] || fail "IMAGE must be <name>:<40-hex sha>, got '$IMAGE'"
[[ "$DEPLOY_HOST" =~ ^[A-Za-z0-9.:-]+$ ]] || fail "DEPLOY_HOST has an invalid value"
[[ "$port" =~ ^[0-9]{1,5}$ && "$port" -ge 1 && "$port" -le 65535 ]] || fail "DEPLOY_PORT must be 1-65535"
[[ "$DEPLOY_USER" =~ ^[a-z_][a-z0-9_-]*$ ]] || fail "DEPLOY_USER has an invalid value"
dir_re='^(/|~/)[A-Za-z0-9_./-]*$'
[[ "$dir" =~ $dir_re && "$dir" != *..* ]] || fail "DEPLOY_COMPOSE_DIR must be an absolute or ~/ path"

# Deploy by digest: resolve the SHA tag once, then the host pulls and pins <image>@<digest>.
name_only=${IMAGE%:*}
digest=$(docker buildx imagetools inspect "$IMAGE" --format '{{json .Manifest}}' | jq -r '.digest')
[[ "$digest" =~ ^sha256:[0-9a-f]{64}$ ]] || fail "no digest for $IMAGE"

ssh_dir=$(mktemp -d "${RUNNER_TEMP:-/tmp}/deploy-ssh.XXXXXX")
trap 'rm -rf "$ssh_dir"' EXIT
(
  umask 077
  base64 -d <<<"$DEPLOY_SSH_KEY_B64" >"$ssh_dir/key"
  base64 -d <<<"$DEPLOY_KNOWN_HOSTS_B64" >"$ssh_dir/known_hosts"
) || fail "DEPLOY_SSH_KEY_B64 or DEPLOY_KNOWN_HOSTS_B64 is not valid base64"
[[ -s "$ssh_dir/key" && -s "$ssh_dir/known_hosts" ]] || fail "the SSH key or known_hosts is empty"
cat >"$ssh_dir/config" <<EOF
Host deploy-target
  HostName $DEPLOY_HOST
  Port $port
  User $DEPLOY_USER
  IdentityFile $ssh_dir/key
  IdentitiesOnly yes
  UserKnownHostsFile $ssh_dir/known_hosts
  GlobalKnownHostsFile /dev/null
  StrictHostKeyChecking yes
  BatchMode yes
  ConnectTimeout 15
  ServerAliveInterval 15
EOF

echo "deploy $name_only@$digest (${SHA:-unknown sha}) to $ENVIRONMENT: $dir ($service)"
ssh -F "$ssh_dir/config" deploy-target bash -s -- \
  deploy "$name_only" "$digest" "$service" "$dir" compose.yaml 120 \
  <"$(dirname "$0")/remote.sh"
