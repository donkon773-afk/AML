# Замечания по aml-nam review-fix-r1 — прогон на целевой машине

Дата: 2026-09-15. Кто: Claude (куратор проекта NeroAI), по поручению пользователя.
Ревизия пакета: `review-fix-r1`, архив `aml-nam-review-fix-r1.zip`, MANIFEST.sha256 сошёлся 35/35.
Код пакета не изменялся. Все замечания воспроизводимы командами ниже.

## Окружение

- Windows 11 Pro, локаль ru-RU (консольная кодировка cp1251).
- Python 3.12.10 (`C:\Users\Admin\AppData\Local\Programs\Python\Python312`), venv, `requirements-test.txt` + `requirements-client.txt`: lark 1.2.2, jsonschema 4.23.0, lmstudio 1.5.0 — версии совпадают с `evidence/provenance.json`.
- Node v24.20.0, `npm ci` → ровно 2 пакета (gbnf 0.1.41, @toon-format/toon 2.0.0).
- LM Studio, сервер `http://127.0.0.1:1234`. Модель: `qwen3.5-9b-uncensored-hauhaucs-aggressive@q4_k_m` (Q4_K_M, файнтюн Qwen 3.5 9B — целевая модель проекта), RTX 3060 12 GB.
- Во время проб Arma 3 не запущена, других нагрузок на GPU нет.

## 1. Офлайн-проверка: 22/63 Python-тестов падают на Windows без `PYTHONUTF8=1`

**Воспроизведение**

```
python verify.py            # → OFFLINE FAILED, exit 1, failures=22
set PYTHONUTF8=1
python verify.py            # → OFFLINE PASSED, exit 0, построчно = evidence/verify.txt
```

**Причина.** Все 22 падения — один и тот же `AssertionError` в `test_regressions.py::roundtrip` (строка 51): wire-строка, вернувшаяся из JS-кодека, отличается от Python-строки только символом `→` (U+2192) в конверте `observer→gw`. Помощник `js()` (строка 38) вызывает `subprocess.run([...], text=True, ...)` без `encoding=` — на Windows подставляется локальная кодировка (cp1251), Node пишет UTF-8. На Linux/UTF-8 (где сделан `evidence/verify.txt`) расхождения нет.

**Что это значит.** Дефект тестового моста Python↔Node, не кодека: JS-тесты 14/14, GBNF-проверка и демо зелёные в обоих прогонах, Python-only тесты тоже. Но `verify.py` в текущем виде не даёт `OFFLINE PASSED` на целевой машине проекта.

**Предложение.** `encoding="utf-8"` в `subprocess.run` в `tests/test_regressions.py::js()` и в `scripts/benchmark_vs_toon.py::to_toon` (тот же паттерн); либо `verify.py` выставляет `PYTHONUTF8=1` для дочерних процессов и пишет об этом в отчёт. Упомянуть требование в README («Проверка»).

Логи: `verify-local.txt` (FAILED), `verify-local-utf8.txt` (PASSED).

## 2. SDK-проба (`llm_client_sdk.py --selftest-grammar`) не проходит на reasoning-модели, хотя грамматика работает

**Воспроизведение**

```
python scripts\llm_client_sdk.py --selftest-grammar --model qwen3.5-9b-uncensored-hauhaucs-aggressive@q4_k_m
# → AMLError: SDK completion not confirmed: maxPredictedTokensReached, exit 1
```

**Причина (две части).**

a) `selftest()` вызывает `ask(..., max_tokens=128)` и не пробрасывает `no_think`; флаги `--no-think`/`--max-tokens` действуют только на `--baseline`. Контрольный запрос «Reply with exactly HELLO WORLD» на этой модели не укладывается в 128 токенов.

b) `/no_think` в system-сообщении этот файнтюн игнорирует: и с ним, и после отключения размышлений в UI LM Studio (переключатель на SDK-маршрут не действует) модель начинает ответ с открытого текста `Thinking Process: …`, а LM Studio вставляет синтетический разделитель `__LM_STUDIO_INTERNAL_LSEP_SYNTHETIC_REASONING_END_<hex>__`. `strip_reasoning()` его знает, но в `probe_mode` (строка 104) намеренно не применяется — комментарий «Under an enforced grammar there is no reasoning» на этом сервере/модели неверен: разделитель приходит и под грамматикой.

