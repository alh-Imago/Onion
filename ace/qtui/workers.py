"""
workers.py — Run blocking calls (search, compress, unwrap, delete, verify)
off the Qt main thread, so the UI never freezes during disk I/O.

Usage:
    worker = Worker(some_function, arg1, arg2, kwarg=value)
    worker.signals.finished.connect(on_success)
    worker.signals.error.connect(on_error)
    QThreadPool.globalInstance().start(worker)
"""

import traceback
from PyQt6.QtCore import QObject, QRunnable, pyqtSignal


class WorkerSignals(QObject):
    finished = pyqtSignal(object)   # emits the callable's return value
    error = pyqtSignal(str)         # emits a human-readable error message


class Worker(QRunnable):
    def __init__(self, fn, *args, **kwargs):
        super().__init__()
        self.fn = fn
        self.args = args
        self.kwargs = kwargs
        self.signals = WorkerSignals()

    def run(self):
        try:
            result = self.fn(*self.args, **self.kwargs)
        except Exception as e:
            self.signals.error.emit(f"{e}\n{traceback.format_exc(limit=3)}")
        else:
            self.signals.finished.emit(result)
