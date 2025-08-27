import os, re, sys, time, zipfile, io, gzip, xml.etree.ElementTree as ET
import requests
from bs4 import BeautifulSoup
from pathlib import Path

API_KEY = os.getenv("DART_API_KEY", "0f50640c8260194ec3ee604bacbaba2bc8ca5e2a")
TARGET_CORP = "한국맥널티"     # 회사명으로 corp_code 조회
YEAR = "2025"                 # 필요 연도
REPORT_CODE = "11011"         # 11011=사업보고서

def get_corp_code(api_key, target_corp):
    url = "https://opendart.fss.or.kr/api/corpCode.xml"
    z = zipfile.ZipFile(io.BytesIO(requests.get(url, params={"crtfc_key": api_key}).content))
    xml_bytes = z.read(z.namelist()[0])
    root = ET.fromstring(xml_bytes.decode("utf-8"))
    for el in root.iter("list"):
        if el.findtext("corp_name") == target_corp:
            return el.findtext("corp_code")
    raise ValueError("corp_code not found")

def get_rcp_no(api_key, corp_code, year, report_code):
    """
    접수일 기준 기간을 넓게 잡아 조회한다: [year-1-01-01, year+1-12-31]
    우선 정기공시(A)로 조회하고, 필요 시 세부유형(A001, 사업보고서)로 재시도한다.
    반환은 reprt_code 일치(예: 11011) 우선, 없으면 보고서명에 '사업보고서' 포함 항목.
    """
    url = "https://opendart.fss.or.kr/api/list.json"
    yr = int(year)
    bgn_de = f"{yr-1}0101"
    end_de = f"{yr+1}1231"

    def _call(pblntf_ty: str = "A", pblntf_detail_ty: str = ""):
        params = {
            "crtfc_key": api_key,
            "corp_code": corp_code,
            "bgn_de": bgn_de,
            "end_de": end_de,
            "pblntf_ty": pblntf_ty,
            "pblntf_detail_ty": pblntf_detail_ty,
            "page_no": 1,
            "page_count": 1000,
        }
        return requests.get(url, params=params).json()

    data = _call(pblntf_ty="A", pblntf_detail_ty="")
    if data.get("status") != "000" or not data.get("list"):
        # 세부유형 A001(사업보고서)로 재시도
        data = _call(pblntf_ty="A", pblntf_detail_ty="A001")
    if data.get("status") != "000":
        raise RuntimeError(data.get("message"))

    items = data.get("list", [])
    # 최신 접수일 우선 정렬
    def _rcp_dt(it):
        return it.get("rcp_dt") or ""
    items_sorted = sorted(items, key=_rcp_dt, reverse=True)

    # 기대 연도(사업연도): YEAR-1의 12월 표기(예: (2024.12)) 우선
    report_year = int(year) - 1
    tag = f"({report_year}.12)"

    # 1차: reprt_code 일치 + 보고서명에 '사업보고서' 및 연도 태그 포함 + rcept_no 존재
    cand = [
        it for it in items_sorted
        if it.get("reprt_code") == report_code
        and (it.get("report_nm") or it.get("rpt_nm") or "").find("사업보고서") >= 0
        and (it.get("report_nm") or it.get("rpt_nm") or "").find(tag) >= 0
        and it.get("rcept_no")
    ]
    if cand:
        return cand[0]["rcept_no"]

    # 2차: reprt_code 일치 + rcept_no 존재
    cand = [it for it in items_sorted if it.get("reprt_code") == report_code and it.get("rcept_no")]
    if cand:
        return cand[0]["rcept_no"]

    # 3차: 보고서명에 '사업보고서' 포함 + rcept_no 존재
    cand = []
    for it in items_sorted:
        name = it.get("report_nm") or it.get("rpt_nm") or ""
        if "사업보고서" in name and it.get("rcept_no"):
            cand.append(it)
    if cand:
        return cand[0]["rcept_no"]

    raise ValueError(f"사업보고서 rcp_no not found (searched: {bgn_de}~{end_de})")

def fetch_document_response(api_key, rcp_no):
    url = "https://opendart.fss.or.kr/api/document.xml"
    res = requests.get(url, params={"crtfc_key": api_key, "rcept_no": rcp_no}, timeout=30)
    return res

