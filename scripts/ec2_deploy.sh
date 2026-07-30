#!/usr/bin/env bash
#
# ec2_deploy.sh - (re)build and restart the Evidence Monitoring Agent on EC2.
#
# Runs ON the EC2 host. The Bitbucket pipeline rsyncs the working tree to
# ~/evidence-monitoring-agent/ and then invokes this script over SSH:
#
#     bash ~/evidence-monitoring-agent/scripts/ec2_deploy.sh
#
# It builds a single Docker image (nginx + uvicorn) from the working tree and
# replaces the running container. The SQLite DB and the .env live on the host
# (mounted / passed in), so they survive redeploys. No registry, no compose.

set -euo pipefail

# Resolve the app directory as this script's parent's parent, so the script
# works regardless of the caller's CWD.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${APP_DIR}"

IMAGE_NAME="evidence-monitoring-agent"
CONTAINER_NAME="evidence-monitoring-agent"
HOST_PORT="${HOST_PORT:-80}"          # public port on the EC2 box
ENV_FILE="${ENV_FILE:-${APP_DIR}/.env}"
DATA_DIR="${APP_DIR}/data"            # persisted SQLite DB (mounted to /app/data)
GIT_SHA="$(cat "${APP_DIR}/.git-sha" 2>/dev/null || echo "unknown")"

# --- NMA sidecar -------------------------------------------------------------
# ON by default, because on the corpus we actually have it is not optional.
#
# The tempting reading of the topology is "0 independent loops, so it is a star, so Bucher
# covers it". That is wrong, and the two facts are different questions:
#
#   NET-PSORIATIC-ARTHRITIS-PSA_ACR50_W16
#     loop_count 1   independent_loops 0   has_multi_arm True (NCT03895203)
#     rule NETMETA_IF_LOOPS_OR_MULTI_ARM_ELSE_BUCHER  ->  NETMETA
#
# `independent_loop_count` is 0, which means no INCONSISTENCY ASSESSMENT is possible.
# `has_multi_arm_studies` is true, which means NETMETA IS REQUIRED - Bucher cannot represent
# the within-study correlation a multi-arm trial induces. So the only real network selects
# netmeta for EVERY pair, and with the sidecar absent every comparison on it returns
# NMA_SERVICE_UNAVAILABLE. "Optional" would describe a corpus we do not have.
#
# Set NMA_SIDECAR=0 to opt out on a box that only needs the non-evidence surfaces.
NMA_SIDECAR="${NMA_SIDECAR:-1}"
NMA_IMAGE="evidence-nma-sidecar"
NMA_CONTAINER="evidence-nma-sidecar"
DOCKER_NET="evidence-net"
# Set to 1 only once the sidecar is built, running and answering. The app container is wired
# to it on that basis alone - see the note above the container swap.
NMA_READY=0

echo "==> Deploying ${IMAGE_NAME} (commit: ${GIT_SHA})"
echo "    app dir : ${APP_DIR}"
echo "    env file: ${ENV_FILE}"
echo "    host port: ${HOST_PORT}"

# --- Pre-flight checks ------------------------------------------------------
if ! command -v docker >/dev/null 2>&1; then
  echo "ERROR: docker is not installed on this host. See docs/deploy.md." >&2
  exit 1
fi

if [ ! -f "${ENV_FILE}" ]; then
  echo "ERROR: env file not found at ${ENV_FILE}." >&2
  echo "       Copy deploy/.env.production.example there and fill it in (one-time setup)." >&2
  exit 1
fi

mkdir -p "${DATA_DIR}"

# --- Tell the RUNNING app that a deploy is staging --------------------------
# The container swap near the end of this script destroys whatever is in flight. Every
# step between here and that swap (prune + image build) takes MINUTES, and throughout it
# the OLD container keeps serving normally - so an operator can start an hour-long run
# seconds before it is killed, with nothing in the UI to warn them. That is exactly how
# run a6c3f5d5 died: rsync landed 04:02:15, run started 04:02:24, container replaced
# 04:06:05, 244 of ~4360 responses kept.
#
# DATA_DIR is bind-mounted to /app/data, so the running backend sees this file and
# refuses to START runs while it exists (services/deploy_lock.py). Reads, cancels and
# every other endpoint stay live. The EXIT trap covers a failed or aborted build, and the
# backend additionally ignores the marker once it is older than 45 minutes, so a
# hard-killed deploy cannot wedge the platform.
DEPLOY_LOCK="${DATA_DIR}/.deploy-in-progress"
trap 'rm -f "${DEPLOY_LOCK}"' EXIT
: > "${DEPLOY_LOCK}"
echo "==> Deploy lock set (${DEPLOY_LOCK}); the app will refuse to start new runs"

