#!/usr/bin/env bash
# Start MFA's database, then the HTTP server.
#
# MFA 3.x keeps its corpus state in a PostgreSQL instance that it manages
# itself via `mfa server`. That has to be running before any `mfa align` call,
# and it cannot be a plain CMD because Cloud Run needs uvicorn to own PID 1 and
# receive SIGTERM for graceful shutdown.
#
# A failed database start is not fatal here: the server still boots and reports
# it through /ready, which is far easier to debug than a container that exits
# during startup with no logs.
set -uo pipefail

echo '{"severity":"INFO","message":"starting MFA database"}'
if mfa server start 2>&1 | sed 's/^/[mfa-server] /'; then
    echo '{"severity":"INFO","message":"MFA database started"}'
else
    # `server init` is only needed the first time; on a fresh container the
    # image build may not have left an initialised cluster behind.
    echo '{"severity":"WARNING","message":"mfa server start failed, trying init"}'
    mfa server init 2>&1 | sed 's/^/[mfa-init] /' || true
    mfa server start 2>&1 | sed 's/^/[mfa-server] /' \
        || echo '{"severity":"ERROR","message":"MFA database unavailable; /ready will report not-ready"}'
fi

# exec so uvicorn replaces this shell as PID 1 and gets SIGTERM directly.
exec uvicorn server:app \
    --host 0.0.0.0 \
    --port "${PORT:-8080}" \
    --workers 1 \
    --timeout-keep-alive 620 \
    --no-access-log
