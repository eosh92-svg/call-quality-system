import os
import uuid
import json
import requests
from pathlib import Path
from datetime import datetime, timedelta
from fastapi import FastAPI, File, UploadFile, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.security import OAuth2PasswordRequestForm
from fastapi.staticfiles import StaticFiles
from sqlalchemy import create_engine, Column, Integer, String, Boolean, DateTime, JSON
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, Session
from dotenv import load_dotenv
from jose import JWTError, jwt
from passlib.context import CryptContext
import boto3
from botocore.config import Config

load_dotenv()

# ========== БАЗА ДАННЫХ ==========
DATABASE_URL = "sqlite:///./db.sqlite"
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    username = Column(String, unique=True, index=True)
    hashed_password = Column(String)
    role = Column(String, default="user")
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)

Base.metadata.create_all(bind=engine)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# ========== АУТЕНТИФИКАЦИЯ ==========
SECRET_KEY = os.getenv("SECRET_KEY", "your-secret-key-change-this")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 30

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

def verify_password(plain_password, hashed_password):
    return pwd_context.verify(plain_password, hashed_password)

def get_password_hash(password):
    return pwd_context.hash(password)

def authenticate_user(db: Session, username: str, password: str):
    user = db.query(User).filter(User.username == username).first()
    if not user or not verify_password(password, user.hashed_password) or not user.is_active:
        return None
    return user

def create_access_token(data: dict, expires_delta: timedelta = None):
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt

async def get_current_user(request: Request, db: Session = Depends(get_db)):
    token = request.cookies.get("access_token")
    if not token:
        auth_header = request.headers.get("Authorization")
        if auth_header and auth_header.startswith("Bearer "):
            token = auth_header.split(" ")[1]
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username: str = payload.get("sub")
        if username is None:
            raise HTTPException(status_code=401, detail="Invalid token")
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid token")
    user = db.query(User).filter(User.username == username).first()
    if user is None:
        raise HTTPException(status_code=401, detail="User not found")
    return user

def require_role(role: str):
    async def role_checker(current_user: User = Depends(get_current_user)):
        if current_user.role != role:
            raise HTTPException(status_code=403, detail="Insufficient permissions")
        return current_user
    return role_checker

# ========== S3 СТОРОДЖ (ТОЛЬКО ЧТЕНИЕ/ЗАПИСЬ) ==========
class S3Storage:
    def __init__(self):
        self.s3 = boto3.client(
            's3',
            endpoint_url=os.getenv('AWS_ENDPOINT_URL'),
            aws_access_key_id=os.getenv('AWS_ACCESS_KEY_ID'),
            aws_secret_access_key=os.getenv('AWS_SECRET_ACCESS_KEY'),
            config=Config(signature_version='s3v4')
        )
        self.raw_bucket = os.getenv('RAW_BUCKET')
        self.reports_bucket = os.getenv('REPORTS_BUCKET')
        self.transcripts_bucket = os.getenv('TRANSCRIPTS_BUCKET')

    def upload_file(self, file_content, key):
        self.s3.put_object(Bucket=self.raw_bucket, Key=key, Body=file_content)

    def list_audio_files(self, prefix='', start_date=None, end_date=None):
        response = self.s3.list_objects_v2(Bucket=self.raw_bucket, Prefix=prefix)
        files = []
        if 'Contents' in response:
            for obj in response['Contents']:
                key = obj['Key']
                if key.lower().endswith(('.mp3', '.wav', '.m4a', '.ogg', '.flac')):
                    last_modified = obj['LastModified'].replace(tzinfo=None)
                    if start_date and last_modified < start_date:
                        continue
                    if end_date and last_modified > end_date:
                        continue
                    files.append({
                        'key': key,
                        'last_modified': last_modified,
                        'size': obj['Size']
                    })
        return files

    def get_report(self, audio_key):
        base_name = os.path.splitext(os.path.basename(audio_key))[0]
        report_key = f"reports/{base_name}.json"
        try:
            obj = self.s3.get_object(Bucket=self.reports_bucket, Key=report_key)
            return json.loads(obj['Body'].read().decode('utf-8'))
        except self.s3.exceptions.NoSuchKey:
            return None

    def get_transcript(self, audio_key):
        base_name = os.path.splitext(os.path.basename(audio_key))[0]
        transcript_key = f"transcripts/{base_name}.json"
        try:
            obj = self.s3.get_object(Bucket=self.transcripts_bucket, Key=transcript_key)
            return json.loads(obj['Body'].read().decode('utf-8'))
        except self.s3.exceptions.NoSuchKey:
            return None

# ========== GITHUB ACTIONS КЛИЕНТ ==========
class GitHubActionsClient:
    def __init__(self):
        self.repo = os.getenv("GITHUB_REPO")
        self.token = os.getenv("GITHUB_TOKEN")

    def trigger_workflow(self, audio_key: str) -> bool:
        url = f"https://api.github.com/repos/{self.repo}/actions/workflows/quality-assessment.yml/dispatches"
        headers = {
            "Authorization": f"token {self.token}",
            "Accept": "application/vnd.github.v3+json"
        }
        data = {
            "ref": "main",
            "inputs": {"audio_key": audio_key}
        }
        response = requests.post(url, headers=headers, json=data)
        return response.status_code == 204

# ========== ИНИЦИАЛИЗАЦИЯ ==========
app = FastAPI(title="QCall - AI Quality Assessment System")
app.mount("/static", StaticFiles(directory="static"), name="static")

s3_storage = S3Storage()
github_client = GitHubActionsClient()

# Промпт не используется в веб-приложении, но оставляем для возможного расширения
prompt_path = Path(__file__).parent / "config" / "system_prompt.txt"
if prompt_path.exists():
    with open(prompt_path, 'r', encoding='utf-8') as f:
        SYSTEM_PROMPT = f.read()
else:
    SYSTEM_PROMPT = ""

