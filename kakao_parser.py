#!/usr/bin/env python3
"""
카카오톡 대화 분석기
사용법: python kakao_parser.py
→ 브라우저에서 http://localhost:5000 열림
"""

import re
import json
import os
import time
import datetime as _dt
import urllib.request as _urlreq
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse
import webbrowser
import threading

# ── 카카오톡 파일 파싱 ───────────────────────────────────────────────
def parse_kakao_file(text: str) -> list[dict]:
    """
    카카오톡 텍스트 내보내기 파싱 (견고 버전).
    - \r\n / \r 줄바꿈 정규화
    - 한 메시지가 여러 줄에 걸친 경우 합침
    - "님이 들어왔습니다/나갔습니다" 시스템 줄 제외
    - 날짜를 ISO(YYYY-MM-DD)로도 저장
    """
    import re as _re
    text = text.replace('\r\n', '\n').replace('\r', '\n')
    lines = text.split('\n')

    header_pat = _re.compile(r'^\[(.+?)\] \[(오전|오후) (\d{1,2}:\d{2})\] (.*)$')
    date_pat = _re.compile(r'^-+ (\d{4}년 \d{1,2}월 \d{1,2}일) .+ -+$')
    date_pat2 = _re.compile(r'^(\d{4}년 \d{1,2}월 \d{1,2}일).+요일$')
    pc_msg = _re.compile(r'^(\d{4}년 \d{1,2}월 \d{1,2}일) .+요일 (오전|오후) (\d{1,2}:\d{2}), (.+?) : (.+)$')

    def to_iso(d):
        m = _re.match(r'(\d{4})년 (\d{1,2})월 (\d{1,2})일', d)
        return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}" if m else ''

    def to_min(ampm, t):
        h, mm = map(int, t.split(':'))
        if ampm == '오후' and h != 12: h += 12
        if ampm == '오전' and h == 12: h = 0
        return h * 60 + mm

    messages = []
    cur = None
    cur_date = ''
    for line in lines:
        dm = date_pat.match(line) or date_pat2.match(line)
        if dm:
            cur_date = dm.group(1)
            continue
        # PC 구형 (한 줄 완결형)
        pm = pc_msg.match(line)
        if pm:
            if cur: messages.append(cur)
            cur = None
            d = pm.group(1)
            messages.append({
                'date': d, 'iso': to_iso(d), 'ampm': pm.group(2), 'time': pm.group(3),
                'min': to_min(pm.group(2), pm.group(3)),
                'sender': pm.group(4).strip(), 'content': pm.group(5).strip(),
                'datetime_str': f"{d} {pm.group(2)} {pm.group(3)}",
            })
            continue
        hm = header_pat.match(line)
        if hm:
            if cur: messages.append(cur)
            cur = {
                'date': cur_date, 'iso': to_iso(cur_date),
                'ampm': hm.group(2), 'time': hm.group(3),
                'min': to_min(hm.group(2), hm.group(3)),
                'sender': hm.group(1).strip(), 'content': hm.group(4),
                'datetime_str': f"{cur_date} {hm.group(2)} {hm.group(3)}",
            }
        elif cur is not None:
            if '님이 들어왔습니다' in line or '님이 나갔습니다' in line:
                continue
            cur['content'] += '\n' + line
    if cur:
        messages.append(cur)

    for m in messages:
        m['content'] = m['content'].strip()
    return messages


# ── 유사어 묶음 ─────────────────────────────────────────────────────
# 한 단어를 입력하면 같은 그룹 전체를 검색
SYNONYMS = [
    ['프로모션', '프모', '플모', '프모션', '프로모', 'promotion', 'promo'],
    ['이벤트', '이벤', 'event'],
    ['미팅', '회의', '미팅', 'meeting'],
    ['일정', '스케줄', 'schedule'],
    ['오티', 'ot', 'OT', '오리엔테이션'],
]

def expand_keywords(keyword: str) -> list[str]:
    """입력 키워드가 유사어 그룹에 있으면 그룹 전체 반환, 없으면 그대로"""
    kw = keyword.lower().strip()
    for group in SYNONYMS:
        if kw in [g.lower() for g in group]:
            return [g.lower() for g in group]
    return [kw]


def filter_messages(messages, keyword='', sender='', date_from='', date_to=''):
    result = messages
    if keyword:
        keywords = expand_keywords(keyword)
        result = [m for m in result if any(kw in m['content'].lower() for kw in keywords)]
    if sender:
        result = [m for m in result if sender in m['sender']]
    # 날짜 필터 (간단 문자열 비교, "yyyy년 mm월 dd일" 형식)
    if date_from:
        result = [m for m in result if m['date'] >= date_from]
    if date_to:
        result = [m for m in result if m['date'] <= date_to]
    return result


# ── 프로모션(OT 수당) 추출 엔진 ──────────────────────────────────────
# 검증으로 확정한 로직:
#  - 키워드: 프모/플모/프로모 (프로모션 풀어쓴 것도 포함)
#  - 공지자만: 실장/팀장/매니저/스케줄러
#  - 시간대: 범위형(19:00~20:00) + 단일시각형(19시)
#  - 금액: 5,000원 / 5천원 / 10000 등
#  - 같은 날+시간대 중복 시 '가장 늦게 공지된' 값 채택(최종 인상 반영)
#  - 방: 발신자/내용에 ss→SS, edp→EDP

PROMO_KW = ['프모', '플모', '프로모']
NOTICE_KW = ['실장', '팀장', '매니저', '스케줄러']

