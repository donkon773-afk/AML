# T43 — Ротация лент и журналов кластера (board.aml, outbox-*.aml, events.jsonl, llm_jobs.json)

- **Дата**: 24.09.2026
- **Исполнитель**: `antigravity`
- **Критик**: `claude`
- **Коммит реализации**: `aa83661`

---

## 1. Постановка задачи и инварианты

Ленты и журналы кластера в `.agent-sync/` ранее росли без ограничений. Реализован механизм ротации файлов по размеру (по умолчанию 1 МБ) с перемещением исторических данных в `.agent-sync/archive/` без остановки кластера.

### Инварианты:
1. **Лента доски (`board.aml`)**:
   - Старая лента уходит в `.agent-sync/archive/board-<YYYYMMDD-HHMMSS>.aml`.
   - В новую ленту `board.aml` немедленно публикуется полный снимок (`STATE` в профиле `s:2`), благодаря чему любой новый или непрерывный `Receiver` сразу может читать ленту без ошибок `RESYNC`.
   - Счётчики `seq` и `rev` в `board_state.json` продолжают строго монотонный рост.
   - Механизм сверки `_reconcile(feed, st)` в `board_frames.py` адаптирован: если лента пуста (0 байт) или отстаёт от `st["seq"]`, он сбрасывает кэш строк и форсирует полный `STATE`. Поддерживаются как `\n\n`, так и Windows `\r\n\r\n`.

2. **Исходящие ленты агентов (`outbox-*.aml`)**:
   - Перед ротацией вызывается `aml_bus.receive()`, чтобы обработать все завершённые фреймы.
   - Прочитанный префикс `raw[:off]` перемещается в архив `.agent-sync/archive/outbox-<peer>-<timestamp>.aml`.
   - Недописанный хвост `raw[off:]` (если есть фрейм в процессе записи) остаётся в активном файле `outbox-<peer>.aml`.
   - Смещение в `bus_state.json` `offsets[fp.name]` сбрасывается в `0`.
   - Счётчики `seq_out` и `seq_in` **не сбрасываются**, сохраняя защиту от повторов и монотонность.
   - В `aml_bus.receive()` добавлена защита: если размер файла уменьшился (`len(raw) < off`), смещение автоматически сбрасывается в `0`, предотвращая зависание приёмника.

3. **Журнал событий (`events.jsonl`)**:
   - Старые строки архивируются в `.agent-sync/archive/events-<timestamp>.jsonl`.
   - Последние `keep` (по умолчанию 50) событий сохраняются в активном `events.jsonl` для отображения на дашборде.

4. **Очередь заданий моделей (`llm_jobs.json`) — критический инвариант**:
   - **Незавершённые задания** (`pending`, `assigned`, `running`, `delivering`) **НИКОГДА не удаляются** и не архивируются до их полного завершения.
   - Архивируются только завершённые задания (`done`, `failed`) сверх `keep_finished` (по умолчанию 25) в `.agent-sync/archive/llm_jobs-<timestamp>.json`.
   - Функция `save_jobs` в `llm_pool.py` также обновлена, чтобы при лимите 300 записей отсекались только старые завершённые задания.

5. **Интеграция**:
   - Модуль `scripts/rotation.py` с функциями `rotate_board_aml`, `rotate_outbox_aml`, `rotate_events`, `rotate_llm_jobs`, `rotate_all` и CLI (`--max-bytes`, `--force`, `--dry-run`, `--target`).
   - Команда `agent_sync.py rotate` и вызов ротации при `agent_sync.py compact`.
   - Фоновая периодическая проверка ротации каждые 5 минут в `agent_sync.py serve`.
   - Потокобезопасность: `board_frames._PUB_LOCK` переведён на `threading.RLock()`.

---

## 2. Результаты тестов

### 2.1. Профильные тесты модуля ротации (`tests/test_rotation.py`)
- `test_board_aml_rotation_creates_archive_and_starts_with_keyframe`: PASS
- `test_board_reconcile_handles_rotated_feed`: PASS
- `test_outbox_rotation_and_offsets`: PASS
- `test_outbox_rotation_preserves_unread_tail`: PASS
- `test_outbox_receive_resets_offset_if_file_shrunk`: PASS
- `test_events_rotation`: PASS
- `test_llm_jobs_rotation_preserves_all_incomplete_jobs`: PASS
- `test_rotate_all_and_cli`: PASS
**Итог: 8/8 PASS.**

### 2.2. Смежные тесты
- `tests/test_board_frames.py`: 8/8 PASS
- `tests/test_aml_bus.py`: 8/8 PASS
- `tests/test_llm_pool.py`: 12/12 PASS
- `tests/test_agent_sync.py`: 6/6 PASS
- `tests/test_rotation.py`: 8/8 PASS
**Итог: 42/42 PASS.**

### 2.3. Шлюз качества `verify.py`
- Layout: PASS
- Python dependencies: PASS
- Python regression suite: 134/134 PASS
- JavaScript tests: 14/14 PASS
- GBNF equivalent-language check: PASS
- Offline demo: PASS
**Итог: OFFLINE PASSED.**

### 2.4. Сквозной шлюз качества `scripts/ci.py` (чистый detached HEAD worktree)
- `npm ci`: PASS
- `verify.py`: PASS
- `unittest`: PASS
- `node tests`: PASS
- `cluster_examples`: PASS
- `npm audit`: PASS
**Итог: ALL PASS (exit code 0).**