# ========== HTML-СТРАНИЦЫ ==========
# (весь HTML код из второго приложения с небольшими изменениями)
# Изменения: в функции renderMainInterface кнопка загрузки теперь вызывает
# последовательно upload_to_s3 и затем process_audio.
# Для краткости вставлю готовый HTML с исправленным JavaScript.
HTML_PAGE = """
<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>QCall | AI Quality Assessment</title>
    <link href="https://fonts.googleapis.com/css2?family=Inter:opsz,wght@14..32,300;14..32,400;14..32,500;14..32,600;14..32,700&display=swap" rel="stylesheet">
    <style>
        /* ... весь CSS из второго кода (оставляем без изменений) ... */
        * { margin:0; padding:0; box-sizing:border-box; }
        body {
            font-family: 'Inter', sans-serif;
            background: linear-gradient(135deg, #0a192f 0%, #0f2a3f 100%);
            min-height: 100vh;
            padding: 32px;
            color: #fff;
        }
        .app-container {
            max-width: 1400px;
            margin: 0 auto;
            background: rgba(10,25,47,0.85);
            backdrop-filter: blur(12px);
            border-radius: 32px;
            box-shadow: 0 25px 50px -12px rgba(0,0,0,0.5), 0 0 0 1px rgba(74,222,128,0.2);
            overflow: hidden;
            border: 1px solid rgba(74,222,128,0.3);
        }
        .header {
            padding: 24px 32px;
            display: flex;
            align-items: center;
            justify-content: space-between;
            flex-wrap: wrap;
            gap: 16px;
            border-bottom: 1px solid rgba(74,222,128,0.2);
        }
        .logo-area {
            display: flex;
            align-items: center;
            gap: 20px;
        }
        .logo-icon {
            width: 64px;
            height: 64px;
            border-radius: 20px;
            object-fit: contain;
            filter: drop-shadow(0 0 8px rgba(74,222,128,0.3));
        }
        .logo-badge {
            background: rgba(74,222,128,0.15);
            padding: 6px 14px;
            border-radius: 40px;
            font-size: 0.9rem;
            font-weight: 600;
            color: #4ade80;
            border: 1px solid rgba(74,222,128,0.3);
        }
        .btn {
            background: rgba(255,255,255,0.05);
            border: 1px solid rgba(74,222,128,0.3);
            padding: 8px 20px;
            border-radius: 40px;
            font-size: 0.85rem;
            font-weight: 500;
            color: #e2e8f0;
            cursor: pointer;
            transition: all 0.2s ease;
            text-decoration: none;
            display: inline-block;
        }
        .btn:hover {
            background: rgba(74,222,128,0.1);
            border-color: #4ade80;
            transform: translateY(-1px);
        }
        .content {
            padding: 32px;
        }
        .login-wrapper {
            max-width: 420px;
            margin: 40px auto;
        }
        .login-card {
            background: rgba(15,42,63,0.8);
            border-radius: 28px;
            padding: 40px 36px;
            backdrop-filter: blur(4px);
            border: 1px solid rgba(74,222,128,0.3);
        }
        .login-title {
            font-size: 1.8rem;
            font-weight: 600;
            margin-bottom: 8px;
        }
        .login-subtitle {
            font-size: 0.9rem;
            color: #9ca3af;
            margin-bottom: 32px;
        }
        .input-group {
            margin-bottom: 20px;
        }
        .input-group label {
            display: block;
            font-size: 0.8rem;
            font-weight: 500;
            color: #e2e8f0;
            margin-bottom: 6px;
        }
        .input-group input {
            width: 100%;
            padding: 12px 16px;
            background: rgba(0,0,0,0.4);
            border: 1px solid #2d3e5a;
            border-radius: 16px;
            font-size: 0.95rem;
            font-family: inherit;
            color: white;
            transition: all 0.2s;
        }
        .input-group input:focus {
            outline: none;
            border-color: #4ade80;
            box-shadow: 0 0 0 3px rgba(74,222,128,0.2);
            background: rgba(0,0,0,0.6);
        }
        .login-btn {
            width: 100%;
            background: linear-gradient(135deg, #4ade80 0%, #22c55e 100%);
            border: none;
            padding: 12px;
            border-radius: 40px;
            font-weight: 600;
            font-size: 0.95rem;
            color: #0a192f;
            cursor: pointer;
            transition: all 0.2s;
            margin-top: 8px;
        }
        .login-btn:hover {
            transform: translateY(-1px);
            box-shadow: 0 8px 20px rgba(74,222,128,0.3);
        }
        .login-error {
            color: #f87171;
            font-size: 0.85rem;
            margin-top: 16px;
            text-align: center;
        }
        .services-bento {
            display: grid;
            grid-template-columns: repeat(3,1fr);
            gap: 20px;
            margin-bottom: 40px;
        }
        .service-card {
            background: rgba(15,42,63,0.7);
            border-radius: 24px;
            padding: 20px;
            display: flex;
            align-items: center;
            gap: 16px;
            transition: all 0.25s ease;
            border: 1px solid rgba(74,222,128,0.2);
            backdrop-filter: blur(4px);
        }
        .service-card:hover {
            transform: translateY(-2px);
            border-color: #4ade80;
            background: rgba(15,42,63,0.85);
        }
        .service-icon {
            width: 52px;
            height: 52px;
            background: rgba(0,0,0,0.4);
            border-radius: 20px;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 1.6rem;
        }
        .service-info h4 {
            font-size: 1rem;
            font-weight: 600;
            margin-bottom: 4px;
            display: flex;
            align-items: center;
            gap: 8px;
        }
        .service-info p {
            font-size: 0.75rem;
            color: #9ca3af;
        }
        .service-status {
            display: inline-block;
            width: 10px;
            height: 10px;
            border-radius: 50%;
            margin-left: 8px;
        }
        .status-green { background-color: #4ade80; box-shadow:0 0 5px #4ade80; }
        .status-red { background-color: #ef4444; box-shadow:0 0 5px #ef4444; }
        .upload-bento {
            background: rgba(15,42,63,0.6);
            border: 2px dashed rgba(74,222,128,0.4);
            border-radius: 28px;
            padding: 48px 32px;
            text-align: center;
            transition: all 0.25s ease;
            cursor: pointer;
            margin-bottom: 32px;
        }
        .upload-bento:hover, .upload-bento.dragover {
            border-color: #4ade80;
            background: rgba(15,42,63,0.8);
        }
        .upload-icon {
            width: 72px;
            height: 72px;
            background: rgba(74,222,128,0.1);
            border-radius: 40px;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 2rem;
            margin: 0 auto 20px;
        }
        .upload-bento h3 {
            font-size: 1.2rem;
            font-weight: 600;
            margin-bottom: 8px;
        }
        .upload-bento p {
            color: #9ca3af;
            font-size: 0.9rem;
        }
        .file-name {
            margin-top: 16px;
            font-size: 0.85rem;
            color: #4ade80;
            font-weight: 500;
        }
        input[type="file"] {
            display: none;
        }
        .analyze-btn {
            background: linear-gradient(135deg, #4ade80 0%, #22c55e 100%);
            border: none;
            padding: 12px 32px;
            border-radius: 40px;
            font-weight: 600;
            font-size: 0.9rem;
            color: #0a192f;
            cursor: pointer;
            transition: all 0.2s;
            margin-top: 24px;
        }
        .analyze-btn:hover {
            transform: translateY(-2px);
            box-shadow: 0 8px 20px rgba(74,222,128,0.3);
        }
        .period-picker {
            display: flex;
            gap: 20px;
            align-items: flex-end;
            flex-wrap: wrap;
            margin-bottom: 32px;
            background: rgba(15,42,63,0.5);
            padding: 20px;
            border-radius: 28px;
        }
        .period-picker .input-group {
            margin-bottom: 0;
            flex: 1;
        }
        .reports-table {
            width: 100%;
            border-collapse: collapse;
            margin-top: 20px;
        }
        .reports-table th, .reports-table td {
            padding: 12px;
            text-align: left;
            border-bottom: 1px solid rgba(74,222,128,0.2);
        }
        .reports-table th {
            color: #4ade80;
            font-weight: 600;
        }
        .tag {
            background: rgba(74,222,128,0.1);
            padding: 4px 12px;
            border-radius: 30px;
            font-size: 0.75rem;
            color: #4ade80;
            border: 1px solid rgba(74,222,128,0.3);
            display: inline-block;
        }
        .loader {
            display: none;
            text-align: center;
            padding: 40px;
        }
        .spinner {
            width: 48px;
            height: 48px;
            border: 3px solid #2d3e5a;
            border-top-color: #4ade80;
            border-radius: 50%;
            animation: spin 0.8s linear infinite;
            margin: 0 auto 16px;
        }
        @keyframes spin { to { transform: rotate(360deg); } }
        .results-bento {
            margin-top: 32px;
            display: none;
            animation: fadeIn 0.4s ease;
        }
        @keyframes fadeIn {
            from { opacity: 0; transform: translateY(12px); }
            to { opacity: 1; transform: translateY(0); }
        }
        .metrics-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 16px;
            margin-bottom: 24px;
        }
        .metric-card {
            background: rgba(15,42,63,0.7);
            border-radius: 24px;
            padding: 20px;
            text-align: center;
            border: 1px solid rgba(74,222,128,0.2);
        }
        .metric-label {
            font-size: 0.75rem;
            text-transform: uppercase;
            letter-spacing: 0.5px;
            color: #9ca3af;
            margin-bottom: 8px;
        }
        .metric-value {
            font-size: 1.8rem;
            font-weight: 700;
        }
        .sentiment-positive { color: #4ade80; }
        .sentiment-neutral { color: #facc15; }
        .sentiment-negative { color: #f87171; }
        .topics-section, .recommendations-list {
            margin-bottom: 24px;
        }
        .section-title {
            font-size: 0.85rem;
            font-weight: 600;
            color: #9ca3af;
            margin-bottom: 12px;
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }
        .tags {
            display: flex;
            flex-wrap: wrap;
            gap: 10px;
        }
        .recommendation-item {
            background: rgba(15,42,63,0.6);
            border-left: 3px solid #4ade80;
            padding: 12px 16px;
            border-radius: 16px;
            font-size: 0.85rem;
            margin-bottom: 8px;
        }
        .transcript-box {
            background: rgba(0,0,0,0.4);
            border-radius: 20px;
            padding: 20px;
            font-size: 0.8rem;
            line-height: 1.5;
            white-space: pre-wrap;
            max-height: 200px;
            overflow-y: auto;
            color: #cbd5e6;
            border: 1px solid rgba(74,222,128,0.2);
        }
        .error-message {
            background: rgba(248,113,113,0.15);
            border-left: 3px solid #f87171;
            padding: 16px;
            border-radius: 16px;
            margin-top: 20px;
            display: none;
            color: #fecaca;
        }
        footer {
            padding: 20px 32px;
            text-align: center;
            font-size: 0.7rem;
            color: #6b7280;
            border-top: 1px solid rgba(74,222,128,0.2);
        }
        @media (max-width:768px){
            body{padding:16px;}
            .services-bento{grid-template-columns:1fr;}
            .metrics-grid{grid-template-columns:1fr;}
            .header{flex-direction:column;align-items:flex-start;}
            .period-picker{flex-direction:column;align-items:stretch;}
            .content{padding:20px;}
        }
    </style>
</head>
<body>
<div class="app-container" id="app">
    <div class="header">
        <div class="logo-area">
            <img src="/static/logo.png" alt="QCall Logo" class="logo-icon">
            <div class="logo-badge">AI-Powered</div>
        </div>
        <div id="headerButtons"></div>
    </div>
    <div class="content" id="content"></div>
    <footer>QCall Дипломный проект 2026</footer>
</div>
<script>
    let role = null;
    let serviceStatus = {};

    async function fetchStatus() {
        try {
            const response = await fetch('/status');
            if (response.ok) {
                const data = await response.json();
                serviceStatus = data;
                return true;
            }
            return false;
        } catch(e) { return false; }
    }

    async function renderHeader() {
        const headerDiv = document.getElementById('headerButtons');
        const status = await fetchStatus();
        if (status) {
            const roleResp = await fetch('/whoami');
            if (roleResp.ok) {
                const data = await roleResp.json();
                role = data.role;
            } else {
                role = null;
            }
        } else {
            role = null;
        }

        headerDiv.innerHTML = '';
        if (role === 'admin') {
            const adminLink = document.createElement('a');
            adminLink.href = '/admin';
            adminLink.className = 'btn';
            adminLink.textContent = 'Управление';
            adminLink.style.marginRight = '10px';
            headerDiv.appendChild(adminLink);
            const logoutBtn = document.createElement('button');
            logoutBtn.className = 'btn';
            logoutBtn.textContent = 'Выйти';
            logoutBtn.onclick = async () => {
                await fetch('/logout', { method: 'POST' });
                role = null;
                renderLoginForm();
            };
            headerDiv.appendChild(logoutBtn);
        } else if (role === 'user') {
            const logoutBtn = document.createElement('button');
            logoutBtn.className = 'btn';
            logoutBtn.textContent = 'Выйти';
            logoutBtn.onclick = async () => {
                await fetch('/logout', { method: 'POST' });
                role = null;
                renderLoginForm();
            };
            headerDiv.appendChild(logoutBtn);
        } else {
            const guestBtn = document.createElement('button');
            guestBtn.className = 'btn';
            guestBtn.textContent = 'Войти как гость';
            guestBtn.onclick = () => startGuestMode();
            headerDiv.appendChild(guestBtn);
            const loginBtn = document.createElement('button');
            loginBtn.className = 'btn';
            loginBtn.textContent = 'Войти';
            loginBtn.onclick = () => renderLoginForm();
            headerDiv.appendChild(loginBtn);
        }
    }

    async function renderLoginForm() {
        await renderHeader();
        document.getElementById('content').innerHTML = `
            <div class="login-wrapper">
                <div class="login-card">
                    <div class="login-title">Добро пожаловать</div>
                    <div class="login-subtitle">Войдите в систему анализа качества звонков</div>
                    <div class="input-group">
                        <label>Логин</label>
                        <input type="text" id="loginUsername" placeholder="admin" autocomplete="off">
                    </div>
                    <div class="input-group">
                        <label>Пароль</label>
                        <input type="password" id="loginPassword" placeholder="••••••••">
                    </div>
                    <button class="login-btn" id="loginBtn">Войти</button>
                    <div id="loginError" class="login-error"></div>
                </div>
            </div>
        `;
        document.getElementById('loginBtn').addEventListener('click', async () => {
            const username = document.getElementById('loginUsername').value.trim();
            const password = document.getElementById('loginPassword').value;
            if (!username || !password) {
                document.getElementById('loginError').innerText = 'Заполните оба поля';
                return;
            }
            const formData = new URLSearchParams();
            formData.append('username', username);
            formData.append('password', password);
            try {
                const response = await fetch('/token', { method: 'POST', body: formData });
                if (response.ok) {
                    await renderHeader();
                    await fetchStatus();
                    renderMainInterface();
                } else {
                    const data = await response.json();
                    document.getElementById('loginError').innerText = data.detail || 'Неверный логин или пароль';
                }
            } catch(e) {
                document.getElementById('loginError').innerText = 'Ошибка соединения';
            }
        });
    }

    let selectedFile = null;
    let isGuestMode = false;

    async function renderMainInterface() {
        await renderHeader();
        const yandexStatus = serviceStatus.yandex_speechkit ? 'status-green' : 'status-red';
        const gigachatStatus = serviceStatus.gigachat ? 'status-green' : 'status-red';
        const telegramStatus = serviceStatus.telegram ? 'status-green' : 'status-red';

        const contentDiv = document.getElementById('content');
        contentDiv.innerHTML = `
            <div class="services-bento">
                <div class="service-card">
                    <div class="service-icon">🎙️</div>
                    <div class="service-info">
                        <h4>Yandex SpeechKit <span class="service-status ${yandexStatus}"></span></h4>
                        <p>Автоматическое распознавание речи</p>
                    </div>
                </div>
                <div class="service-card">
                    <div class="service-icon">🧠</div>
                    <div class="service-info">
                        <h4>GigaChat LLM <span class="service-status ${gigachatStatus}"></span></h4>
                        <p>Анализ диалогов и оценка качества</p>
                    </div>
                </div>
                <div class="service-card">
                    <div class="service-icon">📱</div>
                    <div class="service-info">
                        <h4>Telegram Bot <span class="service-status ${telegramStatus}"></span></h4>
                        <p>Мгновенные уведомления</p>
                    </div>
                </div>
            </div>

            <div class="upload-bento" id="uploadArea">
                <div class="upload-icon">📁</div>
                <h3>Загрузите аудиозапись разговора</h3>
                <p>Поддерживаются форматы: MP3, WAV, M4A</p>
                <div class="file-name" id="fileName"></div>
                <input type="file" id="fileInput" accept="audio/*">
                <button class="analyze-btn" id="uploadBtn">Анализировать</button>
            </div>

            <div class="period-picker">
                <div class="input-group">
                    <label>Начало периода</label>
                    <input type="date" id="startDate">
                </div>
                <div class="input-group">
                    <label>Конец периода</label>
                    <input type="date" id="endDate">
                </div>
                <button class="analyze-btn" id="fetchReportsBtn">Показать отчёты</button>
            </div>

            <div id="reportsTable" style="display:none;">
                <div class="results-header">Список звонков</div>
                <table class="reports-table">
                    <thead><tr><th>Файл</th><th>Дата</th><th>Статус</th><th>Действия</th></tr></thead>
                    <tbody id="reportsTableBody"></tbody>
                </table>
            </div>

            <div class="loader" id="loader">
                <div class="spinner"></div>
                <p>Обработка нейросетями... Это может занять 1-2 минуты</p>
            </div>
            <div class="results-bento" id="resultCard">
                <div class="results-header">📊 Результаты анализа</div>
                <div class="metrics-grid" id="metricsGrid"></div>
                <div class="topics-section">
                    <div class="section-title">Ключевые темы</div>
                    <div id="topicsTags" class="tags"></div>
                </div>
                <div class="topics-section">
                    <div class="section-title">Рекомендации</div>
                    <div id="recommendationsList" class="recommendations-list"></div>
                </div>
                <div class="transcript-box" id="transcript"></div>
            </div>
            <div class="error-message" id="errorMessage"></div>
        `;

        const uploadArea = document.getElementById('uploadArea');
        const fileInput = document.getElementById('fileInput');
        const fileNameSpan = document.getElementById('fileName');
        const uploadBtn = document.getElementById('uploadBtn');
        const loader = document.getElementById('loader');
        const resultCard = document.getElementById('resultCard');
        const metricsGrid = document.getElementById('metricsGrid');
        const topicsTags = document.getElementById('topicsTags');
        const recommendationsList = document.getElementById('recommendationsList');
        const transcriptDiv = document.getElementById('transcript');
        const errorMessageDiv = document.getElementById('errorMessage');

        function updateFileName() { fileNameSpan.textContent = selectedFile ? selectedFile.name : ''; }

        uploadArea.addEventListener('dragover', (e) => { e.preventDefault(); uploadArea.classList.add('dragover'); });
        uploadArea.addEventListener('dragleave', () => uploadArea.classList.remove('dragover'));
        uploadArea.addEventListener('drop', (e) => {
            e.preventDefault();
            uploadArea.classList.remove('dragover');
            if (e.dataTransfer.files.length) {
                selectedFile = e.dataTransfer.files[0];
                updateFileName();
            }
        });
        uploadArea.addEventListener('click', () => fileInput.click());
        fileInput.addEventListener('change', () => {
            if (fileInput.files.length) {
                selectedFile = fileInput.files[0];
                updateFileName();
            }
        });

        uploadBtn.addEventListener('click', async () => {
            if (!selectedFile) {
                errorMessageDiv.textContent = 'Выберите аудиофайл';
                errorMessageDiv.style.display = 'block';
                return;
            }
            resultCard.style.display = 'none';
            errorMessageDiv.style.display = 'none';
            loader.style.display = 'block';
            uploadBtn.disabled = true;

            // 1. Загружаем файл в S3
            const formData = new FormData();
            formData.append('file', selectedFile);
            let uploadResponse;
            try {
                uploadResponse = await fetch('/api/upload_to_s3', { method: 'POST', body: formData });
                if (!uploadResponse.ok) {
                    const err = await uploadResponse.json();
                    throw new Error(err.detail || 'Ошибка загрузки файла');
                }
                const uploadData = await uploadResponse.json();
                const audioKey = uploadData.key;

                // 2. Запускаем GitHub Actions workflow
                const processResponse = await fetch(`/api/process/${encodeURIComponent(audioKey)}`, { method: 'POST' });
                if (!processResponse.ok) {
                    const err = await processResponse.json();
                    throw new Error(err.detail || 'Ошибка запуска обработки');
                }

                // Успех
                alert('Файл загружен, обработка запущена. Отчёт появится в Telegram и в списке отчётов через несколько минут.');
                // Обновим список отчётов, чтобы увидеть новый файл (пока без отчёта)
                document.getElementById('fetchReportsBtn').click();
            } catch (err) {
                errorMessageDiv.textContent = err.message;
                errorMessageDiv.style.display = 'block';
            } finally {
                loader.style.display = 'none';
                uploadBtn.disabled = false;
            }
        });

        function displayResults(data) {
            metricsGrid.innerHTML = '';
            topicsTags.innerHTML = '';
            recommendationsList.innerHTML = '';
            transcriptDiv.innerHTML = '';

            const metrics = [
                { label: 'Удовлетворённость', value: data.satisfaction_score + '/5' },
                { label: 'Следование скрипту', value: data.adherence_to_script + '/5' },
                { label: 'Вежливость', value: data.politeness + '/5' },
                { label: 'Проблема решена', value: data.problem_solved ? '✅ Да' : '❌ Нет' },
                { label: 'Тональность', value: data.sentiment, extraClass: `sentiment-${data.sentiment}` }
            ];
            metrics.forEach(m => {
                const div = document.createElement('div');
                div.className = 'metric-card';
                div.innerHTML = `<div class="metric-label">${m.label}</div><div class="metric-value ${m.extraClass || ''}">${m.value}</div>`;
                metricsGrid.appendChild(div);
            });

            if (data.key_topics && data.key_topics.length) {
                data.key_topics.forEach(topic => {
                    const tag = document.createElement('span');
                    tag.className = 'tag';
                    tag.textContent = topic;
                    topicsTags.appendChild(tag);
                });
            } else {
                const tag = document.createElement('span');
                tag.className = 'tag';
                tag.textContent = 'Не определены';
                topicsTags.appendChild(tag);
            }

            if (data.recommendations && data.recommendations.length) {
                data.recommendations.forEach(rec => {
                    const div = document.createElement('div');
                    div.className = 'recommendation-item';
                    div.textContent = '💡 ' + rec;
                    recommendationsList.appendChild(div);
                });
            } else {
                const div = document.createElement('div');
                div.className = 'recommendation-item';
                div.textContent = '👍 Отличная работа, рекомендаций нет';
                recommendationsList.appendChild(div);
            }

            transcriptDiv.textContent = data.transcript || 'Транскрипт отсутствует';
            resultCard.style.display = 'block';
        }

        const fetchReportsBtn = document.getElementById('fetchReportsBtn');
        const reportsTable = document.getElementById('reportsTable');
        const reportsTableBody = document.getElementById('reportsTableBody');

        fetchReportsBtn.addEventListener('click', async () => {
            const start = document.getElementById('startDate').value;
            const end = document.getElementById('endDate').value;
            const params = new URLSearchParams();
            if (start) params.append('start_date', start);
            if (end) params.append('end_date', end);
            try {
                const response = await fetch(`/api/reports/period?${params}`);
                if (response.status === 401) {
                    renderLoginForm();
                    return;
                }
                const data = await response.json();
                reportsTableBody.innerHTML = '';
                if (data.length === 0) {
                    reportsTableBody.innerHTML = '<tr><td colspan="4">Нет файлов за выбранный период</td></tr>';
                } else {
                    data.forEach(item => {
                        const row = reportsTableBody.insertRow();
                        row.insertCell(0).innerText = item.audio_key.split('/').pop();
                        row.insertCell(1).innerText = new Date(item.last_modified).toLocaleString();
                        const statusCell = row.insertCell(2);
                        if (item.has_report) {
                            statusCell.innerHTML = '<span class="tag">Готов</span>';
                        } else {
                            statusCell.innerHTML = '<span class="tag" style="background:#555;">Не обработан</span>';
                        }
                        const actionsCell = row.insertCell(3);
                        if (item.has_report) {
                            const viewBtn = document.createElement('button');
                            viewBtn.className = 'analyze-btn';
                            viewBtn.textContent = 'Просмотр';
                            viewBtn.style.padding = '6px 12px';
                            viewBtn.style.fontSize = '0.75rem';
                            viewBtn.onclick = () => viewReport(item.audio_key);
                            actionsCell.appendChild(viewBtn);
                        } else if (role === 'admin') {
                            const processBtn = document.createElement('button');
                            processBtn.className = 'analyze-btn';
                            processBtn.textContent = 'Обработать';
                            processBtn.style.padding = '6px 12px';
                            processBtn.style.fontSize = '0.75rem';
                            processBtn.onclick = () => processAudio(item.audio_key);
                            actionsCell.appendChild(processBtn);
                        }
                    });
                }
                reportsTable.style.display = 'block';
            } catch(e) {
                console.error(e);
            }
        });

        async function viewReport(audioKey) {
            const response = await fetch(`/api/report/${encodeURIComponent(audioKey)}`);
            const data = await response.json();
            const report = data.report;
            const transcript = data.transcript?.transcript || '';
            const analysis = report.analysis || report;
            displayResults({
                satisfaction_score: analysis.satisfaction_score || 0,
                sentiment: analysis.sentiment || 'neutral',
                problem_solved: analysis.problem_solved || false,
                adherence_to_script: analysis.adherence_to_script || 0,
                politeness: analysis.politeness || 0,
                key_topics: analysis.key_topics || [],
                recommendations: analysis.recommendations || [],
                transcript: transcript
            });
        }

        async function processAudio(audioKey) {
            const response = await fetch(`/api/process/${encodeURIComponent(audioKey)}`, { method: 'POST' });
            if (response.ok) {
                alert('Обработка запущена. Отчёт появится через несколько минут.');
                fetchReportsBtn.click();
            } else {
                alert('Ошибка запуска обработки');
            }
        }
    }

    async function startGuestMode() {
        isGuestMode = true;
        role = 'guest';
        await renderHeader();
        renderMainInterface();
    }

    async function init() {
        const status = await fetchStatus();
        if (status) {
            const who = await fetch('/whoami');
            if (who.ok) {
                const data = await who.json();
                role = data.role;
                renderMainInterface();
                return;
            }
        }
        renderLoginForm();
    }

    init();
</script>
</body>
</html>
"""

