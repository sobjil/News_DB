// 경북귀농일기(game/farmstory) 정책 돌발 이벤트 풀 생성.
// articles.json(정부 보도자료) → 농업 관련 부처 필터 → Gemini로 게임 효과 분류 + 서사 1문장
// → data/farmstory-events.json (7일 보존). 게임은 이 파일을 정적 fetch(사용자 키 불필요).
// 효과 '수치'는 게임 코드(engine/events.js)가 결정 — AI는 분류·서사만(환각 차단).
import fs from 'fs';

const AGENCIES = ['농림축산식품부', '해양수산부', '행정안전부', '기후에너지환경부', '기상청', '산림청'];
const KEYS = [1, 2, 3, 4, 5].map(i => process.env['GEMINI_API_KEY_' + i]).filter(Boolean);
const MODELS = ['gemini-3.5-flash', 'gemini-2.5-flash', 'gemini-2.0-flash', 'gemini-flash-latest'];
const RETAIN_DAYS = 7, MAX_NEW = 14;
// AI의 direction → 게임 이벤트 kind(engine/events.js가 효과 수치 매핑)
const DIR_KIND = { 지원금: '지원금', 판매가상승: '시세호재', 판매가하락: '시세악재', 비용증가: '시세악재', 재해: '재해', 중립: '중립' };
const EMOJI = { 지원금: '💰', 시세호재: '🌾', 시세악재: '🥀', 재해: '🌀', 중립: '📰' };

async function gemini(prompt) {
  for (const model of MODELS) {
    for (const key of KEYS) {
      try {
        const r = await fetch(`https://generativelanguage.googleapis.com/v1beta/models/${model}:generateContent?key=${key}`, {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ contents: [{ parts: [{ text: prompt }] }], generationConfig: { responseMimeType: 'application/json', temperature: 0.7 } })
        });
        if (r.status === 429 || r.status >= 500) continue;
        if (!r.ok) { if (r.status === 404) break; continue; } // 404=모델 없음 → 다음 모델
        const d = await r.json();
        const txt = d?.candidates?.[0]?.content?.parts?.[0]?.text;
        if (txt) return JSON.parse(txt);
      } catch { /* 다음 키/모델 */ }
    }
  }
  return null;
}

const idOf = a => (String(a.url).match(/newsId=(\d+)/) || [])[1] || a.url;
const ts = d => { const t = new Date(d).getTime(); return Number.isFinite(t) ? t : 0; };

const articles = (JSON.parse(fs.readFileSync('data/articles.json', 'utf8')).articles) || [];
const cutoff = Date.now() - RETAIN_DAYS * 86400000;

// 기존 풀 로드 + 7일 지난 것 제거
let pool = [];
try { pool = JSON.parse(fs.readFileSync('data/farmstory-events.json', 'utf8')).events || []; } catch { }
pool = pool.filter(e => ts(e.pubDate) >= cutoff);
const have = new Set(pool.map(e => e.id));

// 농업 관련 부처 + 최근 7일 + 아직 분류 안 한 것 (최신순)
const cand = articles
  .filter(a => AGENCIES.includes(a.agency) && ts(a.pubDate) >= cutoff && !have.has(idOf(a)))
  .sort((a, b) => ts(b.pubDate) - ts(a.pubDate));

if (!KEYS.length) { console.error('GEMINI_API_KEY 없음 — 분류 생략(기존 풀 유지)'); }

let added = 0;
for (const a of cand) {
  if (added >= MAX_NEW || !KEYS.length) break;
  const prompt = `당신은 '경북 귀농 농장 경영 게임'의 이벤트 작가입니다.
아래 정부 보도자료가 플레이어의 농장(농사·축산·어업·가공·직판)에 줄 영향을 분류하세요.

부처: ${a.agency}
제목: ${a.title}
내용: ${String(a.description || '').slice(0, 600)}

아래 JSON으로만 답하세요:
{"sector":"농사|축산|어업|가공|직판|전체|무관","direction":"지원금|판매가상승|판매가하락|비용증가|재해|중립","strength":"약|중|강","flavor":"이 정책이 내 농장에 반영된 모습을 친근한 1문장(예: 우리 농장에도 정착지원금이 들어왔어요!)"}
규칙: 농업·수산·임업·농촌·재해·기상과 무관하면 sector=무관·direction=중립. 보도자료 내용에 근거(환각 금지). flavor는 한국어 한 문장.`;
  const cl = await gemini(prompt);
  if (!cl) continue;
  const kind = DIR_KIND[cl.direction] || '중립';
  const strength = ['약', '중', '강'].includes(cl.strength) ? cl.strength : '중';
  const scope = ['농사', '축산', '어업', '가공', '직판', '전체', '무관'].includes(cl.sector) ? cl.sector : '전체';
  pool.push({
    id: idOf(a), agency: a.agency, title: a.title, url: a.url, pubDate: a.pubDate,
    scope, kind, strength, emoji: EMOJI[kind] || '📰',
    flavor: String(cl.flavor || '').slice(0, 120),
    weight: kind === '중립' ? 2 : 5
  });
  added++;
}

const out = { updated: new Date().toISOString().slice(0, 10), retentionDays: RETAIN_DAYS, count: pool.length, events: pool };
fs.writeFileSync('data/farmstory-events.json', JSON.stringify(out, null, 2));
console.log(`farmstory policy events: +${added} new · total ${pool.length} (후보 ${cand.length})`);