def has_promo(s):
    return any(k in s for k in PROMO_KW)

def is_notice_sender(sender):
    return any(k in sender for k in NOTICE_KW)

def detect_room_by_majority(messages):
    """
    파일(메시지 묶음) 전체 발신자명을 보고 다수결로 방을 정한다.
    관리자(SS 소속)가 EDP방에 섞여 있어도, 대다수가 EDP면 EDP방으로 판정.
    """
    ss = edp = 0
    for m in messages:
        name = m['sender'].lower()
        if 'edp' in name:
            edp += 1
        elif 'ss' in name:
            ss += 1
    if edp > ss:
        return 'EDP'
    if ss > edp:
        return 'SS'
    # 동률/불명: 첫 줄 메타나 내용으로 보조 판단은 호출부에서
    return ''


def detect_room(sender, content, default_room='SS'):
    """단일 메시지 기준(보조용). 기본은 파일 다수결을 우선 사용."""
    s = (sender + ' ' + content).lower()
    if 'edp' in s:
        return 'EDP'
    if 'ss' in s:
        return 'SS'
    return default_room

def parse_amount(s):
    s = s.replace(',', '').replace(' ', '')
    m = re.search(r'(\d+)천', s)
    if m:
        return int(m.group(1)) * 1000
    m = re.search(r'(\d{4,})', s)
    return int(m.group(1)) if m else None

_RANGE_PAT = re.compile(r'(\d{1,2})(?::(\d{2}))?\s*[~\-]\s*(\d{1,2})(?::(\d{2}))?')
_SINGLE_PAT = re.compile(r'(\d{1,2})\s*시')
_MONEY_PAT = re.compile(r'(\d{1,3}(?:,\d{3})+|\d+\s*천|\d{4,})\s*원?')

def extract_promo_slots(content):
    """한 메시지에서 (시간대, 금액, 인원) 추출. 범위형+단일시각형 모두."""
    results = []
    for part in re.split(r'[\n/]', content):
        if not has_promo(part):
            continue
        am = _MONEY_PAT.search(part)
        amt = parse_amount(am.group(1)) if am else None
        if not amt:
            continue
        pm = re.search(r'(\d+)\s*명', part)
        ppl = int(pm.group(1)) if pm else None
        rm = _RANGE_PAT.search(part)
        if rm:
            h1, m1, h2, m2 = rm.groups()
            slot = f"{int(h1):02d}:{m1 or '00'}~{int(h2):02d}:{m2 or '00'}"
            results.append({'slot': slot, 'amount': amt, 'people': ppl})
        else:
            sm = _SINGLE_PAT.search(part)
            if sm:
                h = int(sm.group(1))
                if h <= 27:
                    slot = f"{h:02d}:00~{(h+1):02d}:00"
                    results.append({'slot': slot, 'amount': amt, 'people': ppl})
    return results


def _shift_iso(iso, days):
    y, m, d = map(int, iso.split('-'))
    t = _dt.date(y, m, d) + _dt.timedelta(days=days)
    return t.isoformat()


def _iso_to_kr(iso):
    y, m, d = map(int, iso.split('-'))
    return f"{y}년 {m}월 {d}일"


def detect_target_iso(content, iso):
    """
    공지 내용에서 '적용 날짜'를 판별한다.
    우선순위: 첫 1~2줄의 명시 날짜(25일, (10/10), 6/2) > 내일/모레 > 공지일 그대로
    예) 6/1에 "내일 OT 모집합니다" → 6/2로 보정
    """
    if not iso:
        return iso
    y, mo, d = map(int, iso.split('-'))
    head = '\n'.join(content.split('\n')[:2])

    # 명시 M/D (예: (10/10), 6/2)
    m = re.search(r'(\d{1,2})\s*/\s*(\d{1,2})', head)
    if m:
        mm, dd = int(m.group(1)), int(m.group(2))
        if 1 <= mm <= 12 and 1 <= dd <= 31:
            try:
                t = _dt.date(y, mm, dd)
                base = _dt.date(y, mo, d)
                if (t - base).days < -180:  # 연말→연초 넘어감
                    t = _dt.date(y + 1, mm, dd)
                return t.isoformat()
            except ValueError:
                pass

    # 명시 N일 (예: 25일(내일,수), 31일 OT 대모집)
    m = re.search(r'(\d{1,2})일', head)
    if m:
        dd = int(m.group(1))
        if 1 <= dd <= 31:
            yy, mm = y, mo
            if dd < d:  # 이미 지난 날짜면 다음 달
                mm += 1
                if mm == 13:
                    mm = 1
                    yy += 1
            try:
                return _dt.date(yy, mm, dd).isoformat()
            except ValueError:
                pass

    # 내일/모레
    if '모레' in content:
        return _shift_iso(iso, 2)
    if '내일' in content:
        return _shift_iso(iso, 1)
    return iso