# Административная страница (оставляем без изменений из второго кода)
ADMIN_PAGE = """
<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>QCall | Управление пользователями</title>
    <link href="https://fonts.googleapis.com/css2?family=Inter:opsz,wght@14..32,300;14..32,400;14..32,500;14..32,600;14..32,700&display=swap" rel="stylesheet">
    <style>
        /* ... (весь стиль из второго кода) ... */
        * { margin:0; padding:0; box-sizing:border-box; }
        body {
            font-family: 'Inter', sans-serif;
            background: linear-gradient(135deg, #0a192f 0%, #0f2a3f 100%);
            min-height: 100vh;
            padding: 32px;
            color: #fff;
        }
        .container {
            max-width: 1200px;
            margin: 0 auto;
            background: rgba(10, 25, 47, 0.85);
            backdrop-filter: blur(12px);
            border-radius: 32px;
            padding: 32px;
            border: 1px solid rgba(74, 222, 128, 0.3);
            box-shadow: 0 25px 50px -12px rgba(0,0,0,0.5);
        }
        h1, h2 {
            font-weight: 600;
            margin-bottom: 24px;
        }
        h1 {
            font-size: 1.8rem;
            border-left: 4px solid #4ade80;
            padding-left: 16px;
        }
        h2 {
            font-size: 1.4rem;
            margin-top: 32px;
        }
        table {
            width: 100%;
            border-collapse: collapse;
            margin-bottom: 32px;
            background: rgba(15, 42, 63, 0.6);
            border-radius: 20px;
            overflow: hidden;
        }
        th, td {
            padding: 14px 16px;
            text-align: left;
            border-bottom: 1px solid rgba(74, 222, 128, 0.2);
        }
        th {
            background: rgba(74, 222, 128, 0.1);
            color: #4ade80;
            font-weight: 600;
            font-size: 0.9rem;
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }
        tr:last-child td {
            border-bottom: none;
        }
        tr:hover td {
            background: rgba(74, 222, 128, 0.05);
        }
        .role-badge {
            display: inline-block;
            padding: 4px 12px;
            border-radius: 20px;
            font-size: 0.75rem;
            font-weight: 600;
            text-transform: uppercase;
        }
        .role-admin {
            background: rgba(74, 222, 128, 0.2);
            color: #4ade80;
        }
        .role-user {
            background: rgba(100, 100, 100, 0.3);
            color: #ccc;
        }
        button {
            background: linear-gradient(135deg, #4ade80 0%, #22c55e 100%);
            border: none;
            padding: 6px 14px;
            border-radius: 30px;
            color: #0a192f;
            cursor: pointer;
            font-weight: 600;
            font-size: 0.75rem;
            transition: all 0.2s ease;
            margin: 0 4px;
        }
        button:hover {
            transform: translateY(-1px);
            box-shadow: 0 4px 12px rgba(74, 222, 128, 0.3);
        }
        .delete-btn {
            background: linear-gradient(135deg, #ef4444 0%, #dc2626 100%);
        }
        .delete-btn:hover {
            box-shadow: 0 4px 12px rgba(239, 68, 68, 0.3);
        }
        .form-group {
            background: rgba(15, 42, 63, 0.6);
            border-radius: 20px;
            padding: 24px;
            margin: 24px 0 32px;
            display: flex;
            flex-wrap: wrap;
            gap: 20px;
            align-items: flex-end;
        }
        .form-field {
            flex: 1;
            min-width: 180px;
        }
        .form-field label {
            display: block;
            font-size: 0.75rem;
            margin-bottom: 8px;
            color: #9ca3af;
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }
        input, select {
            width: 100%;
            padding: 12px 14px;
            background: rgba(0, 0, 0, 0.4);
            border: 1px solid #2d3e5a;
            border-radius: 16px;
            font-size: 0.9rem;
            font-family: inherit;
            color: white;
            transition: all 0.2s;
        }
        input:focus, select:focus {
            outline: none;
            border-color: #4ade80;
            box-shadow: 0 0 0 3px rgba(74, 222, 128, 0.2);
            background: rgba(0, 0, 0, 0.6);
        }
        .form-action {
            display: flex;
            align-items: center;
            margin-bottom: 0;
        }
        .form-action button {
            padding: 12px 28px;
            font-size: 0.9rem;
            margin: 0;
            white-space: nowrap;
        }
        .error {
            color: #f87171;
            font-size: 0.85rem;
            margin-top: -16px;
            margin-bottom: 16px;
            padding: 8px 12px;
            background: rgba(248, 113, 113, 0.1);
            border-radius: 12px;
            display: inline-block;
        }
        .back-link {
            display: inline-flex;
            align-items: center;
            gap: 8px;
            margin-top: 32px;
            color: #4ade80;
            text-decoration: none;
            font-weight: 500;
            transition: all 0.2s;
        }
        .back-link:hover {
            gap: 12px;
            text-decoration: underline;
        }
        .custom-modal {
            position: fixed;
            top: 0;
            left: 0;
            width: 100%;
            height: 100%;
            background: rgba(0, 0, 0, 0.7);
            display: flex;
            align-items: center;
            justify-content: center;
            z-index: 1000;
            backdrop-filter: blur(4px);
        }
        .custom-modal-content {
            background: linear-gradient(135deg, #0f2a3f, #0a192f);
            border-radius: 28px;
            padding: 32px;
            max-width: 400px;
            text-align: center;
            border: 1px solid rgba(74, 222, 128, 0.3);
            box-shadow: 0 25px 50px -12px rgba(0,0,0,0.5);
        }
        .custom-modal-content p {
            font-size: 1rem;
            margin-bottom: 24px;
            color: #fff;
        }
        .confirm-buttons button {
            padding: 8px 24px;
            border-radius: 40px;
            font-weight: 600;
            cursor: pointer;
            transition: all 0.2s;
            margin: 0 8px;
            border: none;
        }
        .btn-confirm-yes {
            background: linear-gradient(135deg, #4ade80, #22c55e);
            color: #0a192f;
        }
        .btn-confirm-yes:hover {
            transform: translateY(-2px);
            box-shadow: 0 8px 20px rgba(74,222,128,0.3);
        }
        .btn-confirm-no {
            background: linear-gradient(135deg, #ef4444, #dc2626);
            color: white;
        }
        .btn-confirm-no:hover {
            transform: translateY(-2px);
            box-shadow: 0 8px 20px rgba(239,68,68,0.3);
        }
        @media (max-width: 768px) {
            body { padding: 16px; }
            .container { padding: 20px; }
            th, td { padding: 10px; }
            button { padding: 6px 10px; font-size: 0.7rem; }
            .form-group { flex-direction: column; align-items: stretch; gap: 16px; }
            .form-field { min-width: 100%; }
            .form-action button { width: 100%; }
        }
    </style>
</head>
<body>
<div class="container">
    <h1>Управление пользователями</h1>
    <div id="userList"></div>
    <h2>Создать нового пользователя</h2>
    <div class="form-group" id="createForm">
        <div class="form-field">
            <label>Логин</label>
            <input type="text" id="newUsername" placeholder="Введите логин" autocomplete="off">
        </div>
        <div class="form-field">
            <label>Пароль</label>
            <input type="password" id="newPassword" placeholder="Введите пароль">
        </div>
        <div class="form-field">
            <label>Роль</label>
            <select id="newRole">
                <option value="user">Пользователь</option>
                <option value="admin">Администратор</option>
            </select>
        </div>
        <div class="form-action">
            <button id="createUserBtn">Создать</button>
        </div>
    </div>
    <div id="createError" class="error"></div>
    <a href="/" class="back-link">← Вернуться на главную</a>
</div>

<div id="customConfirm" class="custom-modal" style="display: none;">
    <div class="custom-modal-content">
        <p>Вы уверены, что хотите удалить этого пользователя?</p>
        <div class="confirm-buttons">
            <button id="confirmYes" class="btn-confirm-yes">Да</button>
            <button id="confirmNo" class="btn-confirm-no">Нет</button>
        </div>
    </div>
</div>

<script>
    let currentUserId = null;
    async function getCurrentUserId() {
        const resp = await fetch('/whoami');
        if (resp.ok) {
            const data = await resp.json();
            return data.id;
        }
        return null;
    }
    async function fetchUsers() {
        const response = await fetch('/admin/users');
        if (response.status === 401 || response.status === 403) {
            alert('Доступ запрещён. Вы не администратор.');
            window.location.href = '/';
            return;
        }
        const users = await response.json();
        const userListDiv = document.getElementById('userList');
        if (!users.length) {
            userListDiv.innerHTML = '<p>Нет пользователей.</p>';
            return;
        }
        let html = '<table><thead><tr><th>ID</th><th>Логин</th><th>Роль</th><th>Дата регистрации</th><th>Действия</th></tr></thead><tbody>';
        for (let u of users) {
            const roleClass = u.role === 'admin' ? 'role-admin' : 'role-user';
            const createdDate = new Date(u.created_at + 'Z').toLocaleString(undefined, { year:'numeric', month:'2-digit', day:'2-digit', hour:'2-digit', minute:'2-digit', second:'2-digit' });
            html += `
                <tr>
                    <td>${u.id}</td>
                    <td>${escapeHtml(u.username)}</td>
                    <td><span class="role-badge ${roleClass}">${u.role}</span></td>
                    <td>${createdDate}</td>
                    <td>
                        <button onclick="changeRole(${u.id}, 'admin')">Сделать админом</button>
                        <button onclick="changeRole(${u.id}, 'user')">Сделать пользователем</button>
                        ${u.id !== currentUserId ? '<button class="delete-btn" onclick="deleteUser(' + u.id + ')">Удалить</button>' : ''}
                    </td>
                </tr>
            `;
        }
        html += '</tbody></table>';
        userListDiv.innerHTML = html;
    }
    function escapeHtml(str) {
        if (!str) return '';
        return str.replace(/[&<>]/g, function(m) {
            if (m === '&') return '&amp;';
            if (m === '<') return '&lt;';
            if (m === '>') return '&gt;';
            return m;
        });
    }
    async function changeRole(userId, newRole) {
        const response = await fetch(`/admin/users/${userId}/role?role=${newRole}`, { method: 'PUT' });
        if (response.ok) {
            fetchUsers();
        } else {
            alert('Ошибка изменения роли');
        }
    }
    function deleteUser(userId) {
        const modal = document.getElementById('customConfirm');
        const confirmYes = document.getElementById('confirmYes');
        const confirmNo = document.getElementById('confirmNo');
        modal.style.display = 'flex';
        const cleanup = () => {
            modal.style.display = 'none';
            confirmYes.removeEventListener('click', onYes);
            confirmNo.removeEventListener('click', onNo);
        };
        const onYes = async () => {
            cleanup();
            const response = await fetch(`/admin/users/${userId}`, { method: 'DELETE' });
            if (response.ok) {
                fetchUsers();
            } else {
                alert('Ошибка удаления пользователя');
            }
        };
        const onNo = () => {
            cleanup();
        };
        confirmYes.addEventListener('click', onYes);
        confirmNo.addEventListener('click', onNo);
    }
    document.getElementById('createUserBtn').addEventListener('click', async () => {
        const username = document.getElementById('newUsername').value.trim();
        const password = document.getElementById('newPassword').value;
        const role = document.getElementById('newRole').value;
        if (!username || !password) {
            document.getElementById('createError').innerText = 'Заполните логин и пароль';
            return;
        }
        const response = await fetch('/admin/users', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ username, password, role })
        });
        if (response.ok) {
            document.getElementById('newUsername').value = '';
            document.getElementById('newPassword').value = '';
            document.getElementById('createError').innerText = '';
            fetchUsers();
        } else {
            const error = await response.json();
            document.getElementById('createError').innerText = error.detail || 'Ошибка создания';
        }
    });
    (async () => {
        currentUserId = await getCurrentUserId();
        await fetchUsers();
    })();
</script>
</body>
</html>
"""

