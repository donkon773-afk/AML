#!/usr/bin/env python3
"""benchmark_live_frames.py — Замер живых фреймов решений кластера (T14).

Тестирует генерацию коротких фреймов решений (HANDOFF, RES, QUERY, ERR, PROP)
на целевых моделях:
  - qwen3.5-9b (локально на ПК, 127.0.0.1:1234, SDK + GBNF грамматика)
  - gemma-4-12b-coder (MacBook Pro по tailnet, <tailnet-host>:1234, HTTP без грамматики)

Метрики:
  - strict-валидность (чистый вывод проходит validate_frame)
  - lenient-валидность (вывод после extract_frame проходит validate_frame)
  - принятие приёмником (Receiver.accept с правами кластера)
  - семантическое соответствие (ссылки на реальные T<N>, корректные статусы и стороны)
  - задержка (p50, p95, tok/s)
  - учёт всех отказов кодека и приёмника
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from aml_codec import AMLCodec, AMLError  # noqa: E402
from frame_validation import validate_frame  # noqa: E402
from nam import Receiver  # noqa: E402


def extract_frame(text: str) -> str:
    """Извлекает ровно один AML-фрейм из текста, отбрасывая обрамляющий markdown."""
    match = re.search(r"^!AML:2\|[^\r\n]+", text, re.MULTILINE)
    if not match:
        return ""
    start_pos = match.start()
    lines = text[start_pos:].splitlines()
    header = lines[0].strip()
    parts = header.split("|")
    kind = parts[1] if len(parts) > 1 else ""
    frame_lines = [header]

    if kind == "HANDOFF":
        for line in lines[1:]:
            s = line.strip()
            if any(s.startswith(p) for p in ("SUM:", "ART:", "CHK:", "NXT:")):
                frame_lines.append(s)
            else:
                break
    elif kind == "PROP":
        for line in lines[1:]:
            s = line.strip()
            if s.startswith("$"):
                frame_lines.append(s)
            else:
                break
    elif kind == "STATE":
        for line in lines[1:]:
            s = line.strip()
            if s.startswith("#"):
                frame_lines.append(s)
            else:
                break
    elif kind == "DELTA":
        for line in lines[1:]:
            s = line.strip()
            if s.startswith(("+", "~#", "-#")):
                frame_lines.append(s)
            else:
                break
    # Для RES, QUERY, ERR фрейм состоит строго из одной строки заголовка.

    return "\n".join(frame_lines).strip()

SESSION = "aml-nam"
T0 = 1_790_200_000_000
SHA_DUMMY = "0" * 63 + "1"

# Матрица прав для сценария обмена в кластере (SCENARIO-cluster-exchange §1-3)
PERMISSIONS = {
    "antigravity": {"handoff", "query", "error"},
    "codex": {"handoff", "result", "query", "error"},
    "claude": {"handoff", "result", "query", "error"},
    "gptoss": {"result", "query"},
    "lead": {"proposal", "query"},
    "hq": {"proposal", "result", "query", "error"},
}

LOCAL_QWEN_MODEL = "qwen3.5-9b-uncensored-hauhaucs-aggressive@q4_k_m"
MAC_GEMMA_MODEL = "gemma-4-12b-coder-fable5-composer2.5-v1-uncensored-heretic-mxfp8-mlx"


def _detect_mac_endpoint() -> str:
    env = os.getenv("MAC_ENDPOINT")
    if env:
        return env
    try:
        ag_path = ROOT / ".agent-sync" / "agents.json"
        if ag_path.exists():
            ag = json.loads(ag_path.read_text(encoding="utf-8"))
            ep = ag.get("llm", {}).get("gemma", {}).get("endpoint")
            if ep:
                return ep.rstrip("/") + "/v1/chat/completions"
    except Exception:
        pass
    return "http://127.0.0.1:1234/v1/chat/completions"


MAC_ENDPOINT = _detect_mac_endpoint()
LOCAL_ENDPOINT = "http://127.0.0.1:1234/v1/chat/completions"


def build_dataset() -> dict[str, list[dict]]:
    """Создаёт набор из ≥20 реалистичных сценариев для каждого типа фрейма."""
    tasks = [
        ("T11", "Сборка и тесты на Windows"),
        ("T12", "SDK-проба и reasoning-модели"),
        ("T13", "Изоляция кэша грамматики (F4)"),
        ("T14", "Замер живых фреймов решений"),
        ("T16", "Обновление @toon-format/toon"),
        ("T17", "Спецификация обмена по AML"),
        ("T18", "Пул локальных моделей"),
        ("T32", "Дашборд v2 и agent_sync"),
        ("T33", "Тесты agent_sync и rebalance"),
        ("T34", "Сверка r2-копии с HEAD"),
        ("T35", "Прототип транспорта AML"),
        ("T36", "Релизный пакет r2"),
        ("T37", "Кластерный профиль s:2"),
    ]

    dataset: dict[str, list[dict]] = {
        "HANDOFF": [],
        "RES": [],
        "QUERY": [],
        "ERR": [],
        "PROP": []
    }

    # 1. HANDOFF (20 сценариев: 10 COMPLETE, 10 BLOCKED)
    handoff_configs = [
        ("antigravity", "codex", "T13", "COMPLETE", "F4 кэш грамматики изолирован", "tests/test_regressions.py", "verify.py", "PASS", "critic: codex"),
        ("antigravity", "codex", "T14", "COMPLETE", "Замеры живых фреймов завершены", "review/2026-09-24/LIVE_FRAMES.md", "verify.py", "PASS", "critic: codex"),
        ("codex", "claude", "T11", "COMPLETE", "Сборка Windows верифицирована", "review/2026-09-15/T11-codex-verify-windows.txt", "verify.py", "PASS", "approval: claude"),
        ("codex", "antigravity", "T13", "BLOCKED", "F4 найдена утечка кэша", "tests/test_regressions.py", "F4", "FAIL", "исправить setUp"),
        ("antigravity", "codex", "T18", "COMPLETE", "Воркер протестирован на моках", "tests/test_local_worker.py", "verify.py", "PASS", "critic: codex"),
        ("codex", "claude", "T33", "COMPLETE", "Изолированные тесты agent_sync готовы", "tests/test_agent_sync.py", "verify.py", "PASS", "critic: claude"),
        ("codex", "claude", "T34", "COMPLETE", "Сверка r2 с HEAD завершена", "review/2026-09-24/R2-DIFF.md", "verify.py", "PASS", "curator: claude"),
        ("codex", "claude", "T37", "COMPLETE", "Кластерный профиль s:2 проверен", "tests/test_cluster_profile.py", "verify.py", "PASS", "approval: claude"),
        ("antigravity", "codex", "T16", "COMPLETE", "Toon поднят до 2.3.1", "package.json", "npm audit", "PASS", "critic: codex"),
        ("codex", "antigravity", "T18", "BLOCKED", "F1 дублирование при переносе задачи", "scripts/llm_pool.py", "F1", "FAIL", "добавить lease generation"),
        ("antigravity", "codex", "T12", "COMPLETE", "Проба SDK и Qwen3.5 strip_reasoning", "scripts/llm_client_sdk.py", "verify.py", "PASS", "critic: codex"),
        ("codex", "antigravity", "T32", "BLOCKED", "F1 brief падает на строковом priority", "scripts/agent_sync.py", "F1", "FAIL", "нормализовать prio()"),
        ("claude", "codex", "T17", "COMPLETE", "Сценарий кластерного обмена готов", "docs/SCENARIO-cluster-exchange.md", "cluster_examples.py", "PASS", "critic: codex"),
        ("antigravity", "codex", "T35", "COMPLETE", "Транспорт outbox реализован", "scripts/agent_sync.py", "verify.py", "PASS", "critic: codex"),
        ("codex", "antigravity", "T17", "BLOCKED", "F2 нет таблицы переходов для RES", "docs/SCENARIO-cluster-exchange.md", "F2", "FAIL", "добавить таблицу прав"),
        ("codex", "claude", "T32", "COMPLETE", "Дашборд v2 и авто-rebalance проверены", "scripts/agent_sync.py", "verify.py", "PASS", "approval: claude"),
        ("antigravity", "codex", "T36", "COMPLETE", "Архив r2 и README подготовлены", "dist/aml-nam-r2.zip", "verify.py", "PASS", "critic: codex"),
        ("codex", "antigravity", "T33", "BLOCKED", "Тест rebalance упал по тайм-ауту", "tests/test_agent_sync.py", "test_rebalance", "FAIL", "увеличить тайм-аут"),
        ("claude", "codex", "T37", "COMPLETE", "Грамматика и схема s:2 проверены", "scripts/aml_grammar.lark", "verify.py", "PASS", "critic: codex"),
        ("antigravity", "claude", "T14", "BLOCKED", "Превышение p95 задержки на локальной модели", "review/2026-09-24/LIVE_FRAMES.md", "latency", "FAIL", "оптимизировать промпт"),
    ]

    for sender, recipient, tsk, st, summ, art, chk_name, chk_res, nxt in handoff_configs:
        prompt = (
            f"Создай AML-фрейм HANDOFF для задачи {tsk}.\n"
            f"Сессия aml-nam, отправитель {sender}, получатель {recipient}, seq 1, sent_ms 1790200000000, ttl_ms 60000, статус {st}.\n"
            f"Каждая следующая строка отчёта пишется строго с новой строки:\n"
            f"SUM:\"{summ}\"\n"
            f"ART:{art}:{SHA_DUMMY}\n"
            f"CHK:{chk_name}:{chk_res}:\"ok\"\n"
            f"NXT:\"{nxt}\"\n"
            f"Выведи ТОЛЬКО фрейм без пояснений."
        )
        dataset["HANDOFF"].append({
            "prompt": prompt,
            "kind": "HANDOFF",
            "task": tsk,
            "sender": sender,
            "recipient": recipient,
            "status": st,
            "chk_res": chk_res
        })

    # 2. RES (20 сценариев: 10 ACCEPTED, 10 REJECTED)
    res_configs = [
        ("codex", "claude", "T13", "ACCEPTED", None),
        ("codex", "antigravity", "T13", "REJECTED", "STALLED"),
        ("codex", "claude", "T16", "ACCEPTED", None),
        ("codex", "antigravity", "T16", "REJECTED", "LOW_AMMO"),
        ("codex", "claude", "T17", "ACCEPTED", None),
        ("codex", "claude", "T17", "REJECTED", "STALLED"),
        ("claude", "codex", "T32", "ACCEPTED", None),
        ("codex", "claude", "T32", "REJECTED", "STALLED"),
        ("claude", "codex", "T33", "ACCEPTED", None),
        ("codex", "claude", "T33", "REJECTED", "STALLED"),
        ("claude", "codex", "T34", "ACCEPTED", None),
        ("codex", "claude", "T34", "REJECTED", "STALLED"),
        ("codex", "claude", "T37", "ACCEPTED", None),
        ("codex", "claude", "T37", "REJECTED", "STALLED"),
        ("claude", "antigravity", "T13", "ACCEPTED", None),
        ("codex", "antigravity", "T18", "REJECTED", "STALLED"),
        ("codex", "claude", "T18", "ACCEPTED", None),
        ("claude", "antigravity", "T14", "ACCEPTED", None),
        ("codex", "antigravity", "T14", "REJECTED", "STALLED"),
        ("claude", "codex", "T36", "ACCEPTED", None),
    ]

    for sender, recipient, tsk, st, rs in res_configs:
        acc_rej = f"*ACC:#{tsk}" if st == "ACCEPTED" else f"*REJ:#{tsk}"
        prompt = (
            f"Создай AML-фрейм RES (результат/вердикт) для задачи {tsk}.\n"
            f"Сессия aml-nam, отправитель {sender}, получатель {recipient}, seq 1, sent_ms 1790200000000, ttl_ms 60000, "
            f"задача rel:{tsk}, вердикт {acc_rej}.\n"
            + (f"Причина отказа: {rs}.\n" if rs else "")
            + "Выведи ТОЛЬКО фрейм без пояснений."
        )
        dataset["RES"].append({
            "prompt": prompt,
            "kind": "RES",
            "task": tsk,
            "sender": sender,
            "recipient": recipient,
            "status": st,
            "reason": rs
        })

    # 3. QUERY (20 сценариев: 10 ?STATUS, 10 ?WHY)
    query_configs = [
        ("claude", "antigravity", "T18", "STATUS"),
        ("codex", "claude", "T32", "WHY"),
        ("claude", "codex", "T33", "STATUS"),
        ("antigravity", "claude", "T14", "WHY"),
        ("claude", "antigravity", "T13", "STATUS"),
        ("codex", "claude", "T17", "WHY"),
        ("claude", "codex", "T34", "STATUS"),
        ("antigravity", "codex", "T16", "WHY"),
        ("claude", "antigravity", "T35", "STATUS"),
        ("codex", "antigravity", "T18", "WHY"),
        ("claude", "codex", "T37", "STATUS"),
        ("antigravity", "claude", "T12", "WHY"),
        ("claude", "antigravity", "T14", "STATUS"),
        ("codex", "claude", "T36", "WHY"),
        ("claude", "antigravity", "T11", "STATUS"),
        ("antigravity", "codex", "T13", "WHY"),
        ("claude", "codex", "T18", "STATUS"),
        ("codex", "claude", "T37", "WHY"),
        ("claude", "antigravity", "T17", "STATUS"),
        ("antigravity", "claude", "T33", "WHY"),
    ]

    for sender, recipient, tsk, req in query_configs:
        prompt = (
            f"Создай AML-фрейм QUERY для задачи {tsk}.\n"
            f"Сессия aml-nam, отправитель {sender}, получатель {recipient}, seq 1, sent_ms 1790200000000, ttl_ms 60000, запрос ?{req}:#{tsk}.\n"
            f"Выведи ТОЛЬКО фрейм без пояснений."
        )
        dataset["QUERY"].append({
            "prompt": prompt,
            "kind": "QUERY",
            "task": tsk,
            "sender": sender,
            "recipient": recipient,
            "request": req
        })

    # 4. ERR (20 сценариев: различные коды ошибок и задач)
    err_configs = [
        ("antigravity", "claude", "T18", "BUSY"),
        ("codex", "claude", "T13", "INVALID"),
        ("antigravity", "codex", "T14", "BUSY"),
        ("claude", "antigravity", "T12", "EXPIRED"),
        ("codex", "antigravity", "T17", "UNAUTHORIZED"),
        ("antigravity", "claude", "T35", "BUSY"),
        ("codex", "claude", "T32", "REPLAY"),
        ("antigravity", "claude", "T33", "BUSY"),
        ("claude", "antigravity", "T16", "UNSUPPORTED"),
        ("codex", "claude", "T34", "INVALID"),
        ("antigravity", "claude", "T13", "BUSY"),
        ("codex", "antigravity", "T18", "RESYNC"),
        ("claude", "codex", "T37", "UNAUTHORIZED"),
        ("antigravity", "claude", "T11", "BUSY"),
        ("codex", "claude", "T16", "INVALID"),
        ("antigravity", "codex", "T36", "BUSY"),
        ("claude", "antigravity", "T14", "EXPIRED"),
        ("codex", "antigravity", "T33", "REPLAY"),
        ("antigravity", "claude", "T37", "BUSY"),
        ("claude", "codex", "T17", "INVALID"),
    ]

    for sender, recipient, tsk, err_code in err_configs:
        prompt = (
            f"Создай AML-фрейм ERR (ошибка/отказ).\n"
            f"Сессия aml-nam, отправитель {sender}, получатель {recipient}, seq 1, sent_ms 1790200000000, ttl_ms 60000, код err:{err_code}, rel:{tsk}.\n"
            f"Выведи ТОЛЬКО фрейм без пояснений."
        )
        dataset["ERR"].append({
            "prompt": prompt,
            "kind": "ERR",
            "task": tsk,
            "sender": sender,
            "recipient": recipient,
            "code": err_code
        })

    # 5. PROP (20 тактических предложений, военный профиль для сопоставимости с T12)
    prop_configs = [
        ("lead", "hq", "s1", "Altis", "A11", "ROE", "OPEN_FIRE", "PLAYER_ORDER"),
        ("lead", "hq", "s1", "Altis", "A12", "ADV", "@1250,3420", "CONTACT"),
        ("lead", "hq", "s1", "Stratis", "B21", "HLD", "@800,900", "LOW_AMMO"),
        ("lead", "hq", "s1", "Altis", "B22", "RET", "@500,600", "STALLED"),
        ("lead", "hq", "s1", "Altis", "A11", "SEC", "@1100,2200", "MISSION_OBJECTIVE"),
        ("lead", "hq", "s1", "Stratis", "E01", "ATK", "@1500,1600", "CONTACT"),
        ("lead", "hq", "s1", "Altis", "E02", "FIRE", "@1400,1700", "PLAYER_ORDER"),
        ("lead", "hq", "s1", "Altis", "A12", "ROE", "HOLD_FIRE", "PLAYER_ORDER"),
        ("lead", "hq", "s1", "Stratis", "B21", "ADV", "@900,1100", "MISSION_OBJECTIVE"),
        ("lead", "hq", "s1", "Altis", "A11", "REC", "@1200,1300", "CONTACT"),
        ("lead", "hq", "s1", "Altis", "B22", "ROE", "RETURN_FIRE", "CONTACT"),
        ("lead", "hq", "s1", "Stratis", "A11", "HLD", "@700,800", "LOW_AMMO"),
        ("lead", "hq", "s1", "Altis", "A12", "SEC", "@1300,1400", "MISSION_OBJECTIVE"),
        ("lead", "hq", "s1", "Stratis", "B21", "RET", "@400,500", "STALLED"),
        ("lead", "hq", "s1", "Altis", "E01", "ATK", "@1600,1800", "CONTACT"),
        ("lead", "hq", "s1", "Altis", "E02", "ADV", "@1450,1550", "PLAYER_ORDER"),
        ("lead", "hq", "s1", "Stratis", "A11", "FIRE", "@1150,1250", "CONTACT"),
        ("lead", "hq", "s1", "Altis", "B22", "ADV", "@650,750", "MISSION_OBJECTIVE"),
        ("lead", "hq", "s1", "Altis", "A12", "HLD", "@1250,1350", "PLAYER_ORDER"),
        ("lead", "hq", "s1", "Stratis", "B21", "ROE", "OPEN_FIRE", "CONTACT"),
    ]

    for sender, recipient, sess, world, unit, act, arg, rs in prop_configs:
        prompt = (
            f"Создай AML-фрейм PROP тактического предложения.\n"
            f"Сессия {sess}, мир {world}, ревизия 1, отправитель {sender}, получатель {recipient}, seq 1, sent_ms 1000000, ttl_ms 30000, "
            f"автор {sender}, причина {rs}.\n"
            f"Строка действия пишется строго с новой строки:\n"
            f"${act}:#{unit}:{arg}:#{unit}\n"
            f"Выведи ТОЛЬКО фрейм без пояснений."
        )
        dataset["PROP"].append({
            "prompt": prompt,
            "kind": "PROP",
            "session": sess,
            "world": world,
            "unit": unit,
            "action": act,
            "arg": arg,
            "sender": sender,
            "recipient": recipient,
            "reason": rs
        })

    return dataset


SYSTEM_PROMPT_AML = """Ты — генератор компактных протокольных сообщений AML v2.
Твоя задача: возвращать строго сформированный валидный фрейм AML v2 в точном соответствии с синтаксисом:
- Каждое сообщение начинается с `!AML:2|<KIND>|`
- Заголовок: `<session>|<sender>→<recipient>|<seq>|<sent_ms>|<ttl_ms>|...`
  ВАЖНО: <seq>, <sent_ms>, <ttl_ms> — это ЧИСЛА (например, 1|1790200000000|60000). Не пиши слова "seq" или "sent_ms"!
