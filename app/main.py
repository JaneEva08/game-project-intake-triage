from __future__ import annotations

import json
import html
import logging
import os
import secrets
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel, Field, field_validator

from app.llm import TriageAnalysis, analyze_message

load_dotenv()

DB_PATH = Path(os.getenv("DATABASE_PATH", "data/triage.sqlite3"))
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
RATE_LIMIT = max(1, int(os.getenv("RATE_LIMIT_PER_MINUTE", "10")))

app = FastAPI(title="Game Project Intake Triage", version="0.1.0")
client_requests: dict[str, list[float]] = {}
logger = logging.getLogger(__name__)
admin_auth = HTTPBasic()


class TriageRequest(BaseModel):
    text: str = Field(min_length=1, max_length=2000)
    channel: Literal["email", "form", "chat"]
    client_id: str = Field(min_length=1, max_length=120)

    @field_validator("text", "client_id")
    @classmethod
    def require_non_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value.strip()


class TriageResponse(BaseModel):
    ticket_id: int
    category: Literal["billing", "support", "complaint", "other"]
    business_type: Literal["incoming_offer", "reply_to_outreach", "request_for_information", "other"]
    text_ru: str | None
    draft_reply: str = Field(min_length=1)
    confidence: Literal["high", "medium", "low"]
    escalate: bool
    missing_fields: list[str]
    form_url: str | None


