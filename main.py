import os
import json
import uuid
import re
import logging
from typing import List, Optional
from dotenv import load_dotenv
import httpx
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

load_dotenv()

TG_BOT_TOKEN  = os.getenv("TG_BOT_TOKEN")
TG_CHAT_ID    = os.getenv("TG_CHAT_ID")
STEAM_API_KEY = os.getenv("STEAM_API_KEY")  # Получи его на steamcommunity.com/dev/apikey

if not TG_BOT_TOKEN or not TG_CHAT_ID:
    raise RuntimeError("TG_BOT_TOKEN и TG_CHAT_ID должны быть в .env файле")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
app.add_middleware(GZipMiddleware, minimum_size=500)
app.mount("/static", StaticFiles(directory="."), name="static")

RAILWAY_WORKER_URL = "https://inv-production-8d86.up.railway.app/api/check-inventory"
DEFAULT_AVATAR = "https://avatars.steamstatic.com/fef49e7fa7e1997310d705b2a6158ff8dc1cdfeb_full.jpg"

_HTTP_CLIENT = httpx.AsyncClient(
    timeout=httpx.Timeout(35.0, connect=5.0),
    limits=httpx.Limits(max_connections=100, max_keepalive_connections=20)
)

# ========================
# HELPERS
# ========================

def load_json_file(path: str) -> list:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else data.get("items", [])
    except Exception as e:
        logger.error(f"{path} ошибка чтения: {e}")
        return []

_MARKETPLACE = load_json_file("marketplace.json")
_BUY_ORDERS  = load_json_file("buy_orders.json")
_PRICES_MAP = {item["name"]: float(item["price"]) for item in _MARKETPLACE if "name" in item and "price" in item}

def build_index_html() -> str:
    try:
        with open("index.html", "r", encoding="utf-8") as f:
            html = f.read()
    except FileNotFoundError:
        return "<h1>index.html не найден</h1>"

    data_script = f"""<script>
window.__MARKETPLACE__ = {json.dumps(_MARKETPLACE, ensure_ascii=False, separators=(',', ':'))};
window.__BUY_ORDERS__ = {json.dumps(_BUY_ORDERS, ensure_ascii=False, separators=(',', ':'))};
</script>"""

    if '<script src=' in html:
        html = html.replace('<script src=', data_script + '\n<script src=', 1)
    else:
        html = html.replace('</body>', data_script + '\n</body>', 1)
    return html

_INDEX_HTML = build_index_html()


async def fetch_steam_profile_direct(steam_id: str) -> dict:
    """Получает ник и реальную аватарку напрямую через Steam Web API без воркера"""
    if not STEAM_API_KEY:
        return {"name": "User", "avatar": DEFAULT_AVATAR}
    try:
        url = f"http://api.steamcommunity.com/ISteamUser/GetPlayerSummaries/v0002/?key={STEAM_API_KEY}&steamids={steam_id}"
        async with httpx.AsyncClient(timeout=4.0) as client:
            resp = await client.get(url)
            if resp.status_code == 200:
                players = resp.json().get("response", {}).get("players", [])
                if players:
                    return {
                        "name": players[0].get("personaname", "User"),
                        "avatar": players[0].get("avatarfull", DEFAULT_AVATAR)
                    }
    except Exception as e:
        logger.error(f"Ошибка получения профиля напрямую из Steam: {e}")
    return {"name": "User", "avatar": DEFAULT_AVATAR}


# ========================
# ROUTES
# ========================

@app.get("/")
def index():
    return HTMLResponse(content=_INDEX_HTML, headers={"Cache-Control": "no-store"})


@app.get("/api/steam-profile/{steam_id}")
async def steam_profile(steam_id: str):
    """Роут профиля: теперь отдает настоящие данные за доли секунды"""
    target = steam_id.strip()
    if not (target.isdigit() and len(target) == 17):
        partner_match = re.search(r"partner=(\d+)", target)
        if partner_match:
            target = str(int(partner_match.group(1)) + 76561197960265728)
        else:
            return {"name": "", "avatar": DEFAULT_AVATAR}
            
    profile_data = await fetch_steam_profile_direct(target)
    return profile_data


