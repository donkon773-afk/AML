"use strict";

const test = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const path = require("node:path");
const codecPath = fs.existsSync(path.join(__dirname, "aml_codec.js"))
  ? "./aml_codec.js"
  : "../scripts/aml_codec.js";
const { AMLCodec, AMLError } = require(codecPath);

test("AML Codec: State Keyframe serialization and roundtrip", () => {
  const original = {
    v: "NAM/1",
    id: "m1",
    session: "test_sess",
    sender: "obs",
    recipient: "gw",
    seq: 1,
    sent_ms: 1000000,
    ttl_ms: 30000,
    kind: "state",
    body: {
      world: "Altis",
      rev: 1,
      entities: [
        {
          id: "A11",
          kind: "group",
          role: "INF",
          count: 20,
          pos: [1250, 3420, 0],
          hp: 1.0,
          ammo: 0.9,
          suppression: 0.0,
          observed_ms: 999500,
          callsign: "Альфа 1-1"
        },
        {
          id: "E01",
          kind: "contact",
          role: "ARM",
          count: 1,
          pos: [1450, 3600, 15],
          hp: 0.85,
          ammo: null,
          suppression: null,
          observed_ms: 998000,
          source: "A11"
        }
      ]
    }
  };

  const aml = AMLCodec.encode(original);
  assert.ok(aml.startsWith("!AML:2|STATE|test_sess|obs→gw|1|1000000|30000|w:Altis|r:1"));
  assert.ok(aml.includes('#A11:grp:INF:20:@1250,3420:%H100A90S0:t999500:C"Альфа 1-1"'));
  assert.ok(aml.includes("#E01:cnt:ARM:1:@1450,3600,15:%H85A-S-:t998000:src:A11"));

  const decoded = AMLCodec.decode(aml);
  assert.strictEqual(decoded.session, "test_sess");
  assert.strictEqual(decoded.sender, "obs");
  assert.strictEqual(decoded.recipient, "gw");
  assert.strictEqual(decoded.body.world, "Altis");
  assert.strictEqual(decoded.body.rev, 1);
  assert.strictEqual(decoded.body.entities.length, 2);

  const e1 = decoded.body.entities[0];
  assert.strictEqual(e1.id, "A11");
  assert.strictEqual(e1.kind, "group");
  // AML's short role code "INF" maps to NAM/1's schema enum "INFANTRY" -- the
  // schema only accepts the full word (see ROLE_MAP in aml_codec.js).
  assert.strictEqual(e1.role, "INFANTRY");
  assert.strictEqual(e1.count, 20);
  assert.deepStrictEqual(e1.pos, [1250, 3420, 0]);
  assert.strictEqual(e1.hp, 1.0);
  assert.strictEqual(e1.ammo, 0.9);
  assert.strictEqual(e1.suppression, 0.0);
  assert.strictEqual(e1.callsign, "Альфа 1-1");
  // Regression: observed_ms must survive the wire, not be re-stamped with
  // Date.now() at decode time.
  assert.strictEqual(e1.observed_ms, 999500);

  const e2 = decoded.body.entities[1];
  assert.strictEqual(e2.id, "E01");
  assert.strictEqual(e2.kind, "contact");
  assert.strictEqual(e2.hp, 0.85);
  assert.strictEqual(e2.ammo, null);
  assert.deepStrictEqual(e2.pos, [1450, 3600, 15]);
  assert.strictEqual(e2.source, "A11");
  assert.strictEqual(e2.observed_ms, 998000);
});

test("AML Codec: entity missing observed_ms is rejected at encode", () => {
  const msg = {
    v: "NAM/1", id: "m1", session: "s", sender: "obs", recipient: "gw",
    seq: 1, sent_ms: 1000000, ttl_ms: 30000, kind: "state",
    body: { world: "Altis", rev: 1, entities: [
      { id: "A11", kind: "group", role: "INF", count: 20, pos: [0, 0, 0] }
    ]}
  };
  assert.throws(() => AMLCodec.encode(msg), AMLError);
});

test("AML Codec: entity row missing observation token is rejected at decode", () => {
  const legacyRow = "!AML:2|STATE|s|obs→gw|1|1000000|30000|w:Altis|r:1\n#A11:grp:INF:20:@1250,3420:%H100A90S0";
  assert.throws(() => AMLCodec.decode(legacyRow), AMLError);
});

