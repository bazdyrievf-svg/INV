from dotenv import load_dotenv
import os

load_dotenv()

TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "5673389610:AAHKwOp4u5F4H8AtgI48XZRC9QdZSgy9BuA")
TG_CHAT_ID   = os.getenv("TG_CHAT_ID", "1024266193")

from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from bs4 import BeautifulSoup
from typing import Optional
import httpx
import re
import logging
import asyncio
import random

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI(title="Steam Inventory & Proxy Tunnel")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# --- НАСТРОЙКИ ВАЛИДАТОРА И ПАРСЕРА ---
ADVANCED_PROXY_URLS = ["https://advanced.name/freeproxy/6a523b381c08c"]
VALIDATOR_TEST_URL = "https://steamcommunity.com/?xml=1"
VALIDATOR_TIMEOUT = 10.0
INVENTORY_TIMEOUT = 12.0
INVENTORY_WORKERS = 10

# --- ГЛОБАЛЬНЫЕ ОБЪЕКТЫ В ОПЕРАТИВНОЙ ПАМЯТИ (IN-MEMORY) ---
IN_MEMORY_CACHE = set()  # Сюда складываем все когда-либо скачанные уникальные прокси
VALID_PROXIES = []       # Список только 100% рабочих прокси, готовых к бою

# ========================
# ТЕЛЕГРАМ УВЕДОМЛЕНИЯ
# ========================
async def send_telegram(text: str):
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            await client.post(url, json={"chat_id": TG_CHAT_ID, "text": text, "parse_mode": "HTML"})
    except Exception as e:
        logger.error(f"Ошибка отправки Telegram: {e}")

# ========================
# ЛОГИКА ВАЛИДАЦИИ И СБОРА ПРОКСИ (ПЕРЕВЕДЕНО НА ASYNC)
# ========================
async def validate_single_proxy(proxy_str: str, client: httpx.AsyncClient) -> Optional[str]:
    proxy_url = f"http://{proxy_str.strip()}"
    try:
        # Быстрый асинхронный запрос через конкретный прокси
        r = await client.get(VALIDATOR_TEST_URL, proxy=proxy_url, timeout=VALIDATOR_TIMEOUT)
        if 200 <= r.status_code < 300:
            return proxy_str
    except Exception:
        pass
    return None

async def batch_validate_proxies():
    """Асинхронная проверка всех прокси в памяти"""
    global VALID_PROXIES
    if not IN_MEMORY_CACHE:
        logger.warning("[⚠️] Нет прокси в кэше для валидации.")
        return

    logger.info(f"[🔍] ВАЛИДАЦИЯ: Проверяю {len(IN_MEMORY_CACHE)} прокси из памяти...")
    
    # Ограничиваем лимит одновременных соединений (аналог max_workers=500)
    limits = httpx.Limits(max_connections=500, max_keepalive_connections=50)
    
    async with httpx.AsyncClient(limits=limits, timeout=VALIDATOR_TIMEOUT) as client:
        tasks = [validate_single_proxy(proxy, client) for proxy in IN_MEMORY_CACHE]
        results = await asyncio.gather(*tasks)
        
        # Собираем только отработавшие
        working = [p for p in results if p is not None]

    VALID_PROXIES = working
    percent = int((len(working) / len(IN_MEMORY_CACHE) * 100) if IN_MEMORY_CACHE else 0)
    
    logger.info(f"[✅] ВАЛИДАЦИЯ ЗАВЕРШЕНА. Рабочих прокси в памяти: {len(VALID_PROXIES)} ({percent}%)")
    
    if len(VALID_PROXIES) == 0:
        await send_telegram("🚨 <b>PROXY WORKER CRITICAL</b>\n❌ Рабочих прокси в памяти: <b>0</b>")

async def load_proxies_from_site():
    """Скачивание новых прокси в оперативную память"""
    global IN_MEMORY_CACHE
    all_new = []
    
    async with httpx.AsyncClient(timeout=15) as client:
        for idx, url in enumerate(ADVANCED_PROXY_URLS, 1):
            try:
                logger.info(f"[📥] Скачиваю прокси из источника #{idx}...")
                headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
                r = await client.get(url, headers=headers)
                r.raise_for_status()
                
                proxy_list = []
                soup = BeautifulSoup(r.content, 'html.parser')
                table = soup.find('table')
                
                if table:
                    rows = table.find_all('tr')[1:]
                    for row in rows:
                        cells = row.find_all('td')
                        if len(cells) >= 2:
                            ip = cells[0].get_text(strip=True)
                            port = cells[1].get_text(strip=True)
                            if re.match(r'^\d+\.\d+\.\d+\.\d+$', ip) and port.isdigit():
                                proxy_list.append(f"{ip}:{port}")
                                
                if not proxy_list:
                    matches = re.findall(r'(\d+\.\d+\.\d+\.\d+):(\d+)', r.text)
                    for ip, port in matches:
                        proxy_list.append(f"{ip}:{port}")
                        
                all_new.extend(proxy_list)
                logger.info(f"[✅] Источник #{idx}: Найдено {len(proxy_list)} прокси.")
            except Exception as e:
                logger.error(f"[❌] Ошибка парсинга источника #{idx}: {e}")

    if all_new:
        old_size = len(IN_MEMORY_CACHE)
        IN_MEMORY_CACHE.update(all_new)
        logger.info(f"[📊] Пул прокси обновлен. Было: {old_size}, Стало в памяти: {len(IN_MEMORY_CACHE)}")
    else:
        logger.warning("[⚠️] С сайта не удалось получить новые прокси.")

