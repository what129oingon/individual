import os, sys, time, re, random, logging, traceback, argparse
from datetime import datetime, timedelta
from dotenv import load_dotenv
from plyer import notification

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# ====== 사용자 설정 ======
ORIGIN = "창원중앙"          # 출발역
DEST = "서울"            # 도착역
DATE = "2025-09-10"      # YYYY-MM-DD
TARGET_WINDOW = ("10:00", "22:00")  # 감시 시각대
TRAIN_TYPES = {"KTX", "SRT"}  # 필터. 비우면 전체
REFRESH_SEC = 20         # 재조회 간격(초). 과도한 요청은 피하세요.
STOP_ON_FIRST_HIT = True # 첫 발견 시 종료 여부
HEADLESS = True          # 로그인이 필요하면 False로 띄워서 처리

# 문자열 패턴(페이지에 실제로 보이는 텍스트에 맞춰 조정)
NOT_AVAILABLE_PAT = re.compile(r"불가|불가능|매진|마감|대기만|대기\s*만|없음", re.I)
AVAILABLE_PAT = re.compile(r"예약\s*가능|잔여\s*좌석|잔여석|여유|가능", re.I)
TIME_PAT = re.compile(r"(\d{2}:\d{2})")
# ====== 셀렉터(한 번만 수정해서 맞추면 됨) ======
SEL = {
    "origin_input": "input[placeholder='출발역'], input[aria-label='출발역']",
    "dest_input":   "input[placeholder='도착역'], input[aria-label='도착역']",
    "date_input":   "input[type='date'], input[aria-label='승차일자'], input[name='rideDate']",
    "ac_list":      "ul[role='listbox'], .autocomplete-list, .suggest-list",
    "ac_option":    "li[role='option'], .autocomplete-item, .suggest-item",
    "search_btn":   "button[type='submit'], button:has-text('조회'), button[aria-label='조회']",
    "result_rows":  "table tbody tr, .result-list .result-item",
    "col_train":    "td:nth-child(1)",
    "col_time":     "td:nth-child(2)",
    "col_status":   "td:nth-child(7)",
    "reserve_btn":  "button:has-text('예약'), button:has-text('구매'), a:has-text('예약')",
    "soldout_badge":".badge:has-text('매진'), .chip:has-text('매진')",
}
URL = "https://www.korail.com/ticket/search/general#"  # 코레일 새 검색 페이지

# ====== 알림 ======
load_dotenv()

def desktop_notify(title, msg):
    try:
        notification.notify(title=title, message=msg, timeout=10)
    except Exception:
        pass

def telegram_notify(text):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return
    import urllib.parse, urllib.request
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        urllib.request.urlopen(url, data=data, timeout=10).read()
    except Exception:
        pass

def is_available(stat_txt: str) -> bool:
    s = (stat_txt or "").strip()
    if NOT_AVAILABLE_PAT.search(s):
        return False
    return bool(AVAILABLE_PAT.search(s))

def safe_text(row, sel: str) -> str:
    try:
        node = row.query_selector(sel)
        return (node.inner_text().strip() if node else "")
    except Exception:
        return ""

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

