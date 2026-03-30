from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from pywebpush import webpush, WebPushException
from apscheduler.schedulers.background import BackgroundScheduler
import requests as req
import json
import os
import time
from datetime import datetime

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# ========== НАСТРОЙКИ ==========
AMO_DOMAIN       = os.getenv("AMO_DOMAIN", "etalongroup.amocrm.ru")
AMO_TOKEN        = os.getenv("AMO_TOKEN", "")
CHANNEL_FIELD_ID = 582110
CITY_FIELD_ID    = 575680
SOURCE_FIELD_ID  = 582898
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

# Загружаем менеджеров для архива
def load_user_map():
    try:
        data = req.get(BASE + "/api/v4/users", headers=AMO_HEADERS, params={"limit": 250}, timeout=15).json()
        return {u["id"]: u["name"] for u in data.get("_embedded", {}).get("users", [])}
    except:
        return {}

user_map = {}

subscriptions    = []
processed_ids    = set()
next_manager_idx = 0
# Хранилище лидов: ключ = номер телефона (или id если телефона нет)
# Значение = последний лид с этим телефоном
leads_by_phone = {}  # phone -> lead_info

def get_leads_list():
    """Возвращает дедуплицированный список лидов, отсортированный по дате (новые первые)"""
    seen = {}
    for phone, lead in leads_by_phone.items():
        seen[phone] = lead
    return sorted(seen.values(), key=lambda x: x.get("created_ts", 0), reverse=True)

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

def normalize_phone(phone):
    """Нормализуем телефон для сравнения — только цифры"""
    if not phone or phone == "—":
        return None
    digits = "".join(c for c in phone if c.isdigit())
    if len(digits) >= 10:
        return digits[-10:]  # последние 10 цифр
    return None

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
        except Exception as e:
            print("Push error:", e)
    subscriptions = [s for s in subscriptions if s not in dead]

def check_amo():
    global next_manager_idx, processed_ids, leads_by_phone
    if not AMO_TOKEN:
        return
    try:
        # Только сегодняшние сделки — с начала текущего дня (00:00 МСК = UTC-3)
        from datetime import date
        today_start = int(datetime.combine(date.today(), datetime.min.time()).timestamp())
        # Небольшой запас — берём с вчера 20:00 UTC чтобы не пропустить ночные заявки
        since = today_start - 4 * 3600

        all_leads = []
        for page in range(1, 5):
            data = amo_get(BASE + "/api/v4/leads", {
                "limit": 250,
                "page": page,
                "order[id]": "desc",
                "with": "contacts",
                "filter[created_at][from]": since
            })
            page_leads = data.get("_embedded", {}).get("leads", [])
            if not page_leads:
                break
            all_leads.extend(page_leads)
            if len(page_leads) < 250:
                break

        print("Проверка: получено " + str(len(all_leads)) + " сделок за сегодня")

        new_count = 0
        for lead in all_leads:
            lid = lead["id"]
            if lid in processed_ids:
                continue
            processed_ids.add(lid)

            channel = get_field(lead, CHANNEL_FIELD_ID)
            source  = get_field(lead, SOURCE_FIELD_ID)
            is_pr     = channel.lower() in TARGET_VALUES
            is_tilda  = "тильда" in source.lower()
            if not is_pr and not is_tilda:
                continue

            manager = MANAGERS[next_manager_idx % len(MANAGERS)]
            next_manager_idx = (next_manager_idx + 1) % len(MANAGERS)

            city    = get_field(lead, CITY_FIELD_ID) or "—"
            name    = lead.get("name") or "Без названия"
            price   = lead.get("price") or 0
            created_ts = lead.get("created_at", 0)
            created = datetime.fromtimestamp(created_ts).strftime("%d.%m %H:%M") if created_ts else "—"

            # Получаем телефон
            phone_raw = "—"
            try:
                contacts = lead.get("_embedded", {}).get("contacts", [])
                if contacts:
                    contact_id = contacts[0]["id"]
                    contact_data = amo_get(BASE + "/api/v4/contacts/" + str(contact_id))
                    for f in (contact_data.get("custom_fields_values") or []):
                        if f.get("field_code") == "PHONE":
                            vals = f.get("values", [])
                            if vals:
                                phone_raw = vals[0].get("value", "—")
                            break
            except Exception as e:
                print("Ошибка получения телефона:", e)

            lead_info = {
                "id": lid,
                "name": name,
                "channel": channel,
                "city": city,
                "price": price,
                "phone": phone_raw,
                "source": source or "—",
                "manager": manager["name"],
                "created": created,
                "created_ts": created_ts,
                "url": "https://" + AMO_DOMAIN + "/leads/detail/" + str(lid)
            }

            # Дедупликация по телефону — оставляем последнюю сделку
            phone_key = normalize_phone(phone_raw)
            if phone_key:
                existing = leads_by_phone.get(phone_key)
                if existing and existing.get("created_ts", 0) > created_ts:
                    # Уже есть более новая сделка с этим телефоном — пропускаем
                    continue
                leads_by_phone[phone_key] = lead_info
            else:
                # Нет телефона — используем ID как ключ
                leads_by_phone["id_" + str(lid)] = lead_info

            new_count += 1

            # Push только для действительно новых лидов (за последние 2 минуты)
            if created_ts and (time.time() - created_ts) < 120:
                body_text = channel + " · " + city + "\n👤 " + manager["name"]
                if phone_raw and phone_raw != "—":
                    body_text += "\n📞 " + phone_raw
                send_push_all("🔔 Новый лид: " + name, body_text, lead_info)
                print("[" + created + "] Новый лид #" + str(lid) + " → " + manager["name"])

        if new_count:
            print("Добавлено/обновлено лидов: " + str(new_count))

    except Exception as e:
        print("Ошибка проверки AmoCRM:", e)

