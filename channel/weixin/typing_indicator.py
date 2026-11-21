"""Best-effort Weixin typing leases, isolated from the message handler.

A single daemon worker serializes all requests for a receiver, including across
lease generations and login changes. No network I/O happens under the lock.
"""
import threading
import time

from common.log import logger


_REFRESH_SECONDS = 5.0


def _ok(response):
    return isinstance(response, dict) and not response.get("ret") and not response.get("errcode")


class _State:
    def __init__(self):
        self.count = 0
        self.generation = 0
        self.api = None
        self.token = ""
        self.ticket = ""
        self.ticket_api = None
        self.ticket_generation = 0
        # A timed-out start may still have reached the server; stop it too.
        self.started = False
        self.next_attempt = 0.0


class _Lease:
    def __init__(self, state, generation):
        self.state = state
        self.generation = generation
        self.released = False


class TypingIndicator:
    def __init__(self, refresh_seconds=_REFRESH_SECONDS):
        self._condition = threading.Condition()
        self._states = {}
        self._enabled = True
        self._refresh_seconds = refresh_seconds

    def open(self, stop_event):
        with self._condition:
            if not stop_event.is_set():
                self._enabled = True

    def close(self):
        """Invalidate leases immediately; workers finish best-effort stops asynchronously."""
        with self._condition:
            self._enabled = False
            for state in self._states.values():
                state.count = 0
                state.generation += 1
            self._condition.notify_all()

    def acquire(self, receiver, api, token, is_current=None):
        if not receiver or not api or not token:
            return None
        with self._condition:
            if not self._enabled or (is_current is not None and not is_current(api)):
                return None
            state = self._states.get(receiver)
            if state is None:
                state = self._states[receiver] = _State()
                try:
                    threading.Thread(target=self._run, args=(receiver, state),
                                     daemon=True, name="weixin-typing").start()
                except Exception:
                    del self._states[receiver]
                    logger.warning("[Weixin] Could not start typing worker")
                    return None
            if not state.count:
                state.generation += 1
                state.api = api
                state.token = token
                state.next_attempt = 0.0
            state.count += 1
            lease = _Lease(state, state.generation)
            self._condition.notify_all()
            return lease

    def release(self, lease):
        if lease is None:
            return
        with self._condition:
            if lease.released:
                return
            lease.released = True
            state = lease.state
            if state.generation == lease.generation and state.count:
                state.count -= 1
                self._condition.notify_all()

    def _run(self, receiver, state):
        while True:
            with self._condition:
                # A start in flight is always followed by its stop before a
                # newer generation can obtain or send a different ticket.
                stale = state.ticket_generation != state.generation
                if state.ticket and state.started and (not state.count or stale):
                    action = "stop"
                    api, ticket = state.ticket_api, state.ticket
                elif not state.count:
                    del self._states[receiver]
                    return
                elif stale and state.ticket:
                    state.ticket = ""
                    state.ticket_api = None
                    continue
                else:
                    delay = state.next_attempt - time.monotonic()
                    if delay > 0:
                        self._condition.wait(delay)
                        continue
                    if not state.ticket:
                        action = "config"
                        api, token, generation = state.api, state.token, state.generation
                    else:
                        action = "start"
                        api, ticket = state.ticket_api, state.ticket
                        state.started = True
                    state.next_attempt = time.monotonic() + self._refresh_seconds

            try:
                if action == "config":
                    response = api.get_config(receiver, token)
                    valid = _ok(response) and isinstance(response.get("typing_ticket"), str) and bool(response["typing_ticket"])
                    if not valid:
                        logger.warning("[Weixin] Typing configuration unavailable")
                    with self._condition:
                        if valid and state.count and state.generation == generation:
                            state.ticket = response["typing_ticket"]
                            state.ticket_api = api
                            state.ticket_generation = generation
                            state.next_attempt = 0.0
                else:
                    response = api.send_typing(receiver, ticket, 1 if action == "start" else 2)
                    if not _ok(response):
                        logger.warning("[Weixin] Typing request rejected")
            except Exception:
                # Exception strings and response bodies can contain credentials.
                logger.warning("[Weixin] Typing request failed")
            finally:
                if action == "stop":
                    with self._condition:
                        state.started = False
                        state.ticket = ""
                        state.ticket_api = None
                        state.next_attempt = 0.0
