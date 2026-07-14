# mcp_discord

Read/post-only MCP-сервер для безопасной работы с Discord-каналами сообщества.

## Возможности v0.2

- `list_allowed_scope` - показать env-файл и allowlist без токена.
- `list_channels` - список текстовых/news/forum-каналов разрешенного сервера.
- `get_recent_messages` - последние сообщения разрешенного канала.
- `send_message` - отправка обычного сообщения.
- `send_embed` - отправка простого embed-анонса.
- `create_text_channel` - создать текстовый канал в разрешенном сервере.
- `update_channel` - изменить имя/topic разрешенного канала.
- `pin_message` / `unpin_message` - закрепить или открепить сообщение.
- `edit_own_message` / `delete_own_message` - редактировать или удалить только сообщение этого бота.

## Безопасность

- Используется Discord Bot token, не пользовательский аккаунт.
- Токен хранится только локально в env-файле или MCP config.
- Серверы и каналы ограничиваются allowlist.
- `allowed_mentions` отключен для отправки сообщений.
- Изменяющие операции требуют `approved=true`.
- В v0.2 нет редактирования ролей, банов, создания webhook, массовых операций или удаления чужих сообщений.

## Установка из Git

```toml
[mcp_servers.mcp_discord]
command = "uvx"
args = [
  "--refresh",
  "--from",
  "git+https://github.com/johnniewalker89/ai-mcp.git#subdirectory=mcp_discord",
  "mcp-discord"
]

[mcp_servers.mcp_discord.env]
DISCORD_MCP_ENV_FILE = "C:\\Users\\Admin\\.codex\\discord-mcp.env"
```

## Env-файл

```dotenv
DISCORD_MCP_BOT_TOKEN=<local only>
DISCORD_MCP_ALLOWED_GUILD_IDS=<server id>
DISCORD_MCP_ALLOWED_CHANNEL_IDS=<channel id>
DISCORD_MCP_RELEASE_CHANNEL_IDS=<release channel id>
```

Для первого теста лучше использовать приватный канал `#bot-lab`.

## Discord permissions

Для v0.2 нужны:

- `View Channels`
- `Send Messages`
- `Read Message History`
- `Embed Links`
- `Manage Channels`
- `Manage Messages`

Не выдавайте `Administrator`, `Manage Roles`, `Mention Everyone`, `Kick Members` или `Ban Members` без отдельного решения.
