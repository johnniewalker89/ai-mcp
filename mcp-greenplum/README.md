# mcp-greenplum

Privileged entrypoint: `mcp-greenplum-privileged`.

`mcp-greenplum` remains available as a backward-compatible alias.

MCP-сервер для Greenplum 6. Позволяет AI-ассистентам (Claude Desktop, Cursor и др.)
читать метаданные и выполнять SELECT-запросы к Greenplum через протокол MCP.

## Возможности

| Инструмент | Описание |
|---|---|
| `list_databases` | Список баз данных на сервере |
| `list_schemas` | Список схем в базе данных |
| `list_tables` | Таблицы схемы с типом хранения (heap/ao_row/ao_column), политикой дистрибуции и ключами распределения |
| `describe_table` | Колонки таблицы с типами данных, nullable и комментариями |
| `run_select_query` | Выполнение SELECT/WITH/EXPLAIN запросов (только чтение) |
| `run_query` | Выполнение одного SQL statement; write/drop включаются env-флагами |
| `start_async_query` | Запуск одного statement в фоне для длинных approved sandbox actions |
| `get_query_status` | Получение статуса/результата фонового statement по `job_id` |
| `cancel_query` | Отмена собственного фонового statement этого MCP-сервера |

## Требования

Нужен **uv** — менеджер Python-пакетов.

**macOS** — выполни в Terminal:
```bash
# через Homebrew (проще)
brew install uv

# или через официальный установщик
curl -LsSf https://astral.sh/uv/install.sh | sh
```

**Windows** — выполни в PowerShell:
```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

После установки перезапусти терминал и проверь: `uv --version`

## Переменные окружения

### Подключение к Greenplum (обязательные)

| Переменная | Описание |
|---|---|
| `GREENPLUM_HOST` | Хост Greenplum |
| `GREENPLUM_USER` | Пользователь |
| `GREENPLUM_PASSWORD` | Пароль |

### Подключение к Greenplum (опциональные)

| Переменная | По умолчанию | Описание |
|---|---|---|
| `GREENPLUM_PORT` | `5432` | Порт |
| `GREENPLUM_DATABASE` | `postgres` | База данных по умолчанию |
| `GREENPLUM_SSLMODE` | `prefer` | SSL-режим (`disable`/`prefer`/`require`) |
| `GREENPLUM_CONNECT_TIMEOUT` | `300` | Таймаут подключения, секунды |
| `GREENPLUM_ALLOW_WRITE_ACCESS` | `false` | Разрешить DDL/DML через `run_query` |
| `GREENPLUM_ALLOW_DROP` | `false` | Разрешить DROP/TRUNCATE через `run_query` |

### Настройки MCP-сервера

| Переменная | По умолчанию | Описание |
|---|---|---|
| `GREENPLUM_MCP_SERVER_TRANSPORT` | `stdio` | Транспорт: `stdio`, `http`, `sse` |
| `GREENPLUM_MCP_QUERY_TIMEOUT` | `300` | Таймаут запроса, секунды |
| `GREENPLUM_MCP_BIND_HOST` | `127.0.0.1` | Хост для HTTP/SSE транспорта |
| `GREENPLUM_MCP_BIND_PORT` | `8000` | Порт для HTTP/SSE транспорта |
| `GREENPLUM_MCP_AUTH_TOKEN` | — | Bearer-токен для HTTP/SSE (обязателен если не отключена аутентификация) |
| `GREENPLUM_MCP_AUTH_DISABLED` | `false` | Отключить аутентификацию для HTTP/SSE (только для разработки) |

## Подключение к Claude Desktop

Открой файл конфига:
- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
- Windows: `%APPDATA%\Claude\claude_desktop_config.json`

Клонировать репозиторий не нужно — `uvx` скачает и запустит пакет автоматически.

**macOS:**
```json
{
  "mcpServers": {
    "greenplum": {
      "command": "uvx",
      "args": [
        "--from", "git+https://gitlab.x340.org/bi/mcp-greenplum.git",
        "mcp-greenplum"
      ],
      "env": {
        "GREENPLUM_HOST": "bi-gptest82.x340.org",
        "GREENPLUM_PORT": "5432",
        "GREENPLUM_USER": "your_user",
        "GREENPLUM_PASSWORD": "your_password",
        "GREENPLUM_DATABASE": "profi",
        "GREENPLUM_SSLMODE": "prefer",
        "GREENPLUM_MCP_QUERY_TIMEOUT": "300"
      }
    }
  }
}
```

**Windows** — нужно указать полный путь до `uvx.exe`. Узнать его можно командой `where uvx` в PowerShell:
```json
{
  "mcpServers": {
    "greenplum": {
      "command": "C:\\Users\\your_username\\.local\\bin\\uvx.exe",
      "args": [
        "--from", "git+https://gitlab.x340.org/bi/mcp-greenplum.git",
        "mcp-greenplum"
      ],
      "env": {
        "GREENPLUM_HOST": "bi-gptest82.x340.org",
        "GREENPLUM_PORT": "5432",
        "GREENPLUM_USER": "your_user",
        "GREENPLUM_PASSWORD": "your_password",
        "GREENPLUM_DATABASE": "profi",
        "GREENPLUM_SSLMODE": "prefer",
        "GREENPLUM_MCP_QUERY_TIMEOUT": "300"
      }
    }
  }
}
```

После изменения конфига — перезапустить Claude Desktop.

## Подключение к Claude Code

MCP-серверы в Claude Code можно добавить тремя способами: через CLI-команду, через глобальный конфиг или через конфиг проекта.

### Способ 1: CLI-команда (рекомендуется)

> ⚠️ Используй синтаксис `--env=KEY=VALUE` (через `=`), а не `--env KEY=VALUE`. Флаг `--env` принимает несколько значений, и без `=` парсер может «съесть» имя сервера как очередное значение env.

Выполни в терминале:

**macOS:**

```bash
claude mcp add --scope user \
  --env=GREENPLUM_HOST=bi-gptest82.x340.org \
  --env=GREENPLUM_PORT=5432 \
  --env=GREENPLUM_USER=your_user \
  --env=GREENPLUM_PASSWORD=your_password \
  --env=GREENPLUM_DATABASE=profi \
  --env=GREENPLUM_SSLMODE=prefer \
  --env=GREENPLUM_MCP_QUERY_TIMEOUT=300 \
  greenplum \
  -- uvx --from git+https://gitlab.x340.org/bi/mcp-greenplum.git mcp-greenplum
