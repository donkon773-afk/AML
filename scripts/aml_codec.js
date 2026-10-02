"use strict";

/**
 * AML v2.0 (Agent Machine Language) Guarded compact codec & delta engine
 * High-density, BPE-aligned machine language for AI-to-AI (A2A) communication.
 * 
 * Complies with NeroAI Eye architecture specifications.
 */

const ACTION_MAP = {
  ADV: "ADVANCE",
  ATK: "ASSAULT",
  HLD: "HOLD",
  RET: "RETREAT",
  REC: "RECON",
  FIRE: "REQUEST_FIRE",
  BRD: "BOARD",
  DIS: "DISEMBARK",
  REP: "REPAIR",
  ROE: "SET_ROE",
  SEC: "SET_SECTOR",
  RPT: "REPORT",
  CAN: "CANCEL",
  SUPP: "REQUEST_SUPPORT",
  INTEL: "SHARE_INTEL",
  NEG: "NEGOTIATE",
  DES: "DESIGNATE",
  AUTO: "AUTONOMY",
  // Cluster profile (payload schema s:2, T37): task/agent coordination.
  RSG: "REASSIGN",
  PAU: "PAUSE",
  RSM: "RESUME",
  RTN: "RETURN",
  APV: "APPROVE"
};

const REV_ACTION_MAP = Object.fromEntries(
  Object.entries(ACTION_MAP).map(([k, v]) => [v, k])
);

const KIND_MAP = {
  grp: "group",
  unt: "unit",
  veh: "vehicle",
  cnt: "contact"
};

const REV_KIND_MAP = Object.fromEntries(
  Object.entries(KIND_MAP).map(([k, v]) => [v, k])
);

// AML's compact role codes vs. NAM/1's full-word role enum
// (nam.schema.json $defs.entity.properties.role). These never matched before
// this fix: every "INF"/"REC"/"MEC"/"ARM"/"UNK" entity produced by the AML
// encoder failed NAM/1 schema validation the moment it was decoded and
// re-checked -- nothing in the original test suite ever ran that check.
const ROLE_MAP = {
  INF: "INFANTRY",
  REC: "RECON",
  MEC: "MECHANIZED",
  ARM: "ARMORED",
  UNK: "UNKNOWN"
};

const REV_ROLE_MAP = Object.fromEntries(
  Object.entries(ROLE_MAP).map(([k, v]) => [v, k])
);

// ---- Cluster profile (T37) ------------------------------------------------
// Task/agent rows ride in STATE/DELTA/PROP only under payload schema s:2 and
// never share a frame with field (Arma) rows -- see checkProfile().
const CLUSTER_SCHEMA = 2;
const CLUSTER_KINDS = ["task", "agent"];
const CLUSTER_ACTIONS = ["REASSIGN", "PAUSE", "RESUME", "RETURN", "APPROVE"];
const TASK_STATUS_MAP = {
  BKL: "backlog",
  INP: "in_progress",
  REV: "review",
  APR: "approval",
  DON: "done",
  CLS: "closed"
};
const REV_TASK_STATUS_MAP = Object.fromEntries(
  Object.entries(TASK_STATUS_MAP).map(([k, v]) => [v, k])
);
const AGENT_STATE_MAP = {
  ACT: "active",
  STL: "stale",
  WAT: "watching",
  OFF: "offline"
};
const REV_AGENT_STATE_MAP = Object.fromEntries(
  Object.entries(AGENT_STATE_MAP).map(([k, v]) => [v, k])
);

class AMLError extends Error {
  constructor(message) {
    super(message);
    this.name = "AMLError";
  }
}

function requireCondition(condition, message) {
  if (!condition) throw new AMLError(message);
}

/**
 * Format metric (0.0..1.0 or null) to compact representation (0..100 or -)
 */
function packMetric(val) {
  if (val === null || val === undefined) return "-";
  requireCondition(typeof val === "number" && Number.isFinite(val) && val >= 0 && val <= 1, "Invalid metric");
  const n = Math.round(val * 100);
  requireCondition(Math.abs(val - n / 100) <= 1e-12, "Metric precision loss; use NAM JSON");
  return String(n);
}

/**
 * Parse compact metric (0..100 or -) back to 0.0..1.0 or null
 */
function unpackMetric(str) {
  if (str === "-") return null;
  requireCondition(/^[0-9]+$/.test(str) && Number(str) <= 100, "Invalid metric");
  return Number(str) / 100;
}

/**
 * Format coordinate array [x, y, z?] to @x,y or @x,y,z
 */
