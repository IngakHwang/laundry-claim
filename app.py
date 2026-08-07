# -*- coding: utf-8 -*-
r"""
app.py — 세탁공장 컴플레인 티켓 시스템 v1

구조 (요청이 들어오면):
  브라우저 → FastAPI(이 파일의 함수들) → SQLite(db.sqlite3) → HTML(templates\) → 브라우저

화면 5개:
  /            공장 대시보드 — 미처리 티켓이 위에, 15초마다 자동 새로고침 (공장 모니터용)
  /new         컴플레인 접수 폼
  /c/{id}      티켓 상세 — 조치 일지(타임라인) + 지시·처리·상태 변경
  /me          담당자 선택 (로그인 대신 — v1은 증명용이라 이름 선택으로 충분)
  /me/{id}     내 티켓 — 나에게 배정된 것만, 확인 버튼 (기사가 폰으로 보는 화면)

원칙:
  - 조치 기록(actions)은 append-only — 한 번 쓰면 안 고친다. 수정 대신 새 기록을 쌓는다.
  - SMS 발송은 v2 — notify() 함수가 그 자리다. 지금은 기록만 하고 아무것도 안 보낸다.

실행:  .venv\Scripts\python -m uvicorn app:app --reload
"""

import sqlite3
import uuid
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

BASE = Path(__file__).parent
DB_PATH = BASE / "db.sqlite3"
UPLOAD_DIR = BASE / "uploads"          # 올라온 사진이 저장되는 곳 (DB에는 파일명만)
UPLOAD_DIR.mkdir(exist_ok=True)

app = FastAPI(title="laundry-ticket")
templates = Jinja2Templates(directory=str(BASE / "templates"))
# /uploads/파일명 주소로 저장된 사진을 브라우저에 내보낸다
app.mount("/uploads", StaticFiles(directory=str(UPLOAD_DIR)), name="uploads")

# ── 한국어 표시용 이름표 (DB에는 영문 코드로 저장하고, 화면에서만 한국어로) ──
TYPE_LABEL = {"quality": "세탁 품질", "delivery": "배송", "etc": "기타"}
STATUS_LABEL = {"new": "접수", "acked": "확인됨", "working": "처리중", "done": "완료"}
KIND_LABEL = {"register": "접수", "instruct": "지시", "ack": "확인",
              "work": "처리 보고", "done": "완료 처리", "note": "메모"}
ROLE_LABEL = {"manager": "공장장", "factory": "공장", "driver": "배송기사"}


def now() -> str:
    """지금 시각을 'YYYY-MM-DD HH:MM' 글자로. DB에 시각은 전부 이 형식으로 넣는다."""
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def db() -> sqlite3.Connection:
    """DB 연결을 연다. Row 팩토리 = 결과를 딕셔너리처럼 열 이름으로 꺼내기 위해."""
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")   # 없는 거래처·담당자를 가리키는 데이터가 못 들어오게
    return con


