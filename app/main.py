from __future__ import annotations

import json
import html
import logging
import mimetypes
import os
import re
import secrets
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, File, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel, Field, field_validator

from app.llm import TriageAnalysis, analyze_message

load_dotenv()

DB_PATH = Path(os.getenv("DATABASE_PATH", "data/triage.sqlite3"))
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
RATE_LIMIT = max(1, int(os.getenv("RATE_LIMIT_PER_MINUTE", "10")))
UPLOAD_DIR = Path(os.getenv("UPLOAD_DIR", "data/uploads"))
MAX_UPLOAD_BYTES = 20 * 1024 * 1024
MAX_TOTAL_UPLOAD_BYTES = 40 * 1024 * 1024
MAX_ATTACHMENTS_PER_TICKET = 5
ALLOWED_UPLOAD_EXTENSIONS = {".pdf", ".ppt", ".pptx", ".doc", ".docx", ".zip", ".7z", ".png", ".jpg", ".jpeg", ".webp", ".mp4", ".mov", ".txt"}

app = FastAPI(title="Game Project Intake Triage", version="0.1.0")
client_requests: dict[str, list[float]] = {}
logger = logging.getLogger(__name__)
admin_auth = HTTPBasic()


@app.get("/", response_class=HTMLResponse)
def developer_landing() -> str:
    return """<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="theme-color" content="#10151b"><title id="page-title">Сотрудничество с разработчиками игр</title>
<style>
:root{color-scheme:dark;--bg:#10151b;--panel:#171e26;--text:#f4f6f8;--muted:#a8b3bf;--line:#303c48;--accent:#c7f36b;--accent-ink:#17200c;--blue:#9bc4ff;--danger:#ffb4a9}
:root[data-theme="light"]{color-scheme:light;--bg:#f4f7f8;--panel:#fff;--text:#18232d;--muted:#5f6d79;--line:#d6e0e6;--accent:#b4df59;--accent-ink:#17200c;--blue:#245e9f;--danger:#a52c25}
:root[data-theme="light"] body{background:radial-gradient(ellipse at 10% 0%,#e5f1dc 0,transparent 40%),var(--bg)}
:root[data-theme="light"] .top{border-bottom-color:#18232d18}
:root[data-theme="light"] label{color:#34424e}
:root[data-theme="light"] input,:root[data-theme="light"] textarea{background:#fff;border-color:#c7d3dc;color:var(--text)}
:root[data-theme="light"] input::placeholder,:root[data-theme="light"] textarea::placeholder{color:#7b8994}
:root[data-theme="light"] .result{border-color:#b9d58f;background:#f0f7e8}
:root[data-theme="light"] .result p{color:#2f3d2a}
:root[data-theme="light"] .result .meta{color:#5a6c4e}
:root[data-theme="light"] .result a{color:#345b19}
:root[data-theme="light"] .footer{border-top-color:#18232d18}
*{box-sizing:border-box}body{margin:0;background:radial-gradient(ellipse at 10% 0%,#233340 0,transparent 38%),var(--bg);color:var(--text);font:16px/1.55 Inter,ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif}a{color:var(--blue)}.wrap{width:min(1120px,calc(100% - 40px));margin:auto}.top{height:76px;border-bottom:1px solid #ffffff16;display:flex;align-items:center}.bar{display:flex;justify-content:space-between;align-items:center;gap:20px}.brand{display:flex;align-items:center;gap:12px;font-weight:750;letter-spacing:.02em}.mark{width:34px;height:34px;border-radius:11px;background:var(--accent);color:var(--accent-ink);display:grid;place-items:center;font-weight:900}.right{display:flex;align-items:center;gap:22px}.crumb{font-size:14px;color:var(--muted)}.crumb b{color:var(--text);font-weight:600}.lang,.theme-toggle{border:1px solid var(--line);border-radius:9px;background:var(--panel);color:var(--text);padding:8px 10px;font:inherit;font-size:13px}.theme-toggle{width:38px;height:38px;padding:0;cursor:pointer;font-size:18px;line-height:1}.hero{padding:64px 0 40px;max-width:860px}.eyebrow{color:var(--accent);font-size:13px;font-weight:750;text-transform:uppercase;letter-spacing:.14em}.hero h1{font-size:clamp(38px,5.6vw,66px);line-height:1.03;letter-spacing:-.045em;margin:16px 0 18px;max-width:740px}.hero p{font-size:18px;max-width:650px;color:var(--muted);margin:0}.layout{display:grid;grid-template-columns:minmax(0,860px);gap:24px;align-items:start;padding-bottom:70px}.card{border:1px solid var(--line);border-radius:20px;background:var(--panel);padding:28px;box-shadow:0 20px 70px #0002}.card h2{font-size:25px;line-height:1.2;margin:0 0 8px}.sub{color:var(--muted);margin:0 0 24px}.grid{display:grid;grid-template-columns:1fr 1fr;gap:16px}.field{display:flex;flex-direction:column;gap:7px}.wide{grid-column:1/-1}label{font-size:13px;color:#d7dee5;font-weight:650}input,textarea{width:100%;border:1px solid #465360;border-radius:10px;background:#10161d;color:var(--text);padding:12px 13px;font:inherit;outline:none;transition:border .15s,box-shadow .15s}input::placeholder,textarea::placeholder{color:#778492}input:focus,textarea:focus{border-color:var(--accent);box-shadow:0 0 0 3px #c7f36b20}textarea{min-height:130px;resize:vertical}.help{font-size:12px;color:var(--muted)}.submit{margin-top:20px;border:0;border-radius:10px;background:var(--accent);color:var(--accent-ink);font:inherit;font-size:15px;font-weight:700;padding:13px 20px;cursor:pointer}.submit:disabled{opacity:.6;cursor:wait}.privacy{font-size:12px;color:var(--muted);margin:12px 0 0}.result{display:none;margin-top:22px;border:1px solid #56772d;border-radius:14px;background:#202c1a;padding:18px}.result h3{margin:0 0 8px}.result p{margin:6px 0;color:#e1e9d9;overflow-wrap:anywhere}.result .meta{font-size:13px;color:#c4d1b6}.result a{display:inline-block;margin-top:10px;color:var(--accent)}.error{display:none;margin-top:16px;color:var(--danger);font-size:14px}.footer{border-top:1px solid #ffffff16;padding:22px 0;color:var(--muted);font-size:13px}
@media(max-width:760px){.wrap{width:min(100% - 28px,620px)}.top{height:auto;min-height:64px;padding:10px 0}.bar{gap:8px;flex-wrap:wrap}.right{gap:8px;width:100%;justify-content:space-between}.hero{padding:42px 0 26px;max-width:none}.hero h1{font-size:clamp(36px,11vw,54px)}.hero p{font-size:16px}.layout{grid-template-columns:1fr;gap:16px;padding-bottom:42px}.card{padding:21px 17px;border-radius:16px}.card h2{font-size:22px}.grid{grid-template-columns:1fr;gap:13px}.wide{grid-column:auto}.crumb{font-size:11px}.brand{font-size:12px;gap:8px}.mark{width:30px;height:30px}.lang{padding:7px 6px;font-size:12px}}
</style></head><body>
<header class="top"><div class="wrap bar"><div class="brand"><span class="mark">G</span><span>GAME PARTNERSHIPS</span></div><div class="right"><div class="crumb"><span id="nav-coop">Сотрудничество</span> <span aria-hidden="true">/</span> <b id="nav-dev">Для разработчиков</b></div><label class="help" for="language" id="language-label">Язык</label><select class="lang" id="language" aria-label="Язык"><option value="ru">Русский</option><option value="en">English</option><option value="zh">中文</option></select><button class="theme-toggle" id="theme-toggle" type="button" aria-label="Включить светлую тему" title="Включить светлую тему" aria-pressed="false">☀</button></div></div></header>
<main class="wrap"><section class="hero"><div><div class="eyebrow" id="eyebrow">Для игровых студий</div><h1 id="hero-title">Давайте обсудим локализацию вашей игры</h1><p id="hero-description">Расскажите о проекте и оставьте контакты. Команда изучит предложение и свяжется с вами, если понадобятся дополнительные материалы или данные.</p></div></section>
<section class="layout"><div class="card" id="proposal-form"><h2 id="form-title">Предложить игру</h2><p class="sub" id="form-intro">Поля со звёздочкой обязательны. Ссылки на материалы можно добавить сразу или позже.</p>
<form id="proposal"><div class="grid">
<div class="field"><label for="studio" id="label-studio">Студия / компания *</label><input id="studio" required maxlength="200" placeholder="Например, Pine Lantern Studio"></div>
<div class="field"><label for="game" id="label-game">Название игры *</label><input id="game" required maxlength="200" placeholder="Оригинальное название"></div>
<div class="field"><label for="email" id="label-email">Email для связи *</label><input id="email" type="email" required maxlength="254" placeholder="name@studio.example"></div>
<div class="field"><label for="gameurl" id="label-gameurl">Страница игры</label><input id="gameurl" type="url" maxlength="500" placeholder="https://store.example/game"></div>
<div class="field wide"><label for="materials" id="label-materials">Материалы: скриншоты, трейлер, презентация</label><input id="materials" type="url" maxlength="500" placeholder="https://drive.example/game-materials"><span class="help" id="materials-help">Подойдёт ссылка на облачную папку или страницу с материалами.</span></div>
<div class="field wide"><label for="files" id="label-files">Или прикрепите файлы</label><input id="files" type="file" multiple accept=".pdf,.ppt,.pptx,.doc,.docx,.zip,.7z,.png,.jpg,.jpeg,.webp,.mp4,.mov,.txt"><span class="help" id="files-help">До 5 файлов, до 20 МБ каждый; PDF, презентации, документы, изображения, видео и архивы.</span><span class="help" id="files-selected"></span></div>
<div class="field wide"><label for="message" id="label-message">Коротко расскажите об игре и предложении *</label><textarea id="message" required minlength="1" maxlength="1500" placeholder="Жанр, платформы, текущие языки, планы по локализации и условия сотрудничества"></textarea><span class="help"><span id="count">0</span>/1500 <span id="char-label">символов</span></span></div>
</div><button class="submit" id="send" type="submit">Отправить предложение</button><p class="privacy" id="privacy-note">После отправки вы увидите статус обработки. Это учебный MVP: решение о сотрудничестве принимает сотрудник.</p></form>
<div class="error" id="error" role="alert"></div><section class="result" id="result" aria-live="polite"><h3 id="result-title">Спасибо за предложение!</h3><p id="receipt"></p><p class="meta" id="ticket"></p><p class="meta" id="attachment-warning" hidden></p></section></div>
 </section></main>
<footer class="footer"><div class="wrap" id="footer-text">Предложения по локализации игровых проектов · Учебный MVP</div></footer>
<script>
const copy={
ru:{title:'Сотрудничество с разработчиками игр',coop:'Сотрудничество',dev:'Для разработчиков',language:'Язык',eyebrow:'Для игровых студий',hero:'Давайте обсудим локализацию вашей игры',description:'Расскажите о проекте и оставьте контакты. Команда изучит предложение и свяжется с вами, если понадобятся дополнительные материалы или данные.',formTitle:'Предложить игру',formIntro:'Поля со звёздочкой обязательны. Ссылки на материалы можно добавить сразу или позже.',studio:'Студия / компания *',game:'Название игры *',email:'Email для связи *',gameurl:'Страница игры',materials:'Материалы: скриншоты, трейлер, презентация',materialsHelp:'Подойдёт ссылка на облачную папку или страницу с материалами.',message:'Коротко расскажите об игре и предложении *',messagePlaceholder:'Жанр, платформы, текущие языки, планы по локализации и условия сотрудничества',chars:'символов',send:'Отправить предложение',sending:'Отправляем…',privacy:'Команда рассмотрит ваше предложение. Если проект подойдёт для дальнейшего обсуждения, мы свяжемся с вами.',result:'Спасибо за предложение!',receipt:'Мы получили информацию об игре и передали её команде на рассмотрение. Если проект подойдёт для дальнейшего обсуждения, мы свяжемся с вами по указанному адресу электронной почты.',ticketLabel:'Номер заявки',footer:'Предложения по локализации игровых проектов · Учебный MVP',companyPlaceholder:'Например, Pine Lantern Studio',gamePlaceholder:'Оригинальное название',materialPlaceholder:'https://drive.example/game-materials',countError:'Суммарный текст заявки превышает лимит 2000 символов. Сократите описание или уберите необязательные поля.',sendError:'Не удалось отправить заявку',retry:'Попробуйте ещё раз.'},
en:{title:'Game developer partnerships',coop:'Partnerships',dev:'For developers',language:'Language',eyebrow:'For game studios',hero:'Let’s discuss localizing your game',description:'Tell us about your project and leave your contact details. Our team will review the proposal and reach out if we need more information or materials.',formTitle:'Submit a game proposal',formIntro:'Fields marked with an asterisk are required. You can add links to materials now or later.',studio:'Studio / company *',game:'Game title *',email:'Contact email *',gameurl:'Game page',materials:'Materials: screenshots, trailer, presentation',materialsHelp:'A link to a cloud folder or a page with materials works well.',message:'Briefly describe the game and your proposal *',messagePlaceholder:'Genre, platforms, current languages, localization plans and partnership terms',chars:'characters',send:'Send proposal',sending:'Sending…',privacy:'Our team will review your proposal. If the project is a fit for further discussion, we’ll contact you.',result:'Thank you for your proposal!',receipt:'We’ve received the game information and shared it with our team for review. If the project is a fit for further discussion, we’ll contact you at the email address provided.',ticketLabel:'Request number',footer:'Game localization proposals · Educational MVP',companyPlaceholder:'For example, Pine Lantern Studio',gamePlaceholder:'Original title',materialPlaceholder:'https://drive.example/game-materials',countError:'The complete proposal exceeds the 2,000-character limit. Shorten the description or remove optional fields.',sendError:'Could not submit the proposal',retry:'Please try again.'},
zh:{title:'游戏开发者合作',coop:'合作',dev:'开发者专区',language:'语言',eyebrow:'面向游戏工作室',hero:'让我们聊聊您的游戏本地化',description:'请介绍您的项目并留下联系方式。我们的团队会查看提案；如需更多信息或素材，我们会与您联系。',formTitle:'提交游戏合作提案',formIntro:'带星号的字段为必填项。您可以现在或稍后添加素材链接。',studio:'工作室 / 公司 *',game:'游戏名称 *',email:'联系邮箱 *',gameurl:'游戏页面',materials:'游戏素材：截图、预告片、介绍',materialsHelp:'可以填写云盘文件夹或素材页面链接。',message:'请简单介绍游戏和合作提案 *',messagePlaceholder:'游戏类型、平台、现有语言、本地化计划和合作条件',chars:'个字符',send:'提交提案',sending:'正在提交…',privacy:'我们的团队会审核您的提案。如果项目适合进一步讨论，我们会与您联系。',result:'感谢您的提案！',receipt:'我们已收到游戏资料并提交团队审核。如果项目适合进一步讨论，我们会通过您提供的邮箱与您联系。',ticketLabel:'申请编号',footer:'游戏本地化合作提案 · 教学 MVP',companyPlaceholder:'例如：青竹工作室',gamePlaceholder:'游戏原名',materialPlaceholder:'https://drive.example/game-materials',countError:'提案总内容超过 2,000 字符限制。请缩短说明或删除非必填项。',sendError:'提案提交失败',retry:'请重试。'}
};
const themeCopy={ru:{light:'Включить светлую тему',dark:'Включить тёмную тему'},en:{light:'Enable light theme',dark:'Enable dark theme'},zh:{light:'切换到浅色主题',dark:'切换到深色主题'}};
const ids={coop:'nav-coop',dev:'nav-dev',language:'language-label',eyebrow:'eyebrow',hero:'hero-title',description:'hero-description',formTitle:'form-title',formIntro:'form-intro',studio:'label-studio',game:'label-game',email:'label-email',gameurl:'label-gameurl',materials:'label-materials',materialsHelp:'materials-help',message:'label-message',chars:'char-label',send:'send',privacy:'privacy-note',result:'result-title',receipt:'receipt',footer:'footer-text'};
const filesCopy={ru:{label:'Или прикрепите файлы',help:'До 5 файлов, до 20 МБ каждый; PDF, презентации, документы, изображения, видео и архивы.',selected:'Выбрано файлов',uploaded:'Файлы прикреплены к заявке.',failed:'Заявка сохранена, но файл не удалось загрузить',invalid:'Проверьте: не более 5 файлов, 20 МБ каждый, до 40 МБ суммарно; разрешены PDF, PPT/PPTX, DOC/DOCX, ZIP/7Z, PNG/JPG/WEBP, MP4/MOV и TXT.'},en:{label:'Or attach files',help:'Up to 5 files, 20 MB each; PDF, presentations, documents, images, videos and archives.',selected:'Files selected',uploaded:'Files are attached to the proposal.',failed:'The proposal was saved, but a file could not be uploaded',invalid:'Choose up to 5 allowed files, 20 MB each and 40 MB total. Supported: PDF, PPT/PPTX, DOC/DOCX, ZIP/7Z, PNG/JPG/WEBP, MP4/MOV and TXT.'},zh:{label:'或直接上传文件',help:'最多 5 个文件，每个不超过 20 MB；支持 PDF、演示文稿、文档、图片、视频和压缩包。',selected:'已选择文件',uploaded:'文件已附加到提案。',failed:'提案已保存，但文件上传失败',invalid:'请检查文件：最多 5 个，每个不超过 20 MB，总计不超过 40 MB。支持 PDF、PPT/PPTX、DOC/DOCX、ZIP/7Z、PNG/JPG/WEBP、MP4/MOV 和 TXT。'}};
function updateThemeButton(){const labels=themeCopy[language]||themeCopy.ru,light=currentTheme==='light',label=light?labels.dark:labels.light,button=document.querySelector('#theme-toggle');button.textContent=light?'☾':'☀';button.setAttribute('aria-label',label);button.title=label;button.setAttribute('aria-pressed',String(light));}
function setTheme(theme){currentTheme=theme==='light'?'light':'dark';document.documentElement.dataset.theme=currentTheme;localStorage.setItem('intake-theme',currentTheme);const meta=document.querySelector('meta[name="theme-color"]');if(meta)meta.content=currentTheme==='light'?'#f4f7f8':'#10151b';updateThemeButton();}
function setLanguage(language){const t=copy[language]||copy.ru,f=filesCopy[language]||filesCopy.ru;document.documentElement.lang=language;document.title=t.title;for(const [key,id] of Object.entries(ids)){const element=document.getElementById(id);if(element)element.textContent=t[key]}document.querySelector('#label-files').textContent=f.label;document.querySelector('#files-help').textContent=f.help;document.querySelector('#studio').placeholder=t.companyPlaceholder;document.querySelector('#game').placeholder=t.gamePlaceholder;document.querySelector('#materials').placeholder=t.materialPlaceholder;document.querySelector('#message').placeholder=t.messagePlaceholder;document.querySelector('#language').value=language;updateThemeButton();}
function displayMissing(missing,t){const labels={studio_name:t.studio,game_title:t.game,contact_email:t.email,game_page_url:t.gameurl,materials_url:t.materials};return missing.map(key=>labels[key]||key).join(', ')}
const form=document.querySelector('#proposal'),message=document.querySelector('#message'),count=document.querySelector('#count'),send=document.querySelector('#send'),errorBox=document.querySelector('#error'),result=document.querySelector('#result');
let language=localStorage.getItem('intake-language')||(navigator.language.toLowerCase().startsWith('zh')?'zh':navigator.language.toLowerCase().startsWith('ru')?'ru':'en');let currentTheme=localStorage.getItem('intake-theme')||'dark';let clientId=localStorage.getItem('intake-client-id');if(!clientId){clientId='web-'+crypto.randomUUID();localStorage.setItem('intake-client-id',clientId)}setTheme(currentTheme);setLanguage(language);document.querySelector('#theme-toggle').addEventListener('click',()=>setTheme(currentTheme==='dark'?'light':'dark'));document.querySelector('#language').addEventListener('change',event=>{language=event.target.value;localStorage.setItem('intake-language',language);setLanguage(language)});message.addEventListener('input',()=>count.textContent=message.value.length);document.querySelector('#files').addEventListener('change',event=>{const f=filesCopy[language];document.querySelector('#files-selected').textContent=event.target.files.length?f.selected+': '+event.target.files.length:''});
form.addEventListener('submit',async event=>{event.preventDefault();const t=copy[language],f=filesCopy[language],fileInput=document.querySelector('#files');errorBox.style.display='none';result.style.display='none';document.querySelector('#attachment-warning').hidden=true;document.querySelector('#files-selected').textContent='';const fields=[[t.studio,document.querySelector('#studio').value],[t.game,document.querySelector('#game').value],[t.email,document.querySelector('#email').value],[t.gameurl,document.querySelector('#gameurl').value],[t.materials,document.querySelector('#materials').value]].filter(([,value])=>value.trim());const text=[...fields.map(([label,value])=>label+': '+value.trim()),t.message.replace(' *','')+': '+message.value.trim()].join('\\n');if(text.length>2000){errorBox.textContent=t.countError;errorBox.style.display='block';return}const chosen=Array.from(fileInput.files),allowed=new Set(['.pdf','.ppt','.pptx','.doc','.docx','.zip','.7z','.png','.jpg','.jpeg','.webp','.mp4','.mov','.txt']),total=chosen.reduce((sum,file)=>sum+file.size,0);if(chosen.length>5||total>40*1024*1024||chosen.some(file=>file.size>20*1024*1024||!allowed.has('.'+file.name.split('.').pop().toLowerCase()))){errorBox.textContent=f.invalid;errorBox.style.display='block';return}send.disabled=true;send.textContent=t.sending;try{const response=await fetch('/triage',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({text,channel:'form',client_id:clientId})});const data=await response.json();if(!response.ok)throw new Error(data.detail||t.sendError);let attachmentError='';if(fileInput.files.length){const body=new FormData();for(const file of fileInput.files)body.append('files',file);try{const uploaded=await fetch('/tickets/'+data.ticket_id+'/attachments',{method:'POST',headers:{'X-Attachment-Token':data.attachment_token},body});const uploadData=await uploaded.json();if(!uploaded.ok)throw new Error(uploadData.detail||f.failed)}catch(uploadError){attachmentError=f.failed+': '+uploadError.message}}document.querySelector('#ticket').textContent=t.ticketLabel+': #'+data.ticket_id;if(attachmentError){const warning=document.querySelector('#attachment-warning');warning.textContent=attachmentError;warning.hidden=false}result.style.display='block';result.scrollIntoView({behavior:'smooth',block:'center'})}catch(error){errorBox.textContent=t.sendError+': '+error.message+' '+t.retry;errorBox.style.display='block'}finally{send.disabled=false;send.textContent=t.send}});
</script></body></html>"""


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
    attachment_token: str


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
                attachment_token TEXT,
                form_data TEXT,
                form_submitted_at TEXT,
                staff_note TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS attachments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticket_id INTEGER NOT NULL,
                original_name TEXT NOT NULL,
                stored_path TEXT NOT NULL,
                content_type TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(ticket_id) REFERENCES tickets(id)
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
        if "attachment_token" not in columns:
            db.execute("ALTER TABLE tickets ADD COLUMN attachment_token TEXT")
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
        attachments = db.execute(
            "SELECT id, original_name, size_bytes FROM attachments WHERE ticket_id = ? ORDER BY id",
            (ticket_id,),
        ).fetchall()
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
    attachment_items = "".join(
        f"<li><a href='/admin/attachments/{item['id']}'>{html.escape(item['original_name'])}</a> "
        f"<span>({item['size_bytes'] / (1024 * 1024):.1f} МБ)</span></li>"
        for item in attachments
    ) or "<li>Файлы не приложены.</li>"
    return f"""<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Заявка #{ticket_id}</title><style>body{{font:16px system-ui;max-width:850px;margin:28px auto;padding:0 16px;background:#f4f6fa;color:#172033}}section,form{{background:white;padding:18px;margin:14px 0;border-radius:12px}}label{{display:grid;gap:6px;margin:12px 0}}input,textarea,button{{font:inherit;padding:10px;border:1px solid #cbd3e0;border-radius:8px}}input{{width:100%;box-sizing:border-box}}button{{background:#244fc3;color:white;cursor:pointer}}pre{{white-space:pre-wrap;overflow-wrap:anywhere}}a{{color:#244fc3}}</style>
<p><a href="/admin">← Все заявки</a></p><h1>Заявка #{ticket_id}</h1>
<section><b>Статус:</b> {html.escape(ticket['status'])} · <b>Тип:</b> {html.escape(ticket['business_type'])} · <b>Категория:</b> {html.escape(ticket['category'])}<br><b>Не хватает:</b> {missing}</section>
<section><h2>Оригинал</h2><pre>{source}</pre><h2>Перевод на русский</h2><pre>{translated}</pre></section>
<section><h2>Комментарий разработчика</h2><p>{developer_note}</p></section>
<section><h2>Прикреплённые файлы</h2><ul>{attachment_items}</ul></section>
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
        has_attachments = db.execute("SELECT 1 FROM attachments WHERE ticket_id = ? LIMIT 1", (ticket_id,)).fetchone()
        required_fields = ("studio_name", "game_title", "contact_email", "game_page_url")
        missing = [key for key in required_fields if not str(merged.get(key) or "").strip()]
        if not has_attachments and not str(merged.get("materials_url") or "").strip():
            missing.append("materials_url")
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
    attachment_token = secrets.token_urlsafe(32)
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
                 draft_reply, error, missing_fields, prefill_data, form_token, attachment_token)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (timestamp, payload.client_id, payload.channel, payload.text, translation_ru, category,
                 business_type, "needs_information" if missing else "awaiting_staff_review",
                 confidence, int(escalate), draft, error_code, json.dumps(missing),
                 json.dumps(extracted, ensure_ascii=False), token, attachment_token),
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
        attachment_token=attachment_token,
    )


@app.post("/tickets/{ticket_id}/attachments")
async def upload_ticket_attachments(
    ticket_id: int,
    files: list[UploadFile] = File(...),
    attachment_token: str = Header(alias="X-Attachment-Token"),
) -> dict[str, object]:
    if not files or len(files) > MAX_ATTACHMENTS_PER_TICKET:
        raise HTTPException(status_code=400, detail=f"Upload between 1 and {MAX_ATTACHMENTS_PER_TICKET} files")

    with db_session() as db:
        ticket = db.execute(
            "SELECT attachment_token FROM tickets WHERE id = ?", (ticket_id,)
        ).fetchone()
        if ticket is None:
            raise HTTPException(status_code=404, detail="Ticket not found")
        expected_token = ticket["attachment_token"] or ""
        if not expected_token or not secrets.compare_digest(attachment_token, expected_token):
            raise HTTPException(status_code=403, detail="Invalid attachment token")
        existing_count = db.execute(
            "SELECT COUNT(*) FROM attachments WHERE ticket_id = ?", (ticket_id,)
        ).fetchone()[0]
        if existing_count + len(files) > MAX_ATTACHMENTS_PER_TICKET:
            raise HTTPException(status_code=400, detail=f"A ticket can contain at most {MAX_ATTACHMENTS_PER_TICKET} files")

        saved_paths: list[Path] = []
        uploaded: list[dict[str, object]] = []
        total_bytes = 0
        try:
            ticket_dir = UPLOAD_DIR / str(ticket_id)
            ticket_dir.mkdir(parents=True, exist_ok=True)
            for upload in files:
                original_name = (upload.filename or "").replace("\\", "/").split("/")[-1].strip()
                extension = Path(original_name).suffix.lower()
                if not original_name or extension not in ALLOWED_UPLOAD_EXTENSIONS:
                    raise HTTPException(status_code=415, detail="Unsupported file type")

                safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", original_name).strip("._") or "attachment"
                stored_name = f"{secrets.token_hex(12)}_{safe_name}"
                target = ticket_dir / stored_name
                saved_paths.append(target)
                size_bytes = 0
                with target.open("wb") as destination:
                    while chunk := await upload.read(1024 * 1024):
                        size_bytes += len(chunk)
                        total_bytes += len(chunk)
                        if size_bytes > MAX_UPLOAD_BYTES:
                            raise HTTPException(status_code=413, detail="Each file must be 20 MB or smaller")
                        if total_bytes > MAX_TOTAL_UPLOAD_BYTES:
                            raise HTTPException(status_code=413, detail="The total upload must be 40 MB or smaller")
                        destination.write(chunk)
                if size_bytes == 0:
                    raise HTTPException(status_code=400, detail="Empty files are not allowed")
                content_type = mimetypes.guess_type(safe_name)[0] or "application/octet-stream"
                db.execute(
                    "INSERT INTO attachments (ticket_id, original_name, stored_path, content_type, size_bytes, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (ticket_id, original_name[:255], str(target), content_type, size_bytes, utc_now()),
                )
                uploaded.append({"name": original_name[:255], "size_bytes": size_bytes})
            missing = json.loads(
                db.execute("SELECT missing_fields FROM tickets WHERE id = ?", (ticket_id,)).fetchone()[0] or "[]"
            )
            if uploaded and "materials_url" in missing:
                missing.remove("materials_url")
                status = "needs_information" if missing else "awaiting_staff_review"
                db.execute(
                    "UPDATE tickets SET missing_fields = ?, status = ? WHERE id = ?",
                    (json.dumps(missing), status, ticket_id),
                )
            db.execute(
                "INSERT INTO events (ticket_id, created_at, event_type, details) VALUES (?, ?, ?, ?)",
                (ticket_id, utc_now(), "attachments_uploaded", json.dumps({"count": len(uploaded)})),
            )
        except Exception:
            for saved_path in saved_paths:
                saved_path.unlink(missing_ok=True)
            raise
        updated = db.execute("SELECT missing_fields, status FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
        return {
            "ticket_id": ticket_id,
            "uploaded": uploaded,
            "missing_fields": json.loads(updated["missing_fields"] or "[]"),
            "status": updated["status"],
        }


@app.get("/admin/attachments/{attachment_id}")
def download_attachment(attachment_id: int, username: str = Depends(require_admin)) -> FileResponse:
    with db_session() as db:
        attachment = db.execute(
            "SELECT original_name, stored_path FROM attachments WHERE id = ?", (attachment_id,)
        ).fetchone()
    if attachment is None or not Path(attachment["stored_path"]).is_file():
        raise HTTPException(status_code=404, detail="Attachment not found")
    return FileResponse(
        attachment["stored_path"],
        filename=attachment["original_name"],
        media_type="application/octet-stream",
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
        has_attachments = db.execute(
            "SELECT 1 FROM attachments WHERE ticket_id = ? LIMIT 1", (ticket["id"],)
        ).fetchone()
        required_fields = ("studio_name", "game_title", "contact_email", "game_page_url")
        missing = [name for name in required_fields if not str(merged.get(name) or "").strip()]
        if not has_attachments and not str(merged.get("materials_url") or "").strip():
            missing.append("materials_url")
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
