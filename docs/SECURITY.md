# API security

The service has two independent credentials:

1. `MASTER_TOKEN_FILE` lets the service authenticate to Google NotebookLM. It
   is an internal full-account credential and must never leave the service.
2. `NOTEBOOKLM_API_KEY` lets Ganrl or another approved consumer authenticate to
   this wrapper. The caller sends it in the `X-API-Key` header.

These credentials are not interchangeable.

## Route policy

- `GET /health` is public so Render can monitor the process. It reports only
  service readiness and whether consumer authentication is configured.
- `GET /status` requires `X-API-Key` because it probes the live Google account
  and reports internal database details.
- Every route below `/api/`, including `POST /api/auth/refresh`, requires
  `X-API-Key`.
- `/docs` and `/openapi.json` describe the interface but expose no research
  records. OpenAPI marks every protected operation with the
  `NotebookLMConsumerKey` scheme.

If `NOTEBOOKLM_API_KEY` is absent, protected routes return HTTP 503. A missing
or incorrect caller key returns HTTP 401. There is no local or debug bypass.

## Render setup

Generate one strong random value outside the repository. Configure the same
value as:

- `NOTEBOOKLM_API_KEY` on `notebooklm-api`;
- the corresponding NotebookLM consumer secret on Ganrl.

Ganrl must attach it server-side:

```http
X-API-Key: <consumer credential>
```

Do not put the key in browser JavaScript, URLs, logs, query parameters,
artifacts, or research receipts. After configuration, verify:

1. `GET /health` succeeds without a key and reports `consumer_api_auth` as
   `configured`.
2. `GET /api/notebooks` returns 401 without a key.
3. The same request succeeds with the configured key.
4. `POST /api/auth/refresh` returns 401 without a key. Do not invoke it merely
   as a deployment test; it changes the Google session.

## CORS

Ganrl should normally call this service from its backend, so CORS can remain
disabled. If a trusted browser application must call the service directly, set
`CORS_ALLOWED_ORIGINS` to exact origins separated by commas, for example:

```text
https://research.example.org,https://review.example.org
```

The wildcard origin `*` is rejected. Allowing a browser origin does not replace
API authentication.