test("AML Codec: Delta mutations and state reconstruction", () => {
  const baseState = {
    v: "NAM/1",
    id: "m1",
    session: "s1",
    sender: "obs",
    recipient: "gw",
    seq: 1,
    sent_ms: 1000000,
    ttl_ms: 30000,
    kind: "state",
    body: {
      world: "Altis",
      rev: 1,
      entities: [
        { id: "A11", kind: "group", role: "INF", count: 20, pos: [1250, 3420, 0], hp: 1.0, ammo: 0.9, suppression: 0.0, observed_ms: 1000000 },
        { id: "E01", kind: "contact", role: "ARM", count: 1, pos: [1450, 3600, 0], hp: 1.0, ammo: null, suppression: null, observed_ms: 1000000 }
      ]
    }
  };

  const deltaMsg = {
    v: "NAM/1",
    id: "m2",
    session: "s1",
    sender: "obs",
    recipient: "gw",
    seq: 2,
    sent_ms: 1002000,
    ttl_ms: 30000,
    kind: "delta",
    body: {
      world: "Altis",
      base: 1,
      rev: 2,
      upsert: [
        { id: "E02", kind: "contact", role: "INF", count: 4, pos: [1320, 3480, 0], hp: 1.0, ammo: 0.8, suppression: 0.0, observed_ms: 1002000 }
      ],
      updates: [
        { id: "A11", pos: [1265, 3435, 0], hp: 0.92, suppression: 0.15 }
      ],
      remove: ["E01"]
    }
  };

  const amlDelta = AMLCodec.encode(deltaMsg);
  assert.ok(amlDelta.includes("~#A11:@1265,3435:%H92A-S15"));
  assert.ok(amlDelta.includes("+#E02:cnt:INF:4:@1320,3480:%H100A80S0:t1002000"));
  assert.ok(amlDelta.includes("-#E01"));

  const decodedDelta = AMLCodec.decode(amlDelta);
  assert.strictEqual(decodedDelta.body.base, 1);
  assert.strictEqual(decodedDelta.body.rev, 2);
  assert.strictEqual(decodedDelta.body.upsert.length, 1);
  assert.strictEqual(decodedDelta.body.updates.length, 1);
  assert.deepStrictEqual(decodedDelta.body.remove, ["E01"]);

  // Apply delta onto base state
  const updatedState = AMLCodec.applyDelta(baseState, decodedDelta);
  assert.strictEqual(updatedState.body.rev, 2);
  assert.strictEqual(updatedState.body.entities.length, 2); // A11 updated, E01 deleted, E02 added

  const a11 = updatedState.body.entities.find(e => e.id === "A11");
  assert.ok(a11);
  assert.deepStrictEqual(a11.pos, [1265, 3435, 0]);
  assert.strictEqual(a11.hp, 0.92);
  assert.strictEqual(a11.suppression, 0.15);
  assert.strictEqual(a11.ammo, 0.9); // preserved from base

  const e01 = updatedState.body.entities.find(e => e.id === "E01");
  assert.strictEqual(e01, undefined); // removed

  const e02 = updatedState.body.entities.find(e => e.id === "E02");
  assert.ok(e02);
  assert.deepStrictEqual(e02.pos, [1320, 3480, 0]);
});

test("AML Codec: Tactical Proposal and Action Mapping", () => {
  const proposal = {
    v: "NAM/1",
    id: "m3",
    session: "s1",
    sender: "loc",
    recipient: "gw",
    seq: 3,
    sent_ms: 1003000,
    ttl_ms: 30000,
    kind: "proposal",
    body: {
      world: "Altis",
      rev: 2,
      state_sender: "obs",
      reason: { code: "CONTACT", evidence: ["E02"] },
      actions: [
        { type: "ASSAULT", target: "A11", position: [1320, 3480, 0], op: "op10" },
        { type: "SET_ROE", target: "A11", roe: "OPEN_FIRE", op: "op11" },
        { type: "REQUEST_FIRE", target: "A12", position: [1320, 3480, 0], support: "MORTAR", op: "op12" }
      ]
    }
  };

  const aml = AMLCodec.encode(proposal);
  assert.ok(aml.startsWith("!AML:2|PROP|s1|loc→gw|3|1003000|30000|w:Altis|r:2|by:obs|rs:CONTACT[#E02]"));
  assert.ok(aml.includes("$ATK:#A11:@1320,3480:#op10"));
  assert.ok(aml.includes("$ROE:#A11:OPEN_FIRE:#op11"));
  assert.ok(aml.includes("$FIRE:#A12:@1320,3480:MORTAR:#op12"));

  const decoded = AMLCodec.decode(aml);
  assert.strictEqual(decoded.kind, "proposal");
  assert.strictEqual(decoded.body.reason.code, "CONTACT");
  assert.deepStrictEqual(decoded.body.reason.evidence, ["E02"]);
  assert.strictEqual(decoded.body.actions.length, 3);

  assert.strictEqual(decoded.body.actions[0].type, "ASSAULT");
  assert.strictEqual(decoded.body.actions[0].target, "A11");
  assert.deepStrictEqual(decoded.body.actions[0].position, [1320, 3480, 0]);
  assert.strictEqual(decoded.body.actions[0].op, "op10");

  assert.strictEqual(decoded.body.actions[1].type, "SET_ROE");
  assert.strictEqual(decoded.body.actions[1].roe, "OPEN_FIRE");

  assert.strictEqual(decoded.body.actions[2].type, "REQUEST_FIRE");
  assert.strictEqual(decoded.body.actions[2].support, "MORTAR");
});