# --- Reclaim disk before building -------------------------------------------
# A long-lived Docker host accumulates old image layers and BuildKit cache.
# The build previously died with "[Errno 28] No space left on device" during
# `pip install`. Reclaim space BEFORE building. (The old post-build prune never
# ran when the build itself failed, so images piled up across every deploy.)
echo "==> Disk usage before cleanup:"
df -h / || true

# Stopped containers, dangling images, and unused BuildKit cache.
docker container prune -f >/dev/null 2>&1 || true
docker image prune -f     >/dev/null 2>&1 || true
docker builder prune -af  >/dev/null 2>&1 || true

# Remove OLD tags of this image, keeping :latest (the currently running one) as
# a rollback safety net. Tagged images are NOT removed by `image prune`, so
# without this each deploy leaves a full image behind forever.
docker images "${IMAGE_NAME}" --format '{{.Repository}}:{{.Tag}}' 2>/dev/null \
  | grep -v ':latest$' \
  | xargs -r docker rmi -f >/dev/null 2>&1 || true

echo "==> Disk usage after cleanup:"
df -h / || true

# --- Build ------------------------------------------------------------------
echo "==> Building image ${IMAGE_NAME}:${GIT_SHA}"
docker build \
  --label "git-sha=${GIT_SHA}" \
  -t "${IMAGE_NAME}:${GIT_SHA}" \
  -t "${IMAGE_NAME}:latest" \
  .

# --- NMA sidecar: build ONLY when its own inputs changed --------------------
# An unconditional second `docker build` would reinstall the R stack on EVERY application
# deploy - minutes of build time and a full disk-churn cycle for a sidecar that changes
# monthly at most. The tag is a content hash of the Dockerfile and the plumber script, so
# an unchanged sidecar is a no-op and the box never holds two NMA images at once (~2GB off
# the peak).
start_nma_sidecar() {
  NMA_TAG="$(cat "${APP_DIR}/Dockerfile.nma" "${APP_DIR}/nma-sidecar/plumber.R" \
    | sha256sum | cut -c1-12)" || return 1
  echo "==> NMA sidecar (content tag: ${NMA_TAG})"

  if docker image inspect "${NMA_IMAGE}:${NMA_TAG}" >/dev/null 2>&1; then
    echo "    image already built for this content - skipping build"
  else
    echo "    building ${NMA_IMAGE}:${NMA_TAG} (installs the R stack; several minutes)"
    docker build -f "${APP_DIR}/Dockerfile.nma" -t "${NMA_IMAGE}:${NMA_TAG}" "${APP_DIR}" \
      || return 1
    # Drop every older tag of this image now the new one exists, so the rollback copy the
    # app image deliberately keeps is not also paid for here. The R stack is the single
    # largest thing on the disk and it is rebuildable from a pinned Dockerfile in minutes.
    docker images "${NMA_IMAGE}" --format '{{.Repository}}:{{.Tag}}' 2>/dev/null \
      | grep -v ":${NMA_TAG}$" \
      | xargs -r docker rmi -f >/dev/null 2>&1 || true
  fi

  docker network create "${DOCKER_NET}" >/dev/null 2>&1 || true
  docker rm -f "${NMA_CONTAINER}" >/dev/null 2>&1 || true
  docker run -d \
    --name "${NMA_CONTAINER}" \
    --restart unless-stopped \
    --network "${DOCKER_NET}" \
    "${NMA_IMAGE}:${NMA_TAG}" || return 1

  # Wait for /healthz rather than assuming. A sidecar that is up but not yet serving makes
  # the first resolve report NMA_SERVICE_UNAVAILABLE, which is a retry signal and would look
  # like an evidence problem to whoever saw it first.
  #
  # Probed from INSIDE the container with base R, the same way Dockerfile.nma's own
  # HEALTHCHECK does. Pulling curlimages/curl to ask the question would add a registry round
  # trip to the deploy and let a Docker Hub outage fail a health check that has nothing to do
  # with Docker Hub.
  echo "    waiting for the sidecar to answer /healthz"
  for _ in $(seq 1 45); do
    if docker exec "${NMA_CONTAINER}" \
         R -q -e "invisible(readLines(url('http://127.0.0.1:8000/healthz')))" \
         >/dev/null 2>&1; then
      echo "    sidecar healthy"
      return 0
    fi
    sleep 2
  done
  echo "    sidecar did not answer /healthz within 90s" >&2
  return 1
}