**Ключевой факт.** Диагностическим скриптом поверх функций пакета (`ask(..., grammar_text=..., probe_mode=True, max_tokens=1024)`) грамматика по SDK **применяется**: оба случайных маркера возвращены точно, к ним приклеен только этот разделитель:

```
expected AML_PROBE_40ff6935b2240e70dbb60559
got      'AML_PROBE_40ff6935b2240e70dbb60559__LM_STUDIO_INTERNAL_LSEP_SYNTHETIC_REASONING_END_f4e9a8d2c6b14d0c9e5f3a7b8c1d2e6a__'
```

**Предложение.** (1) В `probe_mode` тоже применять `strip_reasoning` (или сравнивать после среза разделителя). (2) Подавлять размышления через конфиг SDK-запроса, а не через `/no_think` в тексте, и пробросить `--no-think`/`--max-tokens` в `selftest`. (3) В README: проба на reasoning-моделях требует лимита выше 128 или отключения размышлений на уровне запроса.

Логи: `selftest-qwen35-9b-q4.txt`, `selftest-qwen35-9b-q4-nothink.txt`, `diag-qwen35-9b-q4.txt`, `diag-qwen35-9b-q4-run2.txt`.

## 3. HTTP-проба (`llm_client.py --selftest-grammar`): поле `grammar` на LM Studio молча игнорируется

**Воспроизведение**

```
python scripts\llm_client.py --selftest-grammar --model qwen3.5-9b-uncensored-hauhaucs-aggressive@q4_k_m
# → FAIL: grammar-only marker was not returned in full, exit 1
```

**Причина.** Контроль проходит (`reasoning_effort: "none"` здесь работает — модель отвечает ровно `HELLO WORLD`). Но с `payload["grammar"] = 'root ::= "AML_PROBE_…"'` сервер `/v1/chat/completions` возвращает `'HELLO WORLD'`, а не маркер — поле не применяется и ошибки не даёт. Вариант `response_format: {"type": "gbnf", "gbnfGrammar": …}` по HTTP → `HTTP 400 Bad Request`.

**Что это значит.** На LM Studio HTTP-клиент пакета отправляет **неограниченные** запросы, считая их ограниченными. Это самое серьёзное из трёх замечаний: `llm_client.py` без `validate_frame`+`Receiver` после него даёт ложное чувство безопасности. README это допускал («проба HTTP-поля grammar зависит от сервера») — теперь для LM Studio это установлено.

**Предложение.** В `COMPATIBILITY.md`/README явно: на LM Studio грамматика доступна только через SDK-маршрут; HTTP-клиент на этом сервере — без принуждения. Возможно, HTTP-клиент должен падать, если сервер не подтверждает поддержку `grammar` (проба маркером как обязательный шаг перед любым `--ask`).

Логи: `selftest-http-qwen35-9b-q4.txt`, `diag-http-qwen35-9b-q4.txt`.

## Что подтверждено, что нет

Подтверждено на целевой машине: целостность архива; офлайн-набор (63 Python + 14 JS + GBNF + демо) при `PYTHONUTF8=1`; принуждение грамматики через lmstudio-python 1.5.0 на Qwen 3.5 9B (по маркерам).

Не проверялось (и до правок 2–3 нет смысла): полные AML-фреймы на живой модели через `validate_frame` → `Receiver.accept`, задержки, качество решений, нативная загрузка `aml_v2.gbnf` в llama.cpp вне LM Studio.

## Итог для интеграции в NeroAI

Пакет в `bridge/` не интегрирован. Мост проекта сейчас работает по HTTP с облачной моделью (JSON + `response_format: json_schema`), локальный контур на LM Studio — только по HTTP с `reasoning_effort: none`. Для AML на локальной модели потребовался бы SDK-маршрут (Python-процесс рядом с Node-мостом) — это отдельное архитектурное решение, не правка кодека.