def save_photos(photos: list[UploadFile] | None) -> list[str]:
    """올라온 사진들을 uploads\ 에 저장하고 파일명 목록을 돌려준다.
    왜 사진인가: "얼룩이 있대"(말)와 "여기 이 얼룩"(사진)의 차이 —
    어떤 시트의 어느 부분에 오점이 반복되는지를 직원·기사가 눈으로 알게 하려고 (2026-08-07 인각님 추가)."""
    names = []
    for photo in photos or []:
        if not photo.filename:
            continue
        ext = Path(photo.filename).suffix.lower()
        if ext not in {".jpg", ".jpeg", ".png", ".webp"}:
            continue                   # 사진 파일만 받는다 (그 외는 조용히 무시 — v1)
        name = datetime.now().strftime("%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:6] + ext
        (UPLOAD_DIR / name).write_bytes(photo.file.read())
        names.append(name)
    return names


def attach_photos(con: sqlite3.Connection, action_id: int, photos: list[UploadFile] | None) -> None:
    """저장된 사진들을 조치 한 건에 매단다 (photos 표에 한 장당 한 줄)."""
    for name in save_photos(photos):
        con.execute("INSERT INTO photos (action_id, filename) VALUES (?,?)", (action_id, name))


def notify(target: str, message: str) -> None:
    """(v2 자리) 문자·알림 발송 — 지금은 아무것도 안 보낸다.
    v2에서 SMS API(유료·발신번호 등록 필요)를 여기 끼우면 나머지 코드는 그대로다."""
    pass


def init_db() -> None:
    """표 4장을 만들고, 비어 있으면 시연용 예시 데이터를 넣는다."""
    con = db()
    con.executescript("""
    CREATE TABLE IF NOT EXISTS factories (    -- 공장 — 거래처와 인력이 여기 소속된다
        id   INTEGER PRIMARY KEY,
        name TEXT NOT NULL,
        note TEXT DEFAULT ''                  -- 인력 구성 같은 요약
    );
    CREATE TABLE IF NOT EXISTS clients (      -- 거래처
        id         INTEGER PRIMARY KEY,
        factory_id INTEGER REFERENCES factories(id),   -- 어느 공장이 처리하나
        name       TEXT NOT NULL,
        biz_type   TEXT DEFAULT '',           -- 업태 (관광호텔·모텔·요양병원…)
        rooms      INTEGER DEFAULT 0,         -- 객실(침상) 수 — 물량 추정의 근거
        daily_kg   INTEGER DEFAULT 0,         -- 하루 물량 추정(kg)
        note       TEXT DEFAULT ''            -- 상시 특이사항 ("이불은 항상 개별 포장" 같은 것)
    );
    CREATE TABLE IF NOT EXISTS staff (        -- 담당자 (공장장·공장 인력·배송기사를 한 표에, role로 구분)
        id         INTEGER PRIMARY KEY,
        factory_id INTEGER REFERENCES factories(id),
        name       TEXT NOT NULL,
        role       TEXT NOT NULL CHECK (role IN ('manager','factory','driver'))
    );
    CREATE TABLE IF NOT EXISTS complaints (   -- 컴플레인 티켓 (이 시스템의 중심 개체)
        id          INTEGER PRIMARY KEY,
        client_id   INTEGER NOT NULL REFERENCES clients(id),
        type        TEXT NOT NULL CHECK (type IN ('quality','delivery','etc')),
        severity    TEXT NOT NULL DEFAULT 'normal' CHECK (severity IN ('normal','urgent')),
        content     TEXT NOT NULL,            -- 무슨 컴플레인인가 (전화·문자로 들은 그대로)
        channel     TEXT DEFAULT '전화',       -- 어디로 들어왔나 (전화/문자/방문)
        assignee_id INTEGER REFERENCES staff(id),   -- 누가 맡나
        status      TEXT NOT NULL DEFAULT 'new'
                    CHECK (status IN ('new','acked','working','done')),
        created_at  TEXT NOT NULL,
        done_at     TEXT                      -- 완료 시각 (처리 시간 계산용)
    );
    CREATE TABLE IF NOT EXISTS actions (      -- 조치 일지 — append-only, 티켓의 타임라인
        id           INTEGER PRIMARY KEY,
        complaint_id INTEGER NOT NULL REFERENCES complaints(id),
        actor        TEXT NOT NULL,           -- 누가 (이름 글자 — v1은 로그인이 없으므로)
        kind         TEXT NOT NULL CHECK (kind IN ('register','instruct','ack','work','done','note')),
        content      TEXT DEFAULT '',
        created_at   TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS photos (       -- 첨부 사진 — 조치 하나에 여러 장(1:N)이라 표를 따로 둔다
        id        INTEGER PRIMARY KEY,        -- (한 칸에 파일명 여러 개를 욱여넣으면 정규화 위반)
        action_id INTEGER NOT NULL REFERENCES actions(id),
        filename  TEXT NOT NULL               -- uploads\ 안의 파일명
    );
    """)
    # 이관: 예전 판은 actions.photo 한 칸에 사진 하나였다 → photos 표로 옮기고 칸을 없앤다
    old_cols = [row[1] for row in con.execute("PRAGMA table_info(actions)")]
    if "photo" in old_cols:
        con.execute("""INSERT INTO photos (action_id, filename)
                       SELECT id, photo FROM actions WHERE photo != ''""")
        con.execute("ALTER TABLE actions DROP COLUMN photo")
    # 비어 있을 때만 예시 데이터 (시연용 — 실제 도입 시 지우면 됨)
    # ⚠️ 업체 이름은 전부 가상이다. 실존 호텔·모텔의 「유형과 규모」만 본떴다 —
    #    실명에 지어낸 컴플레인을 붙여 공개 배포하면 그 업체에 대한 허위 기록이 되기 때문.
    if con.execute("SELECT COUNT(*) FROM clients").fetchone()[0] == 0:
        con.executemany("INSERT INTO factories (id, name, note) VALUES (?,?,?)", [
            (1, "제1공장 (창동)", "공장장 1 + 인력 16 + 기사 2 · 담당 8개 업체 · 월 매출 약 1.6억 가정(업체당 약 2천만)"),
            (2, "제2공장 (의정부)", "공장장 1 + 인력 4 + 기사 1 · 담당 2개 업체"),
        ])
        # 하루 물량 추정 공식: 객실수 × 가동률 70% × 객실당 리넨 약 4kg
        # (요양병원 = 침상 × 3kg, 웨딩홀 = 행사 기준이라 객실수 0)
        con.executemany("""INSERT INTO clients
            (factory_id, name, biz_type, rooms, daily_kg, note) VALUES (?,?,?,?,?,?)""", [
            (1, "그랜드한강호텔",        "관광호텔",     350, 980, "이불류는 항상 개별 포장"),
            (1, "수유 리버사이드호텔",   "관광호텔",     300, 840, ""),
            (1, "의정부 엠스테이션호텔", "비즈니스호텔", 200, 560, "고객 유실물 발견 시 즉시 보고"),
            (1, "창동 호텔더블유",       "비즈니스호텔", 180, 500, ""),
            (1, "스테이노원",            "비즈니스호텔", 150, 420, ""),
            (1, "강북성심요양병원",      "요양병원",     300, 900, "환자복·시트 분리 수거, 감염성 세탁물 별도 처리"),
            (1, "도봉 베뉴모텔",         "모텔",          60, 170, ""),
            (1, "미아 클라우드모텔",     "모텔",          45, 130, ""),
            (2, "의정부 그랜드컨벤션",   "웨딩홀·연회",    0, 600, "행사 일정 따라 물량 변동 큼 — 주말 집중"),
            (2, "포천 힐스파리조트",     "온천리조트",   120, 340, "주말 물량 2배"),
        ])
        # 인력: 제1공장 공장장1+16, 제2공장 공장장1+4, 기사 3 (1공장 2·2공장 1)
        rows = [(1, "강만석", "manager"), (2, "윤정례", "manager")]
        rows += [(1, n, "factory") for n in [
            "김세탁", "이다림", "박정리", "최민수", "정호영", "한가람", "오세훈", "서지우",
            "남기웅", "문채원", "임태호", "조성민", "배수지", "신동엽", "권나라", "황보라"]]
        rows += [(2, n, "factory") for n in ["장미란", "전상국", "고아라", "유병재"]]
        rows += [(1, "박기사", "driver"), (1, "최배송", "driver"), (2, "나운전", "driver")]
        con.executemany("INSERT INTO staff (factory_id, name, role) VALUES (?,?,?)", rows)

        def sid(name):
            """이름으로 담당자 id 찾기 (예시 티켓 배정용)."""
            return con.execute("SELECT id FROM staff WHERE name=?", (name,)).fetchone()[0]

        # 예시 티켓 1: 배송 컴플레인 — 기사에게 배정, 아직 미확인
        con.execute("""INSERT INTO complaints
            (client_id, type, severity, content, channel, assignee_id, status, created_at)
            VALUES (1, 'delivery', 'urgent', '어제 납품분에 다른 업체 수건이 섞여 왔음. 회수 요청.',
                    '전화', ?, 'new', ?)""", (sid("박기사"), now()))
        con.execute("""INSERT INTO actions (complaint_id, actor, kind, content, created_at)
            VALUES (1, '사장', 'register', '그랜드한강호텔 지배인 전화 접수', ?)""", (now(),))
        con.execute("""INSERT INTO actions (complaint_id, actor, kind, content, created_at)
            VALUES (1, '사장', 'instruct', '오늘 수거 때 섞인 수건 회수해 올 것', ?)""", (now(),))
        # 예시 티켓 2: 품질 컴플레인 — 공장 인력에게 배정, 처리중
        con.execute("""INSERT INTO complaints
            (client_id, type, severity, content, channel, assignee_id, status, created_at)
            VALUES (6, 'quality', 'normal', '환자복 소매 얼룩이 안 빠진 채 납품됨 (3벌)',
                    '문자', ?, 'working', ?)""", (sid("김세탁"), now()))
        con.execute("""INSERT INTO actions (complaint_id, actor, kind, content, created_at)
            VALUES (2, '사장', 'register', '요양병원 문자 접수, 사진 3장 받음', ?)""", (now(),))
        con.execute("""INSERT INTO actions (complaint_id, actor, kind, content, created_at)
            VALUES (2, '김세탁', 'work', '재세탁 진행. 얼룩 종류가 녹 계열이라 전용 처리 필요', ?)""", (now(),))
    con.commit()
    con.close()


init_db()


# ── 공통: 티켓 목록을 화면용으로 읽는 SQL (거래처·담당자 이름까지 JOIN) ──
TICKET_SELECT = """
SELECT c.*, cl.name AS client_name, s.name AS assignee_name, s.role AS assignee_role,
       f.name AS factory_name
FROM complaints c
JOIN clients cl ON cl.id = c.client_id
LEFT JOIN factories f ON f.id = cl.factory_id
LEFT JOIN staff s ON s.id = c.assignee_id
"""


@app.get("/")
def dashboard(request: Request):
    """공장 대시보드 — 모니터에 띄워두는 화면. 미처리(urgent 먼저, 오래된 것 먼저)가 위."""
    con = db()
    open_tickets = con.execute(TICKET_SELECT + """
        WHERE c.status != 'done'
        ORDER BY (c.severity = 'urgent') DESC, c.created_at ASC""").fetchall()
    done_recent = con.execute(TICKET_SELECT + """
        WHERE c.status = 'done' ORDER BY c.done_at DESC LIMIT 5""").fetchall()
    # 지표: 전화·문자가 절대 못 주는 숫자들
    stats = {
        "open": len(open_tickets),
        "unacked": sum(1 for t in open_tickets if t["status"] == "new"),
        "today": con.execute("SELECT COUNT(*) FROM complaints WHERE created_at LIKE ?",
                             (now()[:10] + "%",)).fetchone()[0],
        # 평균 처리 시간(시간 단위): 완료 티켓의 (완료시각-접수시각) 평균
        "avg_hours": con.execute("""SELECT ROUND(AVG((julianday(done_at)-julianday(created_at))*24),1)
                                    FROM complaints WHERE status='done'""").fetchone()[0],
    }
    by_type = con.execute("""SELECT type, COUNT(*) n FROM complaints
                             GROUP BY type ORDER BY n DESC""").fetchall()
    con.close()
    return templates.TemplateResponse(request, "dashboard.html", {
        "open_tickets": open_tickets, "done_recent": done_recent,
        "stats": stats, "by_type": by_type,
        "TYPE_LABEL": TYPE_LABEL, "STATUS_LABEL": STATUS_LABEL,
    })


@app.get("/new")
def new_form(request: Request):
    """컴플레인 접수 폼 — 거래처·담당자 목록을 공장별로 묶어 보여준다."""
    con = db()
    clients = con.execute("""SELECT c.*, f.name AS factory_name FROM clients c
        LEFT JOIN factories f ON f.id = c.factory_id
        ORDER BY c.factory_id, c.daily_kg DESC""").fetchall()
    staff = con.execute("""SELECT s.*, f.name AS factory_name FROM staff s
        LEFT JOIN factories f ON f.id = s.factory_id
        ORDER BY s.factory_id, CASE s.role WHEN 'manager' THEN 0 WHEN 'driver' THEN 1 ELSE 2 END, s.name""").fetchall()
    con.close()
    return templates.TemplateResponse(request, "new.html", {
        "clients": clients, "staff": staff,
        "TYPE_LABEL": TYPE_LABEL, "ROLE_LABEL": ROLE_LABEL,
    })


@app.get("/clients")
def client_list(request: Request):
    """거래처 현황 — 공장별로 묶어 업태·객실수·하루 물량 추정을 보여준다."""
    con = db()
    factories = con.execute("SELECT * FROM factories ORDER BY id").fetchall()
    clients = con.execute("""SELECT c.*, f.name AS factory_name FROM clients c
        LEFT JOIN factories f ON f.id = c.factory_id
        ORDER BY c.factory_id, c.daily_kg DESC""").fetchall()
    # 공장별 하루 물량 합계 (인력 대비 처리량을 한눈에 보려고)
    totals = {f["id"]: sum(c["daily_kg"] for c in clients if c["factory_id"] == f["id"])
              for f in factories}
    con.close()
    return templates.TemplateResponse(request, "clients.html", {
        "factories": factories, "clients": clients, "totals": totals,
    })


@app.post("/new")
def new_submit(client_id: int = Form(...), type: str = Form(...),
               severity: str = Form("normal"), content: str = Form(...),
               channel: str = Form("전화"), assignee_id: int = Form(...),
               instruction: str = Form(""), photos: list[UploadFile] = File([])):
    """접수 처리: 티켓 생성 + 접수 기록(사진 여러 장 가능) + (지시를 적었으면) 지시 기록 + 담당자 알림(자리)."""
    con = db()
    cur = con.execute("""INSERT INTO complaints
        (client_id, type, severity, content, channel, assignee_id, status, created_at)
        VALUES (?,?,?,?,?,?, 'new', ?)""",
        (client_id, type, severity, content, channel, assignee_id, now()))
    ticket_id = cur.lastrowid
    reg = con.execute("INSERT INTO actions (complaint_id, actor, kind, content, created_at) VALUES (?,?,?,?,?)",
                      (ticket_id, "사장", "register", f"{channel} 접수", now()))
    attach_photos(con, reg.lastrowid, photos)
    if instruction.strip():
        con.execute("INSERT INTO actions (complaint_id, actor, kind, content, created_at) VALUES (?,?,?,?,?)",
                    (ticket_id, "사장", "instruct", instruction.strip(), now()))
    assignee = con.execute("SELECT name FROM staff WHERE id=?", (assignee_id,)).fetchone()
    con.commit(); con.close()
    notify(assignee["name"], f"새 컴플레인 #{ticket_id}")   # v2에서 SMS가 될 자리
    return RedirectResponse(f"/c/{ticket_id}", status_code=303)


@app.get("/c/{ticket_id}")
def ticket_detail(request: Request, ticket_id: int):
    """티켓 상세 — 정보 + 조치 일지(시간순) + 조치 입력 폼."""
    con = db()
    ticket = con.execute(TICKET_SELECT + " WHERE c.id=?", (ticket_id,)).fetchone()
    acts = con.execute("SELECT * FROM actions WHERE complaint_id=? ORDER BY created_at, id",
                       (ticket_id,)).fetchall()
    # 조치별 사진 목록: {조치 id: [파일명, ...]} — 화면에서 각 조치 밑에 사진들을 붙이려고
    photo_map = {}
    for row in con.execute("""SELECT action_id, filename FROM photos
        WHERE action_id IN (SELECT id FROM actions WHERE complaint_id=?)""", (ticket_id,)):
        photo_map.setdefault(row["action_id"], []).append(row["filename"])
    staff = con.execute("SELECT * FROM staff ORDER BY role, name").fetchall()
    con.close()
    return templates.TemplateResponse(request, "ticket.html", {
        "t": ticket, "acts": acts, "staff": staff, "photo_map": photo_map,
        "TYPE_LABEL": TYPE_LABEL, "STATUS_LABEL": STATUS_LABEL, "KIND_LABEL": KIND_LABEL,
    })


# 조치 종류 → 티켓 상태 자동 변경 규칙 (확인하면 '확인됨', 처리 보고하면 '처리중'…)
KIND_TO_STATUS = {"ack": "acked", "work": "working", "done": "done"}


@app.post("/c/{ticket_id}/action")
def add_action(ticket_id: int, actor: str = Form(...), kind: str = Form(...),
               content: str = Form(""), photos: list[UploadFile] = File([])):
    """조치 기록 추가 — 일지에 한 줄 쌓고(사진 여러 장 가능), 종류에 따라 티켓 상태를 옮긴다."""
    con = db()
    cur = con.execute("INSERT INTO actions (complaint_id, actor, kind, content, created_at) VALUES (?,?,?,?,?)",
                      (ticket_id, actor, kind, content.strip(), now()))
    attach_photos(con, cur.lastrowid, photos)
    if kind in KIND_TO_STATUS:
        new_status = KIND_TO_STATUS[kind]
        con.execute("UPDATE complaints SET status=? WHERE id=?", (new_status, ticket_id))
        if new_status == "done":
            con.execute("UPDATE complaints SET done_at=? WHERE id=?", (now(), ticket_id))
    con.commit(); con.close()
    return RedirectResponse(f"/c/{ticket_id}", status_code=303)


@app.get("/me")
def me_select(request: Request):
    """담당자 선택 — v1의 '로그인'. 기사·직원이 자기 이름을 고른다."""
    con = db()
    staff = con.execute("SELECT * FROM staff ORDER BY role, name").fetchall()
    con.close()
    return templates.TemplateResponse(request, "me_select.html",
                                      {"staff": staff, "ROLE_LABEL": ROLE_LABEL})


@app.get("/me/{staff_id}")
def my_tickets(request: Request, staff_id: int):
    """내 티켓 — 나에게 배정된 미완료 티켓. 기사가 폰으로 여는 화면."""
    con = db()
    me = con.execute("SELECT * FROM staff WHERE id=?", (staff_id,)).fetchone()
    tickets = con.execute(TICKET_SELECT + """
        WHERE c.assignee_id=? AND c.status != 'done'
        ORDER BY (c.severity='urgent') DESC, c.created_at ASC""", (staff_id,)).fetchall()
    # 티켓마다 지시 내용과 접수 사진을 같이 보여준다 (기사가 봐야 할 핵심)
    instructions = {}
    for t in tickets:
        rows = con.execute("""SELECT id, kind, content, created_at FROM actions
            WHERE complaint_id=? AND kind IN ('register','instruct')
            ORDER BY created_at""", (t["id"],)).fetchall()
        items = []
        for r in rows:
            photos = [p["filename"] for p in
                      con.execute("SELECT filename FROM photos WHERE action_id=?", (r["id"],))]
            if r["kind"] == "instruct" or photos:   # 접수 줄은 사진이 있을 때만 보여준다
                items.append({"kind": r["kind"], "content": r["content"],
                              "photos": photos, "created_at": r["created_at"]})
        instructions[t["id"]] = items
    con.close()
    return templates.TemplateResponse(request, "me.html", {
        "me": me, "tickets": tickets, "instructions": instructions,
        "TYPE_LABEL": TYPE_LABEL, "STATUS_LABEL": STATUS_LABEL, "ROLE_LABEL": ROLE_LABEL,
    })
