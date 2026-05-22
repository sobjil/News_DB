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
INTER_REQUEST_SLEEP = 0.5  # 부처 간 딜레이 (rate limit 회피)
DESCRIPTION_MAX = 300      # description 길이 한도 (메타만 유지 원칙)

# ─── 유틸 ────────────────────────────────────────────────────────────
HTML_TAG_RE = re.compile(r"<[^>]+>")
WHITESPACE_RE = re.compile(r"\s+")

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

def fetch_rss(code, name):
    """한 부처 RSS feed 호출 → entry 리스트"""
    url = RSS_URL_TPL.format(code=code)
    req = Request(url, headers={
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
    merged = {}
    for a in existing:
        merged[a["url"]] = a
    for a in new_items:
        merged[a["url"]] = a

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

    # 5) 발행일 내림차순 정렬
    kept.sort(key=lambda a: a["pubDate"], reverse=True)

    # 6) 출력 조립
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

    # 7) 요약 출력
    print()
    print("=" * 50)
    print(f"부처 수:         {len(DEPT)}")
    print(f"RSS entry (총):  {fetched_total}")
    print(f"기존:            {len(existing)}")
    print(f"merge 후 unique: {len(merged)}")
    print(f"만료 제거:       {expired}")
    print(f"최종 (30일 내):  {len(kept)}")
    print(f"부처 (실데이터): {len(output['agencies'])}")
    print(f"출력:            {DATA_FILE}")
    print("=" * 50)

if __name__ == "__main__":
    main()
