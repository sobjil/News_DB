#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""첨부 링크 재확인(5.5) 선별 규칙 자가검증. 네트워크 X.

    python .github/scripts/test_crawl_refresh.py
"""
import importlib.util
import os
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("crawl", os.path.join(HERE, "crawl_korea_kr.py"))
C = importlib.util.module_from_spec(spec)
spec.loader.exec_module(C)

now = datetime.now(timezone.utc)
ago = lambda h: (now - timedelta(hours=h)).isoformat()


def due(arts):
    """main() 5.5 의 선별 조건과 동일."""
    cut = now - timedelta(hours=C.ATT_REFRESH_MIN_HOURS)
    out = []
    for a in arts:
        if "attachments" not in a:
            continue
        if a.get("_attRev", 0) >= C.ATT_REFRESH_MAX:
            continue
        last = C.parse_kst(a.get("_attAt")) or C.parse_kst(a.get("pubDate"))
        if last is not None and last > cut:
            continue
        out.append(a["url"])
    return out


base = {"pubDate": ago(48)}
cases = [
    ("최초 추출 전 = 제외",       {**base, "url": "pending"},                                       False),
    ("방금 확인 = 제외",          {**base, "url": "fresh", "attachments": [], "_attAt": ago(1)},     False),
    ("6시간 지남 = 대상",         {**base, "url": "due", "attachments": [], "_attAt": ago(7)},       True),
    ("횟수 소진 = 제외",          {**base, "url": "spent", "attachments": [], "_attAt": ago(99),
                                  "_attRev": C.ATT_REFRESH_MAX},                                    False),
    ("_attAt 없는 옛 기록 = 대상", {**base, "url": "legacy", "attachments": []},                     True),
    ("갓 올라온 옛 기록 = 제외",   {"url": "legacy_new", "pubDate": ago(1), "attachments": []},       False),
]
arts = [c[1] for c in cases]
got = set(due(arts))
fails = 0
for name, a, want in cases:
    ok = (a["url"] in got) == want
    fails += not ok
    print(("  ok  " if ok else "  FAIL") + f"  {name}")

# 갱신 덮어쓰기 규칙: 빈 결과·fetch 실패는 기존 첨부를 지우지 않는다
keep = [{"ext": "hwpx", "url": "u", "filename": "f.hwpx"}]
for atts, want in ((None, keep), ([], keep), ([{"ext": "pdf", "url": "v", "filename": "g.pdf"}], None)):
    a = {"attachments": list(keep)}
    if atts and atts != a["attachments"]:
        a["attachments"] = atts
    exp = want if want is not None else atts
    ok = a["attachments"] == exp
    fails += not ok
    print(("  ok  " if ok else "  FAIL") + f"  덮어쓰기 {atts!r} → {a['attachments']!r}")

print("FAIL" if fails else "PASS", f"({fails} 실패)")
raise SystemExit(1 if fails else 0)