test("AML Codec: proposal action without op is rejected at encode", () => {
  const proposal = {
    v: "NAM/1", id: "m3", session: "s1", sender: "loc", recipient: "gw",
    seq: 3, sent_ms: 1003000, ttl_ms: 30000, kind: "proposal",
    body: { world: "Altis", rev: 2, state_sender: "obs",
      reason: { code: "CONTACT", evidence: ["E02"] },
      actions: [{ type: "HOLD", target: "A11" }] }
  };
  assert.throws(() => AMLCodec.encode(proposal), AMLError);
});

test("AML Codec: action row without op is rejected at decode", () => {
  const legacy = "!AML:2|PROP|s1|loc→gw|3|1003000|30000|w:Altis|r:2|by:obs|rs:CONTACT[#E02]\n$HLD:#A11";
  assert.throws(() => AMLCodec.decode(legacy), AMLError);
});

test("AML Codec: decoded action op is deterministic (no Math.random fallback)", () => {
  const proposal = {
    v: "NAM/1", id: "m3", session: "s1", sender: "loc", recipient: "gw",
    seq: 3, sent_ms: 1003000, ttl_ms: 30000, kind: "proposal",
    body: { world: "Altis", rev: 2, state_sender: "obs",
      reason: { code: "CONTACT", evidence: ["E02"] },
      actions: [{ type: "HOLD", target: "A11", op: "op1" }] }
  };
  const aml = AMLCodec.encode(proposal);
  const first = AMLCodec.decode(aml).body.actions[0].op;
  const second = AMLCodec.decode(aml).body.actions[0].op;
  assert.strictEqual(first, "op1");
  assert.strictEqual(first, second);
});

test("AML Codec: AUTONOMY action roundtrips", () => {
  const proposal = {
    v: "NAM/1", id: "m9", session: "s1", sender: "loc", recipient: "gw",
    seq: 9, sent_ms: 1009000, ttl_ms: 30000, kind: "proposal",
    body: { world: "Altis", rev: 2, state_sender: "obs",
      reason: { code: "STALLED", evidence: [] },
      actions: [{ type: "AUTONOMY", target: "A11", op: "op9" }] }
  };
  const aml = AMLCodec.encode(proposal);
  assert.ok(aml.includes("$AUTO:#A11:#op9"));
  const decoded = AMLCodec.decode(aml);
  assert.strictEqual(decoded.body.actions[0].type, "AUTONOMY");
});

test("AML Codec: Queries and Results (Supervisor-Executive Dialog)", () => {
  // Query
  const query = {
    v: "NAM/1",
    id: "m4",
    session: "s1",
    sender: "cld",
    recipient: "loc",
    seq: 4,
    sent_ms: 1004000,
    ttl_ms: 30000,
    kind: "query",
    body: { request: "EXPLAIN", related: "A11" }
  };
  const amlQ = AMLCodec.encode(query);
  assert.ok(amlQ.includes("?WHY:#A11"));
  const decQ = AMLCodec.decode(amlQ);
  assert.strictEqual(decQ.body.request, "EXPLAIN");
  assert.strictEqual(decQ.body.related, "A11");
  assert.strictEqual(decQ.body.topic, undefined);

  // Result / Explanation response
  const res = {
    v: "NAM/1",
    id: "m5",
    session: "s1",
    sender: "loc",
    recipient: "cld",
    seq: 5,
    sent_ms: 1004200,
    ttl_ms: 30000,
    kind: "result",
    body: {
      status: "ACCEPTED", operations: [],
      related: "A11",
      reason: { code: "LOW_AMMO", evidence: ["E02"] }
    }
  };
  const amlR = AMLCodec.encode(res);
  assert.ok(amlR.includes("rel:A11|*ACC:|rs:LOW_AMMO[#E02]"));
  const decR = AMLCodec.decode(amlR);
  assert.strictEqual(decR.body.status, "ACCEPTED");
  assert.strictEqual(decR.body.related, "A11");
  assert.strictEqual(decR.body.reason.code, "LOW_AMMO");
  assert.deepStrictEqual(decR.body.reason.evidence, ["E02"]);
});

