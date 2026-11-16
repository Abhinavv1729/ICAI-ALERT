"""Private ICAI Nashik website and recurring Telegram batch watcher."""
from __future__ import annotations
import asyncio
import logging
import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
import re

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from pydantic import BaseModel

load_dotenv()
from monitor import SITE_URL, batch_key, capture_batches, format_batch, telegram_send, telegram_recent_chats  # noqa:E402
from storage import Store  # noqa:E402

ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv('DATA_DIR', str(ROOT / 'data')))
CHECK_MINUTES = max(5, int(os.getenv('POLL_MINUTES','5')))
DASHBOARD_PASSWORD = os.getenv('DASHBOARD_PASSWORD', '')
COOKIE_SECURE = os.getenv('COOKIE_SECURE','true').lower() not in ('false','0','no')
# Securely distinct secret is preferable, but derive from the admin password if none supplied.
SESSION_SECRET = os.getenv('SESSION_SECRET') or DASHBOARD_PASSWORD
logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(name)s %(message)s')
log = logging.getLogger('icai.web')

class LoginPayload(BaseModel):
    password: str


class RecipientPayload(BaseModel):
    chat_id: str


def selected_chat_id(app: FastAPI) -> str:
    """Verified dashboard recipient takes precedence over optional env setting."""
    return (app.state.store.get_setting('telegram_chat_id') or
            os.getenv('TELEGRAM_CHAT_ID', '')).strip()


def telegram_enabled(app: FastAPI) -> bool:
    return bool(os.getenv('TELEGRAM_BOT_TOKEN', '').strip() and selected_chat_id(app))


def send_to_recipient(app: FastAPI, message: str) -> None:
    telegram_send(message, chat_id=selected_chat_id(app))


def signing():
    return URLSafeTimedSerializer(SESSION_SECRET, salt='icai-dashboard-v1')


def authorized(request: Request) -> bool:
    raw = request.cookies.get('icai_session')
    if not raw or not SESSION_SECRET:
        return False
    try:
        return signing().loads(raw, max_age=60*60*24*14) == 'admin'
    except (BadSignature,SignatureExpired):
        return False


def assert_auth(request: Request):
    if not authorized(request):
        raise HTTPException(status_code=401, detail='Sign in to use the dashboard')


def csrf_token():
    return signing().dumps('action', salt='icai-csrf-v1')


def assert_write(request: Request):
    assert_auth(request)
    raw = request.headers.get('x-csrf-token','')
    try:
        valid = signing().loads(raw,salt='icai-csrf-v1',max_age=60*60*12) == 'action'
    except (BadSignature,SignatureExpired):
        valid = False
    if not valid:
        raise HTTPException(status_code=403, detail='Security token expired. Refresh the page.')


async def run_scan(app: FastAPI):
    async with app.state.scan_lock:
        app.state.running = True
        scan_id = app.state.store.scan_start()
        first_success = app.state.store.snapshot()['last_success'] is None
        try:
            batches = await asyncio.to_thread(capture_batches)
            new_count = app.state.store.scan_success(scan_id, batches, batch_key)
            log.info('ICAI check done: %d total, %d new',len(batches),new_count)
            if app.state.errors:
                app.state.store.event('success','Monitoring recovered','ICAI batch checks are working again')
                app.state.errors = 0
            if telegram_enabled(app):
                if first_success:
                    try:
                        await asyncio.to_thread(send_to_recipient, app,
                            '✅ ICAI Nashik Batch Watch is active.\nWestern Region • POU Nashik • All courses\n'
                            f'Existing batches recorded: {len(batches)}. New batches will trigger alerts.\n'+SITE_URL)
                        app.state.store.event('sent','Monitoring started','Telegram confirmation delivered')
                    except Exception as exc:
                        app.state.store.event('warning','Startup Telegram confirmation failed',str(exc))
                # New-batch notifications are stored durably and retried until delivered.
                pending = app.state.store.pending_alerts()
                for item in pending:
                    message = format_batch(item)
                    try:
                        await asyncio.to_thread(send_to_recipient, app,message)
                    except Exception as exc:
                        app.state.store.event('error','Telegram delivery failed',str(exc))
                        log.exception('Telegram delivery failed')
                        break  # preserve undelivered messages for the next check
                    else:
                        app.state.store.mark_notified(item['key'])
        except Exception as exc:
            log.exception('ICAI check failed')
            app.state.store.scan_failed(scan_id, str(exc))
            app.state.errors += 1
            if telegram_enabled(app) and app.state.errors in (1,12,72):
                try:
                    await asyncio.to_thread(send_to_recipient, app,
                        f'⚠️ ICAI Nashik batch check failed ({app.state.errors} consecutive errors).\n'
                        f'{str(exc)[:280]}\n'+SITE_URL)
                except Exception:
                    log.exception('Could not deliver failure warning')
        finally:
            app.state.running = False


async def scheduler(app: FastAPI):
    while not app.state.stop.is_set():
        try:
            await run_scan(app)
        except Exception:
            log.exception('Unexpected scheduler error')
        try:
            await asyncio.wait_for(app.state.stop.wait(), CHECK_MINUTES * 60)
        except asyncio.TimeoutError:
            pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not DASHBOARD_PASSWORD or len(DASHBOARD_PASSWORD) < 12:
        raise RuntimeError('Set DASHBOARD_PASSWORD to a strong password (at least 12 characters).')
    app.state.store = Store(DATA_DIR / 'icai.sqlite3')
    app.state.scan_lock = asyncio.Lock()
    app.state.running = False
    app.state.manual_queued = False
    app.state.errors = 0
    app.state.stop = asyncio.Event()
    if not telegram_enabled(app):
        app.state.store.event('warning','Telegram not configured','Add token and chat ID to enable alerts')
    task = asyncio.create_task(scheduler(app))
    try:
        yield
    finally:
        app.state.stop.set()
        await task


