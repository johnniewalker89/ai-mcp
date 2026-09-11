# MCP Metabase

MCP-сервер для работы с Metabase через API key без браузерной авторизации. Он умеет
читать, создавать, копировать, изменять, перемещать в корзину и восстанавливать
карточки, дашборды и коллекции, а также выполнять ограниченный предпросмотр запросов.

Сервер предоставляет 14 инструментов. Произвольные REST-запросы и безвозвратное
удаление недоступны: действия `*_delete` перемещают объекты в Trash.

## Установка из Git

Нужны Git и `uv` 0.11.7. Выбери полный 40-символьный `<COMMIT_SHA>` и новый
`<SOURCE_DIR>` для этой версии. Один раз подготовь runtime по `uv.lock`:

```text
git clone --no-checkout https://github.com/johnniewalker89/ai-mcp.git "<SOURCE_DIR>"
git -C "<SOURCE_DIR>" checkout --detach <COMMIT_SHA>
<ABSOLUTE_PATH_TO_UV> sync --project "<SOURCE_DIR>/mcp-metabase" --locked --no-default-groups --no-editable --link-mode copy --python 3.12.10
```

Готовый executable: `<SOURCE_DIR>/mcp-metabase/.venv/Scripts/mcp-metabase.exe`
на Windows или `<SOURCE_DIR>/mcp-metabase/.venv/bin/mcp-metabase` на macOS/Linux.
Подставь его абсолютный путь как `<READY_ENTRYPOINT>` ниже. Повторная команда
`uv sync --locked` проверяет тот же lock; запуск MCP использует готовый runtime.
Обновление готовь в новом каталоге и переключай command после проверки;
предыдущий executable сохраняй для отката.

Основной способ настройки — параметры и token/password в локальном env-блоке
MCP-клиента. Конфиг с заполненными секретами не публикуй. Затем перезапусти клиент.

```toml
[mcp_servers.metabase_work]
command = "<READY_ENTRYPOINT>"
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

Один и тот же конфиг работает в Windows, macOS и Linux; меняется путь к готовому executable.

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
| `metabase_search` | Ищет объекты с pagination; `include_ranking_details=false` исключает только `scores` |
| `metabase_object_get` | Читает объект целиком или только раскладку дашборда (`view="layout"`) |
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

### Ответы `metabase_search`

По умолчанию `include_ranking_details=true` сохраняет прежний ответ. Для обычного
поиска передай `false`: из каждого элемента `items` будет удалено только поле
`scores`. Состав, порядок, остальные поля объектов и metadata pagination сохраняются.
Это уменьшает объём MCP-ответа; запрос к Metabase и его сетевой ответ не меняются.
Уменьшение bytes не является измерением расхода подписки.

### Ответы `metabase_object_get`

`view="full"` — значение по умолчанию; прежние ответы сохраняются.
У каждого ответа есть `origin`. Полезная нагрузка зависит от `object_type`:

| `object_type` | Где данные в structured result |
| --- | --- |
| `question`, `dashboard` | `object` — полный объект; рядом `object_type`, `object_id`, `state_sha256` |
| `collection`, `database`, `table`, `field` | Одноимённый ключ: `collection`, `database`, `table`, `field` |
| `field_values` | `values` и `truncated` на верхнем уровне; остальные metadata upstream сохранены |

`state_sha256` — hash сравниваемого состояния, а не raw JSON ответа. Для native
field filters сравнение исключает только служебный `lib/uuid` из options
`template-tags[].dimension = ["field", options, field_id]`: Metabase может
генерировать его заново при каждом чтении. Та же нормализация действует для
сессий, exact actions, копирования, проверки результата и отката, включая
вложенные карточки дашборда. Field id, остальные options, SQL, tag/parameter ids
и признаки редактирования сохраняются в сравнении. Исходные snapshots,
возвращаемые объекты и write payload не теряют UUID. После обновления MCP
прежние process-local планы и сессии нужно открыть заново.

Например, поля таблицы находятся в `structuredContent.table.fields`, а не
`structuredContent.object.fields`. `include_fields` применяется только к таблице;
`limit` ограничивает только `field_values`, не количество полей таблицы или
позиций дашборда. `object_id` — положительное целое; для коллекции также
разрешены поддерживаемые ссылки `root` и `trash`.

Для просмотра раскладки вызови:

```json
{"object_type": "dashboard", "object_id": 123, "view": "layout"}
```

Ответ содержит `origin`, `object_type`, `object_id`, `state_sha256` полного
состояния, `projection="layout"` и объект `layout`:

- `name`, `width` — значения дашборда, `null` при отсутствии;
- `tabs` — исходный список вкладок (включая дополнительные поля) либо исходный `null`;
- `dashcards` — все позиции в исходном порядке, без удаления повторных ссылок:
  `id`, `card_id`, `dashboard_tab_id`, `col`, `row`, `size_x`, `size_y`, `name`.
  Геометрия и `id` — целые; ссылки и имя могут быть `null`. Имя берётся из
  вложенной карточки; текстовые/виртуальные позиции без карточки сохраняются.

Пустой `dashcards=[]` возвращается как пустой список. Повреждённый inventory
вызывает ошибку вместо неполной раскладки. Режим применим только к дашборду;
неизвестный `view` и несовместимый `object_type` отклоняются до обращения к API.

Проекция не содержит SQL, вложенных полных карточек, visualization settings
или mappings и не является полным телом для записи. Для их изучения нужен
`view="full"`. Сервер по-прежнему делает один GET полного дашборда: проекция
уменьшает MCP-ответ, а не upstream HTTP payload. Общего cache нет; повторное
чтение получает актуальный объект, а проверки записей работают как прежде.

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
<READY_ENTRYPOINT> --check
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

## Хранение audit log

Audit JSONL: запись ≤16 KiB, месячный файл ≤16 MiB, всего ≤128 MiB и 24 файлов.
Для связи hash chain читается только последний bounded фрагмент файла.
Одновременные writers исключаются локальным exclusive lock. При заполнении
новые audit writes блокируются; перенеси старые завершённые месяцы в свой архив
и повтори операцию после проверки её результата. MCP сам записи не удаляет.
После аварийного завершения stale `.metabase-audit.write-lock` удаляют только
после остановки всех writers. Потеря возможности дописать audit после внешнего
действия не доказывает отсутствие его эффекта: сначала сделай readback.

Превышение response limit, повреждённый JSON или обрыв чтения после HTTP2xx
записи сохраняют outcome_unknown. Не повторяй запись без reconciliation.
