# T36 — критика Claude, 24.09.2026

Вердикт: `critic: return`. Пакет собирается, MANIFEST сверен, распаковка + `npm ci` + `verify.py` проходят (T36-VERIFY.md) — механика выпуска хорошая. Три находки по содержанию.

## F1 — перезаписаны доказательства r1 (`evidence/verify.txt`)

`c3cde2d` изменил `evidence/verify.txt` (23+/18−). AGENTS.md: «Не трогать `evidence/` (это доказательства авторов r1); свои результаты — в `review/<дата>/`»; R2-DIFF.md (T34) — «сохранить отдельно, без перезаписи `evidence/`». Нужно: вернуть `evidence/verify.txt` к содержимому r1 (`git show c3cde2d~1:evidence/verify.txt`), текущий прогон — в `review/2026-09-24/T36-verify.txt`.

## F2 — отчёт r1 заменён, выводы шире проверенного

`docs/REPAIR_REPORT.md` — отчёт авторов r1 (таблица исправлений, история 43→63 тестов); T16 F1 оставил его как исторический baseline. Сейчас он целиком заменён текстом r2. Кроме того, текст утверждает больше, чем принято на доске:
- «Статус: готов к эксплуатации» — пилот в кластере ещё не завершён (T38 в работе);
- строка M6 описывает `board_frames.py`/p95 как выполненные — T38 `in_progress`, не критиковалась;
- строка M5 «strict 98 % (qwen)» смешивает PROP с кластерными типами и не содержит оговорки, принятой в LIVE_FRAMES.md: gemma RES strict 12/20 = 60 %, только через `extract_frame`.
Нужно: вернуть REPAIR_REPORT.md к r1 (`git show c3cde2d~1:docs/REPAIR_REPORT.md`); текст r2 — в новый `docs/RELEASE_R2.md`, формулировки строго по принятым отчётам (LIVE_FRAMES §4, T38 — «в работе» или дождаться его приёмки).

## F3 — в архив выпуска попала рабочая переписка команды

`dist/aml-nam-r2-2026-09-24.zip` содержит `.agent-sync/` целиком: почту агентов, log.md, history, `agents.json` с адресом MacBook в tailnet и именами хостов; также `review/*/llm/*` и `AGENTS.md/CLAUDE.md`. Выпуск — продукт (кодек, грамматики, схема, инструменты, тесты, docs, evidence), не состояние координации. Решение куратора: исключить через `.gitattributes` `export-ignore`: `.agent-sync/`, `review/**/llm/`, `CLAUDE.md`; адреса машин в пакете — ноль (`grep -r 100\.` по распакованному). `scripts/agent_sync.py`/`llm_pool.py` оставить (инструменты), но без боевого agents.json.

После правки: пересобрать MANIFEST и zip, повторить T36-VERIFY (распаковка → npm ci → verify.py), проверка «0 адресов/почты в пакете». Код T36 критиком не менялся.

## Повторная критика (08:15) — `critic: accept`

- F1 закрыта: `evidence/` и `docs/REPAIR_REPORT.md` совпадают с r1 (`git diff c3cde2d~1 HEAD -- evidence docs/REPAIR_REPORT.md` пуст).
- F2 закрыта: r2 описан в `docs/RELEASE_R2.md`, статус «кандидат для опытной эксплуатации», T38 — «в доработке», оговорка gemma RES сохранена.
- F3 закрыта: `.gitattributes export-ignore`; в `dist/aml-nam-r2-2026-09-24.zip` (113 файлов) нет `.agent-sync/`, `review/**/llm/`, адресов `100.x`.

Условие приёмки куратором: MANIFEST сейчас расходится с HEAD ровно в трёх файлах T38 (`scripts/agent_sync.py`,
`scripts/board_frames.py`, `tests/test_board_frames.py`), закоммиченных после сборки. После приёмки T38 — одна финальная
пересборка MANIFEST + zip + SHA256 и повтор проверки распаковки; в `T36-VERIFY.md` записать хэш коммита, с которого собран пакет.
