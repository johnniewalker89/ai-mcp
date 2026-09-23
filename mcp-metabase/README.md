# MCP Metabase

MCP-сервер для работы с Metabase через API key без браузерной авторизации. Он умеет
читать, создавать, копировать, изменять, перемещать в корзину и восстанавливать
карточки, дашборды и коллекции, а также выполнять ограниченный предпросмотр запросов.

Сервер предоставляет 15 инструментов. Произвольные REST-запросы и безвозвратное
удаление недоступны: действия `*_delete` перемещают объекты в Trash.

Exact `question_update` только названия/описания допускает обновление
`result_metadata` после выполнения сохранённого запроса: plan явно помечает
`query_metadata_independent=true`, PUT не отправляет эти метаданные. Все остальные
поля и edit timestamps остаются связаны с планом. Для SQL, параметров, визуализации,
самих метаданных, batch и lifecycle сначала выполни нужный запрос/preview, затем
подготовь exact plan; после `rejected_stale` прочитай состояние и подготовь новый.

## Установка из Git

Диагностика HTTP ошибок возвращает ограниченные `error_type`, ClickHouse
`code`/`symbol` и очищенное сообщение, когда они есть в ответе. SQL, stack trace
и полный response body не выводятся. Семантика `outcome_unknown` и запрет
автоматического повтора записи сохраняются.

При lifecycle дашборда неизменяемые старые mappings не блокируют архивирование;
редактирование `parameters`, `dashcards` или `tabs` по-прежнему проверяет mappings.
В `collection_batch_trash(empty_only=true)` timeline без активных событий
считается служебной оболочкой. Активные события блокируют empty-only; обычный
trash/restore непустых коллекций сохранён. Timeline/event state связан с планом,
неизвестное содержимое и неполный inventory отклоняются до записи.

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
| `metabase_notification_list` | Читает уведомления одной карточки с явной локальной пагинацией |
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
field filters сравнение исключает только служебные `lib/uuid` и
`lib/transformation-added-base-type` из options
`template-tags[].dimension = ["field", options, field_id]`: Metabase может
генерировать UUID заново при чтении и удалять маркер преобразования при записи.
Сами `base-type`, `effective-type` и остальные semantic options остаются связанными.
Та же нормализация действует для
сессий, exact actions, копирования, проверки результата и отката, включая
вложенные карточки дашборда. Field id, остальные options, SQL, tag/parameter ids
и признаки редактирования сохраняются в сравнении. Исходные snapshots,
возвращаемые объекты и write payload сохраняются без удаления этих полей. После обновления MCP
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

### Уведомления карточек и пакетная архивация

`metabase_notification_list(question_id, include_inactive=false, limit=20, offset=0)`
читает API-visible `notification/card`. Вместо `question_id` можно указать
`dashboard_id`: тогда читаются legacy Pulse dashboard subscriptions;
`include_inactive=true` включает архивные подписки. Ровно один selector обязателен.
`metabase_object_get(object_type="dashboard_subscription", object_id=...)`
возвращает безопасную проекцию `subscription` с расписаниями, каналами и получателями.
`settings_complete=false` явно отмечает неполный upstream-снимок. Pulse write пока
не поддерживается. Видимость API key не означает полноту по всему инстансу.
API фильтрует по объекту, но не поддерживает pagination:
`total`, `truncated`, `next_offset` относятся к локальной странице, а upstream
ответ ограничен HTTP byte cap; `upstream_pagination=false` явно отмечает это.
`metabase_object_get(object_type="notification", object_id=...)` возвращает
`notification` — безопасную проекцию расписаний/получателей — и полный state hash.
Channel credentials, hydrated cards/users и неизвестные blobs не выводятся.

Через `metabase_action_prepare` / `metabase_action_execute` доступны:

- `notification_create`: `arguments.body={question_id,cron_schedule,slack_recipient,active,send_once?}`.
  `active` — обязательный boolean: оба режима равноправны, default отсутствует.
  `active=true` сразу включает отправку по расписанию; `false` создаёт выключенную
  рассылку. Выбор связан с exact plan и authoritative readback; отдельное включение
  после создания не требуется. При неизвестном результате create не повторяется.
- `notification_update`: `arguments={notification_id,patch:{send_once?,active?,
  schedules?:[{subscription_id,cron_schedule,ui_display_type?}],
  recipients?:[{handler_id,recipient_id,value}]}}`.
  Расписание — Quartz cron из 6/7 полей; окончательная проверка у провайдера.
  `ui_display_type`: `cron/raw` или `cron/builder`. Timezone задаётся scheduler
  инстанса, отдельного поля timezone в этом API нет.
- `question_batch_trash`: `arguments={question_ids:[...]}`;
  `question_batch_restore`: тот же список плюс optional `collection_id` или
  `to_root=true`. Размер ограничен `max_batch_items` (по умолчанию 100;
  `METABASE_MCP_MAX_BATCH_ITEMS` позволяет установить 1–100); пустой/повторный inventory
  и неверное исходное archived-состояние отклоняются. Список проверяется до записи.
