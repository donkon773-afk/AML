/**
 * Validate docs/aml_v2.gbnf with an independent GBNF implementation.
 *
 * Why this exists: aml_grammar.lark is tested against the codec, but the file
 * actually shipped to llama.cpp is the .gbnf. A hand translation between the
 * two can drift, and engine-specific loading behavior must be checked separately.
 *
 * Two things are checked, and the difference matters:
 *
 *   PREFIX  -- the parser accepts the text so far. Constrained decoding works
 *              on prefixes, so this is what the engine enforces token by token.
 *   ACCEPT  -- the parser can also *terminate* here. A truncated frame is a
 *              perfectly valid prefix, so prefix-validity alone proves nothing
 *              about completeness. The 'end' rule checks grammar completeness, not external token
 *              limits, transport interruption, or actual model sampling.
 *
 * Known divergence: the shipped grammar uses {m,n} repetition ({64} for
 * sha256, {0,128} for entity lists, {1,3} for actions).
 * llama.cpp supports these (src/llama-grammar.cpp, the '{' branch); this npm
 * parser does not, so the test expands them to equivalent bounded expressions.
 * This checks language membership, not native llama.cpp loading or sampling.
 *
 * Run: node tests/test_gbnf_file.mjs
 */
import GBNF from "gbnf";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..");
const RAW = readFileSync(join(ROOT, "docs", "aml_v2.gbnf"), "utf8");
// The npm parser lacks {m,n}. Expand to an equivalent bounded expression,
// never to unbounded * or +. Test the actual cardinality boundaries below.
function bounded(atom, n) {
  let tail = "";
  for (let i=0; i<n; i++) tail = `(${atom}${tail ? " " + tail : ""})?`;
  return tail;
}
const GRAMMAR = RAW
  .replace("[a-f0-9]{64}", Array(64).fill("[a-f0-9]").join(" "))
  .replace('(\"\\n\" entity-row){0,128}', bounded('(\"\\n\" entity-row)', 128))
  .replace('(\"\\n\" delta-line){0,128}', bounded('(\"\\n\" delta-line)', 128))
  .replace('(\"\\n\" action-line){1,3}', '(\"\\n\" action-line) (\"\\n\" action-line)? (\"\\n\" action-line)?')
  .replace('handoff-line{0,50}', bounded('handoff-line', 50))
  .replace('("," "#" ident){0,15}', bounded('("," "#" ident)', 15));