def build_promo_table(messages, date_from='', date_to='', default_room='SS'):
    """
    프모 OT 수당을 (날짜+시간대) 단위로 정리.
    date_from / date_to: ISO(YYYY-MM-DD) 문자열. 비면 전체.
    반환: 시간대별 행 리스트 (정렬됨)
    """
    raw = []
    for m in messages:
        iso = m.get('iso', '')
        if date_from and iso and iso < date_from:
            continue
        if date_to and iso and iso > date_to:
            continue
        if not has_promo(m['content']):
            continue
        if not is_notice_sender(m['sender']):
            continue
        room = m.get('room') or detect_room(m['sender'], m['content'], default_room)
        target_iso = detect_target_iso(m['content'], iso)
        for s in extract_promo_slots(m['content']):
            raw.append({
                'iso': target_iso, 'date': _iso_to_kr(target_iso) if target_iso else m['date'],
                'room': room,
                'slot': s['slot'], 'amount': s['amount'], 'people': s['people'],
                'sender': m['sender'], 'msg_min': m.get('min', 0),
                'notice_iso': iso,
                'time': (f"{int(iso[5:7])}/{int(iso[8:10])} " if iso and iso != target_iso else '')
                        + f"{m['ampm']} {m['time']}",
            })

    # 같은 적용일+방+시간대 → 가장 늦게 공지된 것 (공지일+시각 기준)
    latest = {}
    for r in raw:
        key = (r['iso'], r['room'], r['slot'])
        rk = (r.get('notice_iso', ''), r['msg_min'])
        if key not in latest or rk > (latest[key].get('notice_iso', ''), latest[key]['msg_min']):
            latest[key] = r
    _room_order = {'SS': 0, 'EDP': 1}
    rows = sorted(latest.values(),
                  key=lambda x: (x['iso'], _room_order.get(x['room'], 9), x['slot']))
    return rows


def build_promo_timeline(rows):
    """build_promo_table 결과를 날짜별로 묶은 타임라인."""
    from collections import defaultdict
    by_date = defaultdict(list)
    for r in rows:
        by_date[r['iso']].append(r)
    timeline = []
    for iso in sorted(by_date):
        items = sorted(by_date[iso], key=lambda x: x['slot'])
        amts = sorted(set(r['amount'] for r in items))
        timeline.append({
            'iso': iso,
            'date': items[0]['date'],
            'count': len(items),
            'rooms': ', '.join(sorted(set(r['room'] for r in items))),
            'amounts': ', '.join(f"{a:,}원" for a in amts),
            'slots': items,
        })
    return timeline


