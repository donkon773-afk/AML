"""Compare the bundled full/full and delta/delta fixtures using real tokenizers.
JSON full/delta is reported separately as a transport-policy comparison.
This measures representation size, not model quality, latency or end-to-end cost.
Message streams use newline separators in all formats. AML adds documented
optional defaults; all supplied field values are preserved within float 1e-12.
"""
import copy
import json
import subprocess
import sys
from pathlib import Path

# Keep benchmark evidence reproducible on Windows hosts whose redirected
# Python streams inherit a legacy code page such as cp1252.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent))
from aml_codec import AMLCodec

TOON_HELPER = Path(__file__).resolve().parent / "toon_encode.js"


def to_toon(obj) -> str:
    result = subprocess.run(["node", str(TOON_HELPER)], input=json.dumps(obj),
                            capture_output=True, text=True, encoding="utf-8", check=True)
    return result.stdout


def load_tokenizers():
    toks = {}
    try:
        from transformers import AutoTokenizer
        toks["qwen3-8b"] = AutoTokenizer.from_pretrained("Qwen/Qwen3-8B").encode
    except Exception as exc:
        print(f"(qwen3 tokenizer unavailable: {exc})", file=sys.stderr)
    try:
        import tiktoken
        enc = tiktoken.get_encoding("cl100k_base")
        toks["gpt-cl100k"] = enc.encode
        enc2 = tiktoken.get_encoding("o200k_base")
        toks["gpt-o200k"] = enc2.encode
    except Exception as exc:
        print(f"(tiktoken unavailable: {exc})", file=sys.stderr)
    return toks


def row(name, text, tokenizers):
    counts = {tname: len(fn(text)) for tname, fn in tokenizers.items()}
    return {"format": name, "bytes": len(text.encode("utf-8")), "chars": len(text), **counts}


def report(title, rows, baseline_name, tokenizers):
    print(f"\n=== {title} ===")
    headers = ["format", "bytes", "chars"] + list(tokenizers.keys())
    widths = {h: max(len(h), max(len(str(r[h])) for r in rows)) for h in headers}
    print("  ".join(h.ljust(widths[h]) for h in headers))
    print("  ".join("-" * widths[h] for h in headers))
    for r in rows:
        print("  ".join(str(r[h]).ljust(widths[h]) for h in headers))
    baseline = next(r for r in rows if r["format"] == baseline_name)
    for r in rows:
        if r["format"] == baseline_name:
            continue
        parts = [f"bytes {100*(1-r['bytes']/baseline['bytes']):+.1f}%"]
        for tname in tokenizers:
            parts.append(f"{tname} {100*(1-r[tname]/baseline[tname]):+.1f}%")
        print(f"  {r['format']} vs {baseline_name}: " + ", ".join(parts))


def fixture_nam_stream():
    """Mirrors benchmark_nam.py exactly (10 groups, 10 frames, 2 keyframes)."""
    now = 1000000
    entities = [{"id": f"G{i}", "kind": "group", "pos": [1000 + i * 20, 2000, 0],
                 "observed_ms": now, "role": "INFANTRY", "count": 20, "ammo": 0.9} for i in range(10)]
    full_msgs, delta_msgs = [], []
    for rev in range(1, 11):
        if rev > 1:
            entities[0]["pos"][0] += 5
            entities[0]["observed_ms"] = now + rev * 1000
        full = {"v": "NAM/1", "id": f"m{rev}", "session": "bench", "sender": "observer",
                "recipient": "gateway", "seq": rev, "sent_ms": now + rev * 1000, "ttl_ms": 30000,
                "kind": "state", "body": {"world": "Altis.bench", "rev": rev,
                                           "entities": copy.deepcopy(entities)}}
        delta = copy.deepcopy(full)
        if rev not in (1, 6):
            delta["kind"] = "delta"
            delta["body"] = {"world": "Altis.bench", "base": rev - 1, "rev": rev,
                              "upsert": [copy.deepcopy(entities[0])], "remove": []}
        full_msgs.append(full)
        delta_msgs.append(delta)
    return full_msgs, delta_msgs


