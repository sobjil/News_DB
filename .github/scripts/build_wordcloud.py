#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
data/articles.json -> data/wordcloud.json 생성.

GDI-Apps WeeklyBrief (주간정책동향 딸깍) 인덱스 우측 사이드바의
"최근 한 달간의 주요 이슈" 워드클라우드 데이터 빌더.

전략 (v2):
  - kiwipiepy 형태소 분석 -> 명사(NNG/NNP) 추출, 2자 이상
  - 인접 NNG/NNP 자동 결합 ("인공"+"지능" -> "인공지능", "산업"+"통상"+"자원"+"부" -> "산업통상자원부")
  - 정부 보도자료 특화 불용어 제거 (정책/사업/지원/추진/운영/계획/장관/국장/...)
  - 부처별 동적 불용어: 부처 label 자체 토큰 + 부처명 결합 형태 제외 (예: 과학기술정보통신부 결과에서 자기 부처명 빼기)
  - 전체 + 부처별(52개) top 150/100 단어 빈도 누적
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

# 전역 불용어 (모든 부처 공통, v2: 사람 직책 강화)
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
    "위원회", "부처", "정부", "원회",
    "분야", "방안", "대책", "마련", "추가", "공동", "협력", "강화", "확대", "개선",
    "구축", "도입", "조성", "개발", "활용", "참여", "현황", "체계", "역할",
    "최근", "이상", "이하", "이내", "정도", "총", "전년", "동기", "비",
    "주요", "주", "중", "본", "당", "현", "함께", "기간",
    "관계자", "담당자",
    "보도", "자료", "참고", "별첨", "붙임", "첨부",
    # 너무 일반적 단위·접미사
    "원", "개", "명", "건", "곳", "단계", "단위", "수준",
    # v2: 사람 직책 (이름 자체는 NNP 라 거의 못 빼지만 직책 토큰은 제거)
    "장관", "차관", "국장", "실장", "처장", "원장", "단장", "팀장", "과장", "부장",
    "지사", "위원장", "위원", "의장", "총리", "대통령", "대변인", "보좌관",
    "비서관", "심의관", "주무관", "사무관", "서기관", "과학관", "주재관",
    "박사", "교수", "연구원", "선임", "수석", "책임", "전문관",
    "이사장", "사장", "회장", "부사장", "전무", "상무",
    # v2: 발언/인용 패턴 자주 등장
    "강조", "당부", "설명", "언급", "지적", "전달", "표명",
}

# 최소 단어 길이 (1자 단어 제외)
MIN_LEN = 2
# 부처별 top N 단어
TOP_N = 150
# 전체 top N (사이드바 큰 워드클라우드용)
TOP_N_ALL = 200
# 결합 후 최대 길이 (너무 긴 합성어 방지)
MAX_LEN = 12


def is_noun_tag(tag: str) -> bool:
    """명사 태그 (NNG: 일반명사, NNP: 고유명사). XSN(접미사) 도 결합용으로만 허용."""
    return tag in ('NNG', 'NNP')


def extract_compound_nouns(text: str, kiwi: Kiwi) -> list:
    """
    Kiwi 형태소 분석 -> 인접한 NNG/NNP 자동 결합.

    "인공" + "지능" (사이 띄어쓰기 X) -> "인공지능"
    "산업" + "통상" + "자원" + "부" -> "산업통상자원부"
    "인공" + " " + "지능" (띄어쓰기 있음) -> "인공", "지능" (분리 유지)

    토큰의 start 와 end (= start + len) 가 정확히 인접한 경우만 결합 = 띄어쓰기 X.
    """
    if not text or not text.strip():
        return []
    try:
        tokens = kiwi.tokenize(text)
    except Exception as e:
        print(f"  WARN: tokenize 실패 ({e}) -- skip")
        return []

    out = []
    i = 0
    n = len(tokens)
    while i < n:
        t = tokens[i]
        if not is_noun_tag(t.tag):
            i += 1
            continue
        # 인접한 명사 결합 (start position 이 이전 token 의 end 와 같으면)
        combined = t.form
        combined_end = t.start + t.len
        j = i + 1
        while j < n and is_noun_tag(tokens[j].tag) and tokens[j].start == combined_end:
            combined += tokens[j].form
            combined_end = tokens[j].start + tokens[j].len
            j += 1
        # 1자 결합도 추가 (예: "...부", "...청") — 위 stopword 에서 제거됨
        if 1 < j - i:
            # 연속 결합. 결합 token 이 너무 길거나 짧으면 제외
            if MIN_LEN <= len(combined) <= MAX_LEN:
                out.append(combined)
            # 결합 안에 들어간 개별 명사도 일부는 의미가 있을 수 있어 추가 (i 만 — 첫 토큰)
            # 단 첫 토큰이 2자 이상 + 너무 일반적이지 않을 때만
            if len(t.form) >= MIN_LEN:
                out.append(t.form)
            i = j
        else:
            # 단독 명사
            if MIN_LEN <= len(t.form) <= MAX_LEN:
                out.append(t.form)
            i += 1
    return out


def filter_words(words: list, stopwords: set) -> list:
    """불용어 + 숫자만 + 한자 1자 등 제거."""
    result = []
    for w in words:
        if not w:
            continue
        if w in stopwords:
            continue
        if w.isdigit():
            continue
        # 단어 안에 한글이 하나도 없으면 제외 (전부 한자/숫자/영문 단독 토큰)
        if not any('가' <= c <= '힣' or 'A' <= c <= 'z' for c in w):
            continue
        result.append(w)
    return result


def agency_stopwords(label: str, kiwi: Kiwi) -> set:
    """
    부처 label 자체에서 추출 가능한 모든 토큰 + 결합형 = 동적 불용어.
    예: '과학기술정보통신부' -> {'과학기술정보통신부', '과학', '기술', '정보', '통신', '과학기술', ...}
    """
    if not label:
        return set()
    out = set()
    out.add(label)  # 전체 형태 그대로
    # 형태소 분석 결과 + 결합 모두 추가
    try:
        tokens = kiwi.tokenize(label)
    except Exception:
        return out
    nouns = [t.form for t in tokens if is_noun_tag(t.tag)]
    out.update(nouns)
    # 인접 결합도 추가 (extract_compound_nouns 와 같은 로직)
    compounds = extract_compound_nouns(label, kiwi)
    out.update(compounds)
    # 끝 글자 (부/청/처/원/위원회 등) 자체도 제외
    if len(label) > 1:
        out.add(label[-1])  # '부', '청', '처', '원' 등
    return out


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

        nouns = extract_compound_nouns(text, kiwi)
        nouns = filter_words(nouns, STOPWORDS)
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

    # v2: 부처별 자기 부처명 추가 제거
    print("Applying per-agency dynamic stopwords (자기 부처명 제거)...")
    for code, info in by_agency.items():
        ag_stops = agency_stopwords(info['label'], kiwi)
        if ag_stops:
            before = sum(info['counter'].values())
            info['counter'] = Counter({w: c for w, c in info['counter'].items() if w not in ag_stops})
            after = sum(info['counter'].values())
            print(f"  {code}({info['label']}): -{before - after} tokens (label stopwords: {len(ag_stops)})")

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
        top5 = all_words.most_common(5)
        print(f"   - top 5: {top5}")


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