def extract_sales_section(document_xml_text):
    # document.xml은 HTML 본문이 CDATA로 들어있음
    try:
        prepared = _prepare_xml_text(document_xml_text)
        if not prepared.startswith("<"):
            return None
        root = ET.fromstring(prepared)
    except ET.ParseError:
        return None
    # 키워드 확장(표현 다양성 대응)
    include_keys = [
        "주요 제품", "주요제품", "주요 품목", "제품", "서비스",
        "매출", "매출액", "매출 현황", "매출비중", "매출 구성",
        "상품매출", "제품매출", "매출유형", "판매", "비중"
    ]

    def _extract_from_html(html: str):
        soup = BeautifulSoup(html, "html.parser")
        lines = [ln.strip() for ln in soup.get_text("\n").splitlines() if ln.strip()]
        if not lines:
            return None
        # 키워드가 포함된 라인 인덱스 수집
        hit_idx = []
        for i, ln in enumerate(lines):
            if any(k in ln for k in include_keys):
                hit_idx.append(i)
        if not hit_idx:
            return None
        # 주변 컨텍스트 포함한 스니펫 구성
        snippets = []
        for i in hit_idx:
            a = max(0, i - 3)
            b = min(len(lines), i + 8)
            snippet = lines[a:b]
            # 너무 짧은 스니펫은 제외
            if len(" ".join(snippet)) >= 40:
                snippets.append(snippet)
        if not snippets:
            return None
        # 가장 정보량이 큰 스니펫 선택
        best = max(snippets, key=lambda ss: len(" ".join(ss)))
        # 중복 제거 후 반환
        seen = set()
        uniq = []
        for ln in best:
            if ln not in seen:
                seen.add(ln)
                uniq.append(ln)
        return "\n".join(uniq[:200])

    # 모든 <content> 태그(네임스페이스 무시)에서 추출
    def _local(tag: str) -> str:
        return tag.rsplit('}', 1)[-1]

    candidates = []
    for el in root.iter():
        if _local(el.tag) == "content":
            html = el.text or ""
            if not html:
                continue
            extracted = _extract_from_html(html)
            if extracted:
                candidates.append(extracted)

    if not candidates:
        return None
    # 가장 긴 결과 반환
    return max(candidates, key=len)

def _decompress_if_needed(raw: bytes) -> bytes:
    # gzip magic
    if raw[:2] == b"\x1f\x8b":
        try:
            return gzip.decompress(raw)
        except Exception:
            return raw
    return raw

def _decode_text(raw: bytes) -> str:
    for enc in ("utf-8", "cp949", "euc-kr", "latin-1"):
        try:
            return raw.decode(enc)
        except Exception:
            continue
    return raw.decode(errors="ignore")

def _extract_xml_from_zip(raw: bytes):
    """
    ZIP 바이트에서 XML 파일을 추출해 (파일명, 바이트) 형태로 반환.
    document.xml 또는 첫 번째 .xml을 우선 선택. 없으면 None 반환.
    """
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            names = zf.namelist()
            # document.xml 우선, 없으면 첫 .xml, 그래도 없으면 첫 파일
            pick = None
            for n in names:
                if n.lower().endswith("document.xml"):
                    pick = n
                    break
            if pick is None:
                xmls = [n for n in names if n.lower().endswith(".xml")]
                pick = (xmls[0] if xmls else (names[0] if names else None))
            if not pick:
                return None, None
            data = zf.read(pick)
            return pick, data
    except Exception:
        return None, None

def _prepare_xml_text(s: str) -> str:
    """Strip BOM/null/leading whitespace and return cleaned text."""
    if not isinstance(s, str):
        try:
            s = s.decode("utf-8", errors="ignore")
        except Exception:
            s = str(s)
    s = s.lstrip("\ufeff\r\n\t ")
    s = s.replace("\x00", "")
    return s

def dump_document_response(res: requests.Response, out_dir: str = "dart_dump") -> str:
    """
    document.xml의 각 <document>/<content>의 HTML을 파일로 저장하고,
    텍스트 버전도 함께 저장한다. 저장된 디렉토리 경로를 반환.
    """
    base = Path(out_dir)
    base.mkdir(parents=True, exist_ok=True)
    # 원문 저장 (Content-Type에 따라 확장자 결정)
    content_type = (res.headers.get("Content-Type") or "").lower()
    raw = _decompress_if_needed(res.content)
    # ZIP(document.xml.zip) 대응
    if raw[:2] == b"PK":
        fname, xml_bytes = _extract_xml_from_zip(raw)
        try:
            (base / (fname or "document.xml")).write_bytes(xml_bytes or b"")
        except Exception:
            pass
        # 이후 xml_bytes로 content 처리하도록 교체
        if xml_bytes:
            raw = xml_bytes
            content_type = "application/xml"

    if raw.lstrip().startswith(b"<") or "xml" in content_type:
        raw_name = "raw_document.xml"
    elif "json" in content_type or raw[:1] == b"{" or raw[:1] == b"[":
        raw_name = "raw_document.json"
    elif "html" in content_type or b"<html" in raw[:200].lower():
        raw_name = "raw_document.html"
    else:
        raw_name = "raw_document.txt"
    try:
        (base / raw_name).write_bytes(raw)
    except Exception:
        pass

    # XML일 때만 파싱 및 내용 분할 저장
    text = None
    try:
        if "xml" in content_type or raw.lstrip().startswith(b"<"):
            text = _decode_text(raw)
            root = ET.fromstring(text)
        else:
            return str(base.resolve())
    except ET.ParseError:
        return str(base.resolve())

    def _local(tag: str) -> str:
        return tag.rsplit('}', 1)[-1]

    idx = 0
    for el in root.iter():
        if _local(el.tag) != "content":
            continue
        html = el.text or ""
        if not html:
            continue
        idx += 1
        html_path = base / f"doc_{idx:03d}.html"
        txt_path = base / f"doc_{idx:03d}.txt"
        try:
            html_path.write_text(html, encoding="utf-8")
            text = BeautifulSoup(html, "html.parser").get_text("\n")
            txt_path.write_text(text, encoding="utf-8")
        except Exception:
            continue
    return str(base.resolve())

