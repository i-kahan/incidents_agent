# Service Dependency Architecture

## Call Chain

Requests flow from `api-gateway` -> `order-service` -> `inventory-db` (with optional `cache-proxy` lookups).

## Retry Amplification

Because `api-gateway` holds open client retries while waiting on
`order-service`, downstream stalls in `inventory-db` inflate
`api_gateway_queue_depth` most visibly even though `api-gateway`
is only a victim.