# ========================
# ФОНОВЫЕ ТАЙМЕРЫ (BACKGROUND LOOPS)
# ========================
async def proxy_loader_cron():
    """Каждые 30 минут качает новые прокси"""
    while True:
        try:
            await load_proxies_from_site()
        except Exception as e:
            logger.error(f"Критическая ошибка в cron загрузки: {e}")
        await asyncio.sleep(30 * 60)

async def proxy_validator_cron():
    """Каждые 5 минут валидирует базу в памяти"""
    while True:
        try:
            await batch_validate_proxies()
        except Exception as e:
            logger.error(f"Критическая ошибка в cron валидации: {e}")
        await asyncio.sleep(5 * 60)

@app.on_event("startup")
async def startup_event():
    # На старте сразу делаем первичный запуск, чтобы память заполнилась
    logger.info("[🚀] Запуск базовой структуры микросервиса Railway...")
    await load_proxies_from_site()
    await batch_validate_proxies()
    
    # Запускаем бесконечные фоновые задачи в цикле событий FastAPI
    asyncio.create_task(proxy_loader_cron())
    asyncio.create_task(proxy_validator_cron())

# ========================
# ПАРСИНГ ПРОФИЛЯ И ИНВЕНТАРЯ RUST
# ========================
async def fetch_profile_xml(steam_id: str) -> dict:
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(f"https://steamcommunity.com/profiles/{steam_id}/?xml=1", headers={"User-Agent": "Mozilla/5.0"})
            if r.status_code == 200:
                text = r.text
                name_m   = re.search(r'<steamID><!\[CDATA\[(.+?)\]\]></steamID>', text)
                avatar_m = re.search(r'<avatarFull><!\[CDATA\[(.+?)\]\]></avatarFull>', text)
                if name_m:
                    return {
                        "name": name_m.group(1),
                        "avatar": avatar_m.group(1) if avatar_m else ""
                    }
    except Exception as e:
        logger.error(f"Ошибка сбора профиля XML: {e}")
    return {"name": "Unknown", "avatar": ""}

async def inventory_worker_task(steam_id: str, success_event: asyncio.Event, result_container: dict):
    url = f"https://steamcommunity.com/inventory/{steam_id}/252490/2?l=english&count=2000"
    
    while not success_event.is_set():
        if not VALID_PROXIES:
            await asyncio.sleep(0.5)
            continue
            
        proxy_str = random.choice(VALID_PROXIES)
        proxy_url = f"http://{proxy_str}"
        
        try:
            async with httpx.AsyncClient(proxy=proxy_url, timeout=INVENTORY_TIMEOUT) as client:
                r = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
                
                if success_event.is_set():
                    return

                if r.status_code in [403, 401, 404]:
                    success_event.set()
                    result_container["status"] = "private"
                    return
                
                if r.status_code == 429:
                    await asyncio.sleep(0.2)
                    continue
                    
                if r.status_code != 200:
                    continue
                
                success_event.set()
                result_container["status"] = "success"
                result_container["data"] = r.json()
                return
        except Exception:
            continue

# ========================
# API ЭНДПОИНТ ДЛЯ ТВОЕГО САЙТА
# ========================
class InventoryRequest(BaseModel):
    target: str  # Сюда можно слать хоть SteamID, хоть трейд-ссылку

@app.post("/api/check-inventory")
async def check_inventory(req: InventoryRequest):
    # Извлекаем чистый SteamID
    target = req.target.strip()
    if target.isdigit() and len(target) == 17:
        steam_id = target
    else:
        partner_match = re.search(r"partner=(\d+)", target)
        if partner_match:
            steam_id = str(int(partner_match.group(1)) + 76561197960265728)
        else:
            raise HTTPException(status_code=400, detail="Неверный формат ссылки или SteamID")

    success_event = asyncio.Event()
    result_container = {"status": "failed", "data": None}
    
    # 1. Запускаем параллельный сбор аватарки/ника
    profile_task = asyncio.create_task(fetch_profile_xml(steam_id))
    
    # 2. Штурмуем инвентарь 10 асинхронными воркерами из памяти прокси
    workers_count = INVENTORY_WORKERS if VALID_PROXIES else 1
    tasks = [inventory_worker_task(steam_id, success_event, result_container) for _ in range(workers_count)]
    
    await asyncio.gather(*tasks)
    profile_info = await profile_task

    # 3. Анализируем ответы
    if result_container["status"] == "private":
        return {"status": "private", "profile": profile_info, "message": "Инвентарь закрыт настройками приватности Steam."}
        
    if result_container["status"] != "success" or not result_container["data"]:
        return {"status": "failed", "profile": profile_info, "message": "Steam отклонил запросы. Прокси перегружены."}

    raw_data = result_container["data"]
    
    # Отдаем сырой ответ Steam + профиль. Твой server main переварит это и наложит цены!
    return {
        "status": "success",
        "steam_id": steam_id,
        "profile": profile_info,
        "assets": raw_data.get("assets", []),
        "descriptions": raw_data.get("descriptions", [])
    }