def scrape_once(page):
    TIMEOUT_MS = 60000
    page.goto(URL, wait_until="domcontentloaded", timeout=TIMEOUT_MS)
    # 입력
    page.fill(SEL["origin_input"], "")
    page.fill(SEL["origin_input"], ORIGIN)
    # 자동완성 확정(가능한 경우)
    try:
        ac = page.locator(SEL["ac_list"]).first
        if ac.is_visible():
            page.locator(SEL["ac_option"]).first.click()
        else:
            page.keyboard.press("Enter")
    except Exception:
        pass

    page.fill(SEL["dest_input"], "")
    page.fill(SEL["dest_input"], DEST)
    try:
        ac = page.locator(SEL["ac_list"]).first
        if ac.is_visible():
            page.locator(SEL["ac_option"]).first.click()
        else:
            page.keyboard.press("Enter")
    except Exception:
        pass

    # 날짜 설정: type=date가 아니면 JS로 value 설정 후 change 이벤트 디스패치
    try:
        page.fill(SEL["date_input"], DATE)
    except Exception:
        try:
            page.eval_on_selector(
                SEL["date_input"],
                "(el, v)=>{el.value=v; el.dispatchEvent(new Event('input',{bubbles:true})); el.dispatchEvent(new Event('change',{bubbles:true}));}",
                arg=DATE,
            )
        except Exception:
            pass

    page.click(SEL["search_btn"])
    # 네트워크 안정 상태 대기
    try:
        page.wait_for_load_state("networkidle", timeout=TIMEOUT_MS)
    except Exception:
        pass
    # 결과 탐색: 현재 페이지 → 프레임 → 새 탭/팝업
    rows = []
    try:
        page.wait_for_selector(SEL["result_rows"], timeout=TIMEOUT_MS)
        rows = page.query_selector_all(SEL["result_rows"])
    except Exception:
        rows = []
    if not rows:
        try:
            for f in page.frames:
                if f is page.main_frame:
                    continue
                try:
                    f.wait_for_selector(SEL["result_rows"], timeout=3000)
                    rows = f.query_selector_all(SEL["result_rows"])
                    if rows:
                        break
                except Exception:
                    continue
        except Exception:
            rows = []
    if not rows:
        try:
            for p in reversed(page.context.pages):
                if p is page:
                    continue
                try:
                    p.wait_for_selector(SEL["result_rows"], timeout=3000)
                    cand = p.query_selector_all(SEL["result_rows"])
                    if cand:
                        rows = cand
                        break
                except Exception:
                    continue
        except Exception:
            rows = []
    # 디버깅: 여전히 못 찾았으면 스냅샷 저장
    if not rows:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        try:
            page.screenshot(path=f"korail_snapshot_{ts}.png")
        except Exception:
            pass
        try:
            html = page.content()
            with open(f"korail_page_{ts}.html", "w", encoding="utf-8") as f:
                f.write(html)
        except Exception:
            pass
    hits = []
    for r in rows:
        # 카드형 대비: 기본은 테이블 열, 보조로 버튼/배지 확인
        train_txt = safe_text(r, SEL["col_train"])
        time_txt  = safe_text(r, SEL["col_time"])
        stat_txt  = safe_text(r, SEL["col_status"])
        try:
            has_reserve = bool(r.query_selector(SEL["reserve_btn"]))
            is_soldout  = bool(r.query_selector(SEL["soldout_badge"]))
            if has_reserve:
                stat_txt = (stat_txt + " 예약가능").strip()
            if is_soldout:
                stat_txt = (stat_txt + " 매진").strip()
        except Exception:
            pass

        # 시간 추출
        m = TIME_PAT.search(time_txt)
        dep_time = m.group(1) if m else None

        if not dep_time:
            continue
        if not filter_train_type(train_txt):
            continue
        if not in_window(dep_time, TARGET_WINDOW):
            continue
        if is_available(stat_txt):
            hits.append((train_txt, dep_time, stat_txt))
    return hits

