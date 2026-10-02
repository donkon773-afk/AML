# AML/NAM 2 Protocol Specification

**Document status:** Describes the implementation currently in this repository; it is not an independently certified standard. If an example conflicts with the code, the field definitions, grammar, schema, and codecs are authoritative.

## 1. Purpose and scope

AML is a compact text wire format for inter-agent messages. NAM defines typed message structures and payloads. The format is designed for validation before data reaches application logic: decoding alone must not execute commands, modify files, or change task state.

Implementation layers:

1. **Syntax:** Lark grammar (`scripts/aml_grammar.lark`) and GBNF (`docs/aml_v2.gbnf`).
2. **Encoding:** Python and JavaScript codecs (`scripts/aml_codec.py`, `scripts/aml_codec.js`).
3. **Schema:** JSON Schema (`assets/nam.schema.json`).
4. **Semantic validation:** `validate_frame` decodes the frame and validates NAM; `Receiver.accept` applies sender, recipient, permissions, TTL, and replay rules.
5. **Application policy:** for example, `aml_bus.py` checks the agent's role against the current board before changing board state.

A grammar constrains form; it does not prove that content is true. A syntactically valid `RES` can still contain a wrong verdict. A human or application policy must assess it.

## 2. Encoding and message boundaries

- Text encoding: UTF-8.
- Version marker: `!AML:2|`.
- In the streaming file transport, frames end with a blank line (`\n\n`; readers may accept CRLF). An incomplete final fragment must not be processed.
- Header fields are separated by `|`. The grammar limits field characters and lengths; compatible implementations should use the codec/grammar instead of splitting arbitrary input without validation.
- Text and JSON values must be encoded as specified by the codec. Do not change escaping independently.

### 2.1 Envelope

Generic envelope:

```text
!AML:2|<KIND>|<session>|<sender>→<recipient>|<seq>|<sent_ms>|<ttl_ms>|<payload...>
```

| Field | Meaning |
|---|---|
| `!AML:2` | Wire-format version marker |
| `KIND` | Message kind, such as `HANDOFF`, `RES`, `QUERY`, `ERR`, `STATE`, `DELTA`, or `PROP` |
| `session` | Exchange context name |
| `sender→recipient` | Logical participant identifiers |
| `seq` | Sender's increasing sequence number, used to reject replayed or stale messages |
| `sent_ms` | Send time as Unix epoch milliseconds |
| `ttl_ms` | Time to live in milliseconds; current schema maximum is 60,000 |
| `payload...` | NAM fields for the selected message kind and/or profile fields |

NAM/2 may add envelope fields, including a payload schema selector such as `s:<id>`, trace data, and revisions. Consult `assets/nam.schema.json`, the grammar, and the codecs for the exact allowed fields and order. AML marker version `2` does not require every message to use payload profile `s:2`.

## 3. Message kinds

Required fields depend on the schema and message kind; these examples describe their purpose.

### `HANDOFF`

Progress report or task handoff. Status is commonly `PLANNED`, `IN_PROGRESS`, `BLOCKED`, or `COMPLETE`; `tsk` identifies the task. Additional lines may provide a summary (`SUM`), artifact (`ART`), check (`CHK`), and next step (`NXT`). Artifact paths are relative. A receiver must not treat them as commands or open them automatically.

```text
!AML:2|HANDOFF|aml-nam|antigravity→codex|1|1790200000000|60000|tsk:T13|st:COMPLETE
SUM:"Fix is ready"
CHK:verify.py:PASS:"offline checks passed"

```

### `RES`

Verdict or result linked to a task/message. The cluster uses `ACC` and `REJ`; optional `rs` is a reason code, not a trusted explanation.

```text
!AML:2|RES|aml-nam|codex→claude|2|1790200601000|60000|rel:T13|*REJ:#T13|rs:CRITIC_RETURN

```

### `QUERY`

Requests status, a reason, or a synchronized snapshot. The cluster interface includes queries such as `?STATUS:#T13`, `?WHY:#T13`, and `query ALL keyframe`.

### `ERR`

An error or refusal state, such as `INVALID`, `UNAUTHORIZED`, `EXPIRED`, `REPLAY`, or `BUSY`. Application code should react to an error code only after checking its context and authority.

