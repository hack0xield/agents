#!/usr/bin/env bash
# Put a Node that satisfies OpenClaw's engine requirement (>=22.22.3) on PATH.
#
# The system node here is v20.9.0 and OpenClaw hard-refuses to start on it.
# Rather than change the global node (which other projects on this machine
# depend on), Node 24 lives in a user-owned prefix and is prepended to PATH
# only for the processes this repo starts.
#
# Sourced by the other scripts; not meant to be run directly.

N_PREFIX="${N_PREFIX:-$HOME/.local/n}"

if [ -x "$N_PREFIX/bin/node" ]; then
  PATH="$N_PREFIX/bin:$PATH"
  export PATH
fi

_node_major="$(node -p 'process.versions.node.split(".")[0]' 2>/dev/null || echo 0)"
if [ "$_node_major" -lt 22 ]; then
  echo "error: node >=22.22.3 required, found $(node --version 2>/dev/null || echo none)." >&2
  echo "       install it with:  N_PREFIX=\"$N_PREFIX\" n 24" >&2
  exit 1
fi