def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    logging.info(
        f"시작: {ORIGIN}->{DEST} {DATE} {TARGET_WINDOW[0]}~{TARGET_WINDOW[1]} / 간격 {REFRESH_SEC}s"
    )

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=HEADLESS)
        ctx = browser.new_context()

        # 리소스 차단(속도 향상)
        def _route_handler(route):
            try:
                rtype = getattr(route.request, "resource_type", None)
                if callable(rtype):
                    rtype = rtype()
                if rtype in {"image", "font", "stylesheet"}:
                    return route.abort()
                return route.continue_()
            except Exception:
                try:
                    return route.continue_()
                except Exception:
                    return None

        try:
            ctx.route("**/*", _route_handler)
        except Exception:
            pass

        page = ctx.new_page()
        try:
            ctx.set_default_timeout(30000)
            page.set_default_timeout(30000)
        except Exception:
            pass

        seen = set()

        try:
            while True:
                try:
                    hits = scrape_once(page)
                except PWTimeout:
                    logging.warning("페이지 타임아웃")
                    hits = []
                except Exception:
                    logging.error("예외 발생:\n" + traceback.format_exc())
                    hits = []

                if hits:
                    # 중복 제거
                    new_hits = []
                    for h, t, s in [(h, t, s) for h, t, s in hits]:
                        key = (DATE, h, t)
                        if key not in seen:
                            seen.add(key)
                            new_hits.append((h, t, s))

                    if new_hits:
                        msg = "\n".join(f"{t} | {h} | {s}" for h, t, s in new_hits)
                        line = f"예약가능 발견\n{msg}"
                        logging.info(line.replace("\n", " | "))
                        desktop_notify("코레일 예약 가능", msg)
                        telegram_notify(line)
                        if STOP_ON_FIRST_HIT:
                            break
                    else:
                        logging.info("변경 없음(기존 알림과 동일)")
                else:
                    logging.info("없음")

                sleep_sec = max(1.0, REFRESH_SEC + random.uniform(-3, 3))
                time.sleep(sleep_sec)
        finally:
            try:
                ctx.close()
            finally:
                browser.close()

def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="코레일 예약 감시기")
    parser.add_argument("--origin", type=str, default=ORIGIN)
    parser.add_argument("--dest", type=str, default=DEST)
    parser.add_argument("--date", type=str, default=DATE, help="YYYY-MM-DD")
    parser.add_argument(
        "--window",
        type=str,
        default=f"{TARGET_WINDOW[0]},{TARGET_WINDOW[1]}",
        help="HH:MM,HH:MM",
    )
    parser.add_argument(
        "--train-types",
        type=str,
        default=",".join(sorted(TRAIN_TYPES)) if TRAIN_TYPES else "",
        help="콤마로 구분된 열차 유형. 비우면 전체",
    )
    parser.add_argument("--refresh", type=int, default=REFRESH_SEC)
    try:
        bool_action = argparse.BooleanOptionalAction
    except Exception:
        bool_action = None
    if bool_action:
        parser.add_argument("--headless", action=bool_action, default=HEADLESS)
        parser.add_argument("--stop-on-first", action=bool_action, default=STOP_ON_FIRST_HIT)
    else:
        parser.add_argument("--headless", type=str, default=str(HEADLESS))
        parser.add_argument("--stop-on-first", type=str, default=str(STOP_ON_FIRST_HIT))
    parser.add_argument("--url", type=str, default=URL)
    return parser.parse_args(argv)

def apply_cli_overrides(args):
    global ORIGIN, DEST, DATE, TARGET_WINDOW, TRAIN_TYPES, REFRESH_SEC, STOP_ON_FIRST_HIT, HEADLESS, URL
    ORIGIN = args.origin
    DEST = args.dest
    DATE = args.date
    try:
        a, b = [s.strip() for s in args.window.split(",", 1)]
        TARGET_WINDOW = (a, b)
    except Exception:
        pass
    if isinstance(args.train_types, str):
        types = [t.strip() for t in args.train_types.split(",") if t.strip()]
        TRAIN_TYPES = set(types)
    REFRESH_SEC = int(args.refresh)
    if hasattr(args, "headless") and isinstance(args.headless, bool):
        HEADLESS = args.headless
    elif hasattr(args, "headless"):
        HEADLESS = str(args.headless).lower() in {"1", "true", "yes", "y"}
    if hasattr(args, "stop_on_first") and isinstance(args.stop_on_first, bool):
        STOP_ON_FIRST_HIT = args.stop_on_first
    elif hasattr(args, "stop_on_first"):
        STOP_ON_FIRST_HIT = str(args.stop_on_first).lower() in {"1", "true", "yes", "y"}
    URL = args.url

if __name__ == "__main__":
    args = parse_args()
    apply_cli_overrides(args)
    main()