# 숫자/단위 기반 매출 후보 추출
NUM_PAT = re.compile(r"(?<!\d)(\d{1,3}(?:,\d{3})+|\d{5,})(?!\d)")

def _detect_unit_factor(lines, idx):
    window = lines[max(0, idx - 15): idx + 1]
    text = " ".join(window)
    # 흔한 단위 표기 탐지
    if "조원" in text or "(단위: 조원)" in text or "(단위 : 조원)" in text:
        return 10**12
    if "억원" in text or "(단위: 억원)" in text or "(단위 : 억원)" in text:
        return 10**8
    if "백만원" in text or "백만 원" in text or "(단위: 백만원)" in text or "(단위 : 백만원)" in text:
        return 10**6
    if "천원" in text or "천 원" in text or "(단위: 천원)" in text or "(단위 : 천원)" in text:
        return 10**3
    # 기본: 원
    return 1

def extract_revenue_candidates(document_xml_text):
    try:
        prepared = _prepare_xml_text(document_xml_text)
        if not prepared.startswith("<"):
            return []
        root = ET.fromstring(prepared)
    except ET.ParseError:
        return []
    keywords = ["매출액", "매출", "영업수익", "매출총액", "매출 구성", "매출비중"]
    candidates = []
    def _local(tag: str) -> str:
        return tag.rsplit('}', 1)[-1]

    for el in root.iter():
        if _local(el.tag) != "content":
            continue
        html = el.text or ""
        if not html:
            continue
        soup = BeautifulSoup(html, "html.parser")
        lines = [ln.strip() for ln in soup.get_text("\n").splitlines() if ln.strip()]
        for i, ln in enumerate(lines):
            if not any(k in ln for k in keywords):
                continue
            factor = _detect_unit_factor(lines, i)
            a = max(0, i - 2)
            b = min(len(lines), i + 8)
            ctx = lines[a:b]
            ctx_text = " ".join(ctx)
            for m in NUM_PAT.finditer(ctx_text):
                try:
                    num = int(m.group(1).replace(",", "")) * factor
                except Exception:
                    continue
                if num < 10_000_000:  # 1천만원 미만 제외
                    continue
                snippet = ln if len(ln) < 200 else ln[:200]
                candidates.append((num, snippet))
    # 중복 값 제거 후 큰 값 우선
    uniq = {}
    for val, ctx in candidates:
        if val not in uniq:
            uniq[val] = ctx
    ranked = sorted(uniq.items(), key=lambda x: x[0], reverse=True)
    return ranked

def main():
    corp_code = get_corp_code(API_KEY, TARGET_CORP)
    rcp_no = get_rcp_no(API_KEY, corp_code, YEAR, REPORT_CODE)
    res = fetch_document_response(API_KEY, rcp_no)
    # 디코딩 보강 적용
    raw = _decompress_if_needed(res.content)
    # ZIP(document.xml.zip) 대응
    if raw[:2] == b"PK":
        _, xml_from_zip = _extract_xml_from_zip(raw)
        if xml_from_zip:
            raw = xml_from_zip
    xml_bytes = raw
    xml_text = _decode_text(xml_bytes)
    section = extract_sales_section(xml_text)
    if section:
        print("=== 추출 결과(요약) ===")
        print(section)
    else:
        print("섹션을 자동 추출하지 못했습니다. document.xml을 수동 확인하세요.")
        print("rcp_no:", rcp_no)
        outdir = dump_document_response(res)
        if outdir:
            print("원문 덤프 디렉토리:", outdir)
    # 매출 후보 상위 5개 표시
    rev = extract_revenue_candidates(xml_text)
    if rev:
        print("\n=== 매출 후보(상위 5, 원 단위) ===")
        for val, ctx in rev[:5]:
            print(f"{val:,} | {ctx}")

if __name__ == "__main__":
    main()