class FormSubmission(BaseModel):
    studio_name: str = Field(min_length=1, max_length=200)
    game_title: str = Field(min_length=1, max_length=200)
    contact_email: str = Field(min_length=3, max_length=254)
    game_page_url: str = Field(min_length=1, max_length=500)
    materials_url: str = Field(min_length=1, max_length=500)
    note: str = Field(default="", max_length=2000)

    @field_validator("studio_name", "game_title", "contact_email", "game_page_url", "materials_url")
    @classmethod
    def require_non_blank_submission_fields(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value.strip()

    @field_validator("contact_email")
    @classmethod
    def validate_email_shape(cls, value: str) -> str:
        if "@" not in value or "." not in value.rsplit("@", 1)[-1]:
            raise ValueError("must be a valid email address")
        return value

    @field_validator("game_page_url", "materials_url")
    @classmethod
    def validate_http_url(cls, value: str) -> str:
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("must be an http(s) URL")
        return value


def connect_db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    return connection


@contextmanager
def db_session():
    connection = connect_db()
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def init_db() -> None:
    with db_session() as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS tickets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                client_id TEXT NOT NULL,
                channel TEXT NOT NULL,
                text TEXT NOT NULL,
                text_ru TEXT,
                category TEXT NOT NULL,
                business_type TEXT NOT NULL DEFAULT 'other',
                status TEXT NOT NULL DEFAULT 'needs_information',
                confidence TEXT NOT NULL,
                escalate INTEGER NOT NULL,
                draft_reply TEXT NOT NULL,
                error TEXT,
                missing_fields TEXT NOT NULL,
                prefill_data TEXT NOT NULL DEFAULT '{}',
                form_token TEXT UNIQUE,
                form_data TEXT,
                form_submitted_at TEXT,
                staff_note TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticket_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                event_type TEXT NOT NULL,
                details TEXT NOT NULL,
                FOREIGN KEY(ticket_id) REFERENCES tickets(id)
            );
            """
        )
        columns = {row["name"] for row in db.execute("PRAGMA table_info(tickets)")}
        if "prefill_data" not in columns:
            db.execute("ALTER TABLE tickets ADD COLUMN prefill_data TEXT NOT NULL DEFAULT '{}'")
        if "text_ru" not in columns:
            db.execute("ALTER TABLE tickets ADD COLUMN text_ru TEXT")
        if "business_type" not in columns:
            db.execute("ALTER TABLE tickets ADD COLUMN business_type TEXT NOT NULL DEFAULT 'other'")
        if "status" not in columns:
            db.execute("ALTER TABLE tickets ADD COLUMN status TEXT NOT NULL DEFAULT 'needs_information'")
        if "staff_note" not in columns:
            db.execute("ALTER TABLE tickets ADD COLUMN staff_note TEXT NOT NULL DEFAULT ''")
        # Move notes saved by the first MVP version out of game data, including existing DBs.
        for row in db.execute("SELECT id, prefill_data, staff_note FROM tickets").fetchall():
            prefill = json.loads(row["prefill_data"] or "{}")
            legacy_note = prefill.pop("note", None)
            if legacy_note:
                db.execute(
                    "UPDATE tickets SET staff_note = ?, prefill_data = ? WHERE id = ?",
                    (row["staff_note"] or legacy_note, json.dumps(prefill, ensure_ascii=False), row["id"]),
                )


@app.on_event("startup")
def startup() -> None:
    init_db()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def check_rate_limit(client_id: str) -> None:
    now = time.monotonic()
    recent = [stamp for stamp in client_requests.get(client_id, []) if now - stamp < 60]
    if len(recent) >= RATE_LIMIT:
        raise HTTPException(status_code=429, detail="Rate limit exceeded for client_id")
    recent.append(now)
    client_requests[client_id] = recent


def detect_language(text: str) -> str:
    if any("\u4e00" <= char <= "\u9fff" for char in text):
        return "zh"
    if any("\u0400" <= char <= "\u04ff" for char in text):
        return "ru"
    return "en"


def fallback_reply(text: str) -> str:
    language = detect_language(text)
    if language == "zh":
        return "感谢您联系合作团队。我们会安排同事核对信息并与您联系。"
    if language == "ru":
        return "Спасибо за предложение. Мы передадим обращение сотруднику для проверки и уточнения данных."
    return "Thank you for contacting our partnerships team. A colleague will review the request and follow up."


def form_request_sentence(text: str, form_url: str) -> str:
    language = detect_language(text)
    if language == "zh":
        return f"为了继续评估该游戏，请通过此链接补充缺少的信息：{form_url}"
    if language == "ru":
        return f"Чтобы продолжить оценку игры, дополните сведения по ссылке: {form_url}"
    return f"To continue reviewing the game, please add the missing details here: {form_url}"


def require_admin(credentials: HTTPBasicCredentials = Depends(admin_auth)) -> str:
    expected_user = os.getenv("ADMIN_USER", "")
    expected_password = os.getenv("ADMIN_PASSWORD", "")
    valid_user = secrets.compare_digest(credentials.username, expected_user) if expected_user else False
    valid_password = secrets.compare_digest(credentials.password, expected_password) if expected_password else False
    if not expected_user or not expected_password:
        raise HTTPException(status_code=503, detail="Admin credentials are not configured")
    if not (valid_user and valid_password):
        raise HTTPException(status_code=401, detail="Invalid admin credentials", headers={"WWW-Authenticate": "Basic"})
    return credentials.username


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/admin", response_class=HTMLResponse)
def admin_list(username: str = Depends(require_admin)) -> str:
    with db_session() as db:
        tickets = db.execute(
            "SELECT id, created_at, category, business_type, status, text, text_ru "
            "FROM tickets ORDER BY id DESC LIMIT 100"
        ).fetchall()
    rows = []
    for ticket in tickets:
        rows.append(
            "<tr>"
            f"<td><a href='/admin/tickets/{ticket['id']}'>#{ticket['id']}</a></td>"
            f"<td>{html.escape(ticket['created_at'])}</td>"
            f"<td>{html.escape(ticket['status'])}</td>"
            f"<td>{html.escape(ticket['category'])} / {html.escape(ticket['business_type'])}</td>"
            f"<td>{html.escape(ticket['text'][:240])}</td>"
            f"<td>{html.escape((ticket['text_ru'] or '')[:240])}</td>"
            "</tr>"
        )
    return f"""<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Заявки — панель сотрудника</title><style>body{{font:15px system-ui;margin:24px;background:#f4f6fa;color:#172033}}main{{max-width:1200px;margin:auto}}table{{border-collapse:collapse;width:100%;background:white}}th,td{{padding:10px;border-bottom:1px solid #dde2eb;text-align:left;vertical-align:top}}td{{max-width:280px;overflow-wrap:anywhere}}a{{color:#244fc3}}.muted{{color:#687386}}</style>
<main><h1>Заявки на рассмотрение</h1><p class="muted">Вход выполнен: {html.escape(username)} · последние 100 записей</p>
<table><thead><tr><th>ID</th><th>Создано</th><th>Статус</th><th>Тип</th><th>Оригинал</th><th>Перевод</th></tr></thead><tbody>{''.join(rows)}</tbody></table></main></html>"""


@app.get("/admin/tickets/{ticket_id}", response_class=HTMLResponse)
def admin_ticket(ticket_id: int, username: str = Depends(require_admin)) -> str:
    with db_session() as db:
        ticket = db.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
    if ticket is None:
        raise HTTPException(status_code=404, detail="Ticket not found")
    existing = json.loads(ticket["prefill_data"] or "{}")
    submitted = json.loads(ticket["form_data"] or "{}")
    values = {**existing, **submitted}
    labels = {
        "studio_name": "Компания / студия",
        "game_title": "Название игры",
        "contact_email": "Email контакта",
        "game_page_url": "Страница игры",
        "materials_url": "Ссылка на материалы",
    }
    inputs = []
    for key, label in labels.items():
        value = html.escape(str(values.get(key) or ""), quote=True)
        kind = "email" if key == "contact_email" else "url" if key.endswith("_url") else "text"
        inputs.append(f'<label>{label}<input name="{key}" type="{kind}" value="{value}" required maxlength="500"></label>')
    source = html.escape(ticket["text"])
    translated = html.escape(ticket["text_ru"] or "Перевод недоступен: заявка обработана через fallback.")
    missing = html.escape(", ".join(json.loads(ticket["missing_fields"] or "[]")) or "нет")
    staff_note = ticket["staff_note"] or existing.get("note") or ""
    developer_note = html.escape(str(submitted.get("note") or "Не указано."))
    return f"""<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Заявка #{ticket_id}</title><style>body{{font:16px system-ui;max-width:850px;margin:28px auto;padding:0 16px;background:#f4f6fa;color:#172033}}section,form{{background:white;padding:18px;margin:14px 0;border-radius:12px}}label{{display:grid;gap:6px;margin:12px 0}}input,textarea,button{{font:inherit;padding:10px;border:1px solid #cbd3e0;border-radius:8px}}input{{width:100%;box-sizing:border-box}}button{{background:#244fc3;color:white;cursor:pointer}}pre{{white-space:pre-wrap;overflow-wrap:anywhere}}a{{color:#244fc3}}</style>
<p><a href="/admin">← Все заявки</a></p><h1>Заявка #{ticket_id}</h1>
<section><b>Статус:</b> {html.escape(ticket['status'])} · <b>Тип:</b> {html.escape(ticket['business_type'])} · <b>Категория:</b> {html.escape(ticket['category'])}<br><b>Не хватает:</b> {missing}</section>
<section><h2>Оригинал</h2><pre>{source}</pre><h2>Перевод на русский</h2><pre>{translated}</pre></section>
<section><h2>Комментарий разработчика</h2><p>{developer_note}</p></section>
<form id="edit"><h2>Проверить и дополнить данные</h2>{''.join(inputs)}<label>Заметка сотрудника<textarea name="note" maxlength="2000">{html.escape(str(staff_note))}</textarea></label><button>Сохранить и передать на проверку</button><p id="result" role="status"></p></form>
<script>document.querySelector('#edit').addEventListener('submit',async e=>{{e.preventDefault();const r=await fetch('/admin/tickets/{ticket_id}/update',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify(Object.fromEntries(new FormData(e.target)))}});document.querySelector('#result').textContent=r.ok?'Данные сохранены. Заявка передана на проверку.':await r.text();}});</script></html>"""


@app.post("/admin/tickets/{ticket_id}/update")
def admin_update(ticket_id: int, payload: FormSubmission, username: str = Depends(require_admin)) -> dict[str, str | int]:
    timestamp = utc_now()
    with db_session() as db:
        ticket = db.execute("SELECT id, prefill_data FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
        if ticket is None:
            raise HTTPException(status_code=404, detail="Ticket not found")
        data = payload.model_dump()
        previous = json.loads(ticket["prefill_data"] or "{}")
        previous.pop("note", None)
        staff_note = data.pop("note", "")
        merged = {**previous, **{key: value for key, value in data.items() if value}}
        missing = [key for key in ("studio_name", "game_title", "contact_email", "game_page_url", "materials_url") if not str(merged.get(key) or "").strip()]
        status = "needs_information" if missing else "awaiting_staff_review"
        db.execute(
            "UPDATE tickets SET prefill_data = ?, missing_fields = ?, status = ?, staff_note = ? WHERE id = ?",
            (json.dumps(merged, ensure_ascii=False), json.dumps(missing), status, staff_note, ticket_id),
        )
        db.execute(
            "INSERT INTO events (ticket_id, created_at, event_type, details) VALUES (?, ?, ?, ?)",
            (ticket_id, timestamp, "staff_updated", f"Updated by {username}; status={status}; missing={len(missing)}"),
        )
    return {"ticket_id": ticket_id, "status": status}


@app.post("/triage", response_model=TriageResponse, status_code=201)
def triage(payload: TriageRequest) -> TriageResponse:
    check_rate_limit(payload.client_id)
    analysis: TriageAnalysis | None = None
    error_code: str | None = None
    try:
        analysis = analyze_message(payload.text)
        extracted = analysis.extracted_data.model_dump()
    except Exception as exc:
        if str(exc) == "llm_not_configured":
            error_code = "llm_not_configured"
        elif type(exc) is ValueError:
            error_code = f"analysis_validation:{str(exc)[:100]}"
        else:
            error_code = type(exc).__name__
        logger.warning("Triage analyzer unavailable: %s", error_code)
        translation_ru = None
        extracted = {
            "studio_name": None,
            "game_title": None,
            "contact_email": None,
            "game_page_url": None,
            "materials_url": None,
        }

    required_fields = ["studio_name", "game_title", "contact_email", "game_page_url", "materials_url"]
    missing = [name for name in required_fields if not (extracted.get(name) or "").strip()]
    if analysis is None:
        category, business_type, confidence, escalate = "other", "other", "low", True
        draft = fallback_reply(payload.text)
    else:
        category = analysis.category
        business_type = analysis.business_type
        translation_ru = analysis.translation_ru
        confidence = analysis.confidence
        escalate = analysis.escalate or confidence == "low"
        draft = analysis.draft_reply

    token = secrets.token_urlsafe(24) if missing else None
    form_url = f"{PUBLIC_BASE_URL}/forms/{token}" if token else None
    if missing:
        confidence, escalate = "low", True
        if form_url and form_url not in draft:
            draft = f"{draft.rstrip()} {form_request_sentence(payload.text, form_url)}"
    timestamp = utc_now()

    try:
        with db_session() as db:
            cursor = db.execute(
                """INSERT INTO tickets
                (created_at, client_id, channel, text, text_ru, category, business_type, status, confidence, escalate,
                 draft_reply, error, missing_fields, prefill_data, form_token)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (timestamp, payload.client_id, payload.channel, payload.text, translation_ru, category,
                 business_type, "needs_information" if missing else "awaiting_staff_review",
                 confidence, int(escalate), draft, error_code, json.dumps(missing),
                 json.dumps(extracted, ensure_ascii=False), token),
            )
            ticket_id = int(cursor.lastrowid)
            db.execute(
                "INSERT INTO events (ticket_id, created_at, event_type, details) VALUES (?, ?, ?, ?)",
                (ticket_id, timestamp, "triage_fallback" if analysis is None else "triage_completed",
                 error_code or json.dumps({"category": category, "missing_fields": missing}, ensure_ascii=False)),
            )
    except sqlite3.Error as exc:
        logger.exception("Could not write triage record")
        raise HTTPException(status_code=500, detail="Could not save request") from exc

    return TriageResponse(
        ticket_id=ticket_id,
        category=category,
        business_type=business_type,
        text_ru=translation_ru,
        draft_reply=draft,
        confidence=confidence,
        escalate=escalate,
        missing_fields=missing,
        form_url=form_url,
    )


