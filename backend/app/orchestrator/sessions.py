"""Short in-memory conversation memory per session (last few turns). Single user, single process;
nothing is persisted."""

from __future__ import annotations

import threading
from collections import OrderedDict, deque

Turn = tuple[str, str]


class SessionStore:
    def __init__(self, *, max_sessions: int = 50, max_turns: int = 6) -> None:
        self._data: OrderedDict[str, deque[Turn]] = OrderedDict()
        self._max_sessions, self._max_turns = max_sessions, max_turns
        self._lock = threading.Lock()

    def history(self, session_id: str) -> list[Turn]:
        with self._lock:
            return list(self._data.get(session_id, ()))

    def add(self, session_id: str, user: str, assistant: str) -> None:
        with self._lock:
            turns = self._data.setdefault(session_id, deque(maxlen=self._max_turns))
            turns.append((user, assistant))
            self._data.move_to_end(session_id)
            while len(self._data) > self._max_sessions:
                self._data.popitem(last=False)