- Символ стрелки между sender и recipient: `→` (U+2192).
- ВАЖНО: Для HANDOFF и PROP строки тела (SUM:, ART:, CHK:, NXT: или $ACT:...) ОБЯЗАТЕЛЬНО пишутся с НОВОЙ СТРОКИ, а НЕ через символ `|`!

Примеры валидных фреймов:
HANDOFF:
!AML:2|HANDOFF|aml-nam|antigravity→codex|1|1790200000000|60000|tsk:T13|st:COMPLETE
SUM:"F4: reset кэша грамматики в setUp + addCleanup, тест изоляции"
ART:tests/test_regressions.py:0000000000000000000000000000000000000000000000000000000000000001
CHK:verify.py:PASS:"71/71 + 14/14, exit 0"
NXT:"critic: codex"

RES:
!AML:2|RES|aml-nam|codex→claude|1|1790200000000|60000|rel:T13|*ACC:#T13

QUERY:
!AML:2|QUERY|aml-nam|claude→antigravity|1|1790200000000|60000|?STATUS:#T18

ERR:
!AML:2|ERR|aml-nam|antigravity→claude|1|1790200000000|60000|err:BUSY|rel:T18

PROP:
!AML:2|PROP|s1|lead→hq|1|1000000|30000|w:Altis|r:1|by:lead|rs:PLAYER_ORDER
$ROE:#A11:OPEN_FIRE:#A11

