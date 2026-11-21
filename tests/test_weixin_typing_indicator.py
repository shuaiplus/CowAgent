"""Typing is best-effort and never blocks the Weixin message handler."""
import queue
import threading
from unittest.mock import patch

import pytest
import requests

from bridge.context import Context, ContextType
from channel.chat_channel import ChatChannel
from channel.weixin.typing_indicator import TypingIndicator
from channel.weixin.weixin_api import WeixinApi
from channel.weixin.weixin_channel import WeixinChannel


class FakeAPI:
    def __init__(self):
        self.calls = queue.Queue()
        self.config_enter = threading.Event()
        self.config_gate = threading.Event()
        self.config_gate.set()
        self.start_enter = threading.Event()
        self.start_gate = threading.Event()
        self.start_gate.set()
        self.stop_enter = threading.Event()
        self.stop_gate = threading.Event()
        self.stop_gate.set()
        self.config_result = {"ret": 0, "typing_ticket": "ticket"}
        self.start_result = {"ret": 0}
        self.stop_result = {"ret": 0}

    def get_config(self, receiver, token):
        self.calls.put(("config", receiver, token))
        self.config_enter.set()
        assert self.config_gate.wait(2)
        if isinstance(self.config_result, Exception):
            raise self.config_result
        return self.config_result

    def send_typing(self, receiver, ticket, status):
        self.calls.put((status, receiver, ticket))
        event, gate = (self.start_enter, self.start_gate) if status == 1 else (self.stop_enter, self.stop_gate)
        event.set()
        assert gate.wait(2)
        result = self.start_result if status == 1 else self.stop_result
        if isinstance(result, Exception):
            raise result
        return result

    def next(self):
        return self.calls.get(timeout=2)


def wait_empty(manager):
    # Predicate checked under the same lock as the worker's removal.
    for _ in range(2000):
        with manager._condition:
            if not manager._states:
                return
        threading.Event().wait(.001)
    pytest.fail("typing worker did not exit")


def acquire(manager, api, receiver="user"):
    return manager.acquire(receiver, api, "context")


def test_start_refresh_and_stop():
    api = FakeAPI()
    manager = TypingIndicator()
    assert manager._refresh_seconds == 5.0
    lease = acquire(manager, api)
    assert api.next() == ("config", "user", "context")
    assert api.next() == (1, "user", "ticket")
    with manager._condition:
        manager._states["user"].next_attempt = 0
        manager._condition.notify_all()
    assert api.next() == (1, "user", "ticket")
    manager.release(lease)
    assert api.next() == (2, "user", "ticket")
    wait_empty(manager)


def test_overlapping_leases_share_worker_and_final_release_stops():
    api = FakeAPI()
    manager = TypingIndicator(refresh_seconds=100)
    a = acquire(manager, api)
    assert api.next()[0] == "config"
    assert api.next()[0] == 1
    b = acquire(manager, api)
    manager.release(a)
    manager.release(a)  # a duplicate release must not end b's lease
    with manager._condition:
        assert manager._states["user"].count == 1
    assert api.calls.empty()
    manager.release(b)
    assert api.next()[0] == 2
    wait_empty(manager)


def test_slow_config_after_release_cannot_start():
    api = FakeAPI()
    api.config_gate.clear()
    manager = TypingIndicator()
    a = acquire(manager, api)
    assert api.next()[0] == "config"
    manager.release(a)
    api.config_gate.set()
    wait_empty(manager)
    assert api.calls.empty()


def test_new_lease_while_old_config_is_in_flight_discards_old_result():
    api = FakeAPI()
    api.config_gate.clear()
    manager = TypingIndicator(refresh_seconds=100)
    a = acquire(manager, api)
    assert api.next()[0] == "config"
    manager.release(a)
    b = manager.acquire("user", api, "new-context")
    api.config_gate.set()
    assert api.next() == ("config", "user", "new-context")
    assert api.next()[0] == 1
    manager.release(b)
    assert api.next()[0] == 2
    wait_empty(manager)


def test_inflight_start_and_stop_serialize_with_new_acquisition():
    api = FakeAPI()
    api.start_gate.clear()
    api.stop_gate.clear()
    manager = TypingIndicator(refresh_seconds=100)
    a = acquire(manager, api)
    assert api.next()[0] == "config"
    assert api.next()[0] == 1
    manager.release(a)
    b = acquire(manager, api)
    api.start_gate.set()
    assert api.next()[0] == 2
    assert api.calls.empty()
    # Stop is in flight; a new generation cannot start until it completes.
    api.stop_gate.set()
    assert api.next()[0] == "config"
    assert api.next()[0] == 1
    manager.release(b)
    assert api.next()[0] == 2
    wait_empty(manager)


