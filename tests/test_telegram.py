"""Tests for src/findmy/telegram.py's alert formatting and transport."""

from unittest.mock import MagicMock
from unittest.mock import patch

import pytest
import requests

from src.findmy.telegram import TELEGRAM_MAX_MESSAGE_LENGTH
from src.findmy.telegram import TelegramSendError
from src.findmy.telegram import send_movement_alert
from src.findmy.telegram import send_telegram_message


def _alert(**overrides) -> dict:
    base = {"id": 1, "device_name": "Steve's *Keys*", "threshold_m": 100.0, "device_icon": None}
    base.update(overrides)
    return base


@patch("src.findmy.telegram.TELEGRAM_CHAT_ID", "chat-1")
@patch("src.findmy.telegram.TELEGRAM_API_TOKEN", "token-1")
@patch("src.findmy.telegram.requests.post")
def test_send_telegram_message_posts_markdown(mock_post):
    mock_post.return_value = MagicMock(ok=True)
    send_telegram_message("*hello*")
    args, kwargs = mock_post.call_args
    assert args[0] == "https://api.telegram.org/bottoken-1/sendMessage"
    assert kwargs["data"]["chat_id"] == "chat-1"
    assert kwargs["data"]["text"] == "*hello*"
    assert kwargs["data"]["parse_mode"] == "Markdown"


@patch("src.findmy.telegram.TELEGRAM_CHAT_ID", "")
@patch("src.findmy.telegram.TELEGRAM_API_TOKEN", "")
@patch("src.findmy.telegram.requests.post")
def test_send_telegram_message_warns_and_skips_when_unconfigured(mock_post, caplog):
    with caplog.at_level("WARNING", logger="src.findmy.telegram"):
        send_telegram_message("*hello*")
    mock_post.assert_not_called()
    assert "not configured" in caplog.text


@patch("src.findmy.telegram.TELEGRAM_CHAT_ID", "chat-1")
@patch("src.findmy.telegram.TELEGRAM_API_TOKEN", "token-1")
@patch("src.findmy.telegram.requests.post")
def test_send_telegram_message_truncates_overlong(mock_post):
    mock_post.return_value = MagicMock(ok=True)
    send_telegram_message("x" * (TELEGRAM_MAX_MESSAGE_LENGTH + 50))
    text = mock_post.call_args.kwargs["data"]["text"]
    assert len(text) <= TELEGRAM_MAX_MESSAGE_LENGTH
    assert text.endswith("...(truncated)")


@patch("src.findmy.telegram.TELEGRAM_CHAT_ID", "chat-1")
@patch("src.findmy.telegram.TELEGRAM_API_TOKEN", "token-1")
@patch(
    "src.findmy.telegram.requests.post",
    side_effect=requests.ConnectionError("Max retries exceeded with url: /bottoken-1/sendMessage"),
)
def test_send_telegram_message_raises_transport_errors_without_the_token(mock_post):
    with pytest.raises(TelegramSendError) as excinfo:
        send_telegram_message("ping")
    assert "token-1" not in str(excinfo.value)
    assert excinfo.value.__cause__ is None and excinfo.value.__suppress_context__


@patch("src.findmy.telegram.TELEGRAM_CHAT_ID", "chat-1")
@patch("src.findmy.telegram.TELEGRAM_API_TOKEN", "token-1")
@patch("src.findmy.telegram.requests.post")
def test_send_telegram_message_raises_api_errors_with_status_and_description(mock_post):
    mock_post.return_value = MagicMock(
        ok=False, status_code=400, text='{"ok":false,"description":"Bad Request: can\'t parse entities"}'
    )
    with pytest.raises(TelegramSendError) as excinfo:
        send_telegram_message("ping")
    assert "400" in str(excinfo.value)
    assert "can't parse entities" in str(excinfo.value)
    assert "token-1" not in str(excinfo.value)


@patch("src.findmy.telegram.send_telegram_message")
def test_send_movement_alert_formats_name_and_distance(mock_send):
    send_movement_alert(_alert(), 250.4)
    message = mock_send.call_args.args[0]
    assert message.startswith("📍 *Steve's *\\**Keys*\\*** moved")
    assert "250m" in message
    assert "100m" in message


@patch("src.findmy.telegram.send_telegram_message")
def test_send_movement_alert_escapes_underscores_outside_the_bold_name(mock_send):
    send_movement_alert(_alert(device_name="my_tag", device_icon="_x_"), 250.4)
    assert mock_send.call_args.args[0].startswith("\\_x\\_ *my_tag* moved")


@patch("src.findmy.telegram.send_telegram_message")
def test_send_movement_alert_prefixes_the_devices_own_icon(mock_send):
    send_movement_alert(_alert(device_icon="🚲"), 250.4)
    assert mock_send.call_args.args[0].startswith("🚲 ")