# ========== ФУНКЦИЯ ИНИЦИАЛИЗАЦИИ БАЗЫ ДАННЫХ ==========
def init_db():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    if db.query(User).count() == 0:
        default_admin = User(
            username="admin",
            hashed_password=get_password_hash("admin"),
            role="admin",
            is_active=True
        )
        db.add(default_admin)
        db.commit()
        print("✅ База данных инициализирована. Создан пользователь admin с паролем admin")
    db.close()

init_db()

# ========== ЭНДПОИНТЫ ==========
@app.get("/", response_class=HTMLResponse)
async def upload_page():
    return HTMLResponse(content=HTML_PAGE)

@app.get("/admin", response_class=HTMLResponse)
async def admin_page(current_user: User = Depends(require_role("admin"))):
    return HTMLResponse(content=ADMIN_PAGE)

@app.get("/whoami")
async def whoami(current_user: User = Depends(get_current_user)):
    return {"id": current_user.id, "role": current_user.role, "username": current_user.username}

@app.get("/status")
async def get_service_status(current_user: User = Depends(get_current_user)):
    return {
        "yandex_speechkit": bool(os.getenv("SPEECHKIT_API_KEY") and os.getenv("SPEECHKIT_FOLDER_ID")),
        "gigachat": bool(os.getenv("GIGACHAT_CREDENTIALS")),
        "telegram": bool(os.getenv("TELEGRAM_BOT_TOKEN") and os.getenv("TELEGRAM_CHAT_ID"))
    }