function packCoords(pos) {
  requireCondition(Array.isArray(pos) && pos.length === 3, "Expected three coordinates");
  requireCondition(pos.every(x => Number.isSafeInteger(x) && Math.abs(x) <= 100000), "Coordinate precision/range unsupported; use NAM JSON");
  return `@${pos[0]},${pos[1]}` + (pos[2] ? `,${pos[2]}` : "");
}

/**
 * Parse @x,y[,z] to [x, y, z]
 */
function unpackCoords(str) {
  if (!str || !str.startsWith("@")) throw new AMLError(`Invalid coordinate format: ${str}`);
  requireCondition(/^@-?[0-9]+,-?[0-9]+(?:,-?[0-9]+)?$/.test(str), "Malformed coordinates");
  const parts = str.slice(1).split(",").map(Number);
  if (parts.length < 2 || parts.some(isNaN)) {
    throw new AMLError(`Malformed coordinates: ${str}`);
  }
  return [parts[0], parts[1], parts.length >= 3 ? parts[2] : 0];
}

/**
 * Pack metrics tuple %H..A..S..
 */
function packMetrics(hp, ammo, supp) {
  return `%H${packMetric(hp)}A${packMetric(ammo)}S${packMetric(supp)}`;
}

/**
 * Unpack metrics tuple %H..A..S..
 */
function unpackMetrics(str) {
  requireCondition(typeof str === "string" && str.startsWith("%"), "Invalid metrics");
  const m = str.slice(1).match(/^H(-|\d+)A(-|\d+)S(-|\d+)$/);
  requireCondition(m, "Invalid metrics");
  return {
    hp: unpackMetric(m[1]),
    ammo: unpackMetric(m[2]),
    suppression: unpackMetric(m[3])
  };
}

/**
 * Pack the mandatory observation timestamp as a single 't<ms>' token.
 * NAM/1 requires observed_ms on every entity for staleness/replay checks;
 * AML must carry it on the wire instead of letting decode() fabricate "now",
 * which would silently defeat every freshness check downstream.
 */
function packObserved(observedMs) {
  requireCondition(observedMs !== undefined && observedMs !== null && !isNaN(observedMs),
    "Entity is missing required 'observed_ms'");
  requireCondition(Number.isSafeInteger(observedMs) && observedMs >= 0, "Invalid observation time");
  return `t${observedMs}`;
}

function unpackObserved(token) {
  requireCondition(typeof token === "string" && /^t-?\d+$/.test(token),
    `Missing/invalid observation timestamp token: ${token}`);
  return parseInt(token.slice(1), 10);
}

function packLimit(val) {
  if (val === null || val === undefined) return "-";
  requireCondition(Number.isInteger(val) && val >= 0 && val <= 100, "Invalid limit percent");
  return String(val);
}

function unpackLimit(str) {
  if (str === "-") return null;
  requireCondition(/^[0-9]+$/.test(str) && Number(str) <= 100, "Invalid limit percent");
  return Number(str);
}

/** #T13:task:INP:antigravity:codex:P1:t<ms>[:dep:#T17,#T18] | #codex:agent:ACT:L72:W45:t<ms>[:tsk:#T34] */
function encodeClusterRow(ent, prefix) {
  const observed = packObserved(ent.observed_ms);
  if (ent.kind === "task") {
    requireCondition(Object.hasOwn(REV_TASK_STATUS_MAP, ent.status), "Invalid task status");
    requireCondition(Number.isInteger(ent.priority) && ent.priority >= 0 && ent.priority <= 99, "Invalid task priority");
    let row = `${prefix}${ent.id}:task:${REV_TASK_STATUS_MAP[ent.status]}:${ent.assignee}:${ent.critic}:P${ent.priority}:${observed}`;
    if (ent.depends && ent.depends.length) row += ":dep:" + ent.depends.map((d) => `#${d}`).join(",");
    return row;
  }
  requireCondition(Object.hasOwn(REV_AGENT_STATE_MAP, ent.state), "Invalid agent state");
  let row = `${prefix}${ent.id}:agent:${REV_AGENT_STATE_MAP[ent.state]}:L${packLimit(ent.limit)}:W${packLimit(ent.weekly)}:${observed}`;
  if (ent.task) row += `:tsk:#${ent.task}`;
  return row;
}