app = FastAPI(title='ICAI Nashik Batch Watch', docs_url=None, redoc_url=None, lifespan=lifespan)
app.mount('/static',StaticFiles(directory=ROOT/'static'),name='static')

@app.get('/healthz')
def health():
    return {'ok':True}

@app.get('/login')
def login_page(request: Request):
    if authorized(request):
        return RedirectResponse('/',status_code=303)
    return FileResponse(ROOT/'static'/'login.html',headers={'Cache-Control':'no-store'})

@app.post('/api/login')
def login(payload: LoginPayload,request: Request):
    if not DASHBOARD_PASSWORD or not secrets.compare_digest(payload.password,DASHBOARD_PASSWORD):
        raise HTTPException(status_code=401,detail='Incorrect password')
    res = JSONResponse({'ok':True})
    res.set_cookie('icai_session',signing().dumps('admin'),max_age=60*60*24*14,
                   secure=COOKIE_SECURE,httponly=True,samesite='lax',path='/')
    return res

@app.get('/')
def index(request: Request):
    if not authorized(request):
        return RedirectResponse('/login',status_code=303)
    return FileResponse(ROOT/'static'/'index.html',headers={'Cache-Control':'no-store'})

@app.get('/api/state')
def get_state(request: Request):
    assert_auth(request)
    data = request.app.state.store.snapshot()
    data.update({'running':request.app.state.running,
                 'telegram_ready':telegram_enabled(request.app),
                 'telegram_bot_ready':bool(os.getenv('TELEGRAM_BOT_TOKEN', '').strip()),
                 'telegram_chat_id':selected_chat_id(request.app),
                 'telegram_bot_username':'PranjalAlert_bot',
                 'poll_minutes':CHECK_MINUTES,'site_url':SITE_URL,'csrf':csrf_token()})
    return JSONResponse(data,headers={'Cache-Control':'no-store'})

async def queued_scan(app: FastAPI):
    try:
        await run_scan(app)
    finally:
        app.state.manual_queued = False


@app.post('/api/check')
async def check_now(request: Request):
    assert_write(request)
    if request.app.state.scan_lock.locked() or request.app.state.manual_queued:
        return JSONResponse({'ok':False,'message':'A check is already in progress'},status_code=409)
    request.app.state.manual_queued = True
    asyncio.create_task(queued_scan(request.app))
    return JSONResponse({'ok':True,'message':'Check queued'},status_code=202)

@app.post('/api/telegram/test')
async def test_telegram(request: Request):
    assert_write(request)
    if not telegram_enabled(request.app):
        raise HTTPException(status_code=400,detail='Configure the bot token and verify a chat in Settings first.')
    try:
        await asyncio.to_thread(send_to_recipient, request.app,
            '✅ ICAI Nashik Batch Watch connected.\nWestern Region • POU Nashik • All courses\n'+SITE_URL)
    except Exception:
        log.exception('Telegram test failed')
        raise HTTPException(status_code=502,detail='Telegram test failed. Check the token, chat ID, and bot permissions.')
    request.app.state.store.event('sent','Telegram test sent','Connection verified')
    return {'ok':True,'message':'Test message sent to Telegram'}

@app.post('/api/telegram/chats')
async def discover_chats(request: Request):
    """Authenticated-only Telegram recent chat discovery; token remains server-side."""
    assert_write(request)
    if not os.getenv('TELEGRAM_BOT_TOKEN', '').strip():
        raise HTTPException(status_code=400, detail='Configure TELEGRAM_BOT_TOKEN on the server first.')
    try:
        chats = await asyncio.to_thread(telegram_recent_chats)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from None
    return {'ok': True, 'chats': chats, 'message': 'Found %d recent private chat(s)' % len(chats)}


@app.post('/api/telegram/recipient')
async def verify_recipient(payload: RecipientPayload, request: Request):
    """Deliver a fixed test first, then persist only a verified numeric chat ID."""
    assert_write(request)
    chat_id = payload.chat_id.strip()
    if not re.fullmatch(r'[0-9]{5,20}', chat_id):
        raise HTTPException(status_code=422, detail='Enter a numeric private Telegram chat ID, not a phone number.')
    if not os.getenv('TELEGRAM_BOT_TOKEN', '').strip():
        raise HTTPException(status_code=400, detail='Configure TELEGRAM_BOT_TOKEN on the server first.')
    try:
        await asyncio.to_thread(telegram_send,
            '✅ ICAI Nashik BatchWatch connected! This chat will receive new Western Region / Nashik batch alerts.',
            chat_id=chat_id)
    except Exception as exc:
        log.warning('Telegram recipient verification unsuccessful')
        raise HTTPException(status_code=502, detail=str(exc)) from None
    request.app.state.store.set_setting('telegram_chat_id', chat_id)
    request.app.state.store.event('sent', 'Telegram recipient verified', 'A test message was delivered to the selected chat ID.')
    return {'ok': True, 'chat_id': chat_id, 'message': 'Telegram message delivered; alerts will go to this chat.'}


@app.post('/api/logout')
def logout(request: Request):
    assert_write(request)
    res=JSONResponse({'ok':True})
    res.delete_cookie('icai_session',path='/')
    return res