@app.post("/token")
async def login(form_data: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)):
    user = authenticate_user(db, form_data.username, form_data.password)
    if not user:
        raise HTTPException(status_code=401, detail="Incorrect username or password")
    access_token = create_access_token(data={"sub": user.username})
    response = JSONResponse(content={"role": user.role})
    response.set_cookie(
        key="access_token",
        value=access_token,
        httponly=True,
        secure=False,
        samesite="lax",
        max_age=ACCESS_TOKEN_EXPIRE_MINUTES * 60
    )
    return response

@app.post("/logout")
async def logout():
    response = JSONResponse(content={"ok": True})
    response.delete_cookie("access_token")
    return response

# Загрузка файла в S3
@app.post("/api/upload_to_s3")
async def upload_to_s3(file: UploadFile = File(...), current_user: User = Depends(get_current_user)):
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    key = f"raw-recordings/{current_user.id}/{timestamp}_{file.filename}"
    content = await file.read()
    s3_storage.upload_file(content, key)
    return {"key": key, "message": "File uploaded to S3"}

# Запуск обработки через GitHub Actions
@app.post("/api/process/{audio_key:path}")
async def process_audio(audio_key: str, current_user: User = Depends(require_role("admin"))):
    success = github_client.trigger_workflow(audio_key)
    if not success:
        raise HTTPException(status_code=500, detail="Failed to start workflow")
    return {"status": "started", "audio_key": audio_key}

