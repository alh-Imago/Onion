import os
import sys
from PyQt6.QtWidgets import QApplication
from PyQt6.QtCore import QThreadPool
from .main_window import MainWindow
from . import theme as theme_mod


def main():
    app = QApplication(sys.argv)
    theme_mod.apply_theme(app, dark=False)
    paths = sys.argv[1:] if len(sys.argv) > 1 else [os.path.expanduser("~")]
    window = MainWindow(default_paths=paths)
    window.show()
    # Wait for any in-flight background worker (search/compress/unwrap/etc.)
    # to finish before the app tears down its Qt objects -- otherwise a
    # worker that's still running when the window closes can crash trying
    # to emit a signal through an already-deleted object.
    app.aboutToQuit.connect(lambda: QThreadPool.globalInstance().waitForDone(3000))
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