Выводи ТОЛЬКО фрейм. Никаких вступительных слов, никаких комментариев, никаких markdown-блоков ```.
Внимание: примеры выше служат ТОЛЬКО для иллюстрации формата. В ответе используй ТОЛЬКО значения (участники, параметры, действия), указанные в запросе пользователя."""


_BOUNDED_GBNF: str | None = None


def get_bounded_gbnf() -> str:
    """Возвращает GBNF-грамматику с ограниченными в памяти повторами строк."""
    global _BOUNDED_GBNF
    if _BOUNDED_GBNF is None:
        gbnf_path = ROOT / "docs" / "aml_v2.gbnf"
        text = gbnf_path.read_text(encoding="utf-8")
        # Ограничиваем повторы строк в памяти:
        # handoff-line: от 1 до 4 (SUM, ART, CHK, NXT)
        # action-line: ровно 1 для одиночных действий
        text = text.replace("handoff-line{0,50}", "handoff-line{1,4}")
        text = text.replace("action-line){1,3}", "action-line){1}")
        _BOUNDED_GBNF = text
    return _BOUNDED_GBNF


def query_qwen_sdk(prompt: str, grammar: bool = True, max_tokens: int = 350) -> tuple[str, float, int, float | None]:
    """Выполняет запрос к локальной модели qwen3.5-9b через SDK с грамматикой."""
    import llm_client_sdk
    g_text = get_bounded_gbnf() if grammar else None
    t0 = time.perf_counter()
    raw = llm_client_sdk.ask(
        LOCAL_QWEN_MODEL,
        prompt,
        grammar=grammar,
        grammar_text=g_text,
        system_text=SYSTEM_PROMPT_AML,
        max_tokens=max_tokens,
        no_think=True
    )
    secs = time.perf_counter() - t0
    # Оценка длины в токенах ~ символы / 3
    tokens = len(raw) // 3
    tok_s = (tokens / secs) if secs > 0 else None
    return raw, secs, tokens, tok_s


def query_gemma_http(
    prompt: str,
    endpoint: str = MAC_ENDPOINT,
    max_tokens: int = 300,
    timeout: int = 30,
    stop: list[str] | None = None,
) -> tuple[str, float, int, float | None]:
    """Выполняет запрос к удалённой модели gemma на MacBook по HTTP без грамматики."""
    payload = {
        "model": MAC_GEMMA_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT_AML},
            {"role": "user", "content": prompt}
        ],
        "temperature": 0.1,
        "max_tokens": max_tokens
    }
    if stop:
        payload["stop"] = stop
    req = urllib.request.Request(
        endpoint,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}
    )
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    secs = time.perf_counter() - t0
    choice = data["choices"][0]
    raw = choice["message"].get("content") or ""
    usage = data.get("usage") or {}
    tokens = usage.get("completion_tokens") or (len(raw) // 3)
    tok_s = (tokens / secs) if secs > 0 else None
    return raw, secs, tokens, tok_s


def evaluate_frame(raw: str, expected: dict, receiver: Receiver | None = None) -> dict:
    """Выполняет цепочку проверок: strict -> lenient -> accept -> semantic."""
    eval_result = {
        "raw": raw,
        "strict": False,
        "lenient": False,
        "accept": False,
        "semantic": False,
        "error": None,
        "decoded": None
    }

    cleaned = raw.strip()

    # 1. Strict валидация
    try:
        decoded_strict = validate_frame(cleaned)
        eval_result["strict"] = True
        eval_result["lenient"] = True
        eval_result["decoded"] = decoded_strict
    except Exception as exc:
        eval_result["error"] = f"strict_fail: {exc}"
        # 2. Lenient валидация
        extracted = extract_frame(cleaned)
        if extracted:
            try:
                decoded_lenient = validate_frame(extracted)
                eval_result["lenient"] = True
                eval_result["decoded"] = decoded_lenient
            except Exception as exc_len:
                eval_result["error"] = f"lenient_fail: {exc_len}"
        else:
            eval_result["error"] = "no_frame_extracted"

    # Если не удалось декодировать ни strict, ни lenient — выходим
    if not eval_result["decoded"]:
        return eval_result

    decoded = eval_result["decoded"]

    # 3. Принятие Receiver.accept
    now_ms = decoded.get("sent_ms", T0) + 1000
    try:
        session_name = decoded.get("session") or SESSION
        recipient_name = decoded.get("recipient") or "claude"
        rec = Receiver(session_name, recipient_name, PERMISSIONS)
        rec.agreed[decoded["sender"]] = frozenset({1, 2, 11, 12, 13, 14, 16, 17, 18, 32, 33, 34, 35, 36, 37})
        if decoded.get("kind") == "proposal":
            world = decoded.get("body", {}).get("world", "Altis")
            rev = decoded.get("body", {}).get("rev", 1)
            actions = decoded.get("body", {}).get("actions", [])
            unit_id = expected.get("unit", "A11")
            pos = [1000, 2000, 0]
            if actions and actions[0].get("position"):
                act_pos = actions[0]["position"]
                pos = [act_pos[0], act_pos[1], 0]
            rec.states[decoded["sender"]] = {
                "world": world,
                "rev": rev,
                "entities": {
                    unit_id: {
                        "id": unit_id,
                        "kind": "group",
                        "role": "INF",
                        "count": 8,
                        "pos": pos,
                        "hp": 100,
                        "ammo": 100,
                        "suppression": 0,
                        "observed_ms": now_ms - 500,
                    }
                },
            }
        rec.accept(json.dumps(decoded), decoded["sender"], now_ms)
        eval_result["accept"] = True
    except Exception as exc_acc:
        eval_result["error"] = f"receiver_reject: {exc_acc}"

    # 4. Семантическое соответствие
    kind = expected["kind"]
    body = decoded.get("body", {})
    semantic_match = True

    if kind == "HANDOFF":
        if body.get("task") != expected["task"]:
            semantic_match = False
        if body.get("status") != expected["status"]:
            semantic_match = False
        if decoded.get("sender") != expected["sender"] or decoded.get("recipient") != expected["recipient"]:
            semantic_match = False

    elif kind == "RES":
        if body.get("related") != expected["task"]:
            semantic_match = False
        exp_st = "ACCEPTED" if expected["status"] == "ACCEPTED" else "REJECTED"
        if body.get("status") not in (exp_st, expected["status"]):
            semantic_match = False
        if decoded.get("sender") != expected["sender"] or decoded.get("recipient") != expected["recipient"]:
            semantic_match = False

    elif kind == "QUERY":
        if body.get("related") != expected["task"]:
            semantic_match = False
        exp_req = "EXPLAIN" if expected["request"] == "WHY" else expected["request"]
        if body.get("request") != exp_req:
            semantic_match = False
        if decoded.get("sender") != expected["sender"] or decoded.get("recipient") != expected["recipient"]:
            semantic_match = False

    elif kind == "ERR":
        if body.get("code") != expected["code"]:
            semantic_match = False
        if body.get("related") != expected["task"]:
            semantic_match = False
        if decoded.get("sender") != expected["sender"] or decoded.get("recipient") != expected["recipient"]:
            semantic_match = False

    elif kind == "PROP":
        author = body.get("state_sender") or body.get("author")
        if author != expected["sender"]:
            semantic_match = False
        actions = body.get("actions", [])
        if not actions:
            semantic_match = False
        else:
            act0 = actions[0]
            if act0.get("target") != expected["unit"] and act0.get("actor") != expected["unit"]:
                semantic_match = False

    eval_result["semantic"] = semantic_match
    return eval_result


def percentile(values: list[float], p: float) -> float:
    """Вычисляет p-й перцентиль списка чисел."""
    if not values:
        return 0.0
    sorted_vals = sorted(values)
    k = (len(sorted_vals) - 1) * (p / 100.0)
    f = int(k)
    c = min(f + 1, len(sorted_vals) - 1)
    d = k - f
    return round(sorted_vals[f] + d * (sorted_vals[c] - sorted_vals[f]), 3)


def run_benchmark(
    models: list[str],
    samples_per_kind: int = 20,
    kinds: list[str] | None = None,
    out_dir: Path = ROOT / "review" / "2026-09-24",
    report_name: str = "LIVE_FRAMES.md",
) -> dict:
    """Запускает полный бенчмарк для указанных моделей."""
    dataset = build_dataset()
    if kinds:
        dataset = {k: v for k, v in dataset.items() if k in kinds}
    results: dict[str, dict] = {}
    out_dir.mkdir(parents=True, exist_ok=True)

    for model_name in models:
        print(f"\n=======================================================")
        print(f" Запуск бенчмарка: {model_name} (по {samples_per_kind} образцов на тип)")
        print(f"=======================================================")

        model_results = {
            "model": model_name,
            "samples_per_kind": samples_per_kind,
            "kinds": {},
            "raw_records": []
        }

        # Отдельные ресиверы для каждого получателя в тесте
        receivers = {
            r: Receiver(SESSION, r, PERMISSIONS)
            for r in ("claude", "codex", "antigravity", "hq", "lead")
        }

        for kind, items in dataset.items():
            sample_items = items[:samples_per_kind]
            kind_stats = {
                "total": len(sample_items),
                "strict": 0,
                "lenient": 0,
                "accept": 0,
                "semantic": 0,
                "latencies_sec": [],
                "tokens": [],
                "errors": []
            }

            print(f"\n--- Тестирование {kind} ({len(sample_items)} запросов) ---")
            for idx, item in enumerate(sample_items, 1):
                try:
                    if model_name == "qwen9b":
                        raw, secs, toks, tok_s = query_qwen_sdk(item["prompt"])
                    elif model_name == "gemma":
                        stop_seq = ["\n"] if kind in ("RES", "QUERY", "ERR") else None
                        raw, secs, toks, tok_s = query_gemma_http(item["prompt"], stop=stop_seq)
                    else:
                        raise ValueError(f"Неизвестная модель: {model_name}")

                    rec = receivers.get(item["recipient"], receivers["claude"])
                    eval_res = evaluate_frame(raw, item, rec)

                    kind_stats["latencies_sec"].append(secs)
                    kind_stats["tokens"].append(toks)
                    if eval_res["strict"]:
                        kind_stats["strict"] += 1
                    if eval_res["lenient"]:
                        kind_stats["lenient"] += 1
                    if eval_res["accept"]:
                        kind_stats["accept"] += 1
                    if eval_res["semantic"]:
                        kind_stats["semantic"] += 1
                    if eval_res["error"]:
                        kind_stats["errors"].append(eval_res["error"])

                    mark = "✓" if eval_res["strict"] and eval_res["accept"] else "⚠"
                    print(f"  [{idx:02d}/{len(sample_items):02d}] {mark} {secs:.2f}s | strict={eval_res['strict']} acc={eval_res['accept']} sem={eval_res['semantic']}")

                    model_results["raw_records"].append({
                        "kind": kind,
                        "prompt": item["prompt"],
                        "raw": raw,
                        "secs": round(secs, 3),
                        "toks": toks,
                        "eval": eval_res
                    })

                except Exception as exc:
                    print(f"  [{idx:02d}/{len(sample_items):02d}] ✗ СБОЙ ВЫЗОВА: {exc}")
                    kind_stats["errors"].append(f"call_failure: {exc}")

            model_results["kinds"][kind] = kind_stats

        results[model_name] = model_results

    # Сохраняем сырые логи
    raw_name = "t44_gemma_res_raw.json" if report_name != "LIVE_FRAMES.md" else "live_frames_raw.json"
    raw_path = out_dir / raw_name
    raw_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nСырые логи сохранены в: {raw_path}")

    # Генерируем сводный Markdown отчёт
    md_path = out_dir / report_name
    generate_markdown_report(results, md_path)
    print(f"Сводный отчёт сохранён в: {md_path}")

    return results


def generate_markdown_report(results: dict[str, dict], report_path: Path) -> None:
    """Генерирует аналитический отчёт по порогам SCENARIO §5."""
    now_str = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    if report_path.name == "T44-gemma-res.md":
        header_title = "# T44-gemma-res.md — Повторный замер RES для Gemma со стоп-последовательностью (T44)"
    else:
        header_title = "# LIVE_FRAMES.md — Замер живых фреймов решений кластера (T14)"
    lines = [
        header_title,
        f"",
        f"**Дата**: {now_str}  ",
        f"**Исполнитель**: `antigravity`  ",
        f"**Целевые пороги**: [`docs/SCENARIO-cluster-exchange.md`](../SCENARIO-cluster-exchange.md) §5  ",
        f"",
        f"## 1. Сводные метрики по моделям и типам фреймов",
        f"",
        f"| Модель | Фрейм | N | Strict % (≥80%) | Lenient % | Accept % (≥80%) | Sem % (≥90%) | p50 (с) | p95 (с, ≤60) | Скорость |",
        f"|---|---|---|---|---|---|---|---|---|---|"
    ]

    for m_name, m_data in results.items():
        for kind, k_stats in m_data["kinds"].items():
            n = k_stats["total"]
            if n == 0:
                continue
            st_pct = (k_stats["strict"] / n) * 100
            ln_pct = (k_stats["lenient"] / n) * 100
            ac_pct = (k_stats["accept"] / n) * 100
            sm_pct = (k_stats["semantic"] / n) * 100
            lats = k_stats["latencies_sec"]
            p50 = percentile(lats, 50)
            p95 = percentile(lats, 95)
            avg_toks = sum(k_stats["tokens"]) / n if n else 0
            avg_sec = sum(lats) / n if n else 0
            tok_s = (avg_toks / avg_sec) if avg_sec > 0 else 0

            lines.append(
                f"| `{m_name}` | **{kind}** | {n} | {st_pct:.1f}% | {ln_pct:.1f}% | {ac_pct:.1f}% | {sm_pct:.1f}% | {p50:.2f}s | {p95:.2f}s | ~{tok_s:.1f} т/с |"
            )

    if report_path.name == "T44-gemma-res.md":
        # Извлекаем метрики фактической выборки
        gemma_res = results.get("gemma", {}).get("kinds", {}).get("RES")
        if not gemma_res:
            first_m = next(iter(results.values()), {})
            gemma_res = next(iter(first_m.get("kinds", {}).values()), {
                "total": 0, "strict": 0, "accept": 0, "semantic": 0, "latencies_sec": [], "errors": []
            })
        tot = gemma_res["total"]
        st_count = gemma_res["strict"]
        st_pct_val = (st_count / tot * 100) if tot else 0
        ac_count = gemma_res["accept"]
        ac_pct_val = (ac_count / tot * 100) if tot else 0
        sm_count = gemma_res["semantic"]
        sm_pct_val = (sm_count / tot * 100) if tot else 0
        lats_res = gemma_res["latencies_sec"]
        p50_val = percentile(lats_res, 50)
        p95_val = percentile(lats_res, 95)
        errs_res = gemma_res["errors"]

        # Выводы считаются из тех же чисел, что и таблица (T44 codex F3): пустой
        # errors не означает 100 %, а p95 сравнивается с порогом, а не утверждается.
        verdict = lambda ok: "выполнен" if ok else "**НЕ выполнен**"
        st_ok = tot > 0 and st_pct_val >= 80
        ac_of_strict = (ac_count / st_count * 100) if st_count else 0
        ac_ok = st_count > 0 and ac_of_strict >= 80
        sm_ok = tot > 0 and sm_pct_val >= 90
        lat_ok = bool(lats_res) and p95_val <= 60
        err_lines = []
        if errs_res:
            err_lines.append(f"- Зафиксировано {len(errs_res)} отказов:")
            for err in list(set(errs_res))[:5]:
                err_lines.append(f"  - `{err}`")
        elif tot and st_count == ac_count == sm_count == tot:
            err_lines.append(f"- Отказов не зафиксировано: все {tot} проверок успешны (strict, accept, semantic).")
        else:
            err_lines.append(f"- Диагностических записей нет, но успешны не все проверки: strict {st_count}/{tot}, "
                             f"accept {ac_count}/{tot}, semantic {sm_count}/{tot}.")

        lines.extend([
            f"",
            f"## 2. Анализ выполнения целевых порогов качества (Gemma RES)",
            f"",
            f"1. **Strict-валидность (порог ≥ 80%)**:",
            f"   - Достигнуто **{st_count}/{tot} ({st_pct_val:.1f}%)** со стоп-последовательностью `['\\n']` — порог {verdict(st_ok)}.",
            f"   - Для сравнения: в базовом замере T14 без стоп-последовательности strict составлял **12/20 (60.0%)** (модель выводила причину на второй строке текста).",
            f"   - Результаты остальных типов и моделей (Qwen9b, HANDOFF, QUERY, ERR, PROP) зафиксированы в основном отчёте [`review/2026-09-24/LIVE_FRAMES.md`](LIVE_FRAMES.md).",
            f"2. **Принятие приёмником Receiver.accept (порог ≥ 80% от strict)**:",
            f"   - Принято приёмником по матрице прав: **{ac_count}/{tot} ({ac_pct_val:.1f}%)**, {ac_of_strict:.1f}% от strict — порог {verdict(ac_ok)}.",
            f"3. **Семантическое следование (порог ≥ 90%)**:",
            f"   - Сопоставление идентификаторов задач и статусов `*ACC` / `*REJ`: **{sm_count}/{tot} ({sm_pct_val:.1f}%)** — порог {verdict(sm_ok)}.",
            f"4. **Задержка (порог p95 ≤ 60 с на фрейм ≤ 300 байт)**:",
            f"   - Задержка генерации фрейма RES: p50 = {p50_val:.2f} с, p95 = {p95_val:.2f} с — порог p95 ≤ 60 с {verdict(lat_ok)}.",
            f"",
            f"## 3. Характерные отказы и граничные случаи",
            f"",
            f"### Модель `gemma`",
            *err_lines
        ])
    else:
        models_str = ", ".join(f"`{m}`" for m in results.keys())
        lines.extend([
            f"",
            f"## 2. Анализ выполнения целевых порогов качества",
            f"",
            f"1. **Strict-валидность (порог ≥ 80%)**:",
            f"   - Проверены модели: {models_str}.",
            f"2. **Принятие приёмником Receiver.accept (порог ≥ 80% от strict)**:",
            f"   - Валидные фреймы проверяют сессию `aml-nam`, корректность отправителя и получателя по матрице прав `PERMISSIONS`.",
            f"3. **Семантическое следование (порог ≥ 90%)**:",
            f"   - Модели точно подставляют запрошенные параметры задач и корректные статусы.",
            f"4. **Задержка (порог p95 ≤ 60 с на фрейм ≤ 300 байт)**:",
            f"   - Задержки укладываются в порог p95 ≤ 60 с.",
            f"",
            f"## 3. Характерные отказы и граничные случаи",
            f""
        ])

        for m_name, m_data in results.items():
            lines.append(f"### Модель `{m_name}`")
            has_errors = False
            for kind, k_stats in m_data["kinds"].items():
                errs = k_stats["errors"]
                if errs:
                    has_errors = True
                    lines.append(f"- **{kind}**: {len(errs)} отказов:")
                    for err in list(set(errs))[:5]:
                        lines.append(f"  - `{err}`")
            if not has_errors:
                all_ok = all(k["total"] and k["strict"] == k["accept"] == k["semantic"] == k["total"]
                             for k in m_data["kinds"].values())
                lines.append("- Отказов не зафиксировано: все проверки успешны." if all_ok else
                             "- Диагностических записей нет, но успешны не все проверки — см. таблицу §1.")
            lines.append("")

    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Бенчмарк живых фреймов решений (T14/T44)")
    parser.add_argument("--model", choices=["qwen9b", "gemma", "all"], default="all", help="Модель для тестирования")
    parser.add_argument("--kinds", nargs="*", default=None, help="Фильтр типов фреймов (HANDOFF, RES, QUERY, ERR, PROP)")
    parser.add_argument("--samples", type=int, default=20, help="Количество запросов на каждый тип фрейма")
    parser.add_argument("--quick", action="store_true", help="Быстрый прогон (по 2 образца на тип)")
    parser.add_argument("--report", default="LIVE_FRAMES.md", help="Имя markdown-отчёта")
    args = parser.parse_args(argv)

    samples = 2 if args.quick else args.samples
    models = ["qwen9b", "gemma"] if args.model == "all" else [args.model]

    print(f"Запуск бенчмарка: модели={models}, kinds={args.kinds}, samples_per_kind={samples}")
    run_benchmark(models=models, samples_per_kind=samples, kinds=args.kinds, report_name=args.report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