# Получение списка файлов за период с признаком наличия отчёта
@app.get("/api/reports/period")
async def reports_period(
    start_date: str = None,
    end_date: str = None,
    current_user: User = Depends(get_current_user)
):
    start = None
    end = None
    if start_date:
        start = datetime.fromisoformat(start_date).replace(tzinfo=None)
    if end_date:
        end = datetime.fromisoformat(end_date).replace(tzinfo=None)

    audio_files = s3_storage.list_audio_files(start_date=start, end_date=end)
    result = []
    for af in audio_files:
        report = s3_storage.get_report(af['key'])
        result.append({
            'audio_key': af['key'],
            'last_modified': af['last_modified'].isoformat(),
            'size': af['size'],
            'has_report': report is not None,
            'report_summary': {
                'satisfaction_score': report.get('analysis', {}).get('satisfaction_score') if report else None
            } if report else None
        })
    return result

# Получение конкретного отчёта и транскрипта
@app.get("/api/report/{audio_key:path}")
async def get_report_by_audio_key(audio_key: str, current_user: User = Depends(get_current_user)):
    report = s3_storage.get_report(audio_key)
    if not report:
        raise HTTPException(status_code=404, detail="Report not found")
    transcript = s3_storage.get_transcript(audio_key)
    return {"report": report, "transcript": transcript}

