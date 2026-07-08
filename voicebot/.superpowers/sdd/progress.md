# SDD progress — BYO mechanism (2026-07-08)

Plan: hail-website docs/superpowers/plans/2026-07-08-byo-mechanism-hail.md
Worktree: hail/.claude/worktrees/byo-mechanism (branch worktree-byo-mechanism, base b782a49)
RULES: commits OK in worktree branch ONLY; never touch hail main checkout (concurrent SMS session lives there).
Baselines: core 348+1skip, api 276, voicebot 57 — all green.
Task 1: BASE=b782a49 (in progress)
Task 1: complete (commit b570b68, review clean — spec✅ Approved; core 351+1skip; alembic up/down/up verified)
Minor (defer): params Mapped[dict] untyped; onupdate app-level only (matches file pattern).
Task 2: BASE=b570b68 (in progress)
Task 2: complete (commits 8207d33 + fix f05aeb3, review Approved after 1 cycle — env var moved to Hail core section; core 357+1skip)
Minor (defer): redundant quoted return annotation in provider_config.py.
Task 3: BASE=f05aeb3 (in progress)
Task 3: complete (commit b3589eb, review clean — spec✅ Approved; core 366+1skip)
Minor (defer): missing-base_url message says unknown provider (inherited from plan reference code).
Task 4: BASE=b3589eb (in progress)
SECURITY (mid-run): automated review flagged SSRF on openai-compatible base_url (fetched at validation AND call time). Decision (user 2026-07-08): deny private ranges + https-only, NO operator allowlist. Added Task 4b (core url_guard.assert_public_https_url) to plan; wired into provider_validation, Task 5 public LLMConfig.base_url validator, Task 6 call-time \_org_llm. Not yet exploitable — validator not wired to a live route until Task 4.
Task 4: complete (commit ee6a531, review clean — spec✅ Approved; task 7/7, api 283; OpenAPI gate exit 0, openapi.yaml diff empty; db_session→async_session fixture adaptation verified sound)
Minor (defer): validate 422s if api_key given but no stored row (needs known provider); unknown-layer 404 untested.
Task 4b: BASE=ee6a531 (in progress) — SSRF guard