def test_stop_in_flight_then_new_lease_is_restarted_after_stop():
    api = FakeAPI()
    api.stop_gate.clear()
    manager = TypingIndicator(refresh_seconds=100)
    a = acquire(manager, api)
    assert api.next()[0] == "config"
    assert api.next()[0] == 1
    manager.release(a)
    assert api.next()[0] == 2
    b = acquire(manager, api)
    assert api.calls.empty()
    api.stop_gate.set()
    assert api.next()[0] == "config"
    assert api.next()[0] == 1
    manager.release(b)
    assert api.next()[0] == 2
    wait_empty(manager)


@pytest.mark.parametrize("result", [{}, {"ret": -1, "typing_ticket": "ticket"},
                                    {"errcode": 9, "typing_ticket": "ticket"},
                                    requests.Timeout("secret-token")])
def test_config_failure_does_not_start(result):
    api = FakeAPI()
    api.config_result = result
    manager = TypingIndicator(refresh_seconds=100)
    lease = acquire(manager, api)
    assert api.next()[0] == "config"
    manager.release(lease)
    wait_empty(manager)
    assert api.calls.empty()


def test_ambiguous_start_timeout_still_stops_and_does_not_leak_details(caplog):
    api = FakeAPI()
    api.start_result = requests.Timeout("secret-token secret-ticket private-id")
    manager = TypingIndicator(refresh_seconds=100)
    lease = acquire(manager, api)
    assert api.next()[0] == "config"
    assert api.next()[0] == 1
    manager.release(lease)
    assert api.next()[0] == 2
    wait_empty(manager)
    assert "secret-token" not in caplog.text
    assert "secret-ticket" not in caplog.text
    assert "private-id" not in caplog.text


def test_failure_retries_on_heartbeat_then_stops():
    api = FakeAPI()
    api.config_result = {"ret": 1}
    manager = TypingIndicator(refresh_seconds=100)
    lease = acquire(manager, api)
    assert api.next()[0] == "config"
    # Wait until the first failed attempt is completely handled before waking it.
    for _ in range(2000):
        with manager._condition:
            state = manager._states["user"]
            if state.next_attempt:
                state.next_attempt = 0
                manager._condition.notify_all()
                break
        threading.Event().wait(.001)
    assert api.next()[0] == "config"
    manager.release(lease)
    wait_empty(manager)


def test_independent_receivers_and_channels_do_not_block_each_other():
    api = FakeAPI()
    api.config_gate.clear()
    manager = TypingIndicator(refresh_seconds=100)
    a = acquire(manager, api, "a")
    assert api.next()[1] == "a"
    b_api = FakeAPI()
    b = acquire(manager, b_api, "b")
    assert b_api.next()[0] == "config"
    assert b_api.next()[0] == 1
    other = TypingIndicator(refresh_seconds=100)
    c = acquire(other, b_api, "b")
    assert b_api.next()[0] == "config"
    assert b_api.next()[0] == 1
    manager.release(a)
    api.config_gate.set()
    manager.release(b)
    other.release(c)
    assert b_api.next()[0] == 2
    assert b_api.next()[0] == 2
    wait_empty(manager)
    wait_empty(other)


def test_close_invalidates_leases_and_reopen_serializes_old_stop():
    old, new = FakeAPI(), FakeAPI()
    old.stop_gate.clear()
    manager = TypingIndicator(refresh_seconds=100)
    lease = acquire(manager, old)
    assert old.next()[0] == "config"
    assert old.next()[0] == 1
    manager.close()
    assert old.next()[0] == 2
    assert acquire(manager, new) is None
    manager.open(threading.Event())
    newer = acquire(manager, new)
    manager.release(lease)  # stale release cannot release newer lease
    assert new.calls.empty()
    old.stop_gate.set()
    assert new.next()[0] == "config"
    assert new.next()[0] == 1
    manager.release(newer)
    assert new.next()[0] == 2
    wait_empty(manager)
    stop = threading.Event()
    stop.set()
    manager.close()
    manager.open(stop)
    assert acquire(manager, new) is None


def test_relogin_while_config_in_flight_never_starts_old_generation():
    old, new = FakeAPI(), FakeAPI()
    old.config_gate.clear()
    manager = TypingIndicator(refresh_seconds=100)
    a = acquire(manager, old)
    assert old.next()[0] == "config"
    manager.close()
    manager.open(threading.Event())
    b = acquire(manager, new)
    old.config_gate.set()
    assert new.next()[0] == "config"
    assert new.next()[0] == 1
    assert old.calls.empty()
    manager.release(a)
    manager.release(b)
    assert new.next()[0] == 2
    wait_empty(manager)


