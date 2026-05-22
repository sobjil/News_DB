# News_DB — 대한민국 정부 보도자료 자동 크롤링 DB

[GDI-Apps](https://github.com/sobjil/GDI-Apps) 의 **주간정책동향 딸깍** (WeeklyBrief) 앱 데이터 소스.

GitHub Actions 가 10분마다 `korea.kr` 의 부처별 RSS feed 를 크롤링해 `data/articles.json` 을 자동 갱신합니다.

## 데이터 URL

```
https://raw.githubusercontent.com/sobjil/News_DB/main/data/articles.json
```

또는 jsDelivr CDN (글로벌 캐시):

```
https://cdn.jsdelivr.net/gh/sobjil/News_DB@main/data/articles.json
```

## 구조

```json
{
  "updated": "ISO 8601",
  "count": 1859,
  "retentionDays": 30,
  "agencies": ["과학기술정보통신부", "교육부", ...],
  "articles": [
    {
      "title": "보도자료 제목",
      "url": "https://www.korea.kr/...",
      "agency": "부처명",
      "agencyCode": "msit",
      "pubDate": "ISO 8601 (KST)",
      "description": "(처음 300자)"
    }
  ]
}
```

## 동작

- **소스**: `korea.kr` 52개 부처별 RSS (`/rss/dept_{code}.xml`)
- **주기**: 10분마다 (GitHub Actions cron — 5~15분 지연 가능)
- **저장**: 메타만 (제목·URL·부처·일자·짧은 설명)
- **Retention**: 30일 (그 이전 entry 자동 삭제)
- **중복 제거**: URL 기준
- **정렬**: 발행일 내림차순
- **저작권**: 정부 보도자료는 공공저작물 — 자유 이용 (`KOGL Type 1`)

## 수동 트리거

[Actions 탭](https://github.com/sobjil/News_DB/actions/workflows/crawl-policy-news.yml) → `[Run workflow]`

## 자동 편집·수정 금지

이 레포는 GitHub Actions 가 관리하는 데이터 미러입니다. `data/articles.json` 을 직접 수정하면 다음 cron 에서 덮어쓰여집니다. 크롤러 로직 변경은 `.github/scripts/crawl_korea_kr.py` 에서.
