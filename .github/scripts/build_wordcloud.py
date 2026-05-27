#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
data/articles.json → data/wordcloud.json 생성.

GDI-Apps WeeklyBrief (주간정책동향 딸깍) 인덱스 우측 사이드바의
"최근 한 달간의 주요 이슈" 워드클라우드 데이터 빌더.

전략:
  - kiwipiepy 형태소 분석 → 명사(NNG·NNP) 추출, 2자 이상
  - 정부 보도자료 특화 불용어 제거
  - 전체 + 부처별(52개) top 100 단어 빈도 누적
  - articleCount desc 정렬

출력 형식:
  {
    "generated": "ISO 8601 (KST)",
    "totalArticles": 1859,
    "retentionDays": 30,
    "agencies": {
      "all": { "label": "전체", "articleCount": 1859, "words": [{"text": "AI", "count": 412}, ...] },
      "msit": { "label": "과학기술정보통신부", "articleCount": 87, "words": [...] },
      ...
    }
  }

매일 자정 (KST = UTC 15:00) GitHub Actions cron 으로 자동 실행.
의존성: kiwipiepy (pip install kiwipiepy)
"""

import json
import os
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone

try:
    from kiwipiepy import Kiwi
except ImportError:
    print("ERROR: kiwipiepy 설치 필요. pip install kiwipiepy", file=sys.stderr)
    sys.exit(1)

# 불용어 사전 (의미 X 너무 흔한 단어 — 정부 보도자료 특화)
STOPWORDS = {
    # 일반 명사·의존명사
    "것", "수", "등", "및", "시", "이번", "통해", "위해", "관련", "대한", "가운데",
    "가지", "만큼", "모두", "전체", "더", "또", "또한", "각", "각각", "이",
    "한", "두", "세", "년", "월", "일", "분", "초", "회", "차", "건",
    "데", "때", "곳", "쪽", "안", "밖", "위", "아래", "앞", "뒤", "내",
    "전", "후", "신", "구", "구성", "구분", "내용", "결과",
    # 자주 등장 동사 명사형
    "확인", "검토", "준비", "예정", "예상", "전망", "발견", "감사",
    # 정부 도메인 특화 (변별력 X)
    "정책", "사업", "지원", "추진", "운영", "실시", "계획", "발표", "개최", "회의",
    "위원회", "부처", "정부", "원회", "원장", "단장", "장관", "차관",
    "분야", "방안", "대책", "마련", "추가", "공동", "협력", "강화", "확대", "개선",
    "구축", "도입", "조성", "개발", "활용", "참여", "현황", "체계", "역할",
    "최근", "이상", "이하", "이내", "정도", "총", "전년", "동기", "비",
    "주요", "주", "중", "본", "당", "현", "함께", "기간",
    "관계자", "담당자",
    "보도", "자료", "참고", "별첨", "붙임", "첨부",
    # 너무 일반적 단위·접미사
    "원", "개", "명", "건", "곳", "단계", "단위", "수준",
}

# 최소 단어 길이 (1자 단어 제외)
MIN_LEN = 2
# 부처별 top N 단어
TOP_N = 100
# 전체 top N (사이드바 큰 워드클라우드용)
TOP_N_ALL = 150


def extract_nouns(text: str, kiwi: Kiwi) -> list:
    """Kiwi 형태소 분석 → 명사만 추출 (NNG: 일반명사, NNP: 고유명사)."""
    if not text or not text.strip():
        return []
    try:
        tokens = kiwi.tokenize(text)
    except Exception as e:
        print(f"  WARN: tokenize 실패 ({e}) -- skip")
        return []
    nouns = []
    for t in tokens:
        if t.tag not in ('NNG', 'NNP'):
            continue
        form = t.form.strip()
        if len(form) < MIN_LEN:
            continue
        if form in STOPWORDS:
            continue
        # 한자·숫자만으로 된 토큰 제외
        if form.isdigit():
            continue
        nouns.append(form)
    return nouns


def build_wordcloud(articles_path: str, output_path: str) -> None:
    print(f"articles.json load: {articles_path}")
    with open(articles_path, 'r', encoding='utf-8') as f:
        data = json.load(f)

    articles = data.get('articles', [])
    if not articles:
        print("articles empty -- write empty result")
        return

    print(f"loaded {len(articles)} articles. Initializing Kiwi...")
    kiwi = Kiwi()

    # 전체 + 부처별 누적
    all_words = Counter()
    by_agency = {}  # {agencyCode: {label, articleCount, counter}}

    for i, art in enumerate(articles):
        title = art.get('title', '') or ''
        desc = art.get('description', '') or ''
        text = f"{title}\n{desc}"
        code = art.get('agencyCode', '')
        label = art.get('agency', '')

        if not code:
            continue

        nouns = extract_nouns(text, kiwi)
        if not nouns:
            continue

        all_words.update(nouns)
        if code not in by_agency:
            by_agency[code] = {
                'label': label,
                'articleCount': 0,
                'counter': Counter(),
            }
        by_agency[code]['articleCount'] += 1
        by_agency[code]['counter'].update(nouns)

        if (i + 1) % 200 == 0:
            print(f"  ... {i + 1}/{len(articles)} processed")

    def top_words(counter: Counter, n: int) -> list:
        return [{"text": w, "count": c} for w, c in counter.most_common(n)]

    kst = timezone(timedelta(hours=9))
    result = {
        "generated": datetime.now(kst).isoformat(),
        "totalArticles": len(articles),
        "retentionDays": data.get('retentionDays', 30),
        "agencies": {
            "all": {
                "label": "전체",
                "articleCount": len(articles),
                "words": top_words(all_words, TOP_N_ALL),
            }
        }
    }

    # articleCount desc 정렬
    sorted_agencies = sorted(by_agency.items(), key=lambda x: -x[1]['articleCount'])
    for code, info in sorted_agencies:
        result["agencies"][code] = {
            "label": info['label'],
            "articleCount": info['articleCount'],
            "words": top_words(info['counter'], TOP_N),
        }

    print(f"saving: {output_path}")
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    print(f"OK wordcloud.json built")
    print(f"   - total {len(articles)} articles -> top {TOP_N_ALL} words")
    print(f"   - agencies {len(by_agency)} -> each top {TOP_N} words")
    if all_words:
        print(f"   - top word: {all_words.most_common(1)[0]}")


def main() -> int:
    base = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    articles_path = os.path.join(base, 'data', 'articles.json')
    output_path = os.path.join(base, 'data', 'wordcloud.json')

    if not os.path.exists(articles_path):
        print(f"ERROR: {articles_path} missing. Run crawl first.", file=sys.stderr)
        return 1

    try:
        build_wordcloud(articles_path, output_path)
    except Exception as e:
        print(f"ERROR: build_wordcloud failed -- {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
