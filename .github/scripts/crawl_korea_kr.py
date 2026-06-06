#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
korea.kr 부처별 RSS 크롤러 → data/articles.json 갱신.

WeeklyBrief (주간정책동향 딸깍) 의 데이터 소스.
GitHub Actions 가 매일 1회 자동 실행.
의존성: 표준 라이브러리만 (urllib + xml.etree). pip install X.

전략:
  - 52개 부처별 RSS feed (https://www.korea.kr/rss/dept_{code}.xml) 순차 호출
  - 메타만 저장 (제목·URL·부처·일자·짧은 설명) — devplan §주간 정책 브리핑 명시
  - 기존 articles.json 과 merge, URL 기준 중복 제거
  - 30일 이전 entry 자동 삭제 (retention)
  - 발행일 내림차순 정렬

출력 형식:
  {
    "updated": "2026-05-23T23:00:00+00:00",
    "count": 1234,
    "agencies": ["과학기술정보통신부", "교육부", ...],
    "articles": [
      {
        "title": "...",
        "url": "https://www.korea.kr/news/...",
        "agency": "과학기술정보통신부",
        "agencyCode": "msit",
        "pubDate": "2026-05-22T10:30:00+09:00",
        "description": "..."
      },
      ...
    ]
  }
"""

import html as html_mod
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError
from xml.etree import ElementTree as ET

# ─── 부처 코드 → 한글명 (2026-05-23, korea.kr/etc/rss.do 추출) ────────
# 52개 부처·청·위원회. korea.kr 의 RSS 페이지에서 직접 추출.
DEPT = {
    "opm":          "국무조정실",
    "moef":         "재정경제부",
    "msit":         "과학기술정보통신부",
    "moe":          "교육부",
    "mofa":         "외교부",
    "unikorea":     "통일부",
    "moj":          "법무부",
    "mnd":          "국방부",
    "mois":         "행정안전부",
    "mpva":         "국가보훈부",
    "mcst":         "문화체육관광부",
    "mafra":        "농림축산식품부",
    "motir":        "산업통상부",
    "mw":           "보건복지부",
    "mcee":         "기후에너지환경부",
    "moel":         "고용노동부",
    "mogef":        "성평등가족부",
    "molit":        "국토교통부",
    "mof":          "해양수산부",
    "mss":          "중소벤처기업부",
    "mpb":          "기획예산처",
    "mpm":          "인사혁신처",
    "moleg":        "법제처",
    "mfds":         "식품의약품안전처",
    "mods":         "국가데이터처",
    "moip":         "지식재산처",
    "nts":          "국세청",
    "customs":      "관세청",
    "pps":          "조달청",
    "kasa":         "우주항공청",
    "oka":          "재외동포청",
    "spo":          "검찰청",
    "mma":          "병무청",
    "dapa":         "방위사업청",
    "npa":          "경찰청",
    "nfa":          "소방청",
    "khs":          "국가유산청",
    "rda":          "농촌진흥청",
    "forest":       "산림청",
    "kdca":         "질병관리청",
    "kma":          "기상청",
    "sda":          "새만금개발청",
    "kcg":          "해양경찰청",
    "kmcc":         "방송미디어통신위원회",
    "nssc":         "원자력안전위원회",
    "ftc":          "공정거래위원회",
    "fsc":          "금융위원회",
    "acrc":         "국민권익위원회",
    "pipc":         "개인정보보호위원회",
    "k_cohesion":   "국민통합위원회",
    "betterfuture": "저출산고령사회위원회",
    "esdc":         "경제사회노동위원회",
}

RSS_URL_TPL = "https://www.korea.kr/rss/dept_{code}.xml"
RETENTION_DAYS = 30
DATA_FILE = "data/articles.json"
USER_AGENT = (
    "Mozilla/5.0 (compatible; GDI-Apps-WeeklyBrief/1.0; "
    "+https://github.com/sobjil/GDI-Apps)"
)
REQUEST_TIMEOUT = 20      # 초
INTER_REQUEST_SLEEP = 0.5  # 부처 간 / hwpxUrl fetch 간 딜레이 (rate limit 회피)
DESCRIPTION_MAX = 300      # description 길이 한도 (메타만 유지 원칙)
# 매 run 마다 본문 페이지 fetch 해서 hwpxUrl 추출하는 최대 건수.
# 점진 처리 — 한 번에 너무 많이 안 받고 cron 마다 N건씩 쌓음 (사이트 부담 ↓ + workflow timeout 안전망).
HWPX_URL_BATCH_PER_RUN = 30
ATT_MAX_FAILS = 5          # 첨부 fetch 실패 시 재시도 한도(초과하면 첨부없음으로 확정)
# korea.kr 의 RSS 에는 보도자료 + 정책뉴스 + 사실은 + 카드뉴스 등 4가지 카테고리가 섞여 있음.
# WeeklyBrief 사용자는 '보도자료' 만 필요 (HWPX 첨부 + 정부 공식 발표 패턴).
# URL path 로 구분 — /briefing/pressReleaseView.do 만 통과.
ALLOWED_URL_PATH = "/briefing/pressReleaseView.do"

# ─── korea.kr 한국 IP 프록시 (GitHub 미국 IP 차단 우회) ──────────────
# GitHub Actions(미국 IP)에서 korea.kr 직접 호출 시 SSL handshake timeout 으로 막힌다
# (2026-06 진단: 17개+ 부처 RSS·첨부 대량 실패 → 첨부 '수집 중' 이 며칠째 멈춤).
# AISpace(한국 호스팅 IP)에서는 동일 RSS 가 30~90ms 200 으로 받힌다(검증됨).
# KOREA_PROXY_URL(= https://.../api/proxy/korea) 가 설정되면 korea.kr 요청을 이 프록시
# 경유로 우회한다. 프록시가 한국 IP 로 대신 받아 bytes 그대로 돌려준다.
# 미설정이면 종전처럼 직접 호출(로컬 한국망 테스트 등 하위호환).
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
# 본문 페이지 안 첨부 anchor 패턴 — korea.kr 의 download link
HWPX_ANCHOR_RE = re.compile(
    r'<a[^>]+href="(/common/download\.do\?[^"]+)"[^>]*>(.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)

def clean_text(s):
    """HTML entity 디코드 + 다중 공백 정리. 태그가 있으면 같이 제거."""
    if not s:
        return ""
    s = HTML_TAG_RE.sub(" ", s)
    s = html_mod.unescape(s)  # &amp; &middot; &nbsp; &#xAC00; 등 모든 entity 처리
    return WHITESPACE_RE.sub(" ", s).strip()

def parse_pub_date(s):
    """RFC 822 (e.g. 'Wed, 07 May 2025 09:30:00 +0900') → ISO 8601 string"""
    if not s:
        return None
    try:
        dt = parsedate_to_datetime(s.strip())
        if dt.tzinfo is None:
            # tz 없는 경우 한국시간 가정
            dt = dt.replace(tzinfo=timezone(timedelta(hours=9)))
        return dt.isoformat()
    except Exception:
        return None

# 지원 확장자 — 우선순위 (앞일수록 우선)
SUPPORTED_EXTS = ("hwpx", "pdf", "hwp")
EXT_RE = re.compile(r"\.(hwpx|hwp|pdf)\b", re.IGNORECASE)

def extract_attachments(html, article_url):
    """본문 페이지 HTML 에서 모든 첨부 anchor 추출 → [{ext, url, filename}, ...].
    동일 ext 의 첨부 (본문 + 별첨) 모두 포함. URL 기준 중복 제거.
    빈 리스트 = 첨부 없음.
    """
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
            continue  # URL 기준 중복 (보통 같은 페이지 안 [파일명] + [내려받기] 두 anchor 가 동일 URL)
        seen_urls.add(url)
        # filename — anchor inner 의 텍스트 (img 등 태그 제거)
        fname = HTML_TAG_RE.sub("", inner)
        fname = WHITESPACE_RE.sub(" ", fname).strip()
        # 너무 길면 자름 (보통 80자 정도)
        if len(fname) > 120:
            fname = fname[:120]
        out.append({"ext": ext, "url": url, "filename": fname})
    # 우선순위 정렬 (hwpx > pdf > hwp) — 같은 ext 끼리는 원래 순서 (본문 먼저)
    out.sort(key=lambda a: SUPPORTED_EXTS.index(a["ext"]) if a["ext"] in SUPPORTED_EXTS else 99)
    return out

def fetch_attachments_for_article(article_url):
    """본문 페이지 fetch → 첨부 리스트 추출 (빈 리스트 = 첨부 X 또는 fetch 실패)."""
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
        print(f"    [WARN] hwpxUrl HTTP {e.code}", file=sys.stderr)
        return None
    except URLError as e:
        print(f"    [WARN] 첨부 URLError: {e.reason}", file=sys.stderr)
        return None
    except Exception as e:
        print(f"    [WARN] 첨부 추출 실패: {e}", file=sys.stderr)
        return None
    return extract_attachments(html, article_url)

def fetch_rss(code, name):
    """한 부처 RSS feed 호출 → entry 리스트"""
    url = RSS_URL_TPL.format(code=code)
    req = Request(to_fetch_url(url), headers={
        "User-Agent": USER_AGENT,
        "Accept": "application/rss+xml, application/xml, text/xml",
    })
    try:
        with urlopen(req, timeout=REQUEST_TIMEOUT) as r:
            raw = r.read()
    except HTTPError as e:
        print(f"  [WARN] HTTP {e.code} for {code} ({name})", file=sys.stderr)
        return []
    except URLError as e:
        print(f"  [WARN] URLError for {code} ({name}): {e.reason}", file=sys.stderr)
        return []
    except Exception as e:
        print(f"  [WARN] {code} ({name}): {e}", file=sys.stderr)
        return []

    try:
        root = ET.fromstring(raw)
    except ET.ParseError as e:
        print(f"  [WARN] {code} XML parse error: {e}", file=sys.stderr)
        return []

    items = []
    for it in root.iter("item"):
        title = clean_text(it.findtext("title") or "")
        link = (it.findtext("link") or "").strip()
        pub_raw = (it.findtext("pubDate") or "").strip()
        desc = clean_text(it.findtext("description") or "")[:DESCRIPTION_MAX]
        iso_date = parse_pub_date(pub_raw)
        if not (title and link and iso_date):
            continue
        items.append({
            "title": title,
            "url": link,
            "agency": name,
            "agencyCode": code,
            "pubDate": iso_date,
            "description": desc,
        })
    return items

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

    # 2) 부처별 RSS 크롤
    new_items = []
    fetched_total = 0
    for code, name in DEPT.items():
        print(f"fetching dept_{code} ({name}) ...")
        items = fetch_rss(code, name)
        fetched_total += len(items)
        new_items.extend(items)
        print(f"  + {len(items)} entries")
        time.sleep(INTER_REQUEST_SLEEP)

    # 3) merge — URL 기준 중복 제거 (신규 데이터 우선)
    #    ★ 단, 기존 entry 의 첨부 추출 상태(attachments, _attFails)는 보존한다.
    #    RSS item 은 attachments 필드가 없으므로 그대로 덮어쓰면, 아직 RSS 에 떠 있는
    #    최근 기사는 매 run 첨부가 사라져(통째 교체) 영영 "수집 중" 으로 리셋되는 버그.
    #    → 메타(title·desc·pubDate)는 새 RSS 로 갱신하되 첨부 상태만 이어받는다.
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

    # 3.5) URL path 필터 — 보도자료 (pressReleaseView.do) 만 통과
    #      정책뉴스 (policyNewsView.do), 사실은 (actuallyView.do), 카드뉴스 (visualNewsView.do) 제외
    before_filter = len(merged)
    filtered = {url: a for url, a in merged.items() if ALLOWED_URL_PATH in url}
    excluded = before_filter - len(filtered)
    print(f"\n[URL path 필터] {ALLOWED_URL_PATH} 만 유지 - {before_filter} -> {len(filtered)} ({excluded} 제외: 정책뉴스/사실은/카드뉴스)")
    merged = filtered

    # 4) 30일 retention
    cutoff = datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)
    kept = []
    expired = 0
    for a in merged.values():
        try:
            d = datetime.fromisoformat(a["pubDate"])
        except (ValueError, TypeError):
            expired += 1
            continue
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone(timedelta(hours=9)))
        if d >= cutoff:
            kept.append(a)
        else:
            expired += 1

    # 4.5) v2 마이그레이션 — 이전 hwpxUrl 필드 제거 (attachments 재추출 대상)
    migrated = 0
    for a in kept:
        if "hwpxUrl" in a:
            del a["hwpxUrl"]
            migrated += 1
    if migrated:
        print(f"[migrate] hwpxUrl -> attachments 마이그레이션: {migrated} entry 재추출 대상")

    # 5) 첨부 다중 추출 (hwpx/pdf/hwp) — attachments 필드 없는 entry 중 최신순 max N건
    #    Cloudflare Workers fetch 는 korea.kr SSL 차단 (525) — 사전 추출이 유일한 길.
    #    이전 hwpxUrl 필드 → attachments 배열로 전환 (다중 첨부 지원, ext 다양화).
    # 실패 적은 것 우선(아직 시도 안 한 것 먼저), 그다음 오래된 pending 우선 — backlog 가 영영 안 닿는 starvation 방지
    need_atts = sorted(
        [a for a in kept if "attachments" not in a],
        key=lambda a: (a.get("_attFails", 0), a["pubDate"]),
    )
    batch = need_atts[:HWPX_URL_BATCH_PER_RUN]
    att_found = 0    # 최소 1개 이상 첨부 있는 entry
    att_none = 0     # 첨부 0개
    ext_counter = {}
    if batch:
        print(f"\n[attachments] 추출 {len(batch)} of {len(need_atts)} pending")
        for i, a in enumerate(batch, 1):
            atts = fetch_attachments_for_article(a["url"])
            if atts is None:
                # fetch 실패(차단/timeout) — pending 유지하고 다음 실행에 재시도. 한도 초과 시 포기(첨부없음).
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
            # 이전 호환 — hwpxUrl 필드 제거 (있으면)
            if "hwpxUrl" in a:
                del a["hwpxUrl"]
            if i % 20 == 0:
                ext_str = ", ".join(f"{k}:{v}" for k, v in sorted(ext_counter.items()))
                print(f"  progress {i}/{len(batch)} - found {att_found}, none {att_none} ({ext_str})")
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
        "agencies": sorted({a["agency"] for a in kept}, key=lambda x: x),
        "articles": kept,
    }

    os.makedirs(os.path.dirname(DATA_FILE) or ".", exist_ok=True)
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    # 8) 요약 출력
    with_att = sum(1 for a in kept if a.get("attachments"))
    no_att = sum(1 for a in kept if a.get("attachments") == [])
    still_pending = sum(1 for a in kept if "attachments" not in a)
    # ext 별 통계
    ext_total = {}
    for a in kept:
        for x in a.get("attachments") or []:
            ext_total[x["ext"]] = ext_total.get(x["ext"], 0) + 1
    print()
    print("=" * 50)
    print(f"buchu_count: {len(DEPT)}")
    print(f"rss_total:   {fetched_total}")
    print(f"existing:    {len(existing)}")
    print(f"merged uniq: {len(merged)}")
    print(f"expired:     {expired}")
    print(f"final (30d): {len(kept)}")
    print(f"  with att:  {with_att}")
    print(f"  no att:    {no_att}")
    print(f"  pending:   {still_pending}")
    ext_str = ", ".join(f"{k}:{v}" for k, v in sorted(ext_total.items()))
    print(f"  ext total: {ext_str}")
    print(f"agencies:    {len(output['agencies'])}")
    print(f"output:      {DATA_FILE}")
    print("=" * 50)

if __name__ == "__main__":
    main()