```

**Windows:**

```powershell
claude mcp add --scope user `
  --env=GREENPLUM_HOST=bi-gptest82.x340.org `
  --env=GREENPLUM_PORT=5432 `
  --env=GREENPLUM_USER=your_user `
  --env=GREENPLUM_PASSWORD=your_password `
  --env=GREENPLUM_DATABASE=profi `
  --env=GREENPLUM_SSLMODE=prefer `
  --env=GREENPLUM_MCP_QUERY_TIMEOUT=300 `
  greenplum `
  -- C:\Users\your_username\.local\bin\uvx.exe --from git+https://gitlab.x340.org/bi/mcp-greenplum.git mcp-greenplum
```

Флаг `--scope user` добавляет сервер в глобальный конфиг (`~/.claude.json`) — доступен во всех проектах. Замени на `--scope project`, чтобы ограничить текущим проектом (конфиг сохранится в `.claude/settings.json`).

### Способ 2: Ручное редактирование конфига

Глобальный конфиг находится в `~/.claude.json`. Найди или создай секцию `mcpServers` и добавь:

**macOS:**

```json
{
  "mcpServers": {
    "greenplum": {
      "command": "uvx",
      "args": [
        "--from", "git+https://gitlab.x340.org/bi/mcp-greenplum.git",
        "mcp-greenplum"
      ],
      "env": {
        "GREENPLUM_HOST": "bi-gptest82.x340.org",
        "GREENPLUM_PORT": "5432",
        "GREENPLUM_USER": "your_user",
        "GREENPLUM_PASSWORD": "your_password",
        "GREENPLUM_DATABASE": "profi",
        "GREENPLUM_SSLMODE": "prefer",
        "GREENPLUM_MCP_QUERY_TIMEOUT": "300"
      }
    }
  }
}
```

**Windows:**

```json
{
  "mcpServers": {
    "greenplum": {
      "command": "C:\\Users\\your_username\\.local\\bin\\uvx.exe",
      "args": [
        "--from", "git+https://gitlab.x340.org/bi/mcp-greenplum.git",
        "mcp-greenplum"
      ],
      "env": {
        "GREENPLUM_HOST": "bi-gptest82.x340.org",
        "GREENPLUM_PORT": "5432",
        "GREENPLUM_USER": "your_user",
        "GREENPLUM_PASSWORD": "your_password",
        "GREENPLUM_DATABASE": "profi",
        "GREENPLUM_SSLMODE": "prefer",
        "GREENPLUM_MCP_QUERY_TIMEOUT": "300"
      }
    }
  }
}
```

### Способ 3: Конфиг проекта

Для изоляции настроек на уровне репозитория создай файл `.claude/settings.json` в корне проекта:

