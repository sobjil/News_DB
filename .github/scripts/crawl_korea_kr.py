#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
korea.kr 보도자료 목록 스크래퍼 → data/articles.json 갱신.

WeeklyBrief (주간정책동향 딸깍) 의 데이터 소스.

★ 2026-07 변경: korea.kr 정책브리핑 RSS 서비스가 2026-07-01 저작권(권리보호) 사유로
  영구 중단됨(+ data.go.kr 정책브리핑 보도자료/전문자료 Open API 도 함께 폐지). 그래서
  종전 부처별 RSS(dept_{code}.xml) 크롤을 → korea.kr 브리핑룸 '보도자료 목록 페이지'
  (pressReleaseList.do) HTML 스크래핑으로 전환. 나머지(merge·retention·첨부추출·출력)는 동일.

  · 목록 한 페이지에서 항목마다 제목·URL(newsId)·부처·발행일·요약을 직접 파싱 (view 방문 불필요).
  · 첨부(hwpx/pdf) 링크는 종전과 동일하게 view 페이지(pressReleaseView.do)를 프록시로 받아 추출.
  · 저작권: 정부 보도자료 '텍스트'는 공공누리 제1유형(출처표시) 자유이용. 사진/이미지는 저장 안 함.

전략:
  - pressReleaseList.do 를 pageIndex 로 페이징하며 항목 파싱 (신규가 없는 페이지 만나면 중단).
  - 메타만 저장 (제목·URL·부처·일자·짧은 요약).
  - 기존 articles.json 과 merge, URL 기준 중복 제거 (첨부 상태 보존).
  - 30일 이전 entry 자동 삭제 (retention).
  - 발행일 내림차순 정렬.
