# MCP Metabase

MCP-сервер для работы с Metabase через API key без браузерной авторизации. Он умеет
читать, создавать, копировать, изменять, перемещать в корзину и восстанавливать
карточки, дашборды и коллекции, а также выполнять ограниченный предпросмотр запросов.

Сервер предоставляет 14 инструментов. Произвольные REST-запросы и безвозвратное
удаление недоступны: действия `*_delete` перемещают объекты в Trash.

## Установка из Git

1. Найди абсолютный путь к `uvx`: `(Get-Command uvx).Source` в PowerShell или
   `command -v uvx` в macOS/Linux.
2. Выбери точный commit SHA из репозитория.
3. Добавь сервер в MCP-конфиг.
4. Перезапусти MCP-клиент.

```toml
[mcp_servers.metabase_work]
command = "<ABSOLUTE_PATH_TO_UVX>"
args = [
  "--from",
  "git+https://github.com/johnniewalker89/ai-mcp.git@<COMMIT_SHA>#subdirectory=mcp-metabase",
  "mcp-metabase"
]
startup_timeout_sec = 120
tool_timeout_sec = 120
default_tools_approval_mode = "prompt"

[mcp_servers.metabase_work.env]
METABASE_MCP_INSTANCE = "metabase_work"
METABASE_BASE_URL = "https://metabase.example.org"
METABASE_API_KEY = "<API_KEY>"
METABASE_MCP_SOURCE_REVISION = "<COMMIT_SHA>"
# METABASE_MCP_EXPECTED_USER_ID = "123"
```

Храни API key только в локальном конфиге или окружении и не добавляй его в Git.
Пакет не загружает dotenv автоматически; [`metabase.env.example`](metabase.env.example)
служит справочником по доступным переменным.

Один и тот же конфиг работает в Windows, macOS и Linux; меняется только путь к `uvx`.

## Настройка

| Переменная | Назначение | Значение по умолчанию |
| --- | --- | --- |
| `METABASE_BASE_URL` | Точный HTTPS-адрес Metabase | обязательна |
| `METABASE_API_KEY` | API key пользователя Metabase | обязательна |
| `METABASE_MCP_EXPECTED_USER_ID` | Дополнительная проверка владельца key | не задана |
| `METABASE_MCP_SOURCE_REVISION` | Commit SHA установленной версии MCP | `unreported` |
| `METABASE_MCP_SUPPORTED_VERSION_PREFIXES` | Проверенные версии Metabase | `v0.63.,0.63.` |
| `METABASE_MCP_PLAN_TTL_SECONDS` | Срок действия подготовленного плана | 300 секунд |
| `METABASE_MCP_EDIT_SESSION_TTL_SECONDS` | Срок рабочей сессии | 900 секунд |
| `METABASE_MCP_EDIT_SESSION_MAX_ACTIONS` | Максимум действий в сессии | 20 |
| `METABASE_MCP_AUDIT_DIR` | Каталог локального журнала аудита | `~/.codex/metabase-mcp-audit` |

Остальные лимиты перечислены в [`metabase.env.example`](metabase.env.example); обычно
их менять не требуется. Для каждого человека лучше выпускать отдельный API key.

### Подтверждения

Начни с `default_tools_approval_mode = "prompt"`. После проверки `metabase_health`
можно разрешить без повторного prompt чтение, подготовку действий, локальную отмену
плана и работу внутри уже подтверждённой сессии.

После успешного изменения одного объекта сессия обычно открывается автоматически.
`metabase_session_open` нужен, когда работа начинается с уже существующего объекта.

Всегда оставляй в режиме `prompt`:

- `metabase_session_open` — открывает доступ к выбранному объекту или дашборду;
- `metabase_action_execute` — применяет подготовленное изменение;
- `metabase_rollback_execute` — применяет подготовленный откат.

## Безопасность

- API key получает права своей группы Metabase. Используй отдельную группу с минимально
  необходимыми правами, а не администратора.
- Любое разовое изменение сначала готовится через `metabase_action_prepare`, затем
  отдельно подтверждается и выполняется через `metabase_action_execute`.
- Если полное создание дашборда вернуло `500`, безопасный упрощённый вариант
  выполняется в том же подтверждённом плане только после доказательства, что
  исходный запрос ничего не создал. Неоднозначный результат останавливает запись.
