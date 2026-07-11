# Bug Tracking — notebooklm-api

## Lazy-init state never triggered by its own probe (2026-07-11)

**Problem Class**: Chicken-and-egg in lazy initialization — a status endpoint
gates its "probe the client" call on a state that can only be reached BY probing
the client.

**Root Cause**: `/status` only initialized the NotebookLM client when the auth
profile was already seeded (`master_token` / `storage_state_only`), but seeding
the profile from the Render secret file happens INSIDE client init. Fresh deploys
therefore sat in `secret_file_pending` forever unless a real query arrived.

**Files Fixed**:
- `src/routes/health.py:87-90` — include `secret_file_pending` in the states that
  probe (and thereby initialize/seed) the client.

**Pattern to Watch For**: any lazily-initialized singleton whose health/status
reporting reads the same state that initialization would create. Status endpoints
should either attempt initialization or report "uninitialized" explicitly — never
gate init on post-init state.
