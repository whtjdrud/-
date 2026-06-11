#!/usr/bin/env python3
"""
카카오톡 대화 분석기
사용법: python kakao_parser.py
→ 브라우저에서 http://localhost:5000 열림
"""

import re
import json
import os
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse
import webbrowser
import threading

# ── 카카오톡 파일 파싱 ───────────────────────────────────────────────
def parse_kakao_file(text: str) -> list[dict]:
    """
    카카오톡 텍스트 내보내기 포맷 파싱.
    지원 포맷:
      [이름] [오전/오후 H:MM] 메시지       (모바일 신형)
      2024년 1월 1일 월요일               (날짜 구분선)
      yyyy년 M월 d일 요일, 오전/오후 H:MM, 이름 : 메시지  (PC 구형)
    """
    messages = []
    current_date = ""

    # 모바일 신형 패턴
    mobile_msg = re.compile(
        r'^\[(.+?)\] \[(오전|오후) (\d{1,2}:\d{2})\] (.+)$'
    )
    # 날짜 구분선 (모바일)
    date_line = re.compile(
        r'^-+ (\d{4}년 \d{1,2}월 \d{1,2}일).+ -+$'
    )
    # 날짜 구분선 (별도 줄, PC)
    date_line2 = re.compile(
        r'^(\d{4}년 \d{1,2}월 \d{1,2}일).+요일$'
    )
    # PC 구형 패턴
    pc_msg = re.compile(
        r'^(\d{4}년 \d{1,2}월 \d{1,2}일) .+요일 (오전|오후) (\d{1,2}:\d{2}), (.+?) : (.+)$'
    )

    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].strip()

        # 날짜 구분선
        m = date_line.match(line) or date_line2.match(line)
        if m:
            current_date = m.group(1)
            i += 1
            continue

        # 모바일 메시지
        m = mobile_msg.match(line)
        if m:
            sender, ampm, time_str, content = m.group(1), m.group(2), m.group(3), m.group(4)
            # 이어지는 줄 (들여쓰기 없는 텍스트) 합치기
            while i + 1 < len(lines):
                next_line = lines[i + 1]
                if next_line.startswith('[') or next_line.startswith('-') or next_line == '':
                    break
                if mobile_msg.match(next_line):
                    break
                content += '\n' + next_line.strip()
                i += 1
            messages.append({
                'date': current_date,
                'ampm': ampm,
                'time': time_str,
                'sender': sender.strip(),
                'content': content.strip(),
                'datetime_str': f"{current_date} {ampm} {time_str}"
            })
            i += 1
            continue

        # PC 구형 메시지
        m = pc_msg.match(line)
        if m:
            date, ampm, time_str, sender, content = m.group(1), m.group(2), m.group(3), m.group(4), m.group(5)
            messages.append({
                'date': date,
                'ampm': ampm,
                'time': time_str,
                'sender': sender.strip(),
                'content': content.strip(),
                'datetime_str': f"{date} {ampm} {time_str}"
            })
            i += 1
            continue

        i += 1

    return messages


def filter_messages(messages, keyword='', sender='', date_from='', date_to=''):
    result = messages
    if keyword:
        kw = keyword.lower()
        result = [m for m in result if kw in m['content'].lower()]
    if sender:
        result = [m for m in result if sender in m['sender']]
    # 날짜 필터 (간단 문자열 비교, "yyyy년 mm월 dd일" 형식)
    if date_from:
        result = [m for m in result if m['date'] >= date_from]
    if date_to:
        result = [m for m in result if m['date'] <= date_to]
    return result


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
</style>
</head>
<body>
<header>
  <div class="logo">💬</div>
  <div>
    <h1>카카오톡 대화 분석기</h1>
    <small>텍스트 파일을 불러와 키워드·날짜별로 검색하세요</small>
  </div>
</header>

