# Compatibility: Release r2 (2026-09-24)

Release r2 develops AML/NAM as a compact agent-to-agent (A2A) wire protocol for an agent cluster and local models.

The `!AML:2|` protocol marker is unchanged. The new cluster profile `s:2` is isolated from the baseline tactical vocabulary.

## 1. Cluster profile `s:2` (T37)

The NAM/2 `s:2` extension carries board state, agent status, and control proposals:

```text
!AML:2|STATE|aml-nam|agent_sync→claude|4|1790200000000|30000|s:2|w:board|r:4
#T13:task:INP:antigravity:codex:P1:t999500:dep:#T17,#T18
#codex:agent:ACT:L72:W45:t999500:tsk:#T34
```

### Entity rows

- **Task (`task`):** `#<id>:task:<STATUS>:<assignee>:<critic>:P<priority>:t<ms>:dep:<deps>`
  - States: `BKL` (backlog), `INP` (in progress), `REV` (review), `APR` (approval/curator), `DON` (done), `CLS` (closed).
- **Agent (`agent`):** `#<name>:agent:<STATE>:L<limit>:W<weekly>:t<ms>:tsk:<task>`
  - States: `ACT` (active), `STL` (stale), `WAT` (watching/paused), `OFF` (offline).
  - Limits use `L<pct>` and `W<weekly_pct>`; `-` means unknown.

### Actions in `PROP`

- `RSG` — reassign; the validator checks that the new assignee is available and is not the critic.
- `PAU` / `RSM` — pause/resume an agent.
- `RTN` / `APV` — return a task for rework or approve it as curator.
- Reason codes (`rs:`): `LIMIT`, `STALE`, `CRITIC_RETURN`, `DEPENDS`.

### Profile gating

- Frames marked `s:2` reject tactical entities/actions such as `INF`, `REC`, `ASSAULT`, and `SET_ROE`.
- Frames without `s:2` reject task/agent rows and cluster-management actions.
- Mixing vocabularies in one frame raises `AMLError` during encoding/validation.

## 2. Cluster bus (`scripts/aml_bus.py`)

- **Frame delimiter:** two line feeds (`\n\n` or `\r\n\r\n`). The receiver does not read an incomplete file tail; it waits for the sender to finish writing the frame.
- **Task authorization:** determined from `board.json` at receive time, not from the message text:
  - `HANDOFF COMPLETE` is accepted only from the current assignee while the task is `in_progress`.
  - `RES` verdicts are accepted only from the assigned critic (while the task is in `review`) or curator (`claude`).
  - Local models (`qwen9b`, `gemma`) are recorded as advisors and cannot change the board directly.
- **Rejections:** recorded as `INVALID`, `UNAUTHORIZED`, `EXPIRED`, or `REPLAY`, logged, and sent to the sender by mail.

## 3. Clients and local models (T12, T13, T14)

- **LM Studio 0.4.x:**
  - In the SDK path (`scripts/llm_client_sdk.py`), GBNF enforcement was confirmed in the tested setup. For reasoning models, deliberation is stripped using the SDK's response separator.
  - The HTTP `/v1/chat/completions` endpoint ignored its `grammar` field in the tested setup. `scripts/llm_client.py` fails closed: it rejects a request with `AMLError` unless a grammar-support probe has passed. Use `--no-grammar` only when explicitly running without grammar enforcement.
  - Probe results are cached per process and `(endpoint, model)` pair; tests verify isolation.
- **Inter-process bridge:** Python↔Node calls on Windows use explicit UTF-8 decoding.

These are dated observations, not guarantees for other LM Studio versions or backends. Repeat the live probe for the exact version and endpoint you deploy.

## 4. Baseline profile (inherited from r1)

- **RESULT:** requires `related`; markers are `ACC`, `EXEC`, `REJ`, and `FAIL`; optional reason uses `|rs:CODE[#evidence,...]`.
- **Partial updates:** empty updates are forbidden; a partial update does not refresh `observed_ms` (use a complete upsert for a new observation); explicit `null` is rejected by the encoder.
- **Values outside the compact projection:** unsupported fields, non-standard IDs, and floats with precision finer than 0.01 raise `AMLError`; use NAM JSON for such data.