- Одно подтверждение рабочей сессии разрешает серию изменений только выбранного объекта
  или подтверждённого графа дашборда. Внешнее изменение блокирует дальнейшую запись до
  открытия новой сессии.
- Перемещение в корзину и откат всегда подтверждаются отдельно. Безвозвратного удаления,
  произвольного REST API и автоматического повтора записей нет.
- При неизвестной версии Metabase остаются доступны `metabase_health` и чтение, но
  записи блокируются.
- Предпросмотр допускает один `SELECT` или `WITH` только для чтения и ограничивает число
  строк. Подключение Metabase к рабочей базе также должно использовать пользователя с
  правами только на чтение.

## Инструменты

| Инструмент | Что делает |
| --- | --- |
| `metabase_health` | Проверяет подключение, пользователя, версию, режим работы и лимиты |
| `metabase_search` | Ищет объекты Metabase с ограничением количества результатов |
| `metabase_object_get` | Читает карточку, дашборд, коллекцию, базу, таблицу или поле |
| `metabase_collection_items` | Показывает элементы и дочерние коллекции выбранной коллекции |
| `metabase_session_open` | Открывает подтверждаемую рабочую сессию для объекта |
| `metabase_session_apply` | Применяет изменения внутри открытой сессии |
| `metabase_session_query` | Выполняет ограниченный предпросмотр запроса карточки |
| `metabase_session_status` | Показывает состояние и остаток лимитов сессии |
| `metabase_session_close` | Закрывает сессию без изменения объектов Metabase |
| `metabase_action_prepare` | Готовит одно точное действие без записи |
| `metabase_action_execute` | После подтверждения выполняет подготовленное действие |
| `metabase_rollback_prepare` | Готовит откат ранее выполненного изменения |
| `metabase_rollback_execute` | После подтверждения выполняет подготовленный откат |
| `metabase_exact_action_revoke` | Отменяет неиспользованный подготовленный план |

### Основные действия

| Объект | Действия |
| --- | --- |
| Карточка | `question_create`, `question_copy`, `question_update`, `question_delete`, `question_restore` |
| Дашборд | `dashboard_create`, `dashboard_copy`, `dashboard_update`, `dashboard_delete`, `dashboard_restore` |
| Коллекция | `collection_create`, `collection_copy`, `collection_update`, `collection_delete`, `collection_restore` |
| Поля | `field_update`, `field_values_rescan` |
| Несколько объектов | `batch_update` |

Для серии правок одного объекта используй рабочую сессию. Для отдельного создания,
копирования, перемещения в корзину, восстановления или пакетного изменения используй
`metabase_action_prepare` и `metabase_action_execute`.

Если элемент `batch_update` завершился с `outcome_unknown`, MCP внутри того же
одноразового exact plan сначала перечитывает все объекты пакета. Уже сохранённые объекты
он пропускает, а подтверждённо несохранённые применяет по одному с readback. Любой drift
или неубедительный readback останавливает recovery без новых записей; клиент не должен
повторять пакет или создавать новый план вслепую.

## Проверка подключения и типовые проблемы

Проверь установленный Git commit и подключение к Metabase:

```text
<ABSOLUTE_PATH_TO_UVX> --from "git+https://github.com/johnniewalker89/ai-mcp.git@<COMMIT_SHA>#subdirectory=mcp-metabase" mcp-metabase --check
```

После перезапуска клиента вызови `metabase_health`. При рабочей конфигурации он вернёт
`status=ready`, ожидаемый `source_revision` и `writes_ready=true`.

- `401/403`: проверь API key и права его группы Metabase.
- `identity_unverified`: проверь `METABASE_MCP_EXPECTED_USER_ID`.
- `read_only_degraded`: версия Metabase не входит в проверенный список; записи штатно
  заблокированы.
- `rejected_stale`: объект изменился после подготовки действия; перечитай его и создай
  новый план или сессию.
- `outcome_unknown`: не повторяй изменение автоматически. Для batch учитывай результат
  полного server-side readback/recovery; если неопределённость сохранилась, сначала
  перечитай состояние и только затем готовь новый exact plan.
