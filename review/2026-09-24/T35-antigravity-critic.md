# T35 — критика транспорта AML (2-й круг), 24.09.2026

**Исполнитель**: `claude`  
**Критик**: `antigravity`  
**Вердикт**: `critic: accept`  

## 1. Проверка исправления F1 (неполный фрейм в outbox)

Codex в 1-м круге обнаружил, что частичная запись фрейма в outbox приводила к немедленному потреблению среза и ложным отказам `ERR INVALID` с потерей сообщения.

В коммите `0e422fd` в `scripts/aml_bus.py` `receive()` переведён на поиск терминатора пустой строки после текущего смещения:
```python
end = max(raw.rfind(b"\n\n", off), raw.rfind(b"\r\n\r\n", off))
if end < 0:
    continue
end += 4 if raw[end:end + 4] == b"\r\n\r\n" else 2
chunk = raw[off:end].decode("utf-8", "replace").replace("\r\n", "\n")
st["offsets"][fp.name] = end
```
Если терминатор не найден, файл пропускается на текущем тике, смещение `offsets` не сдвигается, недописанный хвост остаётся до следующего прохода.

## 2. Воспроизведение и тесты

1. `tests/test_aml_bus.py`:
   - `test_partially_written_frame_waits_for_its_end`: проверена раздельная запись (заголовок → `receive()` возвращает `[]`, дописка тела с `\n\n` → `receive()` принимает, статус переходит в `review`).
   - Все 7 тестов `tests/test_aml_bus.py` завершились успешно (0.9 с).
2. Общий шлюз:
   - `verify.py`: 116 Python + 14 JS + GBNF + demo PASS (OFFLINE PASSED).
3. Интеграция с шиной кластера:
   - Проверена живая отправка `aml_bus.py send antigravity res T35 ACC` через `agent_sync serve`. Приёмник обработал фрейм штатно, подтвердил полномочия по доске, перевёл статус в `claude` и разослал уведомления в лог и почту.

Замечаний нет. Задача готова к приёмке куратором.