```json
{
  "mcpServers": {
    "greenplum": {
      "command": "uvx",
      "args": [
        "--from", "git+https://gitlab.x340.org/bi/mcp-greenplum.git",
        "mcp-greenplum"
      ],
      "env": {
        "GREENPLUM_HOST": "bi-gptest82.x340.org",
        "GREENPLUM_PORT": "5432",
        "GREENPLUM_USER": "your_user",
        "GREENPLUM_PASSWORD": "your_password",
        "GREENPLUM_DATABASE": "profi",
        "GREENPLUM_SSLMODE": "prefer",
        "GREENPLUM_MCP_QUERY_TIMEOUT": "300"
      }
    }
  }
}
```

> ⚠️ Не коммить этот файл в репозиторий, если в нём содержатся реальные пароли. Добавь `.claude/settings.json` в `.gitignore`.

### Проверка подключения

После добавления сервера проверь, что он подключился:

```bash
claude mcp list
```

Сервер `greenplum` должен появиться в списке со статусом активного.

## Подключение к Cursor

Открой **Settings → MCP** и добавь новый сервер с теми же параметрами что и для Claude Desktop.

## Запуск напрямую (для отладки)

```bash
GREENPLUM_HOST=bi-gptest82.x340.org \
GREENPLUM_USER=your_user \
GREENPLUM_PASSWORD=your_password \
GREENPLUM_DATABASE=profi \
uvx --from git+https://gitlab.x340.org/bi/mcp-greenplum.git mcp-greenplum
```

## Проверка подключения (HTTP-режим)

```bash
GREENPLUM_MCP_SERVER_TRANSPORT=http \
GREENPLUM_MCP_AUTH_DISABLED=true \
GREENPLUM_HOST=bi-gptest82.x340.org \
GREENPLUM_USER=your_user \
GREENPLUM_PASSWORD=your_password \
GREENPLUM_DATABASE=profi \
uvx --from git+https://gitlab.x340.org/bi/mcp-greenplum.git mcp-greenplum &

curl http://127.0.0.1:8000/health
```

## Подключение к Codex (OpenAI)

Файл конфига: `~/.codex/config.toml` (глобальный) или `.codex/config.toml` (проектный).

Добавь секцию:

**macOS / Linux:**

```toml
[mcp_servers.greenplum]
command = "uvx"
args = ["--from", "git+https://gitlab.x340.org/bi/mcp-greenplum.git", "mcp-greenplum"]

[mcp_servers.greenplum.env]
GREENPLUM_HOST = "bi-gptest82.x340.org"
GREENPLUM_PORT = "5432"
GREENPLUM_USER = "your_user"
GREENPLUM_PASSWORD = "your_password"
GREENPLUM_DATABASE = "profi"
GREENPLUM_SSLMODE = "prefer"
GREENPLUM_MCP_QUERY_TIMEOUT = "300"
```

**Windows** — укажи полный путь до `uvx.exe` (`where uvx` в PowerShell):

```toml
[mcp_servers.greenplum]
command = "C:\\Users\\your_username\\.local\\bin\\uvx.exe"
args = ["--from", "git+https://gitlab.x340.org/bi/mcp-greenplum.git", "mcp-greenplum"]

[mcp_servers.greenplum.env]
GREENPLUM_HOST = "bi-gptest82.x340.org"
GREENPLUM_PORT = "5432"
GREENPLUM_USER = "your_user"
GREENPLUM_PASSWORD = "your_password"
GREENPLUM_DATABASE = "profi"
GREENPLUM_SSLMODE = "prefer"
GREENPLUM_MCP_QUERY_TIMEOUT = "300"
```

Либо через CLI:

```bash
codex mcp add greenplum \
  --env GREENPLUM_HOST=bi-gptest82.x340.org \
  --env GREENPLUM_PORT=5432 \
  --env GREENPLUM_USER=your_user \
  --env GREENPLUM_PASSWORD=your_password \
  --env GREENPLUM_DATABASE=profi \
  --env GREENPLUM_SSLMODE=prefer \
  --env GREENPLUM_MCP_QUERY_TIMEOUT=300 \
  -- uvx --from git+https://gitlab.x340.org/bi/mcp-greenplum.git mcp-greenplum
```

## Безопасность

- Все запросы выполняются в транзакции `READ ONLY` — запись в БД невозможна
- Разрешены только `SELECT`, `WITH`, `EXPLAIN`
- Запрещены множественные выражения в одном запросе (`;` внутри запроса)
- Для HTTP/SSE-транспорта требуется Bearer-токен