/** Field and cluster vocabularies never share a frame; cluster needs s:2. */
function checkProfile(msg) {
  const body = msg.body || {};
  const rows = [...(body.entities || []), ...(body.upsert || [])];
  const acts = msg.kind === "proposal" ? (body.actions || []) : [];
  const cluster = rows.filter((r) => CLUSTER_KINDS.includes(r.kind)).length +
    acts.filter((a) => CLUSTER_ACTIONS.includes(a.type)).length;
  if (msg.schema === CLUSTER_SCHEMA) {
    requireCondition(cluster === rows.length + acts.length,
      "Cluster profile (s:2) admits only task/agent rows and cluster actions");
    requireCondition(!(body.updates && body.updates.length), "Cluster profile uses full '+' upserts, not partial updates");
  } else {
    requireCondition(cluster === 0, "task/agent rows and cluster actions require payload schema s:2");
  }
}

/** Shared row-builder for STATE entities and DELTA '+' upserts. */
function encodeEntityRow(ent, prefix = "#") {
  if (CLUSTER_KINDS.includes(ent.kind)) return encodeClusterRow(ent, prefix);
  const id = ent.id;
  const eKind = REV_KIND_MAP[ent.kind] || ent.kind || "grp";
  const role = REV_ROLE_MAP[ent.role] || ent.role || "UNK";
  const count = ent.count !== undefined ? ent.count : 1;
  const coords = packCoords(ent.pos);
  const metrics = packMetrics(ent.hp, ent.ammo, ent.suppression);
  const observed = packObserved(ent.observed_ms);
  let row = `${prefix}${id}:${eKind}:${role}:${count}:${coords}:${metrics}:${observed}`;
  if (ent.callsign) row += `:C"${ent.callsign.replace(/"/g, '""')}"`;
  if (ent.source) row += `:src:${ent.source}`;
  return row;
}

function splitRow(row) {
  const parts = [];
  let current = "";
  let inQuote = false;
  for (let i = 0; i < row.length; i++) {
    const ch = row[i];
    if (ch === '"') {
      if (inQuote && row[i + 1] === '"') {
        current += '"';
        i++;
      } else {
        inQuote = !inQuote;
        current += ch;
      }
    } else if (ch === ":" && !inQuote) {
      parts.push(current);
      current = "";
    } else {
      current += ch;
    }
  }
  parts.push(current);
  return parts;
}

function preserved(source, decoded, path = "message") {
  if (Array.isArray(source)) {
    requireCondition(Array.isArray(decoded) && source.length === decoded.length, `Cannot preserve ${path}`);
    source.forEach((value, i) => preserved(value, decoded[i], `${path}[${i}]`));
  } else if (source !== null && typeof source === "object") {
    requireCondition(decoded !== null && typeof decoded === "object", `Cannot preserve ${path}`);
    for (const [key, value] of Object.entries(source)) {
      requireCondition(Object.hasOwn(decoded, key), `Cannot preserve ${path}.${key}; use NAM JSON/full upsert`);
      preserved(key === "role" ? (ROLE_MAP[value] || value) : value, decoded[key], `${path}.${key}`);
    }
  } else if (typeof source === "number" && typeof decoded === "number") {
    requireCondition(Number.isFinite(source) && Math.abs(source - decoded) <= 1e-12, `Precision loss at ${path}; use NAM JSON`);
  } else {
    requireCondition(source === decoded, `Cannot preserve ${path}; use NAM JSON`);
  }
}

class AMLCodec {
  /**
   * Encode structured object into AML v2 string
   */
  static encode(msg) {
    try {
      requireCondition(msg && ["v","id","session","sender","recipient","seq","sent_ms","ttl_ms","kind","body"].every(k => Object.hasOwn(msg,k)),
        "Complete NAM envelope required; timestamps are never invented");
      return this._encode(msg);
    } catch (e) { if (e instanceof AMLError) throw e; throw new AMLError(`Invalid or unsupported AML input: ${e.message}`); }
  }

  static decode(raw) {
    try { return this._decode(raw); }
    catch (e) { if (e instanceof AMLError) throw e; throw new AMLError(`Invalid AML text: ${e.message}`); }
  }

