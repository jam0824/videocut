"""アプリの起動"""

import sys

from PySide6.QtCore import QLibraryInfo, QLocale, QTranslator
from PySide6.QtWidgets import QApplication

from videocut.ui.main_window import APP_TITLE, MainWindow


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName(APP_TITLE)
    # 「はい」「いいえ」など Qt 標準のボタンを日本語にする
    translator = QTranslator(app)
    if translator.load(QLocale.system(), "qtbase", "_", QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath)):
        app.installTranslator(translator)

    window = MainWindow()
    window.show()
    if len(sys.argv) > 1:
        window.open_file(sys.argv[1])
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
