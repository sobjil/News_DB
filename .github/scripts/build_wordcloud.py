#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
data/articles.json -> data/wordcloud.json 생성.

GDI-Apps WeeklyBrief (주간정책동향 딸깍) 인덱스 우측 사이드바의
"최근 한 달간의 주요 이슈" 워드클라우드 데이터 빌더.

전략 (v5 — 2026-06-17, Gemini 의미 선별 + 통계 안전망):
  [추출]  kiwipiepy 형태소 분석 -> 명사(NNG/NNP) 2자 이상 + 인접 결합("인공"+"지능"->"인공지능")
  [C 결정론] 정부 보도자료 특화 불용어(확장) + 부처별 동적 불용어(자기 부처명·약칭) + doc-frequency/keyness
  [A 의미]   ★ Gemini 가 전체 상위 빈도어를 받아 "정책 주제어 vs 일상·행정·절차어"를 선별 ->
             일상어(점검·논의·대응·방문·참석·간담회 등) 제거. 크기(count=빈도)는 그대로 유지.
             - 멀티키 리볼빙(여러 GEMINI 키 라운드로빈, 429/503 시 다음 키) + 모델 404 폴백
             - 환각 차단: 입력 목록에 실제 있는 단어만 drop, 새 단어 생성 무시
             - 키 미설정·전부 실패 시 자동 폴백(확장 불용어 + keyness) — 빌드는 절대 안 깨짐
  [출력]  전체 + 부처별(52개) top 200/150 단어, articleCount desc 정렬

왜 통계(TF-IDF)만으론 부족한가 (2026-06-17 실측):
  점검(250건)·논의(314건) 같은 상용어도 전체 ~2,600건 중 ~10%에만 나와 idf 가 충분히 안 깎임.
  -> keyness(tf*idf) 순위도 점검·대응·논의가 그대로 상위. "흔한 상용어"와 "흔한 핵심주제(인공지능)"는
     통계로 구분 불가 = 의미 판단(Gemini) 필요. 통계(C)는 안전망·폴백 역할.

출력 형식:
  {
    "generated": "ISO 8601 (KST)",
    "totalArticles": 1859,
    "retentionDays": 30,
    "wordSelection": "gemini" | "fallback",   # 이번 빌드가 Gemini 선별을 썼는지
    "agencies": {
      "all": { "label": "전체", "articleCount": 1859, "words": [{"text": "AI", "count": 412}, ...] },
      "msit": { "label": "과학기술정보통신부", "articleCount": 87, "words": [...] },
      ...
    }
  }

