import os, io, zipfile, argparse, json
import xml.etree.ElementTree as ET
import requests


def fetch_corp_code(api_key: str, corp_name: str) -> str:
    url = "https://opendart.fss.or.kr/api/corpCode.xml"
    res = requests.get(url, params={"crtfc_key": api_key}, timeout=30)
    res.raise_for_status()
    content_type = (res.headers.get("Content-Type") or "").lower()
    raw = res.content
    # ZIP 응답 여부 확인 (Content-Type 또는 시그니처 PK)
    is_zip = ("zip" in content_type) or (raw[:2] == b"PK")
    if not is_zip:
        # 오류 응답 본문을 그대로 보여주어 원인 파악
        preview_text = res.text[:800]
        raise RuntimeError(
            f"corpCode 응답이 ZIP이 아닙니다. status={res.status_code} content-type={content_type}\n"
            f"응답 본문(일부):\n{preview_text}\n"
            "- 인증키 오류/요청 제한/서비스 점검 가능성 확인 필요"
        )
    z = zipfile.ZipFile(io.BytesIO(raw))
    xml_bytes = z.read(z.namelist()[0])
    root = ET.fromstring(xml_bytes.decode("utf-8"))
    for el in root.iter("list"):
        if el.findtext("corp_name") == corp_name:
            return el.findtext("corp_code")
    raise RuntimeError("corp_code not found for corp_name: " + corp_name)


def call_list(api_key: str, corp_code: str, bgn_de: str, end_de: str, pblntf_ty: str = "", pblntf_detail_ty: str = ""):
    url = "https://opendart.fss.or.kr/api/list.json"
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
    r = requests.get(url, params=params, timeout=30)
    try:
        data = r.json()
    except Exception:
        data = {"raw": r.text}
    return data, r.url


def main():
    parser = argparse.ArgumentParser(description="DART API sanity check")
    parser.add_argument("--api-key", type=str, default=os.getenv("DART_API_KEY"), help="인증키. 미지정시 환경변수 DART_API_KEY 사용")
    parser.add_argument("--corp", type=str, default="한국맥널티")
    parser.add_argument("--year", type=int, default=2024)
    args = parser.parse_args()

    if not args.api_key:
        raise SystemExit("API 키가 필요합니다. --api-key 또는 환경변수 DART_API_KEY 설정")

    print("[1] corp_code 조회…", flush=True)
    corp_code = fetch_corp_code(args.api_key, args.corp)
    print("corp_code:", corp_code)

    yr = int(args.year)
    ranges = [
        (f"{yr-1}0101", f"{yr+1}1231", "no filter"),
        (f"{yr-1}0101", f"{yr+1}1231", "pblntf_ty=A (정기공시)"),
        (f"{yr-1}0101", f"{yr+1}1231", "pblntf_detail_ty=A001 (사업보고서)"),
    ]

    print("[2] list.json 점검…", flush=True)
    for bgn_de, end_de, label in ranges:
        if label == "no filter":
            data, url = call_list(args.api_key, corp_code, bgn_de, end_de)
        elif "pblntf_ty=A" in label:
            data, url = call_list(args.api_key, corp_code, bgn_de, end_de, pblntf_ty="A")
        else:
            data, url = call_list(args.api_key, corp_code, bgn_de, end_de, pblntf_ty="A", pblntf_detail_ty="A001")

        status = data.get("status")
        message = data.get("message")
        items = data.get("list") or []
        print(f"\n- range {bgn_de}~{end_de} [{label}]\n  url: {url}\n  status: {status} message: {message}\n  count: {len(items)}")

        for it in items[:5]:
            corp = it.get("corp_name") or ""
            name = it.get("report_nm") or it.get("rpt_nm") or ""
            rcp_no = it.get("rcp_no") or ""
            reprt_code = it.get("reprt_code") or ""
            rcp_dt = it.get("rcp_dt") or ""
            print(f"    - {rcp_dt} {reprt_code} {rcp_no} {corp} | {name}")

    print("\n테스트 완료.")


if __name__ == "__main__":
    main()