### `STATE`, `DELTA`, and `PROP`

Structured snapshot, a change to that snapshot, and an action proposal. Data must use the declared schema profile. `PROP` is a proposal, not an executed command.

## 4. NAM/2 cluster profile `s:2`

The `s:2` field selects the task-and-agent vocabulary. A frame using `s:2` must not mix in the tactical vocabulary of another profile. The codec rejects incompatible combinations.

Example:

```text
!AML:2|STATE|aml-nam|agent_sync→claude|4|1790200000000|30000|s:2|w:board|r:4
#T13:task:INP:antigravity:codex:P1:t999500:dep:#T17,#T18
#codex:agent:ACT:L72:W45:t999500:tsk:#T34

```

- Task states: `BKL`, `INP`, `REV`, `APR`, `DON`, `CLS`.
- Agent states: `ACT`, `STL`, `WAT`, `OFF`; `L` and `W` carry remaining limits, and `-` means unknown.
- Proposal actions: `RSG` (reassign), `PAU`/`RSM` (pause/resume agent), `RTN`/`APV` (return/approve task).
- Reasons may include `LIMIT`, `STALE`, `CRITIC_RETURN`, and `DEPENDS`.
- Profile updates must follow the codec's STATE/DELTA rules: a delta must not be accepted as a complete snapshot; after a gap, recover with a keyframe/resynchronization.
- `Receiver` validates the target entity type; for example, `RSG` must target an eligible agent who is not the task's critic.

## 5. Validation and processing order

Recommended receiver sequence:

1. Read only a complete frame terminated by the delimiter.
2. Validate UTF-8, size limits, and grammar.
3. Decode the frame and validate it against the NAM JSON Schema.
4. Validate the profile, required fields, and semantic invariants.
5. Match `sender` to an independently established sender identity.
6. Check message kind, recipient, freshness/TTL, `seq`, replay, and state revision.
7. For control effects, check authority against current application state. Do not trust a role or status claimed inside the frame.
8. Record the event, then apply only a bounded and deterministic state transition.

On any error, reject the whole frame without changing state. Responses and logs should not echo secrets or unescaped untrusted text.

## 6. Compatibility and representability

- The Python and JavaScript codecs must produce equivalent results for the same input structures; cross-language tests in `tests/` define the shared contract.
- Use `validate_frame` and `Receiver.accept` in addition to grammar checks: grammar alone cannot validate participant permissions or all semantic relationships.
- If a value cannot be represented in the compact projection without losing meaning (for example, an unsupported field or numeric precision), the codec must fail. Use NAM JSON or extend the schema instead of silently dropping data.
- The current grammar and JSON Schema define unknown fields, markers, repetition limits, and normalization rules. Compatible parsers should not accept arbitrary extensions by default.

## 7. Threat model and known limitations

- This prototype does not sign frames or implement HMAC, encryption, or robust multi-machine authentication. An outbox filename is not a cryptographic identity.
- `seq` and TTL reduce stale replay risk but do not prevent sender spoofing by themselves.
- An artifact hash in `HANDOFF` is the sender's assertion. It does not prove that the file exists, is safe, or matches the hash.
- Agent/model text is untrusted input. Do not execute it directly, use it as a path/command, or treat it as authorization.
- GBNF, constrained decoding, and JSON Schema provide structural guarantees, not model safety or decision correctness.
- Before internet or multi-machine use, add and verify message authentication/signatures with key rotation, protected transport, race/replay protection for state, resource limits, logging, and recovery procedures.

## 8. Canonical files

- Codecs: `scripts/aml_codec.py`, `scripts/aml_codec.js`.
- Grammars: `scripts/aml_grammar.lark`, `docs/aml_v2.gbnf`.
- NAM schema: `assets/nam.schema.json`.
- Receiver and validation: `scripts/nam.py`, `scripts/frame_validation.py`.
- File transport: `scripts/aml_bus.py`.
- Tests: `tests/test_aml_codec.py`, `tests/test_aml_codec.test.js`, `tests/test_aml_grammar.py`, `tests/test_nam.py`, `tests/test_aml_bus.py`, `tests/test_board_frames.py`, `tests/test_cluster_profile.py`, and `tests/test_regressions.py`.