# A sidecar failure must NOT abort the application deploy.
#
# The script runs under `set -e` and this block sits before the container swap, so an
# unguarded failure here would leave the old app running and abandon the deploy. That is the
# wrong trade: the platform is questions, scoring, digests and dashboards as well as NMA, and
# blocking a hotfix to all of it because CRAN was briefly unreachable helps nobody.
#
# Degrading is safe *specifically because* NMA_SERVICE_UNAVAILABLE is a retryable service
# status that can never be mistaken for a finding about the evidence. The app simply reports
# that the analysis could not be run, which is true.
if [ "${NMA_SIDECAR}" = "1" ]; then
  if start_nma_sidecar; then
    NMA_READY=1
  else
    echo "WARN: the NMA sidecar did not come up. Deploying the application WITHOUT it." >&2
    echo "      Every comparison on a network with a closed loop or a multi-arm trial will" >&2
    echo "      report NMA_SERVICE_UNAVAILABLE until it is running. On the current corpus" >&2
    echo "      that is every comparison. Re-run this script to retry." >&2
  fi
else
  echo "==> NMA sidecar disabled (NMA_SIDECAR=0)"
fi

# --- Warn about runs this swap is about to kill -----------------------------
# The lock set at the top stops NEW runs, but a run that was already executing when this
# deploy began is still going and the swap below ends it. That is not fatal any more -
# the run keeps every response already captured and is resumable in place from the UI
# (Run Analysis -> Resume) - but it has to be said out loud here rather than discovered
# later as an unexplained "Failed" row. Checked immediately before the swap, because a run
# can start at any point during the (multi-minute) build above.
IN_FLIGHT="$(docker exec -i "${CONTAINER_NAME}" python - <<'PY' 2>/dev/null
import sqlite3
con = sqlite3.connect("file:/app/data/evidence_monitoring.db?mode=ro", uri=True)
print(con.execute("SELECT COUNT(*) FROM runs WHERE status = 'RUNNING'").fetchone()[0])
PY
)" || IN_FLIGHT=0

if [ "${IN_FLIGHT:-0}" != "0" ]; then
  echo ""
  echo "WARN: ${IN_FLIGHT} run(s) are still executing; this swap will stop them." >&2
  echo "      Responses already captured are preserved. Continue them after the deploy" >&2
  echo "      from Run Analysis -> Resume (same run, only the remainder is dispatched)." >&2
  echo ""
fi

# --- Swap the container -----------------------------------------------------
echo "==> Stopping previous container (if any)"
docker rm -f "${CONTAINER_NAME}" >/dev/null 2>&1 || true

echo "==> Starting new container"
# The sidecar URL is injected here rather than written into .env, and keyed on NMA_READY
# rather than on NMA_SIDECAR. Wanting the sidecar and having one are different things: if it
# failed to come up, pointing the app at a dead address would turn a clean
# "not configured" into a connection error on every resolve, and attach the app to a network
# that may not exist. This way the configuration can never claim more than is true.
NMA_ARGS=()
if [ "${NMA_READY}" = "1" ]; then
  NMA_ARGS=(--network "${DOCKER_NET}" -e "NMA_SIDECAR_URL=http://${NMA_CONTAINER}:8000")
fi

docker run -d \
  --name "${CONTAINER_NAME}" \
  --restart unless-stopped \
  -p "${HOST_PORT}:80" \
  --env-file "${ENV_FILE}" \
  -v "${DATA_DIR}:/app/data" \
  "${NMA_ARGS[@]}" \
  "${IMAGE_NAME}:latest"

# --- Seed the brand taxonomy (idempotent; skips if already seeded) ----------
# The taxonomy moved out of brands.yaml and into SQLite so it can be edited at runtime.
# That makes this the one seeder whose skip-if-present behaviour is load-bearing rather
# than merely an optimisation: the image is rebuilt from the working tree on every deploy,
# so a seeder that re-imported the baseline would silently revert every brand anyone had
# added through the UI. Use `--force` by hand to deliberately restore the baseline.
#
# The app also seeds on startup, so this is belt-and-braces — but it runs here too so a
# failure is visible in the deploy log rather than only in the container's.
echo "==> Ensuring the brand taxonomy is seeded"
sleep 3
docker exec "${CONTAINER_NAME}" python -m scripts.seed_brand_taxonomy || \
  echo "WARN: brand taxonomy seed returned non-zero (continuing; it is safe to re-run)."

