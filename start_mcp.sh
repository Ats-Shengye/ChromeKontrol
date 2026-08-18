#!/bin/bash
# ChromeKontrol MCP bridge launcher.
#
# ISSUES.md P0-2: token resolution used to happen here (grep the server's log
# file for an "Auto-generated token" line), which broke because that log file
# never existed under the server's actual runtime (Claude Code background
# task / systemd), and because a fixed env var set at launch time could never
# pick up a token rotated by a later server restart.
#
# Token resolution now lives entirely in mcp_bridge.mjs (readToken()), which
# re-reads the token file on every request and retries once on 401. That is
# the only place resolution can happen correctly: this script runs once at
# MCP server startup, but a server restart can rotate the token at any later
# point during this script's lifetime.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
exec node "$SCRIPT_DIR/mcp_bridge.mjs"
