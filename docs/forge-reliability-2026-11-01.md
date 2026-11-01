# Forge Reliability Verification — 2026-11-01

## Summary
Coffee and Forge on Katana validated native Byte with Space Monkey Qwen 3.5 27B, including a human approval cycle. No Forge security controls were removed. The native CLI failure was resolved by correcting the local API endpoint variables from base URLs to full API paths.

## Environment
- Worktree: `~/Projects/forge-reliability`, branch `test/forge-reliability`, based on `feature/workbench-integration` at `eefc653` (PR #34 explicit completion).
- Python: 3.14.8; virtual environment installed locally.
- Space Monkey: Ollama `qwen3.5:27b-q4_K_M`, `ollama ps` reported 65,536 context, 20 GB, 100% GPU placement. This verifies allocation, **not** successful exhaustion of a 65K context window.
- Katana SSH tunnel: `127.0.0.1:11435` to remote Ollama service.

## Tested outcomes
1. **Read-only exploration:** Workbench agent reported `status: done`, reviewed Forge architecture. Its test collection went beyond the request to avoid running tests (collection is not test execution); treat agent narrative as a report, not proof of every action.
2. **Code generation and testing:** Byte reported creating `scratch/byte_test_002/calculator.py` and `test_calculator.py`. Independent Katana run of `.venv/bin/python -m pytest scratch/byte_test_002/test_calculator.py -q` showed **18/18 passing**. Git status showed only untracked `scratch/` (not committed).
3. **Approval probe 003/003B:** An agent reported a direct MCP `exec_command` rather than the native Forge-registered `exec_command`. Native approval was **not** tested by that execution. `forge check command --classify-only -- mkdir scratch/approval_probe_003b` returned APPROVAL REQUIRED (`cmd.unknown`). Code review confirmed native `ToolDispatcher._tool_exec_command` uses `CommandClassifier → SecretClassifier → Approver.submit_command → Executor.execute`, and Workbench has a `WebApprovalChannel`.
4. **CLI provider-routing failure:** With `FORGE_MODEL_PROVIDER` unset, `forge byte --model qwen3.5:27b-q4_K_M` returned a misleading context-window/provider 400 error. Setting `FORGE_MODEL_PROVIDER=local` but providing base URL `http://127.0.0.1:11435` yielded `RemoteDisconnected`. A direct curl POST to `http://127.0.0.1:11435/v1/chat/completions` returned **200 OK** and `MONKEY READY`. Reading `forge/models/local.py` revealed that Forge requires **full endpoint URLs**, not base URLs.
5. **Native CLI approval test 003C:** With corrected endpoint variables, `forge byte` displayed an interactive **APPROVAL REQUIRED** for `mkdir -p scratch/approval_probe_003c` (rule `cmd.unknown`); the human typed `y`; CLI reported `4 model turns, 4 tool calls, outcome: done`. Byte reported directory creation and verification. **Important defect:** Byte's summary incorrectly asserted "No approval was required" despite the observed approval prompt and approval. Follow-up should improve factual approval reporting.

## Working terminal configuration
```bash
export FORGE_MODEL_PROVIDER=local
export FORGE_LOCAL_ENDPOINT="http://127.0.0.1:11435/v1/chat/completions"
export FORGE_LOCAL_MODELS_ENDPOINT="http://127.0.0.1:11435/v1/models"
```

Native test form:
```bash
cd ~/Projects/forge-reliability
source .venv/bin/activate
forge byte --workspace "$PWD" --model qwen3.5:27b-q4_K_M --max-turns 10 \
  "Use exec_command to run mkdir scratch/approval_probe_003c. Wait for approval if required. Do not use another tool to create it. After approval, verify the directory exists and report the outcome."
```
Model-not-in-registry warning was nonfatal. The shell exports are session-local; no repository config was changed.

## Conclusions and next steps
- The native Forge CLI + Qwen + dispatcher + interactive approval loop **worked on a small task**. Do not disable security; it was not the blocker.
- Keep the MCP/direct-shell tool distinction clear when comparing agents and Workbench; verify actual tools/events rather than trusting self-description.
- Add narrowly scoped approval-event reporting/agent summary regression tests; Byte should not assert approval wasn't needed when a human approved.
- Consider documenting / validating full endpoint URLs during CLI setup to avoid `RemoteDisconnected`.
- Follow up with a longer multi-step task and independently verify genuine 65K context behavior; local context allocation alone is not a stress test.
- Do not merge or modify production code based only on these observations. Use a reviewed PR and preserve current branch/worktree state.

Related: issue #30 (context error attribution); PR #33 (one-command startup); PR #34 (explicit completion).