- `dashboard_batch_trash`: `arguments={dashboard_ids:[...]}`;
  `dashboard_batch_restore`: тот же список плюс optional `collection_id` или
  `to_root=true`. Те же лимит 100, проверка каждого объекта, частичный результат,
  readback/recovery без повторной записи и rollback применённых элементов.
  MCP записывает lifecycle-поля дашбордов, сохраняя layout, tabs и параметры.
  Каскадные эффекты провайдера на принадлежащее дашборду содержимое сохраняются.
  Восстановление не возобновляет подписки, отключённые
  провайдером при архивировании. Generic `batch` по-прежнему запрещает `/archived`.
- `collection_batch_trash`: `arguments={collection_ids:[...],empty_only?:bool}`.
  `empty_only=true` разрешает только деревья без активного содержимого; вложенные
  пустые коллекции допустимы. `false` (по умолчанию) допускает непустые деревья.
  Одиночные `collection_trash`/`collection_restore` сохраняют прежний контракт.
- `collection_batch_restore`: `arguments={collection_ids:[...],parent_id?,to_root?}`.
  Лимит — 100 исходных ID и `max_list_items` на каждое дерево. Дубликаты запрещены;
  выбранные потомки объединяются с выбранным предком до утверждения плана.
  `impact[].target.covered_requested_ids` показывает покрытие исходного списка.
  Восстановление внутрь выбранного дерева запрещено.

Collection batch требует подтверждённого superuser: обычный API-список может скрывать
недоступное содержимое. Системные/личные корневые коллекции не допускаются; отдельные
обычные папки внутри личной коллекции допустимы. Неполный список и неизвестная модель
содержимого отклоняются. Полное пустое первое окно v0.63 с `total=null` распознаётся
по пустому `data`, возвращённым `offset=0` и ожидаемому `limit`.
Полный состав проверяется в prepare, общем preflight и непосредственно перед каждым
PUT; изменение дерева даёт `stale_diagnostic.inventory_drift` с hashes. Это не
атомарный compare-and-swap Metabase: concurrent запись между последним GET и PUT
провайдером не блокируется. После записи `cascade_readback` подтверждает archived
каждого связанного объекта; неполный каскад не считается успехом и не повторяется
вслепую. Частичный результат сохраняет уже выполненные индексы даже при ошибке
следующего preflight; rollback восстанавливает исходный archived-флаг.

Сравнение question state игнорирует генерируемый `lib/uuid` в options MBQL clauses
(поля, агрегаты, фильтры, выражения, сортировка), как и служебные аннотации native
field filters. Ссылки на агрегаты сравниваются по позиции цели внутри своего stage,
поэтому смена UUID не мешает проверке, а смена цели обнаруживается. Неоднозначные
и неизвестные ссылки остаются bound. Literal data, stage/join identities, IDs полей,
типы, alias, имена/формулы выражений, query/parameters/visualization и timestamps
сохраняют проверку; неизвестные поля не исключаются. Все изменения comparison-only;
у field clauses также игнорируется исчезающий при сохранении provenance marker
`lib/transformation-added-base-type`, но сами `base-type`/`effective-type` проверяются;
исходные payload не переписываются. Batch `rejected_stale` содержит
`object_results[].stale_diagnostic`: ограниченные `changed_roots` и hashes до/после,
без значений полей или SQL. Реальные изменения останавливают пакет; до новой
подготовки нужно сверить актуальный состав, не повторять уже применённые элементы.

Update сохраняет неизвестные persisted поля и ID вложенных subscriptions,
handlers и recipients; не заменяет notification неполным payload. Получателя
можно менять только у существующего Slack raw-value recipient (`#channel`/`@user`).
Связанный `details.channel_id`, неполные templates и запрещённый провайдером
`email/handlebars-resource` отклоняются, чтобы не повредить существующие связи.
Prepare показывает изменение и побочные эффекты: включение/отключение notification
может отправить provider subscription emails; активное расписание может сработать позже.
Отдельных send/test endpoint нет. Notification не входит в work session.

Exact rollback поддерживает подтверждённые notification updates и проверяет свежий
state hash. Для batch сохранены пообъектные outcomes, rollback только verified
части и ограниченное восстановление внутри исходного плана при `outcome_unknown`.
Архивация карточки может отключить зависимые уведомления на стороне Metabase;
восстановление/rollback карточки не обещает отмену таких побочных эффектов.
Новые notifications деактивируются через `notification_update(active=false)`;
permanent delete не добавлен.

### Действия по типам объектов

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

У старых карточек Metabase может возвращать `parameters` и `parameter_mappings`
как `null`. При подготовке и проверке изменений MCP трактует их как пустые массивы;
архивирование и восстановление не записывают эти поля. Изменение непустых параметров
по-прежнему блокирует устаревший план, остальные неверные типы отклоняются.

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