# Запускаем планировщик
user_map.update(load_user_map())
scheduler = BackgroundScheduler()
scheduler.add_job(check_amo, "interval", seconds=60, max_instances=1)
scheduler.start()
check_amo()  # первый запуск сразу

# ========== API ==========

@app.get("/")
def root():
    return {"status": "ok", "leads": len(leads_by_phone)}

@app.get("/api/leads")
def get_leads():
    return {"leads": get_leads_list()}

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

@app.get("/api/ping")
def ping():
    return {"ok": True}

@app.get("/api/archive")
def get_archive():
    """Лиды за последние 60 дней — загружается один раз при открытии архива"""
    try:
        since_60d = int(time.time()) - 60 * 86400
        all_leads = []
        for page in range(1, 10):
            data = amo_get(BASE + "/api/v4/leads", {
                "limit": 250,
                "page": page,
                "order[id]": "desc",
                "with": "contacts",
                "filter[created_at][from]": since_60d
            })
            page_leads = data.get("_embedded", {}).get("leads", [])
            if not page_leads:
                break
            all_leads.extend(page_leads)
            if len(page_leads) < 250:
                break

        result = []
        seen_phones = {}
        for lead in all_leads:
            channel = get_field(lead, CHANNEL_FIELD_ID)
            source  = get_field(lead, SOURCE_FIELD_ID)
            is_pr    = channel.lower() in TARGET_VALUES
            is_tilda = "тильда" in source.lower()
            if not is_pr and not is_tilda:
                continue

            city    = get_field(lead, CITY_FIELD_ID) or "—"
            name    = lead.get("name") or "Без названия"
            price   = lead.get("price") or 0
            created_ts = lead.get("created_at", 0)
            created = datetime.fromtimestamp(created_ts).strftime("%d.%m %H:%M") if created_ts else "—"

            phone_raw = "—"
            try:
                contacts = lead.get("_embedded", {}).get("contacts", [])
                if contacts:
                    contact_id = contacts[0]["id"]
                    contact_data = amo_get(BASE + "/api/v4/contacts/" + str(contact_id))
                    for f in (contact_data.get("custom_fields_values") or []):
                        if f.get("field_code") == "PHONE":
                            vals = f.get("values", [])
                            if vals:
                                phone_raw = vals[0].get("value", "—")
                            break
            except:
                pass

            lead_info = {
                "id": lead["id"],
                "name": name,
                "channel": channel,
                "city": city,
                "price": price,
                "phone": phone_raw,
                "source": source or "—",
                "manager": user_map.get(lead.get("responsible_user_id"), "—"),
                "created": created,
                "created_ts": created_ts,
                "url": "https://" + AMO_DOMAIN + "/leads/detail/" + str(lead["id"])
            }

            # Дедупликация по телефону
            phone_key = normalize_phone(phone_raw)
            if phone_key:
                existing = seen_phones.get(phone_key)
                if existing and existing.get("created_ts", 0) > created_ts:
                    continue
                seen_phones[phone_key] = lead_info
            else:
                seen_phones["id_" + str(lead["id"])] = lead_info

        return {"leads": sorted(seen_phones.values(), key=lambda x: x.get("created_ts", 0), reverse=True)}
    except Exception as e:
        return {"leads": [], "error": str(e)}
