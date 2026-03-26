from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pywebpush import webpush, WebPushException
from apscheduler.schedulers.background import BackgroundScheduler
import requests as req
import json
import os
import time
from datetime import datetime

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ========== НАСТРОЙКИ ==========
AMO_DOMAIN       = os.getenv("AMO_DOMAIN", "etalongroup.amocrm.ru")
AMO_TOKEN        = os.getenv("AMO_TOKEN", "")
CHANNEL_FIELD_ID = 582110
CITY_FIELD_ID    = 575680
TARGET_VALUES    = ["pr лид", "pr входящий"]
VAPID_PRIVATE    = os.getenv("VAPID_PRIVATE", "")
VAPID_PUBLIC     = os.getenv("VAPID_PUBLIC", "")
VAPID_EMAIL      = os.getenv("VAPID_EMAIL", "mailto:admin@example.com")

MANAGERS = [
    {"id": 13468817, "name": "Рябчикова Ангелина"},
    {"id": 1680076,  "name": "Юлия Бровенко"},
    {"id": 6340248,  "name": "Анастасия Лебедева"},
    {"id": 3835645,  "name": "Татьяна Селиверстова"},
    {"id": 2942284,  "name": "Лариса Голушко"},
    {"id": 10902929, "name": "Столярова Ахметов"},
]
# ================================

BASE = "https://" + AMO_DOMAIN
AMO_HEADERS = {"Authorization": "Bearer " + AMO_TOKEN}

# Хранилище в памяти
subscriptions = []       # push-подписки устройств
processed_ids = set()    # уже обработанные лиды
next_manager_idx = 0     # счётчик round-robin
recent_leads = []        # последние 50 лидов для отображения

STATE_FILE = "state.json"

def load_state():
    global processed_ids, next_manager_idx, recent_leads
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            s = json.load(f)
            processed_ids = set(s.get("processed_ids", []))
            next_manager_idx = s.get("next_manager_idx", 0)
            recent_leads = s.get("recent_leads", [])

def save_state():
    with open(STATE_FILE, "w") as f:
        json.dump({
            "processed_ids": list(processed_ids)[-2000:],
            "next_manager_idx": next_manager_idx,
            "recent_leads": recent_leads[-50:]
        }, f)

def amo_get(url, params=None):
    r = req.get(url, headers=AMO_HEADERS, params=params, timeout=15)
    r.raise_for_status()
    return r.json()

def amo_patch(url, data):
    r = req.patch(url, headers=AMO_HEADERS, json=data, timeout=15)
    r.raise_for_status()

def get_field(deal, field_id):
    for f in (deal.get("custom_fields_values") or []):
        if f.get("field_id") == field_id:
            vals = f.get("values", [])
            if vals:
                return vals[0].get("value", "")
    return ""

def send_push_all(title, body, data=None):
    global subscriptions
    dead = []
    for sub in subscriptions:
        try:
            webpush(
                subscription_info=sub,
                data=json.dumps({"title": title, "body": body, "data": data or {}}),
                vapid_private_key=VAPID_PRIVATE,
                vapid_claims={"sub": VAPID_EMAIL}
            )
        except WebPushException as e:
            if e.response and e.response.status_code in (404, 410):
                dead.append(sub)
    subscriptions = [s for s in subscriptions if s not in dead]

def check_amo():
    global next_manager_idx, processed_ids, recent_leads
    if not AMO_TOKEN:
        return
    try:
        since = int(time.time()) - 120
        data = amo_get(BASE + "/api/v4/leads", {
            "limit": 50,
            "filter[created_at][from]": since,
            "order[created_at]": "asc"
        })
        leads = data.get("_embedded", {}).get("leads", [])
        for lead in leads:
            lid = lead["id"]
            if lid in processed_ids:
                continue
            processed_ids.add(lid)
            channel = get_field(lead, CHANNEL_FIELD_ID)
            if channel.lower() not in TARGET_VALUES:
                continue

            # Назначаем менеджера по round-robin
            manager = MANAGERS[next_manager_idx % len(MANAGERS)]
            next_manager_idx = (next_manager_idx + 1) % len(MANAGERS)
            try:
                amo_patch(BASE + "/api/v4/leads/" + str(lid), {"responsible_user_id": manager["id"]})
            except Exception as e:
                print("Ошибка назначения:", e)

            city    = get_field(lead, CITY_FIELD_ID) or "—"
            name    = lead.get("name") or "Без названия"
            price   = lead.get("price") or 0
            created = datetime.fromtimestamp(lead.get("created_at", 0)).strftime("%d.%m %H:%M")

            lead_info = {
                "id": lid,
                "name": name,
                "channel": channel,
                "city": city,
                "price": price,
                "manager": manager["name"],
                "created": created,
                "url": "https://" + AMO_DOMAIN + "/leads/detail/" + str(lid)
            }
            recent_leads.insert(0, lead_info)
            recent_leads = recent_leads[:50]

            # Отправляем push всем подписанным устройствам
            body = channel + " · " + city + "\n👤 " + manager["name"]
            if price:
                body += " · " + str(price) + " р."
            send_push_all("🔔 Новый лид: " + name, body, lead_info)
            print("[" + created + "] Новый лид #" + str(lid) + " → " + manager["name"])

        save_state()
    except Exception as e:
        print("Ошибка проверки AmoCRM:", e)

# Запускаем планировщик
load_state()
scheduler = BackgroundScheduler()
scheduler.add_job(check_amo, "interval", seconds=60)
scheduler.start()

# ========== API endpoints ==========

@app.get("/")
def root():
    return {"status": "ok", "leads": len(recent_leads)}

@app.get("/api/leads")
def get_leads():
    return {"leads": recent_leads}

@app.get("/api/vapid-public")
def get_vapid_public():
    return {"key": VAPID_PUBLIC}

@app.post("/api/subscribe")
async def subscribe(request: Request):
    sub = await request.json()
    if sub not in subscriptions:
        subscriptions.append(sub)
    return {"ok": True, "total": len(subscriptions)}

@app.delete("/api/subscribe")
async def unsubscribe(request: Request):
    sub = await request.json()
    if sub in subscriptions:
        subscriptions.remove(sub)
    return {"ok": True}

@app.get("/api/managers")
def get_managers():
    return {"managers": MANAGERS, "next": next_manager_idx % len(MANAGERS)}
