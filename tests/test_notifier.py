import json

from app.integrations import TelegramNotifier


class FakeTransport:
    def __init__(self) -> None:
        self.url = ""
        self.body = b""

    def post(self, url: str, body: bytes, headers: dict[str, str]) -> None:
        self.url = url
        self.body = body
        assert headers["Content-Type"] == "application/json"


def test_disabled_notifier_is_a_noop() -> None:
    assert TelegramNotifier("", "").send("ignored") is False


def test_notifier_sends_bounded_summary_without_repr_secrets() -> None:
    transport = FakeTransport()
    notifier = TelegramNotifier("super-secret-value", "private-destination", transport)
    assert notifier.send("x" * 5000)
    assert transport.url.endswith("/botsuper-secret-value/sendMessage")
    assert len(json.loads(transport.body)["text"]) <= 3920
    assert json.loads(transport.body)["text"].startswith("<b>Remux Dispatcher</b>\n")
    assert json.loads(transport.body)["parse_mode"] == "HTML"
    assert "super-secret-value" not in repr(notifier)
    assert "private-destination" not in repr(notifier)