@app.get("/forms/{token}", response_class=HTMLResponse)
def get_form(token: str) -> str:
    with db_session() as db:
        ticket = db.execute(
            "SELECT id, form_submitted_at, prefill_data, missing_fields FROM tickets WHERE form_token = ?",
            (token,),
        ).fetchone()
    if ticket is None:
        raise HTTPException(status_code=404, detail="Form link not found")
    if ticket["form_submitted_at"]:
        raise HTTPException(status_code=409, detail="This form has already been submitted")

    prefill = json.loads(ticket["prefill_data"] or "{}")
    missing = json.loads(ticket["missing_fields"] or "[]")
    labels = {
        "studio_name": "Studio / rights holder · 工作室或版权方",
        "game_title": "Game title · 游戏名称",
        "contact_email": "Contact email · 联系邮箱",
        "game_page_url": "Game page URL · 游戏页面链接",
        "materials_url": "Screenshots and video · 截图与视频链接",
    }
    form_rows = []
    for field_name, label in labels.items():
        value = html.escape(str(prefill.get(field_name) or ""), quote=True)
        input_type = "email" if field_name == "contact_email" else "url" if field_name.endswith("_url") else "text"
        required = "required" if field_name in missing else ""
        form_rows.append(
            f'<label>{label}<input name="{field_name}" type="{input_type}" value="{value}" '
            f'maxlength="500" {required}></label>'
        )
    missing_labels = ", ".join(labels.get(name, name) for name in missing)

    return f"""<!doctype html>
<html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Project details / 项目资料</title>
<style>body{{font:16px system-ui;max-width:680px;margin:32px auto;padding:0 16px;background:#f5f7fb;color:#172033}}form{{display:grid;gap:14px;background:white;padding:22px;border-radius:14px;box-shadow:0 8px 32px #17203312}}label{{display:grid;gap:6px}}input,textarea,button{{font:inherit;padding:10px;border:1px solid #ccd3df;border-radius:8px}}input{{width:100%;box-sizing:border-box}}button{{background:#234cc2;color:white;cursor:pointer}}#result{{white-space:pre-wrap}}</style>
<h1>Project details / 项目资料</h1><p>Request #{ticket['id']} — please complete the missing fields / 请补充缺少的信息。</p><p>{html.escape(missing_labels)}</p>
<form id="details">
{''.join(form_rows)}
<label>Additional note · 补充说明<textarea name="note" maxlength="2000"></textarea></label>
<button>Submit · 提交</button><div id="result" role="status"></div></form>
<script>document.querySelector('#details').addEventListener('submit',async e=>{{e.preventDefault();const data=Object.fromEntries(new FormData(e.target));const r=await fetch('/forms/{token}/submit',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify(data)}});document.querySelector('#result').textContent=r.ok?'Thank you. Your information has been recorded.':await r.text();if(r.ok)e.target.querySelector('button').disabled=true;}});</script>
</html>"""