매일/매시간 GitHub Actions 로 자동 실행.
의존성: kiwipiepy (pip install kiwipiepy). Gemini 호출은 stdlib(urllib)만 사용 — 추가 의존성 X.
환경변수: GEMINI_API_KEYS (줄바꿈/콤마 구분, 여러 개 권장) 또는 GEMINI_API_KEY (1개). 미설정 시 폴백.
"""

import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timedelta, timezone

try:
    from kiwipiepy import Kiwi
except ImportError:
    print("ERROR: kiwipiepy 설치 필요. pip install kiwipiepy", file=sys.stderr)
    sys.exit(1)

# 전역 불용어 (모든 부처 공통, v3: 더 강화)
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
    # v2: 사람 직책 + v3: 청장 등 추가
    "장관", "차관", "국장", "실장", "처장", "원장", "단장", "팀장", "과장", "부장",
    "지사", "위원장", "위원", "의장", "총리", "대통령", "대변인", "보좌관",
    "비서관", "심의관", "주무관", "사무관", "서기관", "과학관", "주재관",
    "박사", "교수", "연구원", "선임", "수석", "책임", "전문관",
    "이사장", "사장", "회장", "부사장", "전무", "상무",
    "청장", "원장님", "장",
    # v2: 발언/인용 패턴 자주 등장
    "강조", "당부", "설명", "언급", "지적", "전달", "표명",
    # v3: korea.kr 보도자료 시스템 자체 토큰 (전 부처 공통)
    "자료제공", "첨부파일", "보도자료", "용량", "클릭", "다운로드",
    "이미지", "사진", "그림", "동영상", "영상", "파일",
    # v3: 발표·일정 흔한 토큰
    "오전", "오후", "오늘", "내일", "어제", "당일", "당해", "당년",
    "지난해", "올해", "내년", "금년", "예년", "근래",
    "기념", "기념일", "축사", "축하", "인사말",
    # v3: 정부 보도자료 상투어
    "국민", "국가", "지역", "현장", "사회", "공공", "공익", "민간",
    "전국", "전세계", "세계", "글로벌", "국제", "국내", "해외",
    "대상", "대해서", "대해", "이를", "이에", "이는",
    "통한", "이용", "통합", "종합",
    # v3: 추상명사
    "방향", "노력", "기여", "기반", "기여도", "성과", "효과", "변화", "발전",
    "성장", "혁신", "선도", "확보", "제공", "제시", "제출", "제안",
}

# v5 (2026-06-17): 실측 워드클라우드에서 상위를 점령하던 '일상·행정·절차' 상용어 추가.
#   ★ 명백히 동작·절차·회의·일반 서술인 단어만 (특정 정책·기술·산업 주제어는 제외 — Gemini 가 판단).
#   사용자 지적 예시(점검·논의)를 포함해 결정론적으로 보장(Gemini 미가용 시에도 제거됨).
EXTRA_STOPWORDS = {
    "대응", "점검", "논의", "방문", "참석", "간담회", "면담", "본격", "대비",
    "중심", "진행", "선정", "출범", "행사", "주재", "시행", "발생", "증가",
    "확산", "기관", "협약", "체결", "모색", "개막", "토론", "추진단", "착수",
    "방안", "추진", "운영",  # (이미 있을 수 있으나 set 이라 중복 무해)
}
STOPWORDS |= EXTRA_STOPWORDS

# 최소 단어 길이 (1자 단어 제외)
MIN_LEN = 2
# 부처별 top N 단어
TOP_N = 150
# 전체 top N (사이드바 큰 워드클라우드용)
TOP_N_ALL = 200
# 결합 후 최대 길이 (너무 긴 합성어 방지)
MAX_LEN = 12

# ─────────────────────────────────────────────────────────────────────────
# Gemini 의미 선별 설정 (A) — rule #11: 최신 모델 + 404 폴백
# ─────────────────────────────────────────────────────────────────────────
GEMINI_MODELS = ["gemini-3.5-flash", "gemini-3.1-flash-lite"]  # 주모델 -> 한도/404 폴백
GEMINI_TOPK = 400        # 전체 상위 N개 빈도어만 Gemini 분류 (불용어는 고빈도에 집중 = 이걸로 충분)
GEMINI_CHUNK = 120       # 호출당 단어 수 (JSON 안정성 + 토큰 절약)
GEMINI_TIMEOUT = 60
GEMINI_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
DROP_OVERFLOW_RATIO = 0.8   # 한 청크에서 80% 초과 drop = 오작동으로 간주, 무시(안전장치)

DROP_SCHEMA = {
    "type": "object",
    "properties": {"drop": {"type": "array", "items": {"type": "string"}}},
    "required": ["drop"],
}

PROMPT_HEADER = (
    "다음은 한국 정부 보도자료에서 자주 등장한 명사 목록입니다.\n"
    "이 목록에서 '정책 워드클라우드에 주제어로 남길 가치가 없는 단어'만 골라 drop 에 넣으세요.\n\n"
    "■ drop 에 넣을 것(제거): 일상적·행정적·절차적으로 두루 쓰여 변별력이 없는 단어.\n"
    "  예) 점검, 논의, 대응, 방문, 참석, 간담회, 추진, 계획, 진행, 선정, 시행, 발생, 개최,\n"
    "      회의, 발표, 강조, 당부, 모색, 마련, 확대, 강화 같은 동작·절차·회의·일반 서술어.\n"
    "■ drop 에 넣지 말 것(보존): 구체적인 정책·기술·산업·제도 주제어, 그리고 고유명사\n"
    "  (부처·기관·지역·인명·법령·제품·사업명). 분야를 특정하는 명사(예: 인공지능, 반도체,\n"
    "   원전, 탄소중립, 개인정보, 청년, 저출생, 수출, 농업 등)는 반드시 남기세요.\n\n"
    "■ 규칙\n"
    "  - 반드시 아래 입력 목록에 실제로 있는 단어만 drop 에 넣으세요. 새 단어를 만들지 마세요.\n"
    "  - 애매하면 보존하세요(drop 하지 마세요). 과도하게 지우지 마세요.\n"
    '  - JSON 형식 {"drop":[...]} 으로만 답하세요.\n\n'
    "단어 목록:\n"
)


def load_gemini_keys():
    """GEMINI_API_KEYS(줄바꿈/콤마/공백 구분) 우선, 없으면 GEMINI_API_KEY. 중복 제거(순서 보존)."""
    raw = os.environ.get("GEMINI_API_KEYS") or os.environ.get("GEMINI_API_KEY") or ""
    keys = [k.strip() for k in re.split(r"[\n,\s]+", raw) if k.strip()]
    seen, out = set(), []
    for k in keys:
        if k not in seen:
            seen.add(k)
            out.append(k)
    return out


def _gemini_call(model: str, key: str, prompt: str) -> list:
    """단일 Gemini generateContent 호출 -> drop 단어 리스트. 실패 시 예외."""
    url = GEMINI_ENDPOINT.format(model=model, key=urllib.parse.quote(key, safe=""))
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0,
            # 분류 작업엔 사고 불필요 — 토큰 절약 + MAX_TOKENS 방지 (shared/gemini.js 와 동일)
            "thinkingConfig": {"thinkingBudget": 0},
            "responseMimeType": "application/json",
            "responseSchema": DROP_SCHEMA,
        },
    }
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=GEMINI_TIMEOUT) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    cands = payload.get("candidates", [])
    if not cands:
        raise RuntimeError("no candidates")
    parts = cands[0].get("content", {}).get("parts", [])
    text = "".join(p.get("text", "") for p in parts).strip()
    obj = json.loads(text)
    drop = obj.get("drop", [])
    return drop if isinstance(drop, list) else []


def gemini_drop_for_chunk(words: list, keys: list, key_idx: int):
    """
    한 청크 단어 목록 -> drop 리스트 반환. (drop, next_key_idx).
    멀티키 라운드로빈(429/503/5xx -> 다음 키) + 모델 404 폴백(다음 모델).
    전부 실패하면 빈 리스트(폴백).
    """
    prompt = PROMPT_HEADER + ", ".join(words)
    n_keys = len(keys)
    for model in GEMINI_MODELS:
        model_dead = False
        for _ in range(n_keys):
            key = keys[key_idx % n_keys]
            key_idx += 1
            try:
                return _gemini_call(model, key, prompt), key_idx
            except urllib.error.HTTPError as e:
                if e.code in (400, 404):           # 모델 없음/요청형식 -> 다음 모델
                    print(f"    Gemini {model} HTTP {e.code} -> 다음 모델로 폴백")
                    model_dead = True
                    break
                if e.code in (429, 503, 500, 502):  # 한도/과부하 -> 다음 키
                    print(f"    Gemini HTTP {e.code} -> 다음 키로 회전")
                    time.sleep(1.5)
                    continue
                print(f"    Gemini HTTP {e.code} -> 다음 키")  # 403 등(잘못된 키)
                continue
            except Exception as e:               # 네트워크/JSON 등 -> 다음 키
                print(f"    Gemini 호출 실패({type(e).__name__}) -> 다음 키")
                continue
        if model_dead:
            continue
    return [], key_idx


def gemini_drop_set(candidates: list, keys: list) -> set:
    """전체 상위 빈도어(candidates)를 청크로 Gemini 분류 -> drop 단어 집합(환각/오작동 방어)."""
    drop = set()
    key_idx = 0
    n_chunks = (len(candidates) + GEMINI_CHUNK - 1) // GEMINI_CHUNK
    for ci in range(n_chunks):
        chunk = candidates[ci * GEMINI_CHUNK:(ci + 1) * GEMINI_CHUNK]
        d, key_idx = gemini_drop_for_chunk(chunk, keys, key_idx)
        chunkset = set(chunk)
        d = [w for w in d if w in chunkset]              # ★ 환각 차단: 입력에 있는 단어만
        if len(d) > DROP_OVERFLOW_RATIO * len(chunk):    # ★ 안전장치: 과도 drop = 오작동 무시
            print(f"    WARN: 청크 {ci+1}/{n_chunks} drop>{int(DROP_OVERFLOW_RATIO*100)}% ({len(d)}/{len(chunk)}) -> 무시")
            d = []
        drop.update(d)
        print(f"    청크 {ci+1}/{n_chunks}: drop {len(d)}개")
    return drop


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
        # v4: 결합 시 단독 토큰 추가 X (인공지능 OK, "인공"+"지능" 따로 카운트 X)
        if 1 < j - i:
            # 결합만 카운트. MIN_LEN ~ MAX_LEN 범위 안일 때
            if MIN_LEN <= len(combined) <= MAX_LEN:
                out.append(combined)
            i = j
        else:
            # 단독 명사 (인접 결합 X)
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
    부처 label 자체에서 추출 가능한 모든 토큰 + 결합형 + substring 모두 = 동적 불용어.
    예: '과학기술정보통신부' -> {'과학기술정보통신부', '과학', '기술', '정보', '통신', '학기술', ...}
    """
    if not label:
        return set()
    out = set()
    out.add(label)  # 전체 형태 그대로
    # 형태소 분석 결과 + 결합 모두 추가
    try:
        tokens = kiwi.tokenize(label)
        nouns = [t.form for t in tokens if is_noun_tag(t.tag)]
        out.update(nouns)
        compounds = extract_compound_nouns(label, kiwi)
        out.update(compounds)
    except Exception:
        pass
    # 끝 글자 (부/청/처/원/위원회 등) 자체도 제외
    if len(label) > 1:
        out.add(label[-1])
    # v4: label 의 모든 길이 2 이상 연속 substring 추가 (외교부 -> 외교, 교부 / 통신부 -> 통신)
    L = len(label)
    for i in range(L):
        for j in range(i + MIN_LEN, min(L, i + MAX_LEN) + 1):
            sub = label[i:j]
            if sub:
                out.add(sub)
    return out


