import os, sys
import requests
from bs4 import BeautifulSoup

from PyQt5 import QtWidgets, uic, QtCore
from plyer import notification


NAVER_CODE = "222980"  # 종목코드
NAVER_URL = "https://finance.naver.com/item/main.nhn?code={code}"
HEADERS = {"User-Agent": "Mozilla/5.0"}
ALERT_THRESHOLD = 4000 # 이 값 이상이면 데스크톱 알림


def fetch_price(code: str) -> str:
    try:
        res = requests.get(NAVER_URL.format(code=code), headers=HEADERS, timeout=10)
        res.raise_for_status()
        soup = BeautifulSoup(res.text, "html.parser")
        node = soup.select_one("p.no_today span.blind")
        return node.text.strip() if node else "-"
    except Exception:
        return "-"

def _to_int_price(text: str) -> int:
    try:
        digits = "".join(ch for ch in (text or "") if ch.isdigit())
        return int(digits) if digits else -1
    except Exception:
        return -1

def desktop_notify(title: str, msg: str):
    try:
        notification.notify(title=title, message=msg, timeout=5)
    except Exception:
        pass


class PriceWorker(QtCore.QThread):
    priceFetched = QtCore.pyqtSignal(str)

    def __init__(self, code: str, parent=None):
        super().__init__(parent)
        self._code = code
        self._running = True

    def run(self):
        while self._running:
            price = fetch_price(self._code)
            self.priceFetched.emit(price)
            self.msleep(10_000)

    def stop(self):
        self._running = False


class PriceViewer(QtWidgets.QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        ui_path = os.path.join(os.path.dirname(__file__), "simple_digit_viewer.ui")
        self.ui = uic.loadUi(ui_path, self)

        # 반투명 + 항상 위
        try:
            self.setWindowOpacity(0.85)
            self.setWindowFlags(self.windowFlags() | QtCore.Qt.WindowStaysOnTopHint)
        except Exception:
            pass

        # 워커 스레드로 10초마다 비동기 갱신 (UI 비멈춤)
        self.worker = PriceWorker(NAVER_CODE, self)
        self.worker.priceFetched.connect(self.on_price_fetched)
        self.worker.start()

        self._notified_over = False

    @QtCore.pyqtSlot(str)
    def on_price_fetched(self, price: str):
        self.label.setText(price)
        val = _to_int_price(price)
        if val >= 0:
            if val >= ALERT_THRESHOLD and not self._notified_over:
                desktop_notify("noti", f" {val:,}")
                self._notified_over = True
            elif val < ALERT_THRESHOLD and self._notified_over:
                # 임계 아래로 내려오면 다시 알림 가능 상태로 리셋
                self._notified_over = False

    def closeEvent(self, event):
        try:
            if hasattr(self, "worker") and self.worker.isRunning():
                self.worker.stop()
                self.worker.wait(2000)
        finally:
            super().closeEvent(event)


def main():
    app = QtWidgets.QApplication(sys.argv)
    w = PriceViewer()
    w.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()