def fixture_company_snapshot():
    """Mirrors benchmark_tokenomics.js's createCompanyFixture(), for AML with
    the wire-required 'observed_ms' and NAM-schema role words added (the
    original JS fixture predates both fixes and would fail the codec now)."""
    entities = []
    for i in range(1, 9):
        role = "INFANTRY" if i <= 5 else ("RECON" if i <= 7 else "MECHANIZED")
        entities.append({
            "id": f"A1{i}", "kind": "group", "role": role,
            "count": 20 if i <= 5 else (8 if i <= 7 else 12),
            "pos": [1000 + i * 50, 3000 + (i % 3) * 40, 0],
            "hp": 1.0 - (i % 4) * 0.05, "ammo": 0.9 - (i % 3) * 0.1,
            "suppression": 0.15 if i % 2 == 0 else 0.0,
            "observed_ms": 1725830400000,
            "callsign": f"\u041e\u0442\u0434\u0435\u043b\u0435\u043d\u0438\u0435 1-{i}"
        })
    entities.append({"id": "A21", "kind": "vehicle", "role": "ARMORED", "count": 3,
                      "pos": [1150, 2900, 0], "hp": 1.0, "ammo": 1.0, "suppression": 0.0,
                      "observed_ms": 1725830400000, "callsign": "\u0422\u0430\u043d\u043a 2-1"})
    entities.append({"id": "E01", "kind": "contact", "role": "ARMORED", "count": 1,
                      "pos": [1650, 3500, 10], "hp": 1.0, "ammo": None, "suppression": None,
                      "observed_ms": 1725830400000, "source": "A16"})
    entities.append({"id": "E02", "kind": "contact", "role": "INFANTRY", "count": 6,
                      "pos": [1420, 3350, 0], "hp": 0.9, "ammo": 0.8, "suppression": 0.0,
                      "observed_ms": 1725830400000, "source": "A11"})
    return {"v": "NAM/1", "id": "m100", "session": "combat_session_01", "sender": "observer_daemon",
            "recipient": "gateway_host", "seq": 100, "sent_ms": 1725830400000, "ttl_ms": 30000,
            "kind": "state", "body": {"world": "Altis", "rev": 1, "entities": entities}}


def main():
    tokenizers = load_tokenizers()
    if not tokenizers:
        print("No tokenizers available -- aborting rather than falling back to a heuristic.", file=sys.stderr)
        sys.exit(1)

    # ---------- Fixture A: NAM 10-frame stream ----------
    full_msgs, delta_msgs = fixture_nam_stream()
    json_full = "\n".join(json.dumps(m, separators=(",", ":"), ensure_ascii=False) for m in full_msgs)
    json_delta = "\n".join(json.dumps(m, separators=(",", ":"), ensure_ascii=False) for m in delta_msgs)
    aml_full = "\n".join(AMLCodec.encode(m) for m in full_msgs)
    aml_delta = "\n".join(AMLCodec.encode(m) for m in delta_msgs)
    toon_full = "\n".join(to_toon(m) for m in full_msgs)
    toon_delta = "\n".join(to_toon(m) for m in delta_msgs)

    report(
        "Fixture A -- benchmark_nam.py stream, FULL-STATE every frame (no delta)",
        [row("JSON (full, minified)", json_full, tokenizers),
         row("TOON (full)", toon_full, tokenizers),
         row("AML v2.0 (full)", aml_full, tokenizers)],
        "JSON (full, minified)", tokenizers)

    report(
        "Fixture A -- same stream, WITH delta frames (2 keyframes + 8 deltas)",
        [row("JSON (delta, minified)", json_delta, tokenizers),
         row("TOON (delta-aware)", toon_delta, tokenizers),
         row("AML v2.0 (delta-aware)", aml_delta, tokenizers)],
        "JSON (delta, minified)", tokenizers)

    report("Transport policy only: full vs delta JSON",
           [row("JSON full", json_full, tokenizers), row("JSON delta", json_delta, tokenizers)],
           "JSON full", tokenizers)

    # ---------- Fixture B: single tactical snapshot (README's own example) ----------
    snap = fixture_company_snapshot()
    json_snap = json.dumps(snap, separators=(",", ":"), ensure_ascii=False)
    toon_snap = to_toon(snap)
    aml_snap = AMLCodec.encode(snap)

    report(
        "Fixture B -- single company snapshot (README's -82.6%/-75.6%/4.1x example)",
        [row("JSON (minified)", json_snap, tokenizers),
         row("TOON", toon_snap, tokenizers),
         row("AML v2.0", aml_snap, tokenizers)],
        "JSON (minified)", tokenizers)

    print("\n--- for reference, the raw texts (Fixture B) ---")
    print("JSON:", json_snap[:200], "...")
    print("\nTOON:\n" + toon_snap)
    print("\nAML:\n" + aml_snap)


if __name__ == "__main__":
    main()
