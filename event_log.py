"""Keep UI event publication independent of status/event-file latency."""
from collections import deque
from copy import deepcopy
import json
from pathlib import Path
import threading


class EventLogError(RuntimeError):
    """A controlled failure message; never includes raw paths or event data."""


class EventLog:
    ERROR_MESSAGE = '运行记录保存失败，已禁止新的订单提交；已发出的请求仍会核对，请到钉钉确认结果。'

    def __init__(self, state_file, events_file):
        self.state_file = Path(state_file)
        self.events_file = Path(events_file)
        self._condition = threading.Condition()
        self._pending = deque()
        self._submitted = 0
        self._completed = 0
        self._closing = False
        self._error = None
        self._thread = threading.Thread(target=self._run, name='HDU-event-writer', daemon=False)
        self._thread.start()

    @property
    def error(self):
        with self._condition:
            return self._error

    def submit(self, event):
        """Snapshot/enqueue only: even a slow writer never holds this lock for I/O."""
        snapshot = deepcopy(event)
        with self._condition:
            if self._closing:
                raise EventLogError('运行记录已关闭，无法接收新的状态。')
            self._submitted += 1
            self._pending.append(snapshot)
            self._condition.notify_all()

    def check(self):
        """Reject a new side effect after a known recording failure."""
        with self._condition:
            if self._error:
                raise EventLogError(self._error)
            if self._closing:
                raise EventLogError('运行记录已关闭，已禁止新的订单提交。')

    def flush(self):
        """Wait for events submitted so far, or report a latched writer failure."""
        with self._condition:
            target = self._submitted
            while self._completed < target and not self._error:
                self._condition.wait()
            if self._error:
                raise EventLogError(self._error)

    def close(self):
        """Drain and stop the sole writer; safe to repeat, including after failure."""
        with self._condition:
            self._closing = True
            self._condition.notify_all()
        self._thread.join()
        with self._condition:
            if self._error:
                raise EventLogError(self._error)

    def _write(self, event):
        temporary = self.state_file.with_suffix('.tmp')
        temporary.write_text(json.dumps(event, ensure_ascii=False, indent=2), encoding='utf-8')
        temporary.replace(self.state_file)
        with self.events_file.open('a', encoding='utf-8') as log:
            log.write(json.dumps(event, ensure_ascii=False) + '\n')

    def _run(self):
        while True:
            with self._condition:
                while not self._pending and not self._closing:
                    self._condition.wait()
                if not self._pending:
                    return
                event = self._pending[0]
            try:
                self._write(event)
            except Exception:
                # Keep the failed event and the remaining queue in memory. Do not
                # retry an append that may have partly succeeded, or hide the loss.
                with self._condition:
                    self._error = self.ERROR_MESSAGE
                    self._condition.notify_all()
                return
            with self._condition:
                self._pending.popleft()
                self._completed += 1
                self._condition.notify_all()