if (/\{\d+(?:,\d+)?\}/.test(GRAMMAR.replace(/^#.*$/gm, ""))) {
  throw new Error("Untranslated bounded repetition");
}


const ARROW = "\u2192";
const ROUTE = `s1|obs${ARROW}gw|1|1000000|30000`;

/** Does the grammar accept this text AND allow generation to stop here? */
function complete(text) {
  const state = GBNF(GRAMMAR, text);       // throws if the prefix is invalid
  return [...state].some((rule) => rule.type === "end");
}

/** Is this a valid prefix, regardless of whether it may terminate? */
function prefix(text) {
  try { GBNF(GRAMMAR, text); return true; } catch { return false; }
}

const COMPLETE = {
  HANDOFF: `!AML:2|HANDOFF|${ROUTE}|tsk:task1|st:COMPLETE\nSUM:"done"\nART:report.txt:${"a".repeat(64)}`,
  STATE: `!AML:2|STATE|${ROUTE}|w:Altis|r:1\n#A11:grp:INF:20:@1250,3420:%H100A90S0:t999500`,
  "STATE with NAM/2 envelope":
    `!AML:2|STATE|${ROUTE}|s:17|tr:trace.42|w:Altis|r:1\n#A11:grp:INF:20:@1250,3420:%H100A90S0:t999500`,
  DELTA: `!AML:2|DELTA|s1|obs${ARROW}gw|2|1002000|30000|w:Altis|b:1|r:2\n~#A11:@1265,3435:%H92A-S15\n-#E01`,
  PROP: `!AML:2|PROP|s1|loc${ARROW}gw|3|1003000|30000|w:Altis|r:2|by:obs|rs:CONTACT[#E02]\n$ATK:#A11:@1320,3480:#op10`,
  QUERY: `!AML:2|QUERY|s1|cld${ARROW}loc|4|1004000|30000|?WHY:#A11`,
  RES: `!AML:2|RES|s1|loc${ARROW}cld|5|1004200|30000|rel:A11|*ACC:|rs:LOW_AMMO[#E02]`,
  ERR: `!AML:2|ERR|s1|gw${ARROW}obs|6|1006000|30000|err:RESYNC|rel:m2`,
  "STATE cluster profile (T37)":
    `!AML:2|STATE|${ROUTE}|s:2|w:board|r:4\n#T13:task:INP:antigravity:codex:P1:t999500:dep:#T17,#T18\n#codex:agent:ACT:L72:W45:t999500:tsk:#T34\n#gptoss:agent:OFF:L-:W-:t999500`,
  "PROP cluster REASSIGN (T37)":
    `!AML:2|PROP|aml-nam|agent_sync${ARROW}claude|5|1003000|30000|s:2|w:board|r:4|by:agent_sync|rs:LIMIT[#codex]\n$RSG:#T34:claude:#op1`,
};

// Unparseable at any length: the model can never go down these paths.
const IMPOSSIBLE = {
  "unknown action": `!AML:2|PROP|s1|loc${ARROW}gw|3|1003000|30000|w:Altis|r:2|by:obs|rs:CONTACT[#E02]\n$NUKE:#A11:#op1`,
  "full-word role": `!AML:2|STATE|${ROUTE}|w:Altis|r:1\n#A11:grp:INFANTRY:20:@1250,3420:%H100A90S0:t1`,
  "wrong protocol version": `!AML:1|STATE|${ROUTE}|w:Altis|r:1`,
  "plain prose": "HELLO WORLD",
  "colon inside id": `!AML:2|STATE|${ROUTE}|w:Altis|r:1\n#A1:1:grp:INF:20:@1250,3420:%H100A90S0:t1`,
};

// Valid so far, but the grammar must refuse to let generation stop here.
const UNFINISHED = {
  "entity without t<ms>": `!AML:2|STATE|${ROUTE}|w:Altis|r:1\n#A11:grp:INF:20:@1250,3420:%H100A90S0`,
  "action without #op": `!AML:2|PROP|s1|loc${ARROW}gw|3|1003000|30000|w:Altis|r:2|by:obs|rs:CONTACT[#E02]\n$HLD:#A11`,
};

let failures = 0;
const report = (ok, label) => {
  console.log(`  ${ok ? "ok  " : "FAIL"}  ${label}`);
  if (!ok) failures++;
};

console.log("complete frames must parse and be allowed to terminate:");
for (const [name, text] of Object.entries(COMPLETE)) {
  let ok = false;
  try { ok = complete(text); } catch { ok = false; }
  report(ok, name);
}

console.log("malformed frames must be unreachable:");
for (const [name, text] of Object.entries(IMPOSSIBLE)) {
  report(!prefix(text), name);
}

console.log("truncated frames must parse but NOT be allowed to terminate:");
for (const [name, text] of Object.entries(UNFINISHED)) {
  let ok = false;
  try { ok = prefix(text) && !complete(text); } catch { ok = false; }
  report(ok, name);
}

console.log("exact repetition boundaries:");
const stateHead = `!AML:2|STATE|${ROUTE}|w:Altis|r:1`;
const row = "\n#A11:grp:INF:20:@1250,3420:%H100A90S0:t999500";
report(complete(stateHead + row.repeat(128)), "128 rows accepted");
report(!prefix(stateHead + row.repeat(129)), "129 rows rejected");
const propHead = `!AML:2|PROP|${ROUTE}|w:Altis|r:1|by:obs|rs:UNKNOWN`;
report(complete(propHead + "\n$HLD:#A11:#op1".repeat(3)), "3 actions accepted");
report(!prefix(propHead + "\n$HLD:#A11:#op1".repeat(4)), "4 actions rejected");
for (const n of [63,65]) {
  let accepted=false;
  try { accepted=complete(`!AML:2|HANDOFF|${ROUTE}|tsk:t|st:COMPLETE\nART:a.txt:${"a".repeat(n)}`); } catch {}
  report(!accepted, `${n}-character hash rejected`);
}

console.log(failures === 0
  ? "\nGBNF language checks passed after equivalent bounded expansion; native engine untested."
  : `\n${failures} check(s) failed.`);
process.exit(failures === 0 ? 0 : 1);