<div class="wrap">
  <!-- 파일 드롭존 -->
  <div id="dropzone" onclick="document.getElementById('fileInput').click()">
    <div class="icon">📂</div>
    <p><strong>클릭하거나 파일을 끌어다 놓으세요</strong></p>
    <p>카카오톡 대화 내보내기 텍스트 파일 (.txt)</p>
  </div>
  <input type="file" id="fileInput" accept=".txt">

  <!-- 필터 -->
  <div class="panel" id="filterPanel" style="display:none">
    <h2>🔍 검색 필터</h2>
    <div class="filters">
      <div class="field">
        <label>키워드</label>
        <input id="kw" placeholder="예: OT, 프로모션, 일정" />
      </div>
      <div class="field">
        <label>보낸 사람</label>
        <input id="senderInput" placeholder="이름 입력 또는 아래 태그 클릭" />
      </div>
      <div class="field">
        <label>시작 날짜 (예: 2024년 3월 1일)</label>
        <input id="dateFrom" placeholder="2024년 3월 1일" />
      </div>
      <div class="field">
        <label>종료 날짜</label>
        <input id="dateTo" placeholder="2024년 12월 31일" />
      </div>
    </div>
    <div class="actions">
      <button class="btn btn-primary" onclick="doSearch()">검색</button>
      <button class="btn btn-ghost" onclick="clearFilters()">초기화</button>
    </div>
  </div>

  <!-- 통계 / 발신자 태그 -->
  <div id="statsArea" style="display:none">
    <div class="stats">
      <div class="stat-card"><div class="num" id="totalCount">0</div><div class="lbl">전체 메시지</div></div>
      <div class="stat-card"><div class="num" id="resultCount">0</div><div class="lbl">검색 결과</div></div>
      <div class="stat-card"><div class="num" id="senderCount">0</div><div class="lbl">참여자</div></div>
      <div class="stat-card"><div class="num" id="dayCount">0</div><div class="lbl">대화 일수</div></div>
    </div>
    <div class="senders" id="senderTags"></div>
    <div class="export-bar">
      <span>내보내기:</span>
      <button class="btn btn-ghost" onclick="exportTxt()">📄 TXT</button>
      <button class="btn btn-ghost" onclick="exportCsv()">📊 CSV</button>
    </div>
  </div>

  <div id="status"></div>
  <div id="results"></div>
</div>

<script>
let allMessages = [];
let filteredMessages = [];

// 파일 드롭존
const dropzone = document.getElementById('dropzone');
dropzone.addEventListener('dragover', e => { e.preventDefault(); dropzone.classList.add('hover'); });
dropzone.addEventListener('dragleave', () => dropzone.classList.remove('hover'));
dropzone.addEventListener('drop', e => {
  e.preventDefault(); dropzone.classList.remove('hover');
  const file = e.dataTransfer.files[0];
  if (file) loadFile(file);
});
document.getElementById('fileInput').addEventListener('change', e => {
  if (e.target.files[0]) loadFile(e.target.files[0]);
});

function loadFile(file) {
  const reader = new FileReader();
  reader.onload = ev => {
    const text = ev.target.result;
    // 서버에 파싱 요청
    fetch('/parse', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ text })
    })
    .then(r => r.json())
    .then(data => {
      allMessages = data.messages;
      filteredMessages = allMessages;
      document.getElementById('filterPanel').style.display = '';
      document.getElementById('statsArea').style.display = '';
      renderSenderTags();
      updateStats();
      renderResults(allMessages);
      document.getElementById('status').textContent =
        `✅ "${file.name}" 로드 완료 — ${allMessages.length.toLocaleString()}개 메시지`;
    });
  };
  reader.readAsText(file, 'UTF-8');
}

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
            msgs = parse_kakao_file(body['text'])
            stored_messages = msgs
            self._json({'messages': msgs})

        elif path == '/filter':
            result = filter_messages(
                stored_messages,
                keyword=body.get('keyword', ''),
                sender=body.get('sender', ''),
                date_from=body.get('date_from', ''),
                date_to=body.get('date_to', ''),
            )
            self._json({'messages': result})

    def _json(self, data):
        payload = json.dumps(data, ensure_ascii=False).encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def main():
    port = 5000
    server = HTTPServer(('127.0.0.1', port), Handler)
    url = f'http://localhost:{port}'
    print(f"\n✅ 카카오톡 대화 분석기 실행 중")
    print(f"   브라우저: {url}")
    print(f"   종료: Ctrl+C\n")
    threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n종료되었습니다.")


if __name__ == '__main__':
    main()