@app.get("/api/inventory/{steam_id}")
async def inventory(steam_id: str):
    try:
        target = steam_id.strip()
        
        if not (target.isdigit() and len(target) == 17):
            partner_match = re.search(r"partner=(\d+)", target)
            if partner_match:
                target = str(int(partner_match.group(1)) + 76561197960265728)
            else:
                raise HTTPException(status_code=400, detail="Неверный формат ссылки")

        # 1. Параллельно запрашиваем нормальный профиль (чтобы аватарка обновилась на настоящую)
        profile_data = await fetch_steam_profile_direct(target)

        res = None
        # 2. БЫСТРАЯ ПОПЫТКА: Пробуем загрузить инвентарь напрямую со своего сервера за 4 секунды
        try:
            logger.info(f"Пробуем быструю загрузку напрямую для {target}...")
            direct_url = f"https://steamcommunity.com/inventory/{target}/730/2?l=russian&count=5000"
            async with httpx.AsyncClient(timeout=4.0) as client:
                direct_resp = await client.get(direct_url)
                if direct_resp.status_code == 200:
                    res = direct_resp.json()
                    res["status"] = "success"
                    logger.info("Успешная мгновенная загрузка напрямую без воркера!")
        except Exception as e:
            logger.warning(f"Прямой запрос не удался ({e}), переключаемся на облачный воркер...")

        # 3. ЕСЛИ НАПРЯМУЮ НЕ ВЫШЛО — Обращаемся к надежному воркеру Railway с прокси
        if not res:
            try:
                response = await _HTTP_CLIENT.post(RAILWAY_WORKER_URL, json={"target": target})
                if response.status_code != 200:
                    raise HTTPException(status_code=502, detail="Ошибка воркера")
                res = response.json()
            except httpx.ReadTimeout:
                raise HTTPException(status_code=503, detail="Steam перегружен. Пожалуйста, попробуйте еще раз.")

        # Разбор ответа (как от воркера, так и от прямого запроса)
        status = res.get("status")
        if status == "private":
            raise HTTPException(status_code=403, detail="Инвентарь закрыт")
        if status == "failed":
            raise HTTPException(status_code=503, detail="Steam перегружен")

        raw_assets = res.get("assets", [])
        raw_descriptions = res.get("descriptions", [])
        
        all_assets = []
        all_descriptions = []
        seen_desc = set()
        tradable_keys = set()
        
        for d in raw_descriptions:
            key = f"{d['classid']}_{d['instanceid']}"
            if d.get("tradable") == 1:
                tradable_keys.add(key)
            if key not in seen_desc:
                seen_desc.add(key)
                d["price"] = _PRICES_MAP.get(d.get("market_hash_name"), 0.0)
                all_descriptions.append(d)
                
        for asset in raw_assets:
            if f"{asset['classid']}_{asset['instanceid']}" in tradable_keys:
                all_assets.append(asset)

        return {
            "assets": all_assets, 
            "descriptions": all_descriptions,
            "profile": profile_data  # Отправляем реальный ник и аватарку
        }
        
    except HTTPException as he:
        raise he
    except Exception as e:
        logger.exception("Критическая ошибка инвентаря:")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/balance/{steam_id}")
async def get_balance(steam_id: str):
    return {"balance": 0.0, "steam_id": steam_id}

# ========================
# SELL / BUY
# ========================

class SellItem(BaseModel):
    name: str
    price: float

class SellRequest(BaseModel):
    trade_url: str
    items: List[SellItem]
    total: float
    steam_id: Optional[str] = ""

@app.post("/api/buy")
async def buy(req: SellRequest):
    deal_id = str(uuid.uuid4())[:8].upper()
    items_text = "\n".join([f"  • {i.name} — ${i.price:.2f}" for i in req.items])
    msg = f"🛍 <b>Новая покупка</b>\n🆔 <code>{deal_id}</code>\n💰 <b>${req.total:.2f}</b>\n\n🎒 {len(req.items)} шт.:\n{items_text}\n\n🔗 <code>{req.trade_url}</code>"
    try:
        await _HTTP_CLIENT.post(f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage", data={"chat_id": TG_CHAT_ID, "text": msg, "parse_mode": "HTML"})
    except Exception as e:
        logger.error(f"Telegram error: {e}")
    return {"deal_id": deal_id, "total": req.total, "balance": 0.0}

@app.post("/api/sell")
async def sell(req: SellRequest):
    deal_id = str(uuid.uuid4())[:8].upper()
    items_text = "\n".join([f"  • {i.name} — ${i.price:.2f}" for i in req.items])
    msg = f"🛒 <b>Новая продажа</b>\n🆔 <code>{deal_id}</code>\n💰 <b>${req.total:.2f}</b>\n\n🎒 {len(req.items)} шт.:\n{items_text}\n\n🔗 <code>{req.trade_url}</code>"
    try:
        await _HTTP_CLIENT.post(f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage", data={"chat_id": TG_CHAT_ID, "text": msg, "parse_mode": "HTML"})
    except Exception as e:
        logger.error(f"Telegram error: {e}")
    return {"deal_id": deal_id, "total": req.total}