test("AML Codec: Task Handoff Protocol", () => {
  const handoff = {
    v: "NAM/1",
    id: "m6",
    session: "s1",
    sender: "cld",
    recipient: "loc",
    seq: 6,
    sent_ms: 1005000,
    ttl_ms: 30000,
    kind: "handoff",
    body: {
      task: "AML_CODEC_DEV",
      status: "COMPLETE",
      summary: "AML v2.0 codec verified and benchmarked",
      artifacts: [{ path: "scripts/aml_codec.js", sha256: "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855" }],
      checks: [{ name: "unit_tests", result: "PASS", evidence: "All 10 tests green" }],
      next: "Deploy bridge adapter"
    }
  };
  const amlH = AMLCodec.encode(handoff);
  assert.ok(amlH.includes("tsk:AML_CODEC_DEV|st:COMPLETE"));
  assert.ok(amlH.includes('SUM:"AML v2.0 codec verified and benchmarked"'));
  assert.ok(amlH.includes("ART:scripts/aml_codec.js:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"));
  assert.ok(amlH.includes('CHK:unit_tests:PASS:"All 10 tests green"'));

  const decH = AMLCodec.decode(amlH);
  assert.strictEqual(decH.body.task, "AML_CODEC_DEV");
  assert.strictEqual(decH.body.status, "COMPLETE");
  assert.strictEqual(decH.body.summary, "AML v2.0 codec verified and benchmarked");
  assert.strictEqual(decH.body.artifacts.length, 1);
  assert.strictEqual(decH.body.checks.length, 1);
  assert.strictEqual(decH.body.next, "Deploy bridge adapter");
});

test("AML Codec: Error Handling & Invariant Enforcement", () => {
  assert.throws(() => AMLCodec.decode(""), AMLError);
  assert.throws(() => AMLCodec.decode("NOT_AML_HEADER"), AMLError);
  assert.throws(() => AMLCodec.decode("!AML:1|STATE|s1|a→b|1|0|0"), AMLError); // wrong version
  assert.throws(() => AMLCodec.decode("!AML:2|STATE|s1|a_b|1|0|0"), AMLError); // bad route separator
});

test("AML Codec: NAM/2 schema and trace_id survive the compact projection", () => {
  const state = {
    v: "NAM/2", id: "m1", session: "s1", sender: "obs", recipient: "gw",
    seq: 1, sent_ms: 1000000, ttl_ms: 30000, kind: "state",
    schema: 17, trace_id: "trace.42",
    body: { world: "Altis", rev: 1, entities: [
      { id: "A11", kind: "group", role: "INF", count: 20, pos: [1250, 3420, 0],
        hp: 1.0, ammo: 0.9, suppression: 0.0, observed_ms: 1000000 }
    ]}
  };
  const aml = AMLCodec.encode(state);
  assert.ok(aml.includes("|s:17|tr:trace.42|"));

  const decoded = AMLCodec.decode(aml);
  assert.strictEqual(decoded.v, "NAM/2");
  assert.strictEqual(decoded.schema, 17);
  assert.strictEqual(decoded.trace_id, "trace.42");
});

test("AML Codec: frames without NAM/2 fields stay on the NAM/1 dialect", () => {
  const state = {
    v: "NAM/1", id: "m1", session: "s1", sender: "obs", recipient: "gw",
    seq: 1, sent_ms: 1000000, ttl_ms: 30000, kind: "state",
    body: { world: "Altis", rev: 1, entities: [] }
  };
  const decoded = AMLCodec.decode(AMLCodec.encode(state));
  assert.strictEqual(decoded.v, "NAM/1");
  assert.strictEqual(decoded.schema, undefined);
  assert.strictEqual(decoded.trace_id, undefined);
});