@app.post("/forms/{token}/submit")
def submit_form(token: str, payload: FormSubmission) -> dict[str, str | int]:
    timestamp = utc_now()
    with db_session() as db:
        ticket = db.execute("SELECT id, form_submitted_at FROM tickets WHERE form_token = ?", (token,)).fetchone()
        if ticket is None:
            raise HTTPException(status_code=404, detail="Form link not found")
        if ticket["form_submitted_at"]:
            raise HTTPException(status_code=409, detail="This form has already been submitted")
        data = payload.model_dump()
        previous = json.loads(
            db.execute("SELECT prefill_data FROM tickets WHERE id = ?", (ticket["id"],)).fetchone()[0] or "{}"
        )
        merged = {**previous, **{key: value for key, value in data.items() if value}}
        missing = [
            name for name in ("studio_name", "game_title", "contact_email", "game_page_url", "materials_url")
            if not str(merged.get(name) or "").strip()
        ]
        db.execute(
            "UPDATE tickets SET form_data = ?, prefill_data = ?, missing_fields = ?, status = ?, form_submitted_at = ? WHERE id = ?",
            (json.dumps(data, ensure_ascii=False), json.dumps(merged, ensure_ascii=False),
             json.dumps(missing), "needs_information" if missing else "awaiting_staff_review",
             timestamp, ticket["id"]),
        )
        db.execute(
            "INSERT INTO events (ticket_id, created_at, event_type, details) VALUES (?, ?, ?, ?)",
            (ticket["id"], timestamp, "form_submitted", "Developer submitted the missing game details"),
        )
    return {"ticket_id": int(ticket["id"]), "status": "submitted"}
