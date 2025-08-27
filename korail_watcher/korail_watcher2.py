import re, os, time
from datetime import datetime
from bs4 import BeautifulSoup
from plyer import notification
from dotenv import load_dotenv

# ===== 기본 설정 =====
TARGET_WINDOW = ("07:00", "09:59")        # 감시 시각대
AVAILABLE_PAT = re.compile(r"(예약\s*가능|잔여석|가능)", re.I)
TRAIN_TYPES = {"KTX", "SRT"}              # 비우면 전체 통과
REFRESH_SEC = 10                          # 테스트 간격
MAX_LOOPS = 5                             # 테스트 반복 횟수 제한

# ===== 추가: 날짜/열차종류 조건 =====
DATE = "2025-09-12"                 # 원하는 날짜 (페이지에 날짜 컬럼이 있을 때만 사용)
TRAIN_TYPES = {"KTX", "SRT"}        # 비우면 전체 통과

# ===== 컬럼 인덱스 보강 =====
SEL = {
    "rows": "table.result tbody tr",
    "col_train": 0,   # 열차종류
    "col_time": 1,    # 출발시각(HH:MM)
    "col_status": 6,  # 상태/잔여석
    "col_date": 2,    # ← 페이지에 '날짜'가 따로 표기될 경우의 컬럼 인덱스(예시)
}

# ===== 테스트용 HTML 샘플 =====
TEST_HTML = """
<table class="result">
  <tbody>
    <tr>
      <td>KTX</td><td>07:23</td><td>-</td><td>-</td><td>-</td><td>-</td><td>예약가능</td>
    </tr>
    <tr>
      <td>무궁화호</td><td>08:40</td><td>-</td><td>-</td><td>-</td><td>-</td><td>매진</td>
    </tr>
    <tr>
      <td>SRT</td><td>10:05</td><td>-</td><td>-</td><td>-</td><td>-</td><td>잔여석 3</td>
    </tr>
  </tbody>
</table>
"""

# ===== 헬퍼 =====
def filter_train_type(txt: str) -> bool:
    return True if not TRAIN_TYPES else any(kind in txt for kind in TRAIN_TYPES)

def filter_date(txt: str) -> bool:
    # 페이지 날짜 포맷에 맞게 정규식/파싱을 조정하세요.
    # 예: '2025-09-12' 또는 '2025.09.12' 등
    return True if not DATE else DATE in txt

# ===== 파싱 로직 내 조건 추가 =====
def parse_and_find(html):
    soup = BeautifulSoup(html, "html.parser")
    rows = soup.select(SEL["rows"])
    hits = []
    for r in rows:
        cols = [c.get_text(strip=True) for c in r.find_all(["td","th"])]
        need_idx = max(SEL["col_train"], SEL["col_time"], SEL["col_status"], SEL.get("col_date", 0))
        if len(cols) <= need_idx:
            continue

        train_txt = cols[SEL["col_train"]]
        time_txt  = cols[SEL["col_time"]]
        stat_txt  = cols[SEL["col_status"]]
        date_txt  = cols[SEL["col_date"]] if "col_date" in SEL else DATE  # 날짜 열이 없으면 외부 DATE 기준

        if not filter_train_type(train_txt):
            continue
        if not filter_date(date_txt):
            continue
        if not in_window(time_txt, TARGET_WINDOW):
            continue
        if AVAILABLE_PAT.search(stat_txt):
            hits.append((train_txt, time_txt, stat_txt))
    return hits

# ===== 알림 채널 =====
load_dotenv()
def desktop_notify(title, msg):
    try:
        notification.notify(title=title, message=msg, timeout=6)
    except Exception:
        pass
    print(f"[ALERT] {title}: {msg}")

def telegram_notify(text):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return
    import urllib.parse, urllib.request
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        urllib.request.urlopen(url, data=data, timeout=8).read()
    except Exception:
        pass

# ===== 로직 =====
def in_window(t_str, win):
    try:
        t = datetime.strptime(t_str, "%H:%M").time()
        a = datetime.strptime(win[0], "%H:%M").time()
        b = datetime.strptime(win[1], "%H:%M").time()
        return a <= t <= b if a <= b else (t >= a or t <= b)
    except Exception:
        return False

def filter_train_type(txt):
    if not TRAIN_TYPES:
        return True
    return any(kind in txt for kind in TRAIN_TYPES)

def parse_and_find(html):
    soup = BeautifulSoup(html, "html.parser")
    rows = soup.select(SEL["rows"])
    hits = []
    for r in rows:
        cols = [c.get_text(strip=True) for c in r.find_all(["td","th"])]
        if len(cols) <= max(SEL["col_train"], SEL["col_time"], SEL["col_status"]):
            continue
        train_txt = cols[SEL["col_train"]]
        time_txt  = cols[SEL["col_time"]]
        stat_txt  = cols[SEL["col_status"]]

        if not filter_train_type(train_txt):
            continue
        if not in_window(time_txt, TARGET_WINDOW):
            continue
        if AVAILABLE_PAT.search(stat_txt):
            hits.append((train_txt, time_txt, stat_txt))
    return hits

def main():
    print(f"[테스트] 시각대 {TARGET_WINDOW[0]}~{TARGET_WINDOW[1]} / {MAX_LOOPS}회")
    for i in range(MAX_LOOPS):
        hits = parse_and_find(TEST_HTML)
        ts = datetime.now().strftime("%H:%M:%S")
        if hits:
            msg = "\n".join(f"{t} | {h} | {s}" for h,t,s in hits)
            line = f"[{ts}] 예약가능 발견\n{msg}"
            print(line)
            desktop_notify("코레일 예약 가능(테스트)", msg)
            telegram_notify(line)
            break
        else:
            print(f"[{ts}] 발견 없음 (loop {i+1}/{MAX_LOOPS})")
        time.sleep(REFRESH_SEC)

if __name__ == "__main__":
    main()