의존성: 표준 라이브러리만 (urllib + re). pip install X.
"""

import html as html_mod
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

# ─── 부처 코드 → 한글명 (agencyCode 역매핑용) ────────────────────────
# 목록 페이지는 부처 '한글명' 만 주므로, 종전 스키마의 agencyCode(예: 'msit')를
# 유지하기 위해 이름→코드 역매핑에 사용한다. 매핑 없으면 agencyCode 는 "".
DEPT = {
    "opm": "국무조정실", "moef": "재정경제부", "msit": "과학기술정보통신부",
    "moe": "교육부", "mofa": "외교부", "unikorea": "통일부", "moj": "법무부",
    "mnd": "국방부", "mois": "행정안전부", "mpva": "국가보훈부",
    "mcst": "문화체육관광부", "mafra": "농림축산식품부", "motir": "산업통상부",
    "mw": "보건복지부", "mcee": "기후에너지환경부", "moel": "고용노동부",
    "mogef": "성평등가족부", "molit": "국토교통부", "mof": "해양수산부",
    "mss": "중소벤처기업부", "mpb": "기획예산처", "mpm": "인사혁신처",
    "moleg": "법제처", "mfds": "식품의약품안전처", "mods": "국가데이터처",
    "moip": "지식재산처", "nts": "국세청", "customs": "관세청", "pps": "조달청",
    "kasa": "우주항공청", "oka": "재외동포청", "spo": "검찰청", "mma": "병무청",
    "dapa": "방위사업청", "npa": "경찰청", "nfa": "소방청", "khs": "국가유산청",
    "rda": "농촌진흥청", "forest": "산림청", "kdca": "질병관리청", "kma": "기상청",
    "sda": "새만금개발청", "kcg": "해양경찰청", "kmcc": "방송미디어통신위원회",
    "nssc": "원자력안전위원회", "ftc": "공정거래위원회", "fsc": "금융위원회",
    "acrc": "국민권익위원회", "pipc": "개인정보보호위원회", "k_cohesion": "국민통합위원회",
    "betterfuture": "저출산고령사회위원회", "esdc": "경제사회노동위원회",
}
NAME2CODE = {name: code for code, name in DEPT.items()}

# ─── 상수 ────────────────────────────────────────────────────────────
LIST_URL_TPL = "https://www.korea.kr/briefing/pressReleaseList.do?pageIndex={page}"
VIEW_URL_TPL = "https://www.korea.kr/briefing/pressReleaseView.do?newsId={news_id}"
RETENTION_DAYS = 30
DATA_FILE = "data/articles.json"
USER_AGENT = (
    "Mozilla/5.0 (compatible; GDI-Apps-WeeklyBrief/2.0; "
    "+https://github.com/sobjil/GDI-Apps)"
)
REQUEST_TIMEOUT = 20        # 초
INTER_REQUEST_SLEEP = 0.5   # 요청 간 딜레이 (rate limit·사이트 부담 완화)
DESCRIPTION_MAX = 300       # description(lead) 길이 한도
# 목록 페이징 상한 — 한 run 당 최대 이만큼만 페이지를 훑는다.
# 정상(캐치업 완료) 상태에선 신규 없는 페이지에서 조기 중단되므로 1~2페이지만 돈다.
# 최초 전환/공백(7/2~) 백필은 idempotent merge 로 몇 run 에 걸쳐 점진 수집된다.
MAX_PAGES_PER_RUN = 40
KST = timezone(timedelta(hours=9))

# 매 run 마다 view 페이지 fetch 해서 첨부 추출하는 최대 건수 (점진 처리).
HWPX_URL_BATCH_PER_RUN = 30
ATT_MAX_FAILS = 5           # 첨부 fetch 실패 재시도 한도
ALLOWED_URL_PATH = "/briefing/pressReleaseView.do"   # 보도자료만 통과(안전망)

# ─── korea.kr 한국 IP 프록시 (GitHub 미국 IP 차단 우회) ──────────────
# korea.kr 은 해외(GitHub Actions 미국) IP 를 SSL handshake timeout 으로 막는다.
# AISpace(한국 IP) 프록시 경유로 우회. KOREA_PROXY_URL 미설정이면 직접 호출(로컬 한국망 하위호환).
KOREA_PROXY_URL = os.environ.get("KOREA_PROXY_URL", "").strip()
KOREA_PROXY_KEY = os.environ.get("KOREA_PROXY_KEY", "").strip()

def to_fetch_url(url):
    """KOREA_PROXY_URL 설정 시 korea.kr 요청을 프록시 경유 URL 로 변환(미설정이면 원본)."""
    if not KOREA_PROXY_URL:
        return url
    from urllib.parse import quote
    q = "url=" + quote(url, safe="")
    if KOREA_PROXY_KEY:
        q += "&key=" + quote(KOREA_PROXY_KEY, safe="")
    sep = "&" if "?" in KOREA_PROXY_URL else "?"
    return KOREA_PROXY_URL + sep + q

# ─── 유틸 ────────────────────────────────────────────────────────────
HTML_TAG_RE = re.compile(r"<[^>]+>")
WHITESPACE_RE = re.compile(r"\s+")
HWPX_ANCHOR_RE = re.compile(
    r'<a[^>]+href="(/common/download\.do\?[^"]+)"[^>]*>(.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)

def clean_text(s):
    """HTML 태그 제거 + entity 디코드 + 다중 공백 정리."""
    if not s:
        return ""
    s = HTML_TAG_RE.sub(" ", s)
    s = html_mod.unescape(s)
    return WHITESPACE_RE.sub(" ", s).strip()

def parse_list_date(s):
    """'2026.07.09' → ISO 8601 (KST, 날짜 기준). 실패 시 None."""
    if not s:
        return None
    m = re.search(r"(\d{4})[.\-](\d{1,2})[.\-](\d{1,2})", s)
    if not m:
        return None
    try:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        return datetime(y, mo, d, tzinfo=KST).isoformat()
    except ValueError:
        return None

# ─── 목록 파싱 ───────────────────────────────────────────────────────
# 목록 항목 블록: <a href="/briefing/pressReleaseView.do?newsId=NNN...">...</a>
ITEM_RE = re.compile(
    r'<a\s+href="(/briefing/pressReleaseView\.do\?newsId=(\d+)[^"]*)"\s*>(.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)
TITLE_RE = re.compile(r"<strong>(.*?)</strong>", re.IGNORECASE | re.DOTALL)
LEAD_RE = re.compile(r'class="lead"\s*>(.*?)</span>', re.IGNORECASE | re.DOTALL)
# <span class="source"> <span>날짜</span> <span>부처</span> </span>
SOURCE_RE = re.compile(
    r'class="source"\s*>\s*<span>(.*?)</span>\s*<span>(.*?)</span>',
    re.IGNORECASE | re.DOTALL,
)

def parse_listing(html):
    """목록 페이지 HTML → entry 리스트. 항목당 제목·URL·부처·일자·요약."""
    items = []
    for m in ITEM_RE.finditer(html):
        news_id = m.group(2)
        inner = m.group(3)
        t = TITLE_RE.search(inner)
        title = clean_text(t.group(1)) if t else ""
        s = SOURCE_RE.search(inner)
        pub_raw = clean_text(s.group(1)) if s else ""
        agency = clean_text(s.group(2)) if s else ""
        ld = LEAD_RE.search(inner)
        desc = clean_text(ld.group(1))[:DESCRIPTION_MAX] if ld else ""
        iso_date = parse_list_date(pub_raw)
        if not (title and news_id and iso_date):
            continue
        items.append({
            "title": title,
            "url": VIEW_URL_TPL.format(news_id=news_id),
            "agency": agency,
            "agencyCode": NAME2CODE.get(agency, ""),
            "pubDate": iso_date,
            "description": desc,
        })
    return items

def fetch_listing_page(page):
    """보도자료 목록 한 페이지 fetch → parse. 실패 시 빈 리스트."""
    url = LIST_URL_TPL.format(page=page)
    req = Request(to_fetch_url(url), headers={
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml",
    })
    try:
        with urlopen(req, timeout=REQUEST_TIMEOUT) as r:
            if r.status != 200:
                print(f"  [WARN] list p{page} HTTP {r.status}", file=sys.stderr)
                return []
            raw = r.read().decode("utf-8", errors="ignore")
    except HTTPError as e:
        print(f"  [WARN] list p{page} HTTP {e.code}", file=sys.stderr)
        return []
    except URLError as e:
        print(f"  [WARN] list p{page} URLError: {e.reason}", file=sys.stderr)
        return []
    except Exception as e:
        print(f"  [WARN] list p{page}: {e}", file=sys.stderr)
        return []
    return parse_listing(raw)

# ─── 첨부 추출 (view 페이지) — 종전과 동일 ───────────────────────────
SUPPORTED_EXTS = ("hwpx", "pdf", "hwp")
EXT_RE = re.compile(r"\.(hwpx|hwp|pdf)\b", re.IGNORECASE)

def extract_attachments(html, article_url):
    """본문 페이지 HTML 에서 첨부 anchor 추출 → [{ext,url,filename}]. 빈 리스트=첨부없음."""
    from urllib.parse import urljoin
    seen_urls = set()
    out = []
    for m in HWPX_ANCHOR_RE.finditer(html):
        href = m.group(1).replace("&amp;", "&")
        inner = m.group(2)
        ext_m = EXT_RE.search(inner)
        if not ext_m:
            continue
        ext = ext_m.group(1).lower()
        try:
            url = urljoin(article_url, href)
        except Exception:
            continue
        if url in seen_urls:
            continue
        seen_urls.add(url)
        fname = WHITESPACE_RE.sub(" ", HTML_TAG_RE.sub("", inner)).strip()
        if len(fname) > 120:
            fname = fname[:120]
        out.append({"ext": ext, "url": url, "filename": fname})
    out.sort(key=lambda a: SUPPORTED_EXTS.index(a["ext"]) if a["ext"] in SUPPORTED_EXTS else 99)
    return out

def fetch_attachments_for_article(article_url):
    """view 페이지 fetch → 첨부 리스트 추출 (None=fetch 실패, []=첨부없음)."""
    req = Request(to_fetch_url(article_url), headers={
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml",
    })
    try:
        with urlopen(req, timeout=REQUEST_TIMEOUT) as r:
            if r.status != 200:
                return None
            html = r.read().decode("utf-8", errors="ignore")
    except HTTPError as e:
        print(f"    [WARN] 첨부 HTTP {e.code}", file=sys.stderr)
        return None
    except URLError as e:
        print(f"    [WARN] 첨부 URLError: {e.reason}", file=sys.stderr)
        return None
    except Exception as e:
        print(f"    [WARN] 첨부 추출 실패: {e}", file=sys.stderr)
        return None
    return extract_attachments(html, article_url)

# ─── 메인 ────────────────────────────────────────────────────────────
def main():
    # 1) 기존 articles.json 읽기
    existing = []
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                existing = (json.load(f) or {}).get("articles", []) or []
            print(f"기존 articles: {len(existing)}건")
        except (OSError, json.JSONDecodeError) as e:
            print(f"[WARN] 기존 {DATA_FILE} 읽기 실패 — 빈 상태로 시작: {e}", file=sys.stderr)

    existing_urls = {a["url"] for a in existing}
    cutoff = datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)

    # 2) 보도자료 목록 페이징 스크래핑
    #    - 신규(기존에 없던) 항목이 하나도 없는 페이지를 만나면 캐치업 완료 → 중단.
    #    - retention cutoff 보다 오래된 페이지에 닿으면 중단.
    #    - MAX_PAGES_PER_RUN 안전망.
    new_items = []
    seen_urls = set()
    pages_fetched = 0
    for page in range(1, MAX_PAGES_PER_RUN + 1):
        page_items = fetch_listing_page(page)
        pages_fetched += 1
        if not page_items:
            print(f"page {page}: 항목 0 — 중단")
            break
        fresh = 0
        oldest_on_page = None
        for it in page_items:
            if it["url"] in seen_urls:
                continue
            seen_urls.add(it["url"])
            try:
                d = datetime.fromisoformat(it["pubDate"])
            except (ValueError, TypeError):
                d = None
            if d is not None:
                if oldest_on_page is None or d < oldest_on_page:
                    oldest_on_page = d
            if it["url"] not in existing_urls:
                fresh += 1
            new_items.append(it)
        print(f"page {page}: {len(page_items)}건 (신규 {fresh})")
        # 캐치업 완료: 이 페이지에 신규가 없음 → 이후 페이지는 더 오래된 기존 기사뿐
        if fresh == 0:
            print("  신규 없음 — 캐치업 완료, 중단")
            break
        # retention 밖: 이 페이지 최신도 cutoff 보다 오래됨
        if oldest_on_page is not None and oldest_on_page < cutoff:
            print("  cutoff 초과 페이지 도달 — 중단")
            break
        time.sleep(INTER_REQUEST_SLEEP)

    # 3) merge — URL 기준 중복 제거. 기존 첨부 상태(attachments,_attFails)는 보존.
    merged = {}
    for a in existing:
        merged[a["url"]] = a
    for a in new_items:
        prev = merged.get(a["url"])
        if prev:
            for k in ("attachments", "_attFails"):
                if k in prev:
                    a[k] = prev[k]
        merged[a["url"]] = a

    # 3.5) URL path 필터 — 보도자료(pressReleaseView.do)만 유지 (안전망)
    before_filter = len(merged)
    merged = {u: a for u, a in merged.items() if ALLOWED_URL_PATH in u}
    excluded = before_filter - len(merged)
    if excluded:
        print(f"[URL path 필터] {excluded} 제외")

    # 4) 30일 retention
    kept = []
    expired = 0
    for a in merged.values():
        try:
            d = datetime.fromisoformat(a["pubDate"])
        except (ValueError, TypeError):
            expired += 1
            continue
        if d.tzinfo is None:
            d = d.replace(tzinfo=KST)
        if d >= cutoff:
            kept.append(a)
        else:
            expired += 1

    # 4.5) 옛 hwpxUrl 필드 잔재 제거 (있으면)
    for a in kept:
        a.pop("hwpxUrl", None)

    # 5) 첨부 추출 (hwpx/pdf/hwp) — attachments 없는 entry 중 최신 우선 max N건
    need_atts = [a for a in kept if "attachments" not in a]
    need_atts.sort(key=lambda a: a["pubDate"], reverse=True)   # ② 최신 먼저
    need_atts.sort(key=lambda a: a.get("_attFails", 0))        # ① 미시도 먼저(안정)
    batch = need_atts[:HWPX_URL_BATCH_PER_RUN]
    att_found = att_none = 0
    ext_counter = {}
    if batch:
        print(f"\n[attachments] 추출 {len(batch)} of {len(need_atts)} pending")
        for i, a in enumerate(batch, 1):
            atts = fetch_attachments_for_article(a["url"])
            if atts is None:
                a["_attFails"] = a.get("_attFails", 0) + 1
                if a["_attFails"] >= ATT_MAX_FAILS:
                    a["attachments"] = []; a.pop("_attFails", None); att_none += 1
                time.sleep(INTER_REQUEST_SLEEP); continue
            a["attachments"] = atts; a.pop("_attFails", None)
            if atts:
                att_found += 1
                for x in atts:
                    ext_counter[x["ext"]] = ext_counter.get(x["ext"], 0) + 1
            else:
                att_none += 1
            time.sleep(INTER_REQUEST_SLEEP)
        ext_str = ", ".join(f"{k}:{v}" for k, v in sorted(ext_counter.items()))
        print(f"  done - found {att_found}, none {att_none} ({ext_str})")

    # 6) 발행일 내림차순 정렬
    kept.sort(key=lambda a: a["pubDate"], reverse=True)

    # 7) 출력 조립
    output = {
        "updated": datetime.now(timezone.utc).isoformat(),
        "count": len(kept),
        "retentionDays": RETENTION_DAYS,
        "source": "korea.kr pressReleaseList.do (HTML scrape; RSS/API discontinued 2026-07)",
        "agencies": sorted({a["agency"] for a in kept if a.get("agency")}),
        "articles": kept,
    }
    os.makedirs(os.path.dirname(DATA_FILE) or ".", exist_ok=True)
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    # 8) 요약
    with_att = sum(1 for a in kept if a.get("attachments"))
    no_att = sum(1 for a in kept if a.get("attachments") == [])
    still_pending = sum(1 for a in kept if "attachments" not in a)
    ext_total = {}
    for a in kept:
        for x in a.get("attachments") or []:
            ext_total[x["ext"]] = ext_total.get(x["ext"], 0) + 1
    print("\n" + "=" * 50)
    print(f"pages_fetched: {pages_fetched}")
    print(f"scraped uniq:  {len(seen_urls)}")
    print(f"existing:      {len(existing)}")
    print(f"merged uniq:   {before_filter}")
    print(f"expired:       {expired}")
    print(f"final (30d):   {len(kept)}")
    print(f"  with att:    {with_att}")
    print(f"  no att:      {no_att}")
    print(f"  pending:     {still_pending}")
    print(f"  ext total:   {', '.join(f'{k}:{v}' for k, v in sorted(ext_total.items()))}")
    print(f"agencies:      {len(output['agencies'])}")
    print(f"output:        {DATA_FILE}")
    print("=" * 50)

if __name__ == "__main__":
    main()
