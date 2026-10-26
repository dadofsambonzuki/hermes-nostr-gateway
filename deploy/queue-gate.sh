#!/bin/sh
# Queue gate for the Hermes cron job "Gateway queue worker" (every 20 minutes).
#
# Source of truth: this file, in hermes-nostr-gateway. The live copy is
# ~/.hermes/scripts/queue-gate.sh — `hermes cron` only accepts monitor scripts inside its own
# scripts directory, so that copy is a pass-through and this one is what gets edited, committed
# and tested. Keep them byte-identical (`cp` is the whole update step).
#
# Why a monitor rather than a first line in the prompt: the scheduler runs this before the agent
# and suppresses the agent run while the output is unchanged, so an empty queue costs no model
# call and makes no tool attempts — which is what the queue item asked for.
#
# A cron script inherits a minimal PATH: python3 is resolved explicitly, and the interpreter has
# to be able to run a stdlib-only script (it does — no venv, no imports from the package).
set -eu

REPO="${QUEUE_REPO:-/home/ubuntu/Projects/hermes-nostr-gateway}"
QUEUE="${QUEUE_FILE:-$REPO/QUEUE.md}"
PYTHON="$(command -v python3 || echo /usr/bin/python3)"

exec "$PYTHON" "$REPO/scripts/queue_next.py" --queue "$QUEUE"
