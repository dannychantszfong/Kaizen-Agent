# Kaizen containment image - the box a lineage lives inside (SPEC invariant #1).
#
# Two principals share this image, and the whole safety model is the boundary
# between them:
#
#   root   - runs the watchdog (the immortal supervisor). The agent, being a
#            different, non-root uid, cannot signal it (invariant #2) and cannot
#            edit its code (the immortal tree is root-owned, read-only).
#   agent  - runs the runner + the in-process body via `gosu agent`. Its ONLY
#            writable paths are agent/, substrate/state/, substrate/journal/ and
#            the repo's .git. Everything else under /lineage is root:root r-x.
#
# The immortal CODE being read-only to the agent is the PRIMARY agent<->substrate
# boundary (invariant #5); the guardrail hook is inner defense-in-depth. State is
# data the runner must write, so it is deliberately agent-writable (invariant #4)
# and lives on a mounted volume so the lineage survives `docker rm`.
#
# Dependencies are baked at build time; at runtime the container reaches nothing
# but the egress proxy (see docker-compose.yml).

FROM python:3.12-slim

# --- baked dependencies (no network at runtime to install anything) ----------
# git:  the runner commits each generation LOCALLY (no push, no VCS credentials).
# gosu: lets the root watchdog drop to the unprivileged `agent` uid per spawn.
# tini: PID 1 that reaps the runner subprocesses the watchdog spawns.
RUN apt-get update \
 && apt-get install -y --no-install-recommends git gosu tini ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# Provider SDK + schema lib, baked so a live run needs no installs (egress would
# block them anyway). litellm reaches every enabled provider natively:
#   anthropic/ (httpx), openai/ (the `openai` dep litellm pulls in), gemini/
#   (AI Studio REST over httpx), deepseek/ + openrouter/ (OpenAI-compatible, httpx).
# None of the five need an extra SDK; litellm's own deps (openai, httpx, tiktoken)
# cover them. A recent litellm pin keeps its cost map current — the metering circuit
# breaker HALTS on any model it cannot price, so stale pricing would stop the lineage.
# (Vertex/Bedrock would need their SDKs added here; they are not enabled.)
# psutil: the substrate's process-tree liveness observation + child reaping.
RUN pip install --no-cache-dir "pydantic>=2.6" "psutil>=5.9" "litellm>=1.55"

# --- the unprivileged body principal -----------------------------------------
RUN useradd --create-home --uid 1000 --shell /usr/sbin/nologin agent

# Python + git hygiene. Writing bytecode into the read-only immortal tree would
# fail; turn it off so there are no noisy EACCES attempts. Git identity and the
# safe.directory marker travel as env so nothing needs to be written to /etc
# (the root filesystem is mounted read-only at runtime).
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    GIT_CONFIG_COUNT=1 \
    GIT_CONFIG_KEY_0=safe.directory \
    GIT_CONFIG_VALUE_0=/lineage \
    GIT_AUTHOR_NAME=kaizen \
    GIT_AUTHOR_EMAIL=kaizen@localhost \
    GIT_COMMITTER_NAME=kaizen \
    GIT_COMMITTER_EMAIL=kaizen@localhost \
    KAIZEN_RUNNER_PREFIX="gosu agent"

# --- the lineage tree --------------------------------------------------------
# Build context is the repo root (see docker-compose.yml: context: ..). We lay
# the tree down at /lineage, bootstrap a fresh git history (the build context's
# own .git is excluded via .dockerignore), then freeze permissions so the image
# itself encodes the agent<->substrate boundary. A fresh named volume mounted at
# /lineage inherits this content + ownership on first run, then persists.
WORKDIR /lineage
COPY . /lineage

# Bootstrap one known-good commit so `last_good` always has a target, set the
# permission boundary, then hand the writable carve-outs to `agent`.
RUN set -eux; \
    git init -q -b main; \
    git add -A; \
    git commit -q -m "gen baseline (immortal seed)"; \
    chown -R root:root /lineage; \
    find /lineage -type d -exec chmod 0555 {} +; \
    find /lineage -type f -exec chmod 0444 {} +; \
    mkdir -p /lineage/substrate/state /lineage/substrate/journal; \
    for p in agent substrate/state substrate/journal .git; do \
        chown -R agent:agent "/lineage/$p"; \
        find "/lineage/$p" -type d -exec chmod 0775 {} +; \
        find "/lineage/$p" -type f -exec chmod 0664 {} +; \
    done

COPY containment/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod 0555 /usr/local/bin/entrypoint.sh

# tini as PID 1; entrypoint runs as root (it must, to gosu-drop the runner).
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/entrypoint.sh"]