  static _encode(msg) {
    requireCondition(msg && typeof msg === "object", "Message must be an object");
    const kind = (msg.kind || "").toUpperCase();
    const session = msg.session || "default";
    const sender = msg.sender || "agent";
    const recipient = msg.recipient || "peer";
    const seq = msg.seq !== undefined ? msg.seq : 1;
    const sentMs = msg.sent_ms ?? Date.now();
    const ttlMs = msg.ttl_ms ?? 30000;
    const body = msg.body || {};

    // NAM/2 additions ride at the front of the variable header so every
    // message kind picks them up without a per-kind branch.
    let headerExtra = [];
    if (msg.schema !== undefined) headerExtra.push(`s:${msg.schema}`);
    if (msg.trace_id !== undefined) headerExtra.push(`tr:${msg.trace_id}`);
    const lines = [];

    if (kind === "STATE") {
      if (body.world) headerExtra.push(`w:${body.world}`);
      if (body.rev !== undefined) headerExtra.push(`r:${body.rev}`);
      const header = `!AML:2|STATE|${session}|${sender}→${recipient}|${seq}|${sentMs}|${ttlMs}` +
        (headerExtra.length ? "|" + headerExtra.join("|") : "");
      lines.push(header);

      for (const ent of body.entities || []) {
        lines.push(encodeEntityRow(ent, "#"));
      }
    } else if (kind === "DELTA") {
      if (body.world) headerExtra.push(`w:${body.world}`);
      if (body.base !== undefined) headerExtra.push(`b:${body.base}`);
      if (body.rev !== undefined) headerExtra.push(`r:${body.rev}`);
      const header = `!AML:2|DELTA|${session}|${sender}→${recipient}|${seq}|${sentMs}|${ttlMs}` +
        (headerExtra.length ? "|" + headerExtra.join("|") : "");
      lines.push(header);

      // Additions (upsert whole entity)
      for (const ent of body.upsert || []) {
        lines.push(encodeEntityRow(ent, "+#"));
      }

      // Partial updates (if specified)
      for (const upd of body.updates || []) {
        let row = `~#${upd.id}`;
        if (upd.pos) row += `:${packCoords(upd.pos)}`;
        if (upd.hp !== undefined || upd.ammo !== undefined || upd.suppression !== undefined) {
          row += `:${packMetrics(upd.hp, upd.ammo, upd.suppression)}`;
        }
        if (upd.role) row += `:role:${REV_ROLE_MAP[upd.role] || upd.role}`;
        if (upd.count !== undefined) row += `:cnt:${upd.count}`;
        lines.push(row);
      }

      // Removals
      for (const id of body.remove || []) {
        lines.push(`-#${id}`);
      }
    } else if (kind === "PROP" || kind === "PROPOSAL") {
      if (body.world) headerExtra.push(`w:${body.world}`);
      if (body.rev !== undefined) headerExtra.push(`r:${body.rev}`);
      if (body.state_sender) headerExtra.push(`by:${body.state_sender}`);
      if (body.reason) {
        const code = body.reason.code || "UNKNOWN";
        const ev = Array.isArray(body.reason.evidence) && body.reason.evidence.length
          ? `[${body.reason.evidence.map(e => `#${e}`).join(",")}]`
          : "";
        headerExtra.push(`rs:${code}${ev}`);
      }
      const header = `!AML:2|PROP|${session}|${sender}→${recipient}|${seq}|${sentMs}|${ttlMs}` +
        (headerExtra.length ? "|" + headerExtra.join("|") : "");
      lines.push(header);

      for (const act of body.actions || []) {
        requireCondition(act.op, "Proposal action must include 'op' (NAM/1 requires it; " +
          "AML must not synthesize IDs on decode)");
        const aType = REV_ACTION_MAP[act.type] || act.type;
        const target = act.target ? `#${act.target}` : "#ALL";
        let row = `$${aType}:${target}`;
        if (act.position) {
          row += `:${packCoords(act.position)}`;
        }
        const arg = act.argument || act.to || act.roe || act.support;
        if (arg) {
          row += `:${arg}`;
        }
        row += `:#${act.op}`;
        lines.push(row);
      }
    } else if (kind === "QUERY") {
      if (body.request === "KEYFRAME") {
        headerExtra.push("?KEYFRAME");
      } else if (body.request === "STATUS") {
        headerExtra.push(`?STATUS:#${body.related || "ALL"}`);
      } else if (body.request === "EXPLAIN") {
        requireCondition(!body.topic, "topic is not in NAM schema; use related only");
        headerExtra.push(`?WHY:#${body.related}${body.topic ? ":" + body.topic : ""}`);
      }
      const header = `!AML:2|QUERY|${session}|${sender}→${recipient}|${seq}|${sentMs}|${ttlMs}` +
        (headerExtra.length ? "|" + headerExtra.join("|") : "");
      lines.push(header);
    } else if (kind === "RES" || kind === "RESULT") {
      const codes = {ACCEPTED: "ACC", EXECUTED: "EXEC", REJECTED: "REJ", FAILED: "FAIL"};
      requireCondition(Object.hasOwn(codes, body.status) && body.related, "Result needs supported status and related");
      headerExtra.push(`rel:${body.related}`);
      headerExtra.push(`*${codes[body.status]}:${(body.operations || []).map(x => `#${x}`).join(",")}`);
      if (body.reason) {
        const ev = body.reason.evidence.length ? `[${body.reason.evidence.map(x => `#${x}`).join(",")}]` : "";
        headerExtra.push(`rs:${body.reason.code}${ev}`);
      }
      lines.push(`!AML:2|RES|${session}|${sender}→${recipient}|${seq}|${sentMs}|${ttlMs}|${headerExtra.join("|")}`);
    } else if (kind === "ERR" || kind === "ERROR") {
      headerExtra.push(`err:${body.code || "INVALID"}`);
      if (body.related) headerExtra.push(`rel:${body.related}`);
      const header = `!AML:2|ERR|${session}|${sender}→${recipient}|${seq}|${sentMs}|${ttlMs}` +
        (headerExtra.length ? "|" + headerExtra.join("|") : "");
      lines.push(header);
    } else if (kind === "HANDOFF") {
      headerExtra.push(`tsk:${body.task || "TASK"}`);
      headerExtra.push(`st:${body.status || "IN_PROGRESS"}`);
      const header = `!AML:2|HANDOFF|${session}|${sender}→${recipient}|${seq}|${sentMs}|${ttlMs}` +
        (headerExtra.length ? "|" + headerExtra.join("|") : "");
      lines.push(header);
      if (body.summary) lines.push(`SUM:"${body.summary.replace(/"/g, '""')}"`);
      for (const art of body.artifacts || []) {
        lines.push(`ART:${art.path}:${art.sha256}`);
      }
      for (const chk of body.checks || []) {
        lines.push(`CHK:${chk.name}:${chk.result}:"${(chk.evidence || "").replace(/"/g, '""')}"`);
      }
      if (body.next) lines.push(`NXT:"${body.next.replace(/"/g, '""')}"`);
    } else {
      throw new AMLError(`Unsupported message kind: ${kind}`);
    }

    const wire = lines.join("\n");
    preserved(msg, this.decode(wire));
    return wire;
  }

  /**
   * Decode AML v2 string into structured object
   */
  static _decode(raw) {
    requireCondition(typeof raw === "string" && raw.trim().length > 0, "Input must be non-empty string");
    requireCondition(Buffer.byteLength(raw, "utf8") <= 65536, "Message too large");
    const rawLines = raw.split(/\r?\n/).map(l => l.trim()).filter(l => l.length > 0);
    requireCondition(rawLines.length > 0, "No lines found in AML payload");

    const header = rawLines[0];
    requireCondition(header.startsWith("!AML:2|"), "Invalid AML header or version mismatch");

    const headerParts = header.slice(7).split("|");
    requireCondition(headerParts.length >= 6, "Malformed AML header envelope");

    const kind = headerParts[0].toUpperCase();
    requireCondition(["STATE", "DELTA", "PROP", "QUERY", "RES", "ERR", "HANDOFF"].includes(kind), "Unsupported kind");
    requireCondition(headerParts.slice(3, 6).every(x => /^[0-9]+$/.test(x) && Number.isSafeInteger(Number(x))), "Invalid envelope number");
    const session = headerParts[1];
    const route = headerParts[2].split("→");
    requireCondition(route.length === 2, "Malformed route sender→recipient in header");
    const sender = route[0];
    const recipient = route[1];
    const seq = parseInt(headerParts[3], 10);
    const sentMs = parseInt(headerParts[4], 10);
    const ttlMs = parseInt(headerParts[5], 10);

    const extraParams = headerParts.slice(6);
    const extraMap = {};
    for (const param of extraParams) {
      if (param.startsWith("?")) {
        extraMap["QUERY"] = param.slice(1);
      } else if (param.startsWith("*")) {
        extraMap["RESULT"] = param.slice(1);
      } else {
        const colonIdx = param.indexOf(":");
        if (colonIdx > 0) {
          extraMap[param.slice(0, colonIdx)] = param.slice(colonIdx + 1);
        }
      }
    }

    const bodyLines = rawLines.slice(1);
    const msg = {
      v: (extraMap["s"] !== undefined || extraMap["tr"] !== undefined) ? "NAM/2" : "NAM/1",
      id: `m${seq}`,
      session,
      sender,
      recipient,
      seq,
      sent_ms: sentMs,
      ttl_ms: ttlMs,
      kind: ({PROP: "proposal", RES: "result", ERR: "error"})[kind] || kind.toLowerCase(),
      body: {}
    };
    if (extraMap["s"] !== undefined) msg.schema = parseInt(extraMap["s"], 10);
    if (extraMap["tr"] !== undefined) msg.trace_id = extraMap["tr"];

    if (kind === "STATE") {
      msg.body = {
        world: extraMap["w"] || "world",
        rev: extraMap["r"] ? parseInt(extraMap["r"], 10) : 1,
        entities: []
      };

      for (const line of bodyLines) {
        if (!line.startsWith("#")) continue;
        const ent = this._parseEntity(line);
        msg.body.entities.push(ent);
      }
    } else if (kind === "DELTA") {
      msg.body = {
        world: extraMap["w"] || "world",
        base: extraMap["b"] ? parseInt(extraMap["b"], 10) : 0,
        rev: extraMap["r"] ? parseInt(extraMap["r"], 10) : 1,
        upsert: [],
        updates: [],
        remove: []
      };

      for (const line of bodyLines) {
        if (line.startsWith("+#")) {
          msg.body.upsert.push(this._parseEntity(line.slice(1)));
        } else if (line.startsWith("-#")) {
          msg.body.remove.push(line.slice(2));
        } else if (line.startsWith("~#")) {
          msg.body.updates.push(this._parseDeltaUpdate(line));
        }
      }
    } else if (kind === "PROP" || kind === "PROPOSAL") {
      let reasonCode = "UNKNOWN";
      let evidence = [];
      if (extraMap["rs"]) {
        const match = extraMap["rs"].match(/^([A-Z_]+)(?:\[(.*)\])?$/);
        if (match) {
          reasonCode = match[1];
          if (match[2]) {
            evidence = match[2].split(",").map(id => id.replace(/^#/, "").trim()).filter(Boolean);
          }
        }
      }

      msg.body = {
        world: extraMap["w"] || "world",
        rev: extraMap["r"] ? parseInt(extraMap["r"], 10) : 1,
        state_sender: extraMap["by"] || "observer",
        reason: { code: reasonCode, evidence },
        actions: []
      };

      for (const line of bodyLines) {
        if (!line.startsWith("$")) continue;
        const act = this._parseAction(line);
        if (act) msg.body.actions.push(act);
      }
    } else if (kind === "QUERY") {
      const q = extraMap["QUERY"] || "";
      if (q.startsWith("KEYFRAME")) {
        msg.body = { request: "KEYFRAME", related: "ALL" };
      } else if (q.startsWith("STATUS")) {
        const rel = q.replace(/^STATUS:#?/, "");
        msg.body = { request: "STATUS", related: rel || "ALL" };
      } else if (q.startsWith("WHY")) {
        const parts = q.slice(4).split(":");
        requireCondition(parts.length === 1, "topic is not in NAM schema");
        msg.body = {
          request: "EXPLAIN",
          related: parts[0].replace(/^#/, "")
        };
      }
    } else if (kind === "RES" || kind === "RESULT") {
      const match = (extraMap.RESULT || "").match(/^(ACC|EXEC|REJ|FAIL):(.*)$/);
      requireCondition(match && extraMap.rel, "Result needs supported status and related");
      const ops = match[2];
      requireCondition(!ops || /^#[A-Za-z0-9_.-]+(?:,#[A-Za-z0-9_.-]+)*$/.test(ops), "Invalid operations");
      msg.body = {related: extraMap.rel, status: ({ACC:"ACCEPTED",EXEC:"EXECUTED",REJ:"REJECTED",FAIL:"FAILED"})[match[1]], operations: ops ? ops.split(",").map(x => x.slice(1)) : []};
      if (extraMap.rs !== undefined) {
        const r = extraMap.rs.match(/^([A-Z_]+)(?:\[(#[A-Za-z0-9_.-]+(?:,#[A-Za-z0-9_.-]+)*)\])?$/);
        requireCondition(r, "Invalid reason");
        msg.body.reason = {code:r[1], evidence:r[2] ? r[2].split(",").map(x => x.slice(1)) : []};
      }
    } else if (kind === "ERR" || kind === "ERROR") {
      msg.body = {
        code: extraMap["err"] || "INVALID",
        related: extraMap["rel"] || "m0"
      };
    } else if (kind === "HANDOFF") {
      msg.body = {
        task: extraMap["tsk"] || "TASK",
        status: extraMap["st"] || "IN_PROGRESS",
        summary: "",
        artifacts: [],
        checks: [],
        next: ""
      };
      for (const line of bodyLines) {
        if (line.startsWith('SUM:"') && line.endsWith('"')) {
          msg.body.summary = line.slice(5, -1).replace(/""/g, '"');
        } else if (line.startsWith("ART:")) {
          const p = line.slice(4).split(":");
          msg.body.artifacts.push({ path: p[0], sha256: p[1] });
        } else if (line.startsWith("CHK:")) {
          const m = line.slice(4).match(/^([^:]+):([^:]+):"(.*)"$/);
          if (m) {
            msg.body.checks.push({ name: m[1], result: m[2], evidence: m[3].replace(/""/g, '"') });
          }
        } else if (line.startsWith('NXT:"') && line.endsWith('"')) {
          msg.body.next = line.slice(5, -1).replace(/""/g, '"');
        }
      }
    }

    checkProfile(msg);
    return msg;
  }

  static _parseEntity(line) {
    // Format: #A11:grp:INF:20:@1250,3420,0:%H100A90S0:t1000000:C"Alpha 1-1":src:A12
    const parts = splitRow(line.slice(1));
    if (parts.length > 1 && CLUSTER_KINDS.includes(parts[1])) return this._parseClusterEntity(parts, line);
    requireCondition(parts.length >= 7,
      `Entity row missing required fields (incl. observation time): ${line}`);
    const id = parts[0];
    const rawKind = parts[1];
    const kind = KIND_MAP[rawKind] || rawKind;
    const role = ROLE_MAP[parts[2]] || parts[2];
    const count = parseInt(parts[3], 10);
    const pos = unpackCoords(parts[4]);
    const metrics = unpackMetrics(parts[5]);
    const observedMs = unpackObserved(parts[6]);

    const entity = {
      id,
      kind,
      role,
      count,
      pos,
      hp: metrics.hp,
      ammo: metrics.ammo,
      suppression: metrics.suppression,
      observed_ms: observedMs
    };

    for (let i = 7; i < parts.length; i++) {
      const part = parts[i];
      if (part.startsWith('C"') && part.endsWith('"')) {
        entity.callsign = part.slice(2, -1).replace(/""/g, '"');
      } else if (part === "src" && i + 1 < parts.length) {
        entity.source = parts[++i];
      } else if (part.startsWith("src:")) {
        entity.source = part.slice(4);
      }
    }
    return entity;
  }

  static _parseClusterEntity(parts, line) {
    if (parts[1] === "task") {
      requireCondition(parts.length === 7 || parts.length === 9,
        `Task row needs status, assignee, critic, priority, time: ${line}`);
      requireCondition(Object.hasOwn(TASK_STATUS_MAP, parts[2]), "Invalid task status");
      requireCondition(/^P[0-9]{1,2}$/.test(parts[5]), "Invalid task priority");
      const ent = {
        id: parts[0], kind: "task", status: TASK_STATUS_MAP[parts[2]],
        assignee: parts[3], critic: parts[4], priority: Number(parts[5].slice(1)),
        observed_ms: unpackObserved(parts[6]), depends: []
      };
      if (parts.length === 9) {
        requireCondition(parts[7] === "dep" && /^#[A-Za-z0-9_.-]+(?:,#[A-Za-z0-9_.-]+)*$/.test(parts[8]),
          "Invalid task dependencies");
        ent.depends = parts[8].split(",").map((d) => d.slice(1));
      }
      return ent;
    }
    requireCondition(parts.length === 6 || parts.length === 8, `Agent row needs state, limit, weekly, time: ${line}`);
    requireCondition(Object.hasOwn(AGENT_STATE_MAP, parts[2]), "Invalid agent state");
    requireCondition(parts[3].startsWith("L") && parts[4].startsWith("W"), "Invalid agent limits");
    const ent = {
      id: parts[0], kind: "agent", state: AGENT_STATE_MAP[parts[2]],
      limit: unpackLimit(parts[3].slice(1)), weekly: unpackLimit(parts[4].slice(1)),
      observed_ms: unpackObserved(parts[5])
    };
    if (parts.length === 8) {
      requireCondition(parts[6] === "tsk" && /^#[A-Za-z0-9_.-]+$/.test(parts[7]), "Invalid agent task");
      ent.task = parts[7].slice(1);
    }
    return ent;
  }

  static _parseDeltaUpdate(line) {
    // Format: ~#A11:@1260,3420:%H95S10:cnt:18
    const parts = line.slice(2).split(":");
    const id = parts[0];
    const update = { id };
    for (let i = 1; i < parts.length; i++) {
      const p = parts[i];
      if (p.startsWith("@")) {
        requireCondition(!Object.hasOwn(update, "pos"), "Duplicate update field");
        update.pos = unpackCoords(p);
      } else if (p.startsWith("%")) {
        const m = unpackMetrics(p);
        for (const [key, value] of Object.entries(m)) {
          if (value !== null) { requireCondition(!Object.hasOwn(update, key), "Duplicate update field"); update[key] = value; }
        }
      } else if (p === "role" || p === "cnt") {
        requireCondition(i + 1 < parts.length, "Missing update value");
        const key = p === "role" ? "role" : "count";
        requireCondition(!Object.hasOwn(update, key), "Duplicate update field");
        const value = parts[++i];
        requireCondition(p === "role" || /^[0-9]+$/.test(value), "Invalid count");
        update[key] = p === "role" ? (ROLE_MAP[value] || value) : Number(value);
      } else { throw new AMLError("Unknown update token"); }
    }
    requireCondition(Object.keys(update).length > 1, "Empty partial update");
    return update;
  }

  static _parseAction(line) {
    // Format: $ADV:#A11:@1300,3420:OPEN_FIRE:#op1
    const parts = line.slice(1).split(":");
    const rawType = parts[0];
    const type = ACTION_MAP[rawType] || rawType;
    const target = (parts[1] || "").replace(/^#/, "");
    const action = { type, target };

    for (let i = 2; i < parts.length; i++) {
      const p = parts[i];
      if (p.startsWith("@")) {
        action.position = unpackCoords(p);
      } else if (p.startsWith("#")) {
        action.op = p.slice(1);
      } else if (["HOLD_FIRE", "RETURN_FIRE", "OPEN_FIRE"].includes(p)) {
        action.roe = p;
      } else if (["MORTAR", "ARTILLERY", "CAS"].includes(p)) {
        action.support = p;
      } else if (type === "REASSIGN") {
        action.to = p;
      } else {
        action.argument = p;
      }
    }
    requireCondition(action.op, `Action row is missing required '#op' operation id: ${line}`);
    return action;
  }

  /**
   * Apply delta updates on top of a keyframe state to produce updated keyframe state
   */
  static applyDelta(stateMsg, deltaMsg) {
    requireCondition(stateMsg && stateMsg.body && Array.isArray(stateMsg.body.entities), "Invalid base state");
    requireCondition(deltaMsg && deltaMsg.body, "Invalid delta message");
    requireCondition(deltaMsg.body.base === stateMsg.body.rev, `Delta base mismatch: expected ${stateMsg.body.rev}, got ${deltaMsg.body.base}`);

    const entitiesMap = new Map();
    for (const ent of stateMsg.body.entities) {
      entitiesMap.set(ent.id, { ...ent });
    }

    // Process removals
    for (const remId of deltaMsg.body.remove || []) {
      entitiesMap.delete(remId);
    }

    // Process partial updates
    for (const upd of deltaMsg.body.updates || []) {
      if (entitiesMap.has(upd.id)) {
        const existing = entitiesMap.get(upd.id);
        if (upd.pos) existing.pos = upd.pos;
        if (upd.hp !== undefined) existing.hp = upd.hp;
        if (upd.ammo !== undefined) existing.ammo = upd.ammo;
        if (upd.suppression !== undefined) existing.suppression = upd.suppression;
        if (upd.role) existing.role = upd.role;
        if (upd.count !== undefined) existing.count = upd.count;
        // Keep observation time. Full upsert is required to refresh it.
      }
    }

    // Process upserts
    for (const up of deltaMsg.body.upsert || []) {
      entitiesMap.set(up.id, { ...up });
    }

    return {
      ...stateMsg,
      id: deltaMsg.id,
      seq: deltaMsg.seq,
      sent_ms: deltaMsg.sent_ms,
      body: {
        world: deltaMsg.body.world || stateMsg.body.world,
        rev: deltaMsg.body.rev,
        entities: Array.from(entitiesMap.values())
      }
    };
  }
}

module.exports = {
  AMLCodec,
  AMLError,
  ACTION_MAP,
  REV_ACTION_MAP,
  KIND_MAP,
  REV_KIND_MAP,
  ROLE_MAP,
  REV_ROLE_MAP,
  CLUSTER_SCHEMA,
  TASK_STATUS_MAP,
  AGENT_STATE_MAP
};