# Управление пользователями (админ)
@app.get("/admin/users")
async def list_users(db: Session = Depends(get_db), current_user: User = Depends(require_role("admin"))):
    users = db.query(User).all()
    return [{"id": u.id, "username": u.username, "role": u.role, "created_at": u.created_at.isoformat()} for u in users]

@app.post("/admin/users")
async def create_user(user_data: dict, db: Session = Depends(get_db), current_user: User = Depends(require_role("admin"))):
    username = user_data.get("username")
    password = user_data.get("password")
    role = user_data.get("role", "user")
    if not username or not password:
        raise HTTPException(status_code=400, detail="Username and password required")
    existing = db.query(User).filter(User.username == username).first()
    if existing:
        raise HTTPException(status_code=400, detail="Username already exists")
    hashed = get_password_hash(password)
    new_user = User(username=username, hashed_password=hashed, role=role)
    db.add(new_user)
    db.commit()
    db.refresh(new_user)
    return {"id": new_user.id, "username": new_user.username, "role": new_user.role}

@app.put("/admin/users/{user_id}/role")
async def set_role(user_id: int, role: str, db: Session = Depends(get_db), current_user: User = Depends(require_role("admin"))):
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    user.role = role
    db.commit()
    return {"ok": True}

@app.delete("/admin/users/{user_id}")
async def delete_user(user_id: int, db: Session = Depends(get_db), current_user: User = Depends(require_role("admin"))):
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if user.id == current_user.id:
        raise HTTPException(status_code=403, detail="Cannot delete yourself")
    db.delete(user)
    db.commit()
    return {"ok": True}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)