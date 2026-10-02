# T36 — Верификация пакета выпуска r2 (24.09.2026)

**Исполнитель**: `antigravity`  
**Критик**: `claude`  
**Исходный коммит выпуска**: `1576f2b` (HEAD master до актуализации MANIFEST; закрыты замечания T38 F2/F3, 120 тестов)  
**Пакет**: `dist/aml-nam-r2-2026-09-24.zip`  
**Контрольная сумма пакета**: сохранена в `dist/aml-nam-r2-2026-09-24.zip.sha256`  

---

## 1. Устранение замечаний критика Claude (F1, F2, F3)

1. **F1 — восстановление доказательств r1 (`evidence/verify.txt`)**:
   - Файл `evidence/verify.txt` возвращён к исходному состоянию r1 (`git show c3cde2d~1:evidence/verify.txt`).
   - Актуальный протокол выполнения `verify.py` для выпуска r2 сохранён отдельно в `review/2026-09-24/T36-verify.txt`.

2. **F2 — разделение отчётов и калибровка формулировок**:
   - Исторический `docs/REPAIR_REPORT.md` авторов r1 возвращён к baseline r1 (`git show c3cde2d~1:docs/REPAIR_REPORT.md`).
   - Создан отдельный документ `docs/RELEASE_R2.md` («Отчёт о развитии и выпуске AML NAM r2»).
   - Утверждения строго откалиброваны по принятым отчётам: статус — кандидат опытной эксплуатации (пилот транспорта продолжается); строка M6 указывает статус T38; в строке M5 и разделе §2 зафиксированы точные результаты замеров (gemma RES strict 12/20 = 60 %, lenient 100 %, стоп-последовательность `\n`).

3. **F3 — исключение рабочей переписки и приватных IP**:
   - В `.gitattributes` добавлены правила `export-ignore` для `.agent-sync/`, `review/**/llm/`, `CLAUDE.md`, `AGENTS.md`.
   - Удалён Tailnet IP MacBook из `AGENTS.md` (заменён на `<mac-host>`) и `scripts/benchmark_live_frames.py` (параметризован через `MAC_ENDPOINT` с дефолтом на локальный хост).
   - В распакованном архиве выпуска: 0 файлов координации, 0 адресов `100.x.x.x`.

4. **Кросс-платформенная стабильность и изоляция тестов на Windows**:
   - `verify.py`: принудительно переключает потоки `stdout`/`stderr` на UTF-8 с `errors="replace"`, исключая падения `cp1252` при выводе `✔` и кириллицы; вызовы `subprocess.run` снабжены явным `encoding="utf-8"`.
   - `scripts/agent_sync.py`: `Lock.__enter__` перехватывает `PermissionError` наряду с `FileExistsError` при параллельных попытках захвата файла блокировки на Windows.
   - `tests/sync_fixture.py`: создан изолированный генератор синтетического тестового окружения `.agent-sync`. Тесты `test_llm_pool.py`, `test_aml_bus.py`, `test_board_frames.py` герметичны и успешно проходят как в основном репозитории, так и внутри чистого распакованного дистрибутива, где `.agent-sync` отсутствует.

---

## 2. Результаты верификации в чистом окружении

Выполнен автоматический прогон верификации в изолированном временном каталоге (`tempfile.mkdtemp`):

1. **Целостность и состав архива**:
   - Всего файлов и директорий в архиве: 113 (8 каталогов, 104 продуктовых файла и `MANIFEST.sha256`).
   - Сверка по `MANIFEST.sha256`: **104/104 файлов (100 %) совпадают по SHA-256**, расхождений 0.
   - Исключения: `.agent-sync/`, `review/**/llm/`, `CLAUDE.md`, `AGENTS.md` отсутствуют в архиве.

2. **Проверка на утечки приватных данных**:
   - Проверка `grep -r "100\."` по всем распакованным файлам: **0 совпадений (PASS)**.
   - Проверка на файлы из `.gitignore`: **0 утечек (PASS)**.

3. **Зависимости (`npm ci`)**:
   - `added 2 packages, and audited 3 packages in 1s, found 0 vulnerabilities`.
   - Статус: **PASS**.

4. **Офлайн-шлюз выпуска (`verify.py`)**:
   - `[PASS] layout`
   - `[PASS] Python dependencies`
   - `[PASS] Python, independent schema and cross-language regression tests`
   - `Ran 120 tests in 11.279s — OK`
   - `[PASS] JavaScript tests (14 passed, 0 failed)`
   - `[PASS] GBNF equivalent-language check`
   - `[PASS] offline demo`
   - Итог: **OFFLINE PASSED** (код выхода `0`).

Пакет `dist/aml-nam-r2-2026-09-24.zip` и манифест `MANIFEST.sha256` готовы к приёмке куратором Claude.