# v4: 부처 코드 -> 자주 쓰이는 약칭 매핑 (substring 으로 못 잡는 줄임말)
# 자기 부처 워드클라우드에서만 적용. 다른 부처 결과에는 영향 X (참고 정보로 유지 가능).
AGENCY_ALIASES = {
    'msit':   ['과기정통부', '과기부', '과기', '정통부'],
    'motie':  ['산업부', '산자부', '산업통상부', '통상자원부'],
    'moef':   ['기재부', '재정부', '재경부'],
    'mois':   ['행안부', '안행부'],
    'molit':  ['국토부', '국토교통부'],
    'mohw':   ['복지부', '보건부'],
    'moel':   ['고용부', '노동부'],
    'mcst':   ['문체부', '문화부'],
    'me':     ['환경부'],  # 환경부 자체 — 다른 토큰 줄임 X
    'mafra':  ['농식품부', '농림부', '농수산부'],
    'mof':    ['해수부', '해양부'],
    'mssba':  ['중기부', '중소기업부', '벤처부'],
    'mfds':   ['식약처', '식약청'],
    'kcc':    ['방통위'],
    'fsc':    ['금융위'],
    'ftc':    ['공정위'],
    'mpva':   ['보훈부', '보훈처'],
    'unikorea': ['통일부'],
    'opm':    ['총리실', '국조실'],
    'mnd':    ['국방부'],
    'mofa':   ['외교부'],
    'moj':    ['법무부'],
    'moe':    ['교육부'],
    'kcs':    ['관세청'],
    'nts':    ['국세청'],
    'sma':    ['해병대', '해군'],
    'spo':    ['검찰청', '검찰'],
    'kna':    ['경찰청', '경찰'],
}


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
    all_words = Counter()     # 전체 term frequency (단어 크기 신호)
    all_df = Counter()        # 전체 document frequency (기사 단위) — keyness/idf 용 (C)
    n_docs = 0                # 단어가 1개 이상 추출된 기사 수
    by_agency = {}            # {agencyCode: {label, articleCount, counter}}

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

        n_docs += 1
        all_words.update(nouns)
        for w in set(nouns):       # 기사 단위 문서빈도 (C: keyness)
            all_df[w] += 1
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

    # v2~v4: 부처별 자기 부처명 + 약칭 제거
    print("Applying per-agency dynamic stopwords (자기 부처명 + 약칭 제거)...")
    for code, info in by_agency.items():
        ag_stops = agency_stopwords(info['label'], kiwi)
        # v4: 약칭 매핑 추가
        if code in AGENCY_ALIASES:
            ag_stops.update(AGENCY_ALIASES[code])
        if ag_stops:
            info['counter'] = Counter({w: c for w, c in info['counter'].items() if w not in ag_stops})

    # ── C: keyness (tf*idf, 기사 단위 doc-frequency) — Gemini 폴백 순위/안전망 ──
    def idf(w: str) -> float:
        return math.log((n_docs + 1) / (all_df.get(w, 0) + 1)) + 1.0

    # ── A: Gemini 의미 선별 — 전체 상위 빈도어에서 일상·행정·절차어 drop ──
    keys = load_gemini_keys()
    drop_set = set()
    selection_mode = "fallback"
    if keys:
        candidates = [w for w, _ in all_words.most_common(GEMINI_TOPK)]
        print(f"Gemini 선별 시작: 상위 {len(candidates)}개 후보, 키 {len(keys)}개 라운드로빈...")
        try:
            drop_set = gemini_drop_set(candidates, keys)
        except Exception as e:
            print(f"  Gemini 선별 전체 실패({e}) -- 폴백(확장 불용어+keyness)")
            drop_set = set()
        if drop_set:
            selection_mode = "gemini"
        print(f"Gemini drop set: {len(drop_set)}개 -> 모든 클라우드에 적용")
    else:
        print("GEMINI 키 없음(GEMINI_API_KEYS/GEMINI_API_KEY) -- Gemini 선별 skip, 폴백 사용")

    def finalize(counter: Counter, n: int, by_keyness_fallback: bool) -> list:
        """drop_set 제거 -> 단어 목록(크기=count). Gemini 적용 시 count 순,
        폴백(Gemini 미가용) 시 부처 클라우드는 keyness 순(변별력 높은 단어 우선)."""
        items = [(w, c) for w, c in counter.items() if w not in drop_set]
        if selection_mode == "fallback" and by_keyness_fallback:
            items.sort(key=lambda x: -(x[1] * idf(x[0])))   # C: keyness 폴백 순위
        else:
            items.sort(key=lambda x: -x[1])                  # 빈도(count) 순
        return [{"text": w, "count": c} for w, c in items[:n]]

    kst = timezone(timedelta(hours=9))
    result = {
        "generated": datetime.now(kst).isoformat(),
        "totalArticles": len(articles),
        "retentionDays": data.get('retentionDays', 30),
        "wordSelection": selection_mode,
        "agencies": {
            "all": {
                "label": "전체",
                "articleCount": len(articles),
                "words": finalize(all_words, TOP_N_ALL, by_keyness_fallback=False),
            }
        }
    }

    # articleCount desc 정렬
    sorted_agencies = sorted(by_agency.items(), key=lambda x: -x[1]['articleCount'])
    for code, info in sorted_agencies:
        result["agencies"][code] = {
            "label": info['label'],
            "articleCount": info['articleCount'],
            "words": finalize(info['counter'], TOP_N, by_keyness_fallback=True),
        }

    print(f"saving: {output_path}")
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    print(f"OK wordcloud.json built (selection={selection_mode})")
    print(f"   - total {len(articles)} articles -> top {TOP_N_ALL} words")
    print(f"   - agencies {len(by_agency)} -> each top {TOP_N} words")
    allw = result["agencies"]["all"]["words"]
    if allw:
        print(f"   - all top 10: {[w['text'] for w in allw[:10]]}")


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
