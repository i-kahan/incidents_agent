# Authentication Service Runbook

## Overview

The `auth-service` validates JWT signatures using keys fetched from
the identity provider JWKS endpoint.

## JWKS Timeout Errors

If `AUTH_JWKS_TIMEOUT_MS` is set below 50ms, key refresh requests
fail before TLS handshakes complete, causing `auth-service` to
reject valid tokens while CPU remains low. Restore the timeout to
2000ms to mitigate.
