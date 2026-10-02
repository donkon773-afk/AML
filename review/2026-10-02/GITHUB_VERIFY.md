# Офлайн-проверка GitHub-пакета

- Дата: 2026-10-02.
- Исходная ревизия кода: `bc5c074bfe7cc7dece0127ff900f3b34c894dc2f`.
- Среда: Windows, Python 3.12.10, Node.js 24.20.0, npm 11.19.0.
- Метод: `verify.py` запущен на отдельной временной копии исходников; рабочие файлы и локальная доска не использовались как тестовая среда.
- Команда: `python verify.py` после установки `requirements-test.txt` и `npm ci`.

## Результат

```text
Python unittest: 150 tests, OK
JavaScript: 14 tests, pass 14, fail 0
GBNF equivalent-language check: passed
Offline demo: passed
OFFLINE PASSED
exit code: 0
```

Вывод Python содержал одну ожидаемую проверку сценария retry от `test_ci.py`; после retry команда завершила полный набор успешно. Тесты бенчмарка используют заглушки и временные каталоги. Прогон не измеряет новые ответы живой модели и не проверяет нативный движок ограниченной генерации.

Полный воспроизводимый запуск: `python verify.py`. Сводка покрытия и исторические отдельные замеры — [`docs/TESTS_AND_BENCHMARKS.md`](../../docs/TESTS_AND_BENCHMARKS.md).