def channel(api):
    cls = WeixinChannel.__wrapped__
    ch = object.__new__(cls)
    ch.api = api
    ch._typing = TypingIndicator(refresh_seconds=100)
    ch._context_tokens = {"user": "stored"}
    ch._context_tokens_lock = threading.Lock()
    ch._stop_event = threading.Event()
    ch._credentials_path = ""
    return ch


@pytest.mark.parametrize("kind", [ContextType.TEXT, ContextType.VOICE, ContextType.IMAGE_CREATE])
def test_handle_nonblocking_and_covers_full_parent_lifecycle(kind):
    api = FakeAPI()
    api.config_gate.clear()
    ch = channel(api)
    context = Context(kind, "content", {"receiver": "user"})
    parent_called = threading.Event()
    def parent(_):
        assert api.config_enter.wait(2)
        parent_called.set()
    with patch.object(ChatChannel, "_handle", side_effect=parent):
        ch._handle(context)
    assert parent_called.is_set()  # returns even though get_config is blocked
    assert api.next() == ("config", "user", "stored")
    api.config_gate.set()
    wait_empty(ch._typing)
    assert api.calls.empty()  # no stale start after parent returned


def test_handle_finally_releases_on_exception_or_cancellation():
    api = FakeAPI()
    ch = channel(api)
    context = Context(ContextType.TEXT, "hello", {"receiver": "user"})
    def parent(_):
        assert api.next()[0] == "config"
        assert api.next()[0] == 1
        raise KeyboardInterrupt()
    with patch.object(ChatChannel, "_handle", side_effect=parent):
        with pytest.raises(KeyboardInterrupt):
            ch._handle(context)
    assert api.next()[0] == 2
    wait_empty(ch._typing)


def test_interim_and_media_sends_do_not_release_until_parent_finishes():
    api = FakeAPI()
    ch = channel(api)
    context = Context(ContextType.TEXT, "hello", {"receiver": "user"})
    def parent(_):
        assert api.next()[0] == "config"
        assert api.next()[0] == 1
        # Represent interim and media delivery inside the base handler.
        with ch._typing._condition:
            assert ch._typing._states["user"].count == 1
        assert api.calls.empty()
    with patch.object(ChatChannel, "_handle", side_effect=parent):
        ch._handle(context)
    assert api.next()[0] == 2
    wait_empty(ch._typing)


def test_missing_context_or_credentials_skip_typing():
    api = FakeAPI()
    ch = channel(api)
    with patch.object(ChatChannel, "_handle") as parent:
        ch._handle(None)
        ch._handle(Context(ContextType.TEXT, "", {"receiver": "user"}))
        ch._handle(Context(ContextType.TEXT, "hello", {"receiver": "unknown"}))
        assert parent.call_count == 3
    assert api.calls.empty()


def test_channel_stop_closes_indicator_without_waiting_for_stop_request():
    api = FakeAPI()
    api.stop_gate.clear()
    ch = channel(api)
    lease = acquire(ch._typing, api)
    assert api.next()[0] == "config"
    assert api.next()[0] == 1
    with patch.object(ch, "_hold_login"):
        ch.stop()
    assert ch._stop_event.is_set()
    assert api.next()[0] == 2
    assert acquire(ch._typing, api) is None
    ch._typing.release(lease)
    api.stop_gate.set()
    wait_empty(ch._typing)


def test_channel_failed_relogin_stays_closed():
    api = FakeAPI()
    api.base_url = "https://example.invalid"
    ch = channel(api)
    lease = acquire(ch._typing, api)
    assert api.next()[0] == "config"
    assert api.next()[0] == 1
    with patch.object(ch, "_qr_login", return_value=None):
        assert not ch._relogin()
    assert api.next()[0] == 2
    assert acquire(ch._typing, api) is None
    ch._typing.release(lease)
    wait_empty(ch._typing)


def test_stop_request_failure_does_not_block_later_generation():
    api = FakeAPI()
    api.stop_gate.clear()
    api.stop_result = requests.Timeout("secret")
    manager = TypingIndicator(refresh_seconds=100)
    first = acquire(manager, api)
    assert api.next()[0] == "config"
    assert api.next()[0] == 1
    manager.release(first)
    assert api.next()[0] == 2
    second = acquire(manager, api)
    api.stop_gate.set()
    assert api.next()[0] == "config"
    assert api.next()[0] == 1
    manager.release(second)
    assert api.next()[0] == 2
    wait_empty(manager)


def test_typing_api_has_bounded_timeout_and_suppresses_unsafe_low_level_logging():
    api = WeixinApi()
    with patch.object(api, "_post", return_value={}) as post:
        api.get_config("user", "context")
        api.send_typing("user", "ticket", 2)
    assert all(c.kwargs == {"timeout": 10, "log_errors": False} for c in post.call_args_list)
