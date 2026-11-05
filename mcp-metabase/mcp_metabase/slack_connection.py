"""Состояние одного Slack-индикатора и публичное подтверждение действующего подключения."""

from __future__ import annotations

from typing import Any

from mcp_metabase.normalization import MutationValidationError

SLACK_VALID_KEY = "slack-token-valid?"
SLACK_VALID_PATH = "/api/setting/slack-token-valid%3F"
SLACK_APP_INFO_PATH = "/api/slack/app-info"


def indicator_state(value: Any) -> dict[str, bool | None]:
    """Принимает точное значение индикатора, различая false, null и некорректный ответ."""
    if value is not None and type(value) is not bool:
        raise MutationValidationError("Slack indicator must be a boolean or null.")
    return {SLACK_VALID_KEY: value}


def app_identity(raw: Any) -> dict[str, str]:
    """Проверяет результаты auth.test/bots.info по непустым app_id и team_id."""
    if not isinstance(raw, dict):
        raise MutationValidationError("Slack app-info did not confirm the configured connection.")
    result = {}
    for key in ("app_id", "team_id"):
        value = raw.get(key)
        if (
            not isinstance(value, str)
            or not value
            or len(value) > 254
            or any(character.isspace() or not character.isprintable() for character in value)
        ):
            raise MutationValidationError("Slack app-info did not confirm auth.test and bots.info.")
        result[key] = value
    return result