# --- Snapshot the live taxonomy ---------------------------------------------
# The taxonomy is no longer a file in git, so this is the only record of what production
# actually believes. It is not a deploy safety net -- the DB is host-mounted and a deploy
# does not touch it -- it is a recovery point for the two ways the taxonomy can be lost or
# corrupted with nothing to compare against: a mistaken `--force` reseed, and a bad edit
# made through the Add Brand modal.
#
# Written into DATA_DIR because that is the bind-mount, so the snapshots outlive the
# container the same way the database does. Kept to the newest 10 so this cannot grow
# without bound on a box that deploys often.
TAXONOMY_BACKUPS="${DATA_DIR}/taxonomy-snapshots"
mkdir -p "${TAXONOMY_BACKUPS}"
TAXONOMY_SNAPSHOT="${TAXONOMY_BACKUPS}/taxonomy-$(date -u +%Y%m%dT%H%M%SZ)-${GIT_SHA}.yaml"
if docker exec "${CONTAINER_NAME}" python -m scripts.seed_brand_taxonomy --export \
     > "${TAXONOMY_SNAPSHOT}" 2>/dev/null && [ -s "${TAXONOMY_SNAPSHOT}" ]; then
  echo "    taxonomy snapshot: ${TAXONOMY_SNAPSHOT}"
  # Newest 10 kept. `ls -t` then tail is safe here because the names are generated above
  # and contain no spaces or newlines.
  ls -t "${TAXONOMY_BACKUPS}"/taxonomy-*.yaml 2>/dev/null | tail -n +11 | xargs -r rm -f
else
  rm -f "${TAXONOMY_SNAPSHOT}"
  echo "WARN: could not snapshot the taxonomy (continuing; the deploy is unaffected)." >&2
fi

# --- Seed the question banks (idempotent; skips if already seeded) ----------
echo "==> Ensuring question banks are seeded"
docker exec "${CONTAINER_NAME}" python -m scripts.seed_questions || \
  echo "WARN: seed step returned non-zero (continuing; it is safe to re-run)."
docker exec "${CONTAINER_NAME}" python -m scripts.seed_lupron_questions || \
  echo "WARN: Lupron seed step returned non-zero (continuing; it is safe to re-run)."
docker exec "${CONTAINER_NAME}" python -m scripts.seed_rheumatology_questions || \
  echo "WARN: Rheumatology seed step returned non-zero (continuing; it is safe to re-run)."
# Dermatology and Gastroenterology are the two halves of the retired Immunology block.
# Their seeders shipped with that split but were never wired in here, so the banks only
# existed wherever someone had run them by hand.
docker exec "${CONTAINER_NAME}" python -m scripts.seed_dermatology_questions || \
  echo "WARN: Dermatology seed step returned non-zero (continuing; it is safe to re-run)."
docker exec "${CONTAINER_NAME}" python -m scripts.seed_gastroenterology_questions || \
  echo "WARN: Gastroenterology seed step returned non-zero (continuing; it is safe to re-run)."
docker exec "${CONTAINER_NAME}" python -m scripts.seed_vraylar_questions || \
  echo "WARN: Vraylar (Neuroscience) seed step returned non-zero (continuing; it is safe to re-run)."

# --- Seed the Activation & Impact demo portfolio ----------------------------
# Unlike the question banks (insert-if-absent), this seeder WIPES its own
# `actdemo-` marked rows and re-inserts on every run, so each deploy restores
# the demo to its pristine curated state. It is pure ORM (no live model calls,
# no AWS creds) and only touches `actdemo-`/`Q-ACTDEMO-*` rows, never real data.
docker exec "${CONTAINER_NAME}" python -m scripts.seed_activation_demo || \
  echo "WARN: Activation demo seed step returned non-zero (continuing; it is safe to re-run)."

# --- Cleanup ----------------------------------------------------------------
echo "==> Pruning dangling images"
docker image prune -f >/dev/null 2>&1 || true

echo "==> Deploy complete. Container status:"
docker ps --filter "name=${CONTAINER_NAME}" --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'

# Stated at the END as well as where it happened. A warning 200 lines up scrolls past in CI
# output, and "the deploy said OK" is exactly how a silently degraded box stays that way.
if [ "${NMA_SIDECAR}" = "1" ] && [ "${NMA_READY}" != "1" ]; then
  echo ""
  echo "DEGRADED: the application is up, the NMA sidecar is NOT." >&2
  echo "          Network meta-analysis is unavailable on this box." >&2
elif [ "${NMA_READY}" = "1" ]; then
  docker ps --filter "name=${NMA_CONTAINER}" --format 'table {{.Names}}\t{{.Status}}'
fi