# ── HTML UI ─────────────────────────────────────────────────────────
HTML = r"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>카카오톡 대화 분석기</title>
<style>
  :root {
    --yellow: #FEE500;
    --yellow-dark: #E6CE00;
    --bg: #F7F7F7;
    --card: #FFFFFF;
    --text: #1A1A1A;
    --sub: #666;
    --border: #E0E0E0;
    --tag: #3C1E1E;
    --radius: 12px;
    --shadow: 0 2px 12px rgba(0,0,0,0.08);
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: 'Pretendard', 'Apple SD Gothic Neo', sans-serif;
         background: var(--bg); color: var(--text); min-height: 100vh; }

  header {
    background: var(--yellow);
    padding: 18px 32px;
    display: flex; align-items: center; gap: 12px;
    box-shadow: 0 2px 8px rgba(0,0,0,0.10);
  }
  header .logo { font-size: 22px; }
  header h1 { font-size: 18px; font-weight: 700; color: var(--tag); }
  header small { color: var(--tag); opacity: .65; font-size: 12px; }

  .wrap { max-width: 900px; margin: 0 auto; padding: 28px 20px; }

  /* 파일 업로드 */
  #dropzone {
    border: 2px dashed #CCC;
    border-radius: var(--radius);
    background: var(--card);
    padding: 40px 20px;
    text-align: center;
    cursor: pointer;
    transition: border-color .2s, background .2s;
    margin-bottom: 24px;
  }
  #dropzone.hover { border-color: var(--yellow-dark); background: #FFFDE7; }
  #dropzone .icon { font-size: 36px; margin-bottom: 10px; }
  #dropzone p { color: var(--sub); font-size: 14px; }
  #dropzone strong { color: var(--text); }
  #fileInput { display: none; }

  /* 필터 패널 */
  .panel {
    background: var(--card);
    border-radius: var(--radius);
    box-shadow: var(--shadow);
    padding: 20px 24px;
    margin-bottom: 20px;
  }
  .panel h2 { font-size: 14px; font-weight: 700; color: var(--sub);
              text-transform: uppercase; letter-spacing: .05em; margin-bottom: 14px; }
  .filters { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
  @media(max-width:600px){ .filters { grid-template-columns: 1fr; } }
  .field label { font-size: 12px; color: var(--sub); margin-bottom: 4px; display: block; }
  .field input {
    width: 100%; padding: 9px 12px;
    border: 1px solid var(--border); border-radius: 8px;
    font-size: 14px; outline: none;
    transition: border-color .15s;
  }
  .field input:focus { border-color: var(--yellow-dark); }
  .actions { display: flex; gap: 10px; margin-top: 16px; }
  .btn {
    padding: 10px 22px; border: none; border-radius: 8px;
    font-size: 14px; font-weight: 600; cursor: pointer; transition: opacity .15s;
  }
  .btn:hover { opacity: .85; }
  .btn-primary { background: var(--yellow); color: var(--tag); }
  .btn-promo { background: var(--tag); color: var(--yellow); }
  .btn-ghost { background: #EEE; color: var(--text); }

  /* 통계 */
  .stats { display: flex; gap: 12px; flex-wrap: wrap; margin-bottom: 20px; }
  .stat-card {
    background: var(--card); border-radius: var(--radius);
    box-shadow: var(--shadow); padding: 14px 20px;
    flex: 1; min-width: 120px; text-align: center;
  }
  .stat-card .num { font-size: 26px; font-weight: 800; color: var(--tag); }
  .stat-card .lbl { font-size: 11px; color: var(--sub); margin-top: 2px; }

  /* 발신자 태그 */
  .senders { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 20px; }
  .sender-tag {
    background: var(--yellow); color: var(--tag);
    border-radius: 20px; padding: 4px 12px; font-size: 12px; font-weight: 600;
    cursor: pointer; border: 2px solid transparent; transition: border-color .15s;
  }
  .sender-tag:hover, .sender-tag.active { border-color: var(--tag); }

  /* 결과 목록 */
  #results .date-group { margin-bottom: 18px; }
  #results .date-label {
    font-size: 11px; font-weight: 700; color: var(--sub);
    text-transform: uppercase; letter-spacing: .06em;
    padding: 6px 0; border-bottom: 1px solid var(--border); margin-bottom: 8px;
  }
  .msg-row {
    display: flex; gap: 10px; padding: 8px 0;
    border-bottom: 1px solid #F2F2F2; align-items: flex-start;
  }
  .msg-row .time { font-size: 11px; color: #AAA; min-width: 70px; padding-top: 2px; }
  .msg-row .sender { font-size: 12px; font-weight: 700;
                     min-width: 80px; color: #555; padding-top: 2px; }
  .msg-row .content { font-size: 14px; line-height: 1.5; flex: 1; white-space: pre-wrap; }
  .highlight { background: #FFF59D; border-radius: 3px; padding: 0 2px; }

  /* 내보내기 */
  .export-bar { display: flex; gap: 10px; margin-bottom: 16px; align-items: center; }
  .export-bar span { font-size: 13px; color: var(--sub); margin-right: 4px; }

  /* 빈 상태 */
  .empty { text-align: center; padding: 60px 20px; color: var(--sub); }
  .empty .icon { font-size: 48px; margin-bottom: 12px; }

  #status { font-size: 13px; color: var(--sub); margin-bottom: 16px; }

  /* 프로모션 정리 표 */
  .promo-table { width: 100%; border-collapse: collapse; background: var(--card);
                 border-radius: var(--radius); overflow: hidden; box-shadow: var(--shadow); }
  .promo-table th { background: var(--tag); color: var(--yellow);
                    font-size: 12px; font-weight: 700; padding: 12px 14px; text-align: left;
                    letter-spacing: .03em; }
  .promo-table td { padding: 11px 14px; font-size: 13px; border-bottom: 1px solid #F0F0F0;
                    vertical-align: top; }
  .promo-table tr:hover td { background: #FFFDE7; }
  .promo-table .ch-badge { display: inline-block; background: var(--yellow);
                           color: var(--tag); font-size: 11px; font-weight: 700;
                           padding: 2px 8px; border-radius: 12px; }
  .promo-table .sched-badge { display:inline-block; background:#FFF3CD; color:#8A6D00;
                              font-size:11px; font-weight:700; padding:2px 8px; border-radius:6px;
                              white-space:nowrap; }
  .promo-table .ch-empty { color: #CCC; font-size: 11px; }
  .promo-table .ln-self { color:#1A1A1A; padding:1px 0; }
  .promo-table .ln-ctx  { color:#B0B0B0; font-size:11px; padding:1px 0; font-style:italic; }
  .promo-table td.col-content { max-width: 440px; white-space: pre-wrap; word-break: break-word;
                                line-height: 1.6; font-size: 12.5px; color: #333; }
  .promo-table td { vertical-align: top; }

  /* 프모 패널 - 빠른 기간 칩 */
  .quick-range { display:flex; gap:8px; flex-wrap:wrap; }
  .chip { padding:6px 14px; border:1px solid var(--border); border-radius:20px;
          background:#fff; font-size:13px; cursor:pointer; transition:all .15s; }
  .chip:hover { border-color:var(--yellow-dark); background:#FFFDE7; }
  /* 탭 */
  .tabs { display:flex; align-items:center; gap:8px; margin:16px 0 12px; }
  .tab { padding:8px 16px; border:none; border-radius:8px 8px 0 0; background:#EEE;
         font-size:14px; font-weight:600; cursor:pointer; color:#888; }
  .tab.active { background:var(--tag); color:var(--yellow); }
  /* 타임라인 */
  .tl-day { background:var(--card); border-radius:var(--radius); box-shadow:var(--shadow);
            padding:14px 18px; margin-bottom:12px; }
  .tl-head { display:flex; justify-content:space-between; align-items:baseline;
             flex-wrap:wrap; gap:6px; border-bottom:1px solid #F0F0F0; padding-bottom:8px; margin-bottom:10px; }
  .tl-date { font-size:15px; font-weight:800; color:var(--tag); }
  .tl-meta { font-size:12px; color:var(--sub); }
  .tl-slots { display:flex; flex-wrap:wrap; gap:8px; }
  .tl-chip { background:#FFF3CD; color:#5c4a00; border-radius:8px; padding:5px 10px;
             font-size:13px; white-space:nowrap; }
  .tl-chip b { color:#C0392B; }

  /* 프모 표 - 날짜/방 구분선 */
  .promo-table tr.div-date td {
    background:#3C1E1E !important; color:#FEE500;
    font-size:14px; font-weight:800; padding:10px 14px;
    border-top:3px solid #2A1010 !important;
  }
  .promo-table tr.div-room-row td {
    background:#F5F5F0 !important; padding:6px 14px;
    border-top:1px solid #DDD !important;
  }
  .promo-table .div-room {
    display:inline-block; padding:2px 10px; border-radius:12px;
    font-size:11px; font-weight:700; margin-left:8px;
    background:#FEE500; color:#3C1E1E;
  }
  .promo-table .div-room.edp { background:#E3F2FD; color:#1565C0; }
  .promo-table tr.row-ss td { background:#FFFEF6; }
  .promo-table tr.row-edp td { background:#F5FAFE; }
  .promo-table tr.row-ss:hover td { background:#FFF8DC; }
  .promo-table tr.row-edp:hover td { background:#E8F4FD; }

  .tl-room-block { margin-top:10px; padding-top:8px; border-top:1px dashed #E8E8E0; }
  .tl-room-block:first-of-type { border-top:none; margin-top:0; padding-top:0; }
  .tl-room-label { margin-bottom:6px; }
  .tl-chip-edp { background:#E3F2FD; color:#1A4060; }
  .tl-chip-edp b { color:#0D47A1; }
</style>
</head>
<body>
<header>
  <div class="logo">💰</div>
  <div>
    <h1>카카오톡 프모 수당 분석기</h1>
    <small>SS / EDP 대화 파일을 올리면 프모 수당을 자동 정리합니다</small>
  </div>
  <div style="flex:1"></div>
  <button onclick="shutdownApp()" style="background:rgba(60,30,30,.12);border:1px solid rgba(60,30,30,.3);
    color:var(--tag);padding:8px 16px;border-radius:8px;font-size:13px;font-weight:700;cursor:pointer">
    ⏻ 종료
  </button>
</header>

<div class="wrap">
  <!-- 파일 드롭존 -->
  <div id="dropzone" onclick="document.getElementById('fileInput').click()">
    <div class="icon">📂</div>
    <p><strong>클릭하거나 파일을 끌어다 놓으세요</strong></p>
    <p>카카오톡 대화 텍스트 파일 (.txt) — 여러 개 한꺼번에 선택 가능</p>
  </div>
  <input type="file" id="fileInput" accept=".txt" multiple>
  <div id="status"></div>

  <!-- 프모 수당 정리 (메인) -->
  <div id="promoArea" style="display:none">
    <div class="panel" style="margin-bottom:16px">
      <h2>💰 프모(OT 수당) 정리 — 기간 선택</h2>
      <div class="quick-range">
        <button class="chip" onclick="setQuickRange(1)">최근 1개월</button>
        <button class="chip" onclick="setQuickRange(3)">최근 3개월</button>
        <button class="chip" onclick="setQuickRange(6)">최근 6개월</button>
        <button class="chip" onclick="setQuickRange(0)">전체</button>
      </div>
      <div class="filters" style="margin-top:12px">
        <div class="field">
          <label>시작일</label>
          <input type="date" id="promoFrom" />
        </div>
        <div class="field">
          <label>종료일</label>
          <input type="date" id="promoTo" />
        </div>
      </div>
      <div class="actions" style="margin-top:14px">
        <button class="btn btn-primary" onclick="runPromo()">정리하기</button>
      </div>
    </div>

    <div id="promoResult" style="display:none">
      <div class="stats">
        <div class="stat-card"><div class="num" id="pTotalDays">0</div><div class="lbl">프모 적용일</div></div>
        <div class="stat-card"><div class="num" id="pTotalSlots">0</div><div class="lbl">시간대 수</div></div>
        <div class="stat-card"><div class="num" id="pMinAmt">-</div><div class="lbl">최저 수당</div></div>
        <div class="stat-card"><div class="num" id="pMaxAmt">-</div><div class="lbl">최고 수당</div></div>
      </div>

      <div class="tabs">
        <button class="tab active" id="tabTimeline" onclick="switchTab('timeline')">📅 날짜별 타임라인</button>
        <button class="tab" id="tabTable" onclick="switchTab('table')">📋 시간대별</button>
        <div style="flex:1"></div>
        <button class="btn btn-ghost" onclick="exportPromoCsv()">📊 CSV 내보내기</button>
      </div>

      <div id="viewTimeline"></div>
      <div id="viewTable" style="display:none;overflow-x:auto">
        <table id="promoTable" class="promo-table"></table>
      </div>
    </div>
  </div>

  <!-- 검색 기능 제거: 프모 수당 정리 전용 -->
</div>

<script>
let allMessages = [];
let filteredMessages = [];

// 파일 드롭존 (여러 파일 지원)
const dropzone = document.getElementById('dropzone');
dropzone.addEventListener('dragover', e => { e.preventDefault(); dropzone.classList.add('hover'); });
dropzone.addEventListener('dragleave', () => dropzone.classList.remove('hover'));
dropzone.addEventListener('drop', e => {
  e.preventDefault(); dropzone.classList.remove('hover');
  if (e.dataTransfer.files.length) loadFiles(e.dataTransfer.files);
});
document.getElementById('fileInput').addEventListener('change', e => {
  if (e.target.files.length) loadFiles(e.target.files);
});

function readFileAsText(file) {
  return new Promise((resolve) => {
    const reader = new FileReader();
    reader.onload = ev => resolve(ev.target.result);
    reader.readAsText(file, 'UTF-8');
  });
}

async function loadFiles(fileList) {
  const files = Array.from(fileList);
  document.getElementById('status').textContent = `⏳ ${files.length}개 파일 읽는 중...`;
  const texts = await Promise.all(files.map(readFileAsText));
  fetch('/parse', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ texts })
  })
  .then(r => r.json())
  .then(data => {
    allMessages = data.messages;
    const dupMsg = data.duplicates_removed ? ` · 중복 ${data.duplicates_removed.toLocaleString()}개 제거` : '';
    const roomMsg = (data.rooms || []).map((r,i) => `${files[i] ? files[i].name : '파일'+ (i+1)} → ${r.room}방`).join(' / ');
    document.getElementById('status').innerHTML =
      `✅ ${files.length}개 파일 · ${data.count.toLocaleString()}개 메시지${dupMsg}<br>` +
      `<span style="color:#888;font-size:12px">${roomMsg}</span>`;
    // 프모 정리 자동 실행 (최근 1개월)
    openPromoPanel();
  });
}

function toggleSearch() { /* 검색 기능 제거됨 */ }

function doSearch() {
  const kw = document.getElementById('kw').value.trim();
  const sender = document.getElementById('senderInput').value.trim();
  const df = document.getElementById('dateFrom').value.trim();
  const dt = document.getElementById('dateTo').value.trim();

  fetch('/filter', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ keyword: kw, sender, date_from: df, date_to: dt })
  })
  .then(r => r.json())
  .then(data => {
    filteredMessages = data.messages;
    updateStats();
    renderResults(filteredMessages, kw);
    // 유사어 힌트
    const hint = document.getElementById('synonymHint');
    if (data.expanded_keywords && data.expanded_keywords.length > 1) {
      hint.style.display = '';
      hint.innerHTML = '🔎 함께 검색된 단어: ' +
        data.expanded_keywords.map(k => `<strong>${k}</strong>`).join(', ');
    } else {
      hint.style.display = 'none';
    }
  });
}

function clearFilters() {
  ['kw','senderInput','dateFrom','dateTo'].forEach(id => document.getElementById(id).value = '');
  document.querySelectorAll('.sender-tag').forEach(t => t.classList.remove('active'));
  filteredMessages = allMessages;
  updateStats();
  renderResults(allMessages);
}

function renderSenderTags() {
  const senders = [...new Set(allMessages.map(m => m.sender))].sort();
  const container = document.getElementById('senderTags');
  container.innerHTML = senders.map(s =>
    `<span class="sender-tag" onclick="selectSender(this,'${s.replace(/'/g,"\\'")}')">👤 ${s}</span>`
  ).join('');
}

function selectSender(el, name) {
  el.classList.toggle('active');
  const active = [...document.querySelectorAll('.sender-tag.active')].map(t => t.textContent.slice(2));
  document.getElementById('senderInput').value = active[0] || '';
  doSearch();
}

function updateStats() {
  const senders = new Set(allMessages.map(m => m.sender));
  const days = new Set(allMessages.map(m => m.date));
  document.getElementById('totalCount').textContent = allMessages.length.toLocaleString();
  document.getElementById('resultCount').textContent = filteredMessages.length.toLocaleString();
  document.getElementById('senderCount').textContent = senders.size;
  document.getElementById('dayCount').textContent = days.size;
}

function renderResults(msgs, keyword = '') {
  const container = document.getElementById('results');
  if (!msgs.length) {
    container.innerHTML = `<div class="empty"><div class="icon">🔍</div><p>검색 결과가 없습니다.</p></div>`;
    return;
  }

  // 날짜별 그룹핑
  const groups = {};
  for (const m of msgs) {
    if (!groups[m.date]) groups[m.date] = [];
    groups[m.date].push(m);
  }

  let html = '';
  for (const date of Object.keys(groups).sort()) {
    html += `<div class="date-group"><div class="date-label">📅 ${date}</div>`;
    for (const m of groups[date]) {
      const content = keyword
        ? m.content.replace(new RegExp(`(${escRe(keyword)})`, 'gi'),
            '<span class="highlight">$1</span>')
        : escHtml(m.content);
      html += `<div class="msg-row">
        <span class="time">${m.ampm} ${m.time}</span>
        <span class="sender">${escHtml(m.sender)}</span>
        <span class="content">${keyword ? content : escHtml(m.content)}</span>
      </div>`;
    }
    html += `</div>`;
  }
  container.innerHTML = html;
}

function escHtml(s) {
  return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}
function escRe(s) {
  return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

function exportTxt() {
  const lines = filteredMessages.map(m =>
    `[${m.date}] [${m.ampm} ${m.time}] ${m.sender}: ${m.content}`
  );
  download('kakao_export.txt', lines.join('\n'), 'text/plain');
}
function exportCsv() {
  const header = '날짜,오전오후,시간,보낸사람,내용';
  const rows = filteredMessages.map(m =>
    [m.date, m.ampm, m.time, m.sender, `"${m.content.replace(/"/g,'""')}"`].join(',')
  );
  download('kakao_export.csv', '\uFEFF' + [header,...rows].join('\n'), 'text/csv');
}
function download(filename, content, type) {
  const a = document.createElement('a');
  a.href = URL.createObjectURL(new Blob([content], {type}));
  a.download = filename; a.click();
}

let promoRows = [];
let promoBounds = {min:'', max:''};

function openPromoPanel() {
  document.getElementById('promoArea').style.display = '';
  // 기본값: 전체 기간 기준 최근 3개월
  fetch('/promo_table', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({}) })
    .then(r => r.json())
    .then(data => {
      promoBounds = data.bounds || {min:'',max:''};
      if (promoBounds.max) {
        document.getElementById('promoTo').value = promoBounds.max;
        setQuickRange(1);  // 기본 최근 1개월
      } else {
        runPromo();
      }
    });
}

function setQuickRange(months) {
  const to = promoBounds.max || new Date().toISOString().slice(0,10);
  document.getElementById('promoTo').value = to;
  if (months === 0) {
    document.getElementById('promoFrom').value = promoBounds.min || '';
  } else {
    const d = new Date(to);
    d.setMonth(d.getMonth() - months);
    document.getElementById('promoFrom').value = d.toISOString().slice(0,10);
  }
  runPromo();
}

function runPromo() {
  const df = document.getElementById('promoFrom').value;
  const dt = document.getElementById('promoTo').value;
  fetch('/promo_table', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({ date_from: df, date_to: dt })
  })
  .then(r => r.json())
  .then(data => {
    promoRows = data.rows;
    renderPromoStats(data.rows);
    renderPromoTable(data.rows);
    renderPromoTimeline(data.timeline);
    document.getElementById('promoResult').style.display = '';
  });
}

function renderPromoStats(rows) {
  const days = new Set(rows.map(r => r.iso));
  document.getElementById('pTotalDays').textContent = days.size;
  document.getElementById('pTotalSlots').textContent = rows.length;
  if (rows.length) {
    const amts = rows.map(r => r.amount).filter(a => a);
    document.getElementById('pMinAmt').textContent = amts.length ? (Math.min(...amts)/1000)+'천' : '-';
    document.getElementById('pMaxAmt').textContent = amts.length ? (Math.max(...amts)/1000)+'천' : '-';
  } else {
    document.getElementById('pMinAmt').textContent = '-';
    document.getElementById('pMaxAmt').textContent = '-';
  }
}

function renderPromoTable(rows) {
  const t = document.getElementById('promoTable');
  if (!rows.length) {
    t.innerHTML = '<tr><td style="padding:40px;text-align:center;color:#999">해당 기간에 프모 수당 공지가 없습니다.</td></tr>';
    return;
  }
  let html = '<thead><tr><th>날짜</th><th>방</th><th>시간대</th><th>인원</th><th>프모 수당</th><th>최종공지</th><th>공지자</th></tr></thead><tbody>';
  let lastDate = null, lastRoom = null;
  for (const r of rows) {
    const dateChanged = r.date !== lastDate;
    const roomChanged = dateChanged || r.room !== lastRoom;
    // 날짜가 바뀌면: 굵은 날짜 헤더 행 + 방 라벨
    if (dateChanged) {
      html += `<tr class="div-date"><td colspan="7">📅 ${escHtml(r.date)} <span class="div-room ${r.room.toLowerCase()}">${escHtml(r.room)}방</span></td></tr>`;
    } else if (roomChanged) {
      // 같은 날 안에서 방만 바뀜
      html += `<tr class="div-room-row"><td colspan="7"><span class="div-room ${r.room.toLowerCase()}">${escHtml(r.room)}방</span></td></tr>`;
    }
    lastDate = r.date; lastRoom = r.room;
    const ppl = r.people ? r.people + '명' : '<span class="ch-empty">제한없음</span>';
    html += `<tr class="row-${r.room.toLowerCase()}">
      <td style="color:#aaa;font-size:11px">·</td>
      <td><span class="ch-badge">${escHtml(r.room)}</span></td>
      <td style="white-space:nowrap">${escHtml(r.slot)}</td>
      <td style="text-align:center">${ppl}</td>
      <td style="text-align:right"><b>${r.amount.toLocaleString()}원</b></td>
      <td style="white-space:nowrap;color:#999;font-size:12px">${escHtml(r.time)}</td>
      <td style="font-size:12px">${escHtml(r.sender)}</td>
    </tr>`;
  }
  html += '</tbody>';
  t.innerHTML = html;
}

function renderPromoTimeline(timeline) {
  const c = document.getElementById('viewTimeline');
  if (!timeline || !timeline.length) {
    c.innerHTML = '<div class="empty"><div class="icon">📅</div><p>해당 기간에 프모 적용일이 없습니다.</p></div>';
    return;
  }
  let html = '';
  for (const day of timeline) {
    // 방별로 그룹핑 (SS 우선, EDP 그 다음)
    const byRoom = {SS:[], EDP:[]};
    for (const s of day.slots) {
      if (byRoom[s.room]) byRoom[s.room].push(s); else (byRoom[s.room]=[s]);
    }
    html += `<div class="tl-day">
      <div class="tl-head">
        <span class="tl-date">📅 ${escHtml(day.date)}</span>
        <span class="tl-meta">${day.count}개 시간대 · ${escHtml(day.amounts)}</span>
      </div>`;
    for (const room of ['SS','EDP']) {
      const slots = byRoom[room] || [];
      if (!slots.length) continue;
      html += `<div class="tl-room-block">
        <div class="tl-room-label"><span class="div-room ${room.toLowerCase()}">${room}방</span> <span style="color:#999;font-size:11px">${slots.length}개 시간대</span></div>
        <div class="tl-slots">`;
      for (const s of slots) {
        const ppl = s.people ? ` ${s.people}명` : '';
        html += `<span class="tl-chip tl-chip-${room.toLowerCase()}">${escHtml(s.slot)}<b> ${s.amount.toLocaleString()}원</b>${ppl}</span>`;
      }
      html += `</div></div>`;
    }
    html += `</div>`;
  }
  c.innerHTML = html;
}

function switchTab(which) {
  const isTable = which === 'table';
  document.getElementById('tabTable').classList.toggle('active', isTable);
  document.getElementById('tabTimeline').classList.toggle('active', !isTable);
  document.getElementById('viewTable').style.display = isTable ? '' : 'none';
  document.getElementById('viewTimeline').style.display = isTable ? 'none' : '';
}

function exportPromoCsv() {
  const header = '날짜,방,시간대,인원,프모수당,최종공지,공지자';
  const lines = promoRows.map(r =>
    [r.date, r.room, r.slot, r.people || '제한없음', r.amount, r.time, r.sender]
      .map(v => /[",\n]/.test(String(v)) ? `"${String(v).replace(/"/g,'""')}"` : v)
      .join(',')
  );
  download('프모_수당_정리.csv', '\uFEFF' + [header,...lines].join('\n'), 'text/csv');
}

function shutdownApp() {
  if (!confirm('프로그램을 종료할까요?')) return;
  fetch('/shutdown', {method:'POST', headers:{'Content-Type':'application/json'}, body:'{}'})
    .finally(() => {
      document.body.innerHTML = '<div style="padding:80px 20px;text-align:center;font-family:sans-serif">' +
        '<div style="font-size:48px">👋</div><h2>종료되었습니다</h2><p style="color:#888">이 창은 닫으셔도 됩니다.</p></div>';
    });
}
</script>
</body>
</html>
"""


# ── HTTP 서버 ────────────────────────────────────────────────────────
stored_messages = []

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args): pass  # 로그 숨김

    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.end_headers()
        self.wfile.write(HTML.encode('utf-8'))

    def do_POST(self):
        global stored_messages
        length = int(self.headers.get('Content-Length', 0))
        body = json.loads(self.rfile.read(length))
        path = urlparse(self.path).path

        if path == '/parse':
            # 여러 파일 지원: texts(배열) 또는 단일 text
            texts = body.get('texts')
            if texts is None:
                texts = [body['text']]

            # 파일별로 파싱 + 방 판정(다수결 + 첫 줄 메타 보조)
            per_file = []
            room_summary = []
            for t in texts:
                msgs = parse_kakao_file(t)
                # 방 판정: 발신자 다수결 우선
                room = detect_room_by_majority(msgs)
                if not room:
                    # 보조: 첫 줄 "Meta Ss/Edp ..." 확인
                    first = t.replace('\r\n', '\n').split('\n', 1)[0].lower()
                    if 'edp' in first:
                        room = 'EDP'
                    elif 'ss' in first:
                        room = 'SS'
                    else:
                        room = 'SS'
                for m in msgs:
                    m['room'] = room
                room_summary.append({'room': room, 'count': len(msgs)})
                # 파일 내 동일 메시지 도배 보존용 순번
                counter = {}
                for m in msgs:
                    base = (room, m.get('iso', ''), m.get('time', ''), m['sender'], m['content'])
                    n = counter.get(base, 0)
                    counter[base] = n + 1
                    m['_dupkey'] = base + (n,)
                per_file.append(msgs)

            # 파일 간 중복만 제거 (방까지 같은 완전 동일 메시지)
            seen = set()
            merged = []
            for msgs in per_file:
                for m in msgs:
                    k = m['_dupkey']
                    if k in seen:
                        continue
                    seen.add(k)
                    merged.append(m)

            total_before = sum(len(x) for x in per_file)
            for m in merged:
                m.pop('_dupkey', None)
            merged.sort(key=lambda x: (x.get('iso', ''), x.get('min', 0)))
            stored_messages = merged
            self._json({'messages': stored_messages,
                        'count': len(stored_messages),
                        'duplicates_removed': total_before - len(merged),
                        'rooms': room_summary})

        elif path == '/filter':
            kw = body.get('keyword', '')
            expanded = expand_keywords(kw) if kw else []
            result = filter_messages(
                stored_messages,
                keyword=kw,
                sender=body.get('sender', ''),
                date_from=body.get('date_from', ''),
                date_to=body.get('date_to', ''),
            )
            self._json({'messages': result, 'expanded_keywords': expanded})

        elif path == '/promo_table':
            df = body.get('date_from', '')
            dt = body.get('date_to', '')
            rows = build_promo_table(stored_messages, date_from=df, date_to=dt)
            timeline = build_promo_timeline(rows)
            # 데이터의 전체 날짜 범위(빠른 버튼/기본값용)
            isos = [m.get('iso', '') for m in stored_messages if m.get('iso')]
            bounds = {'min': min(isos), 'max': max(isos)} if isos else {'min': '', 'max': ''}
            self._json({'rows': rows, 'timeline': timeline, 'bounds': bounds})

        elif path == '/shutdown':
            self._json({'ok': True})
            def _exit():
                time.sleep(0.3)
                os._exit(0)
            threading.Thread(target=_exit, daemon=True).start()

    def _json(self, data):
        payload = json.dumps(data, ensure_ascii=False).encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def main():
    port = 5000
    # 이전 인스턴스가 떠 있으면 자동 종료 요청
    try:
        _urlreq.urlopen(f'http://127.0.0.1:{port}/shutdown', data=b'{}', timeout=2)
        time.sleep(0.8)
    except Exception:
        pass
    # 포트 바인딩 (그래도 안 되면 5001~5009 시도)
    server = None
    for p in range(port, port + 10):
        try:
            server = HTTPServer(('127.0.0.1', p), Handler)
            port = p
            break
        except OSError:
            continue
    if server is None:
        print("포트(5000~5009)를 열 수 없습니다. 다른 프로그램을 확인해주세요.")
        return
    url = f'http://localhost:{port}'
    print(f"\n✅ 카카오톡 프모 분석기 실행 중")
    print(f"   브라우저: {url}")
    print(f"   종료: 화면의 [종료] 버튼 또는 Ctrl+C\n")
    threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n종료되었습니다.")


if __name__ == '__main__':
    main()
