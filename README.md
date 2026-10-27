# async-agent-demo

Небольшая локальная демка, чтобы проверить **resumable generation** для агентов:

- агент вызывает инструмент и **перестаёт генерировать**;
- run сохраняет точный `messages` (включая `assistant.tool_calls`);
- результат инструмента **будит** run и продолжает тот же transcript;
- heartbeat будит run, если результата всё ещё нет;
- у инструмента **нет таймаута**;
- память — append-only, без compaction;
- снаружи HTTP-запрос может ждать результата, но worker не блокируется на инструменте.

Это не production-код и не часть Nova. Задача — проверить механику и отрефайнить API.

## Запуск

```powershell
cd C:\Users\ruden\Documents\GitHub\async-agent-demo
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[test]"
uvicorn app.main:app --reload --port 8100
```

Открыть `http://127.0.0.1:8100/`.

## Сценарии

В UI:

- `echo hello` — быстрый auto-tool;
- `compile 4` — инструмент работает 4 секунды; раз в 1.5 секунды приходит heartbeat, run просыпается, пишет progress и снова ждёт;
- `manual` — инструмент не завершается сам; нажми **«Завершить вручную»**;
- `fail` — инструмент завершается ошибкой, run продолжается;
- `multi` — два параллельных tool call; run ждёт оба;
- **notification во время ожидания**: нажми «Кинуть уведомление», пока `manual`/`compile` ещё выполняется. Уведомление будит **тот же run**: для pending tool call добавляется interim `tool`-результат «в процессе», затем уведомление как `user`-сообщение; run отвечает и снова ждёт action.

Через API:

```powershell
$body = @{ message = "manual"; heartbeat_interval = 1.5 } | ConvertTo-Json
$run = Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8100/runs `
  -ContentType 'application/json' -Body $body

# посмотреть состояние
Invoke-RestMethod http://127.0.0.1:8100/runs/$($run.run_id)

# завершить manual action (id взять из actions)
Invoke-RestMethod -Method Post `
  -Uri http://127.0.0.1:8100/actions/<action_id>/complete `
  -ContentType 'application/json' -Body '{"result":{"manual":"ok"}}'

# дождаться завершения run
Invoke-RestMethod -Method Post `
  -Uri "http://127.0.0.1:8100/runs/$($run.run_id)/wait?timeout=30"
```

Уведомление, пока action ещё `running`:

```powershell
Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8100/runs/$($run.run_id)/notify" `
  -ContentType 'application/json' `
  -Body (@{ message = "peer ping" } | ConvertTo-Json)
```

Тот же run получает interim `tool`-результат (`status=in_progress`) для pending call, затем уведомление как `user`-сообщение. Модель может ответить «в процессе» и снова уйти в waiting. Когда action завершится, его финальный результат добавляется **независимым system-текстом** `TOOL RESULT (late)`, а не вторым `tool`-результатом с тем же `call_id`.

Частичный вывод инструмента (например, stdout bash) можно положить в action до завершения:

```powershell
Invoke-RestMethod -Method Post `
  -Uri http://127.0.0.1:8100/actions/<action_id>/progress `
  -ContentType 'application/json' `
  -Body '{"progress":{"partial_output":"Compiling module A..."}}'
```

Этот `progress` попадёт в interim `tool`-результат при следующем heartbeat/notification.

SSE-поток событий:

```text
GET /runs/{run_id}/events
```

## Что внутри

```text
app/store.py   SQLite: runs, actions, events, memory
app/engine.py  worker, pause/resume, heartbeat, action completion
app/model.py   детерминированная mock-модель
app/tools.py   mock_echo, mock_compile, mock_manual, mock_fail
app/main.py    FastAPI + SSE + минимальный UI
```

### Инварианты

1. `runs.messages` — точный transcript для LLM. Его нельзя пересобирать через retrieval, пока run ждёт tool result.
2. `actions` живут отдельно от run. Action не имеет timeout.
3. `wake_at` — таймаут ожидания run, а не инструмента.
4. Результат action добавляет `tool`-сообщение с `tool_call_id` и продолжает run.
5. Heartbeat добавляет `system`-сообщение, но не завершает run и не трогает action.
6. Уведомление во время ожидания будит тот же run: сначала interim `tool`-результат, затем `user`-сообщение.
7. Поздний финальный результат добавляется как независимый `system`-текст, потому что `call_id` уже получил interim `tool`-результат.
8. Старые `memory`-записи не переписываются.

## Ограничение

Пауза происходит на границе шага: после `assistant.tool_calls`, перед следующим LLM-вызовом. Остановить и продолжить **посреди одного токена** стандартный LLM API надёжно не умеет; это отдельная задача.

## Тесты

```powershell
pytest -q
```

Покрыто:

- pause/resume после tool call;
- run не блокирует другие run;
- heartbeat не убивает инструмент;
- ошибка инструмента;
- несколько одновременных tool call;
- append-only memory;
- HTTP API + manual completion.
