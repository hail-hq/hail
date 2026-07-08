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
Task 4b: complete (commits 00b630b + fix a2ae57c, review Approved after 1 cycle — SSRF guard; 2 Important fixed: async DNS offload via to_thread, version-independent ipv4_mapped normalization; core 382+1skip; TOCTOU brief-waived)
Task 5: BASE=a2ae57c (in progress) — includes SSRF guard on public LLMConfig.base_url (Step 2b)
Task 5: complete (commit 2967fd7 — encrypted per-call llm key in dispatch, org id in metadata, per-call voice_id, provider_key_error reason)
Task 6: BASE=2967fd7 (in progress) — voicebot per-org BYO resolution, fail-fast + opt-in fallback
Task 6: complete (commit 2567088 — see report; TDD RED->GREEN, 8 tests in test_pipeline_byo.py; voicebot 65, core 382+1skip, api 287 all green). Deviation from brief: agent.py's ProviderKeyError handler now calls on_call_end() directly before ctx.shutdown() — at that point in entrypoint, ctx.add_shutdown_callback(\_shutdown) has not yet been registered, so the brief's snippet as written would have left the Call row unfinalized and leaked the pool reservation on every BYO build failure. Also extended the test fixture's plugin Stub with num_channels/sample_rate/capabilities/label/.on() so the real (unmocked) tts.FallbackAdapter construction in test_fallback_enabled_wraps_house_after_byo doesn't AttributeError — a bare object-stub doesn't satisfy the installed FallbackAdapter's validation, which reads those off each wrapped instance.
Task 6 review fix (commit pending): re-review confirmed the Call-finalization fix correct+idempotent. 1 Important + 1 Minor addressed. Important: org id parse + resolve_org_configs + decrypt_llm_metadata now sit INSIDE the guarded path — a nested try converts {ValueError (malformed org id), InvalidToken (decrypt after HAIL_PROVIDER_SECRET_KEY rotation), SecretKeyMissing, SQLAlchemyError (DB)} into ProviderKeyError so they finalize via the SAME clean path instead of escaping entrypoint() raw and leaking the pool number. Minor: \_org_llm openai-compatible branch uses params.get("base_url") + explicit ProviderKeyError on falsy (was params["base_url"] KeyError escape). New test test_entrypoint_org_config_load_failure_finalizes_cleanly drives real entrypoint() with a fake ctx + monkeypatched resolve raising InvalidToken; asserts Call row -> failed/provider_key_error and no propagation. voicebot 66, core 382+1skip, api 287 all green.
