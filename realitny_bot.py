#!/usr/bin/env python3
"""
Realitný odhadovací chatbot – widget na vloženie na webstránku

Ako to funguje:
  1. Flask backend beží (lokálne na test, neskôr na hostingu).
  2. Na svoj web vložíš jeden riadok:
        <script src="http://TVOJA_DOMENA/widget.js"></script>
     Ten do stránky vloží plávajúcu chat bublinu vpravo dole.
  3. Klient prejde krátkym dotazníkom (typ nehnuteľnosti, lokalita,
     plocha, izby, stav...), dostane orientačný cenový odhad.
  4. Po zobrazení odhadu sa appka spýta na email/telefón. Ak ho zadá,
     uloží sa ako "lead" do leads.csv, aby si ho mohol/a neskôr osloviť.
  5. Leady si pozrieš na /admin?password=TVOJE_HESLO

⚠️ DÔLEŽITÉ: Aby to fungovalo na reálnom webe, tento skript musí bežať
NEPRETRŽITE na serveri (hostingu) – nie len na tvojom počítači. Na lokálne
testovanie stačí spustiť tak ako doteraz.

SPUSTENIE:
    pip install flask anthropic --break-system-packages

    export ANTHROPIC_API_KEY="tvoj-kluc"
    export ADMIN_PASSWORD="tvoje-tajne-heslo"      (pre /admin)
    export SMTP_USER="tvoj.email@gmail.com"        (pre emailové notifikácie o leadoch)
    export SMTP_PASSWORD="app-heslo-z-google-uctu"  (NIE bežné heslo do Gmailu, pozri nižšie)

    python realitny_bot.py

Potom na test otvor: http://127.0.0.1:5000/demo
(demo.html predvádza, ako widget vyzerá vložený na stránke)

AKO ZÍSKAŤ "APP PASSWORD" PRE GMAIL (potrebné pre odosielanie notifikácií):
    1. Na Gmail účte, z ktorého chceš notifikácie posielať, zapni dvojfaktorové
       overenie (2-Step Verification) v myaccount.google.com/security
    2. Choď na myaccount.google.com/apppasswords
    3. Vytvor nové "app password" (heslo pre appky) - Google ti vygeneruje
       16-znakové heslo, ktoré použiješ ako SMTP_PASSWORD (nie svoje bežné heslo!)
    4. SMTP_USER je email tej istej schránky, cez ktorú budeš odosielať
    Notifikácie chodia na bagona@gmail.com (dá sa zmeniť v premennej NOTIFY_EMAIL_TO).
    Ak SMTP_USER/SMTP_PASSWORD nenastavíš, appka funguje ďalej normálne,
    len sa neposlú emailové notifikácie (lead sa aj tak uloží do leads.csv).
"""

import os
import csv
import uuid
import re
import smtplib
from email.mime.text import MIMEText
from datetime import datetime
from flask import Flask, request, jsonify, render_template_string, Response

app = Flask(__name__)

LEADS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "leads.csv")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "zmen-ma")

# --- Emailová notifikácia o novom leade ---
NOTIFY_EMAIL_TO = "bagona@gmail.com"
SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER")          # odosielajúci účet, napr. tvoj Gmail
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD")  # Gmail "App Password", nie bežné heslo!

# ---------------------------------------------------------------------------
# CENOVÁ TABUĽKA (orientačné priemery €/m2 pre byty, kraj. mestá, 2026)
# ⚠️ Toto sú len približné hodnoty na demonštráciu. Odporúčam ich pravidelne
# aktualizovať podľa aktuálnych dát (NBS, ŠÚ SR, realitné portály).
# ---------------------------------------------------------------------------

CITY_PRICE_PER_M2 = {
    "bratislava": 3500, "košice": 3000, "kosice": 3000, "trnava": 2400,
    "trenčín": 2000, "trencin": 2000, "žilina": 2200, "zilina": 2200,
    "banská bystrica": 2000, "banska bystrica": 2000, "nitra": 2100,
    "prešov": 2000, "presov": 2000, "poprad": 2600, "vysoké tatry": 3000,
}
DEFAULT_PRICE_PER_M2 = 1600  # pre mestá/obce mimo zoznamu

# Mestské časti/štvrte - cena sa v rámci jedného mesta môže líšiť aj o desiatky %.
# Máme dostatočne spoľahlivé dáta len pre Bratislavu a Košice; pri ostatných
# mestách sa mestská časť zbiera len informačne (do lead-u), bez vplyvu na cenu.
DISTRICT_MULTIPLIER = {
    "bratislava": {
        "staré mesto": 1.35, "ružinov": 1.05, "nové mesto": 1.05, "karlova ves": 1.00,
        "dúbravka": 0.95, "petržalka": 0.90, "rača": 0.95, "vajnory": 0.90,
        "devín": 1.10, "devínska nová ves": 0.95, "záhorská bystrica": 0.90,
        "vrakuňa": 0.85, "podunajské biskupice": 0.85,
        "jarovce": 0.85, "rusovce": 0.85, "čunovo": 0.85,
    },
    "košice": {
        "staré mesto": 1.15, "sever": 1.00, "juh": 0.95, "západ": 0.90,
        "ťahanovce": 0.95, "dargovských hrdinov": 0.90, "krásna": 0.85, "kvp": 0.95,
    },
}


CONDITION_MULTIPLIER = {
    "novostavba": 1.15,
    "po rekonštrukcii": 1.05,
    "pôvodný dobrý stav": 1.00,
    "potrebuje rekonštrukciu": 0.85,
}

TYPE_MULTIPLIER = {"byt": 1.0, "dom": 0.9, "pozemok": 0.15}  # pozemok = hrubý odhad!

BALCONY_MULTIPLIER = {"áno": 1.03, "ano": 1.03, "nie": 1.00}
GROUND_FLOOR_PENALTY = 0.95  # prízemie je zväčša mierne menej žiadané

HOUSE_TYPE_MULTIPLIER = {
    "bungalov (prízemný)": 1.03, "bungalov": 1.03,
    "poschodový": 1.00, "poschodovy": 1.00,
}
ROOF_TYPE_MULTIPLIER = {
    "šikmá strecha": 1.02, "sikma strecha": 1.02,
    "rovná strecha": 0.98, "rovna strecha": 0.98,
}

# Doplnky sa cenovo pripočítavajú ako paušálna suma (nie percento), keďže
# napr. bazén nezvyšuje hodnotu úmerne k veľkosti domu. Orientačné hodnoty.
AMENITY_PRICE_BONUS = {
    "bazén": 8000, "bazen": 8000,
    "sauna": 3000,
    "samostatná garáž": 6000, "samostatna garaz": 6000,
    "krb": 1500,
    "klimatizácia": 1000, "klimatizacia": 1000,
}


LAND_PRICE_RATIO = 0.15  # pozemok pri dome / stavebný pozemok = podiel z ceny/m2 mesta

# Typ pozemku - relatívne k stavebnému pozemku (=1.0). Orná pôda a les majú
# oveľa nižšiu trhovú hodnotu ako pozemok určený na výstavbu.
LAND_TYPE_MULTIPLIER = {
    "stavebný pozemok": 1.00, "stavebny pozemok": 1.00,
    "orná pôda": 0.10, "orna poda": 0.10,
    "záhrada": 0.45, "zahrada": 0.45,
    "rekreačný pozemok": 0.70, "rekreacny pozemok": 0.70,
    "lesný pozemok": 0.05, "lesny pozemok": 0.05,
    "iný/neviem": 1.00, "iny/neviem": 1.00,
}

# Inžinierske siete - každá pripojená sieť pridáva k hodnote pozemku pár %.
UTILITY_BONUS = {
    "elektrina": 0.05, "voda": 0.05, "plyn": 0.03,
    "kanalizácia": 0.05, "kanalizacia": 0.05,
}

ACCESS_ROAD_PENALTY = 0.90  # ak k pozemku nevedie prístupová cesta


def estimate_price(property_type: str, city: str, district: str, area: float,
                    condition: str, floor: str = "", balcony: str = "",
                    house_type: str = "", roof_type: str = "", amenities: str = "",
                    land_area: float = 0, land_type: str = "", utilities: str = "",
                    access_road: str = "") -> tuple[int, int]:
    city_key = city.strip().lower()
    base = CITY_PRICE_PER_M2.get(city_key, DEFAULT_PRICE_PER_M2)

    district_key = (district or "").strip().lower()
    district_mult = DISTRICT_MULTIPLIER.get(city_key, {}).get(district_key, 1.0)

    # --- samostatný pozemok: úplne samostatná logika ---
    if property_type == "pozemok":
        land_type_mult = LAND_TYPE_MULTIPLIER.get((land_type or "").strip().lower(), 1.0)

        utilities_bonus = 0.0
        for item in (utilities or "").split(","):
            utilities_bonus += UTILITY_BONUS.get(item.strip().lower(), 0.0)

        access_mult = ACCESS_ROAD_PENALTY if (access_road or "").strip().lower() in ("nie", "no") else 1.0

        price_per_m2 = base * district_mult * LAND_PRICE_RATIO * land_type_mult * (1 + utilities_bonus) * access_mult
        total = price_per_m2 * area
        low = round(total * 0.9, -2)
        high = round(total * 1.1, -2)
        return int(low), int(high)

    # --- byt / dom ---
    type_mult = TYPE_MULTIPLIER.get(property_type, 1.0)
    cond_mult = CONDITION_MULTIPLIER.get(condition, 1.0)

    floor_mult = 1.0
    if property_type == "byt" and floor.strip() in ("0", "prízemie", "prizemie"):
        floor_mult = GROUND_FLOOR_PENALTY

    balcony_mult = BALCONY_MULTIPLIER.get(balcony.strip().lower(), 1.0)

    house_type_mult = 1.0
    roof_type_mult = 1.0
    amenity_bonus = 0
    land_value = 0
    if property_type == "dom":
        house_type_mult = HOUSE_TYPE_MULTIPLIER.get(house_type.strip().lower(), 1.0)
        roof_type_mult = ROOF_TYPE_MULTIPLIER.get(roof_type.strip().lower(), 1.0)
        for item in (amenities or "").split(","):
            amenity_bonus += AMENITY_PRICE_BONUS.get(item.strip().lower(), 0)
        # Hodnota domu (podľa zastavanej/úžitkovej plochy) + hodnota pozemku (samostatne)
        land_price_per_m2 = base * district_mult * LAND_PRICE_RATIO
        land_value = land_price_per_m2 * (land_area or 0)

    price_per_m2 = (base * district_mult * type_mult * cond_mult * floor_mult
                     * balcony_mult * house_type_mult * roof_type_mult)
    total = price_per_m2 * area + amenity_bonus + land_value
    low = round(total * 0.9, -2)
    high = round(total * 1.1, -2)
    return int(low), int(high)



# ---------------------------------------------------------------------------
# AI ZHRNUTIE (voliteľné - pekne sformuluje odhad; ak zlyhá, použije šablónu)
# ---------------------------------------------------------------------------

def generate_summary(answers: dict, low: int, high: int) -> str:
    fallback = (
        f"Na základe zadaných údajov odhadujeme hodnotu vašej nehnuteľnosti "
        f"na {low:,} € – {high:,} €. Ide o orientačný odhad, presnú hodnotu "
        f"vie stanoviť len obhliadka na mieste."
    ).replace(",", " ")

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
        prompt = (
            "Si realitný asistent. Na základe týchto údajov o nehnuteľnosti napíš "
            "krátke (3-4 vety), priateľské zhrnutie cenového odhadu v slovenčine. "
            "Spomeň rozpätie ceny a jemne pripomeň, že presnú hodnotu vie stanoviť "
            "len maklér po obhliadke, a že sa mu čoskoro ozveme.\n\n"
            f"Údaje: {answers}\nOdhad: {low} € - {high} €"
        )
        resp = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(b.text for b in resp.content if hasattr(b, "text"))
        return text.strip() or fallback
    except Exception:
        return fallback


# ---------------------------------------------------------------------------
# STAVOVÝ AUTOMAT ROZHOVORU
# ---------------------------------------------------------------------------

SESSIONS: dict[str, dict] = {}  # session_id -> {"step": int, "answers": {...}}

STEPS = [
    {"key": "property_type", "q": "Ahoj! 👋 Rád/a vám dám nezáväzný odhad ceny nehnuteľnosti. O aký typ nehnuteľnosti ide?",
     "type": "buttons", "options": ["Byt", "Dom", "Pozemok"]},
    {"key": "city", "q": "V akom meste alebo obci sa nehnuteľnosť nachádza?", "type": "text"},
    {"key": "district", "q": "V ktorej mestskej časti / štvrti? (ak neviete alebo je to menšia obec bez častí, napíšte 'neviem')",
     "type": "text"},
    {"key": "street", "q": "Na akej ulici? (nepovinné, môžete napísať 'preskočiť')", "type": "text"},
    {"key": "area", "q": lambda a: ("Aká je plocha pozemku (parcely) v m²?" if a.get("property_type") == "Pozemok"
                                     else "Aká je približná úžitková/zastavaná plocha v m²?"),
     "type": "number"},
    {"key": "land_area", "q": "Aká je plocha pozemku (celého parcely) v m²?", "type": "number",
     "skip_if": lambda a: a.get("property_type") != "Dom"},
    {"key": "rooms", "q": "Koľko izieb má nehnuteľnosť?", "type": "number",
     "skip_if": lambda a: a.get("property_type") == "Pozemok"},
    {"key": "house_type", "q": "Je dom bungalov (prízemný) alebo poschodový?", "type": "buttons",
     "options": ["Bungalov (prízemný)", "Poschodový"],
     "skip_if": lambda a: a.get("property_type") != "Dom"},
    {"key": "roof_type", "q": "Aký typ strechy má dom?", "type": "buttons",
     "options": ["Rovná strecha", "Šikmá strecha"],
     "skip_if": lambda a: a.get("property_type") != "Dom"},
    {"key": "amenities", "q": "Má nehnuteľnosť niektoré z týchto doplnkov? (vyberte všetko, čo platí)",
     "type": "multiselect",
     "options": ["Bazén", "Sauna", "Samostatná garáž", "Krb", "Klimatizácia"],
     "skip_if": lambda a: a.get("property_type") != "Dom"},
    {"key": "land_type", "q": "Aký je typ pozemku?", "type": "buttons",
     "options": ["Stavebný pozemok", "Orná pôda", "Záhrada", "Rekreačný pozemok", "Lesný pozemok", "Iný/neviem"],
     "skip_if": lambda a: a.get("property_type") != "Pozemok"},
    {"key": "utilities", "q": "Aké inžinierske siete sú na pozemku dostupné? (vyberte všetko, čo platí)",
     "type": "multiselect",
     "options": ["Elektrina", "Voda", "Plyn", "Kanalizácia"],
     "skip_if": lambda a: a.get("property_type") != "Pozemok"},
    {"key": "access_road", "q": "Vedie k pozemku prístupová cesta?", "type": "buttons",
     "options": ["Áno", "Nie"],
     "skip_if": lambda a: a.get("property_type") != "Pozemok"},
    {"key": "floor", "q": "Na ktorom poschodí sa byt nachádza? (napíšte 0 pre prízemie)", "type": "text",
     "skip_if": lambda a: a.get("property_type") != "Byt"},
    {"key": "condition", "q": "V akom je stave?", "type": "buttons",
     "options": ["Novostavba", "Po rekonštrukcii", "Pôvodný dobrý stav", "Potrebuje rekonštrukciu"],
     "skip_if": lambda a: a.get("property_type") == "Pozemok"},
    {"key": "balcony", "q": "Má balkón alebo terasu?", "type": "buttons", "options": ["Áno", "Nie"],
     "skip_if": lambda a: a.get("property_type") == "Pozemok"},
]

CONTACT_STEP_KEY = "contact"


def get_active_steps(answers: dict) -> list[dict]:
    return [s for s in STEPS if not s.get("skip_if") or not s["skip_if"](answers)]


def format_question(step: dict, answers: dict) -> dict:
    q = step["q"]
    text = q(answers) if callable(q) else q
    return {"message": text, "input_type": step["type"], "options": step.get("options", [])}


@app.route("/api/chat/start", methods=["POST"])
def chat_start():
    session_id = str(uuid.uuid4())
    SESSIONS[session_id] = {"step": 0, "answers": {}}
    first_step = get_active_steps({})[0]
    return jsonify({"session_id": session_id, **format_question(first_step, {}), "done": False})


@app.route("/api/chat/message", methods=["POST"])
def chat_message():
    data = request.get_json(force=True)
    session_id = data.get("session_id")
    user_answer = (data.get("answer") or "").strip()

    session = SESSIONS.get(session_id)
    if not session:
        return jsonify({"error": "Relácia expirovala, obnov stránku."}), 400

    answers = session["answers"]
    active_steps = get_active_steps(answers)
    step_idx = session["step"]

    # --- fáza: zbieranie kontaktu (po zobrazení hrubého odhadu) ---
    if session.get("awaiting_contact"):
        low = session.get("estimate_low")
        high = session.get("estimate_high")
        if user_answer.lower() not in ("preskočiť", "preskocit", "nie", "skip", ""):
            answers[CONTACT_STEP_KEY] = user_answer
            summary = generate_summary(answers, low, high)
            save_lead(answers, low, high)
            return jsonify({
                "message": f"Ďakujeme! 🙌\n\n{summary}\n\nOzveme sa vám čo najskôr s bezplatnou konzultáciou.",
                "input_type": "none", "options": [], "done": True,
            })
        else:
            return jsonify({
                "message": "V poriadku, ak si to rozmyslíte, sme tu pre vás. Pekný deň! 👋",
                "input_type": "none", "options": [], "done": True,
            })

    # --- validácia aktuálnej odpovede ---
    current_step = active_steps[step_idx]
    skip_words = ("preskočiť", "preskocit", "neviem", "-", "nie", "skip")
    optional_keys = ("district", "street")

    if current_step["type"] == "number":
        cleaned = re.sub(r"[^\d.,]", "", user_answer).replace(",", ".")
        if not cleaned:
            return jsonify({"message": "Prosím zadajte číslo.", "input_type": "number",
                             "options": [], "done": False})
        answers[current_step["key"]] = float(cleaned)
    else:
        if not user_answer and current_step["key"] not in optional_keys:
            return jsonify({"message": "Prosím vyberte alebo napíšte odpoveď.",
                             "input_type": current_step["type"],
                             "options": current_step.get("options", []), "done": False})
        if current_step["key"] in optional_keys and user_answer.lower() in skip_words:
            answers[current_step["key"]] = ""
        else:
            answers[current_step["key"]] = user_answer

    # --- ďalší krok ---
    active_steps = get_active_steps(answers)  # môže sa zmeniť po zadaní typu
    step_idx += 1
    session["step"] = step_idx

    if step_idx < len(active_steps):
        next_step = active_steps[step_idx]
        return jsonify({**format_question(next_step, answers), "done": False})

    # --- všetky otázky zodpovedané -> vypočítaj odhad ---
    property_type = answers.get("property_type", "Byt").lower()
    city = answers.get("city", "")
    district = answers.get("district", "")
    area = float(answers.get("area", 0) or 0)
    land_area = float(answers.get("land_area", 0) or 0)
    condition = answers.get("condition", "pôvodný dobrý stav").lower()
    floor = str(answers.get("floor", ""))
    balcony = answers.get("balcony", "")
    house_type = answers.get("house_type", "")
    roof_type = answers.get("roof_type", "")
    amenities = answers.get("amenities", "")
    land_type = answers.get("land_type", "")
    utilities = answers.get("utilities", "")
    access_road = answers.get("access_road", "")

    low, high = estimate_price(property_type, city, district, area, condition,
                                floor, balcony, house_type, roof_type, amenities, land_area,
                                land_type, utilities, access_road)

    session["estimate_low"] = low
    session["estimate_high"] = high
    session["awaiting_contact"] = True

    message = (
        f"Na základe zadaných údajov je orientačný odhad hodnoty vašej nehnuteľnosti "
        f"{low:,} € – {high:,} €.\n\n"
        f"Chcete presnejší odhad s podrobným vysvetlením? Stačí zanechať email alebo "
        f"telefón (alebo napíšte 'preskočiť')."
    ).replace(",", " ")
    return jsonify({"message": message, "input_type": "text", "options": [], "done": False})


# ---------------------------------------------------------------------------
# UKLADANIE LEADOV
# ---------------------------------------------------------------------------

def save_lead(answers: dict, low, high) -> None:
    file_exists = os.path.exists(LEADS_FILE)
    with open(LEADS_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow(["timestamp", "typ", "mesto", "mestska_cast", "ulica", "plocha_m2",
                              "plocha_pozemku_m2", "izby", "typ_domu", "strecha", "doplnky",
                              "typ_pozemku", "siete", "pristupova_cesta",
                              "poschodie", "stav", "balkon_terasa", "odhad_od", "odhad_do", "kontakt"])
        writer.writerow([
            datetime.now().isoformat(timespec="seconds"),
            answers.get("property_type", ""), answers.get("city", ""),
            answers.get("district", ""), answers.get("street", ""),
            answers.get("area", ""), answers.get("land_area", ""), answers.get("rooms", ""),
            answers.get("house_type", ""), answers.get("roof_type", ""), answers.get("amenities", ""),
            answers.get("land_type", ""), answers.get("utilities", ""), answers.get("access_road", ""),
            answers.get("floor", ""), answers.get("condition", ""), answers.get("balcony", ""),
            low, high, answers.get(CONTACT_STEP_KEY, ""),
        ])

    send_lead_notification(answers, low, high)


def send_lead_notification(answers: dict, low: int, high: int) -> None:
    """Pošle email na NOTIFY_EMAIL_TO o novom leade. Ak nie je nastavené
    SMTP prihlásenie, len sa to potichu preskočí (appka aj tak ďalej funguje,
    lead ostáva uložený v leads.csv)."""
    if not SMTP_USER or not SMTP_PASSWORD:
        print("[upozornenie] SMTP_USER/SMTP_PASSWORD nie sú nastavené - email notifikácia sa neposlala.")
        return

    body = (
        f"Nový lead z realitného chatbota!\n\n"
        f"Typ nehnuteľnosti: {answers.get('property_type', '')}\n"
        f"Mesto: {answers.get('city', '')}\n"
        f"Mestská časť: {answers.get('district', '')}\n"
        f"Ulica: {answers.get('street', '')}\n"
        f"Plocha: {answers.get('area', '')} m2\n"
        f"Plocha pozemku: {answers.get('land_area', '')} m2\n"
        f"Izby: {answers.get('rooms', '')}\n"
        f"Stav: {answers.get('condition', '')}\n"
        f"Odhad: {low:,} € - {high:,} €\n\n".replace(",", " ") +
        f"Kontakt: {answers.get(CONTACT_STEP_KEY, '')}\n"
    )

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = f"🏠 Nový lead: {answers.get('city', '')} - {low:,} - {high:,} €".replace(",", " ")
    msg["From"] = SMTP_USER
    msg["To"] = NOTIFY_EMAIL_TO

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.sendmail(SMTP_USER, [NOTIFY_EMAIL_TO], msg.as_string())
    except Exception as e:
        print(f"[chyba] Nepodarilo sa odoslať emailovú notifikáciu: {e}")


# ---------------------------------------------------------------------------
# WIDGET (embed script + chat UI v iframe)
# ---------------------------------------------------------------------------

WIDGET_JS = """
(function() {
  var scriptTag = document.currentScript;
  var origin = new URL(scriptTag.src).origin;

  var bubble = document.createElement('div');
  bubble.innerHTML = '🏠';
  bubble.style.cssText = 'position:fixed;bottom:20px;right:20px;width:60px;height:60px;'
    + 'border-radius:50%;background:#7c9cff;display:flex;align-items:center;justify-content:center;'
    + 'font-size:28px;cursor:pointer;box-shadow:0 4px 16px rgba(0,0,0,0.25);z-index:999999;';
  document.body.appendChild(bubble);

  var teaser = document.createElement('div');
  teaser.textContent = 'Chcete vedieť nezáväzný odhad ceny vašej nehnuteľnosti? 💬';
  teaser.style.cssText = 'position:fixed;bottom:32px;right:90px;max-width:220px;background:#171a21;'
    + 'color:#e8e9ec;padding:10px 14px;border-radius:10px;font-family:sans-serif;font-size:13px;'
    + 'box-shadow:0 4px 16px rgba(0,0,0,0.25);z-index:999999;cursor:pointer;';
  document.body.appendChild(teaser);
  setTimeout(function() { teaser.style.display = 'none'; }, 12000);

  var iframe = null;

  function openChat() {
    teaser.style.display = 'none';
    if (iframe) { iframe.style.display = 'block'; return; }
    iframe = document.createElement('iframe');
    iframe.src = origin + '/chat';
    iframe.style.cssText = 'position:fixed;bottom:90px;right:20px;width:340px;height:480px;'
      + 'border:none;border-radius:14px;box-shadow:0 8px 30px rgba(0,0,0,0.35);z-index:999999;'
      + 'max-width:90vw;max-height:70vh;';
    document.body.appendChild(iframe);
  }

  bubble.onclick = openChat;
  teaser.onclick = openChat;
})();
"""


@app.route("/widget.js")
def widget_js():
    return Response(WIDGET_JS, mimetype="application/javascript")


CHAT_PAGE = """
<!DOCTYPE html>
<html lang="sk">
<head>
<meta charset="UTF-8">
<style>
  * { box-sizing: border-box; }
  body { margin:0; font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
    background:#171a21; color:#e8e9ec; height:100vh; display:flex; flex-direction:column; }
  .header { background:#1d2029; padding:12px 14px; font-weight:600; font-size:0.95rem;
    border-bottom:1px solid #2a2e38; }
  .messages { flex:1; overflow-y:auto; padding:12px; display:flex; flex-direction:column; gap:10px; }
  .msg { max-width:85%; padding:9px 12px; border-radius:12px; font-size:0.85rem; line-height:1.4;
    white-space:pre-wrap; }
  .bot { background:#23283a; align-self:flex-start; border-bottom-left-radius:2px; }
  .user { background:#7c9cff; color:#0f1115; align-self:flex-end; border-bottom-right-radius:2px; }
  .typing { background:#23283a; align-self:flex-start; border-bottom-left-radius:2px;
    padding:10px 14px; display:flex; gap:7px; align-items:center; font-size:0.8rem; color:#9aa0ab; }
  .typing .dot { width:6px; height:6px; border-radius:50%; background:#8a8f9c;
    animation: blink 1.2s infinite ease-in-out; }
  .typing .dot:nth-child(2) { animation-delay: 0.2s; }
  .typing .dot:nth-child(3) { animation-delay: 0.4s; }
  @keyframes blink { 0%, 80%, 100% { opacity: 0.3; } 40% { opacity: 1; } }
  .options { display:flex; flex-wrap:wrap; gap:6px; padding:0 12px 8px; }
  .opt-btn { background:#23283a; border:1px solid #2a2e38; color:#e8e9ec; padding:7px 12px;
    border-radius:8px; cursor:pointer; font-size:0.8rem; }
  .opt-btn:hover { border-color:#7c9cff; }
  .opt-btn.selected { border-color:#7c9cff; background:#2a3350; }
  .confirm-btn { background:#7c9cff; color:#0f1115; border:none; border-radius:8px;
    padding:7px 14px; font-weight:600; cursor:pointer; font-size:0.8rem; margin-top:4px; width:100%; }
  .input-row { display:flex; gap:6px; padding:10px; border-top:1px solid #2a2e38; }
  .input-row input { flex:1; background:#12141a; border:1px solid #2a2e38; color:#e8e9ec;
    border-radius:8px; padding:8px 10px; font-size:0.85rem; }
  .input-row button { background:#7c9cff; color:#0f1115; border:none; border-radius:8px;
    padding:8px 14px; font-weight:600; cursor:pointer; font-size:0.85rem; }
</style>
</head>
<body>
  <div class="header">🏠 Odhad ceny nehnuteľnosti</div>
  <div class="messages" id="messages"></div>
  <div class="options" id="options"></div>
  <div class="input-row" id="inputRow">
    <input type="text" id="userInput" placeholder="Napíšte odpoveď...">
    <button onclick="send()">Poslať</button>
  </div>

<script>
let sessionId = null;

function addMsg(text, who) {
  const el = document.createElement('div');
  el.className = 'msg ' + who;
  el.textContent = text;
  document.getElementById('messages').appendChild(el);
  el.scrollIntoView({behavior:'smooth'});
}

function showTyping() {
  const el = document.createElement('div');
  el.className = 'msg typing';
  el.id = 'typingIndicator';
  el.innerHTML = '<span class="typing-text">Pripravujem odpoveď</span>'
    + '<span class="dot"></span><span class="dot"></span><span class="dot"></span>';
  document.getElementById('messages').appendChild(el);
  el.scrollIntoView({behavior:'smooth'});
}

function hideTyping() {
  const el = document.getElementById('typingIndicator');
  if (el) el.remove();
}

function renderOptions(options) {
  const el = document.getElementById('options');
  el.innerHTML = '';
  options.forEach(opt => {
    const btn = document.createElement('button');
    btn.className = 'opt-btn';
    btn.textContent = opt;
    btn.onclick = () => submitAnswer(opt);
    el.appendChild(btn);
  });
}

function renderMultiSelect(options) {
  const el = document.getElementById('options');
  el.innerHTML = '';
  const selected = new Set();
  options.forEach(opt => {
    const btn = document.createElement('button');
    btn.className = 'opt-btn';
    btn.textContent = opt;
    btn.onclick = () => {
      if (selected.has(opt)) { selected.delete(opt); btn.classList.remove('selected'); }
      else { selected.add(opt); btn.classList.add('selected'); }
    };
    el.appendChild(btn);
  });
  const confirmBtn = document.createElement('button');
  confirmBtn.className = 'confirm-btn';
  confirmBtn.textContent = 'Potvrdiť výber';
  confirmBtn.onclick = () => {
    const answer = selected.size > 0 ? Array.from(selected).join(', ') : 'Žiadne';
    submitAnswer(answer);
  };
  el.appendChild(confirmBtn);
}

async function submitAnswer(answer) {
  if (answer) addMsg(answer, 'user');
  document.getElementById('options').innerHTML = '';
  document.getElementById('userInput').value = '';
  showTyping();

  const res = await fetch('/api/chat/message', {
    method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify({session_id: sessionId, answer: answer})
  });
  const data = await res.json();
  hideTyping();
  addMsg(data.message, 'bot');
  if (data.done) {
    document.getElementById('inputRow').style.display = 'none';
    renderRestartButton();
  } else if (data.input_type === 'multiselect') {
    renderMultiSelect(data.options || []);
  } else {
    renderOptions(data.options || []);
  }
}

function renderRestartButton() {
  const el = document.getElementById('options');
  el.innerHTML = '';
  const btn = document.createElement('button');
  btn.className = 'confirm-btn';
  btn.textContent = '🔄 Chcem ďalší odhad';
  btn.onclick = () => {
    document.getElementById('messages').innerHTML = '';
    document.getElementById('options').innerHTML = '';
    document.getElementById('inputRow').style.display = 'flex';
    start();
  };
  el.appendChild(btn);
}

function send() {
  const val = document.getElementById('userInput').value.trim();
  if (!val) return;
  submitAnswer(val);
}
document.getElementById('userInput').addEventListener('keydown', e => { if (e.key === 'Enter') send(); });

async function start() {
  const res = await fetch('/api/chat/start', {method:'POST'});
  const data = await res.json();
  sessionId = data.session_id;
  addMsg(data.message, 'bot');
  if (data.input_type === 'multiselect') {
    renderMultiSelect(data.options || []);
  } else {
    renderOptions(data.options || []);
  }
}
start();
</script>
</body>
</html>
"""


@app.route("/chat")
def chat_page():
    return render_template_string(CHAT_PAGE)


DEMO_PAGE = """
<!DOCTYPE html>
<html lang="sk"><head><meta charset="UTF-8"><title>Demo webstránka</title>
<style>body{font-family:sans-serif;background:#f4f4f4;padding:40px;}
h1{color:#222} p{color:#555;max-width:600px;}</style></head>
<body>
<h1>Realitná kancelária Príklad, s.r.o.</h1>
<p>Toto je ukážková stránka predvádzajúca, ako bude chat bublina vyzerať
vpravo dole, keď ju vložíš na svoj skutočný web pomocou jedného riadku
&lt;script&gt; tagu.</p>
<script src="/widget.js"></script>
</body></html>
"""


@app.route("/demo")
def demo_page():
    return render_template_string(DEMO_PAGE)


# ---------------------------------------------------------------------------
# ADMIN - zoznam leadov
# ---------------------------------------------------------------------------

ADMIN_PAGE = """
<!DOCTYPE html>
<html lang="sk"><head><meta charset="UTF-8"><title>Leady</title>
<style>
body{font-family:sans-serif;background:#171a21;color:#e8e9ec;padding:24px;}
table{border-collapse:collapse;width:100%;}
th,td{border:1px solid #2a2e38;padding:8px 10px;text-align:left;font-size:0.85rem;}
th{background:#1d2029;}
</style></head><body>
<h1>📋 Leady zo stránky</h1>
{{ table|safe }}
</body></html>
"""


@app.route("/admin")
def admin():
    if request.args.get("password") != ADMIN_PASSWORD:
        return "Neplatné heslo. Pridaj do URL ?password=TVOJE_HESLO", 403

    if not os.path.exists(LEADS_FILE):
        return render_template_string(ADMIN_PAGE, table="<p>Zatiaľ žiadne leady.</p>")

    with open(LEADS_FILE, "r", encoding="utf-8") as f:
        reader = list(csv.reader(f))

    if not reader:
        return render_template_string(ADMIN_PAGE, table="<p>Zatiaľ žiadne leady.</p>")

    header, rows = reader[0], reader[1:]
    html = "<table><tr>" + "".join(f"<th>{h}</th>" for h in header) + "</tr>"
    for row in reversed(rows):  # najnovšie navrch
        html += "<tr>" + "".join(f"<td>{c}</td>" for c in row) + "</tr>"
    html += "</table>"

    return render_template_string(ADMIN_PAGE, table=html)


if __name__ == "__main__":
    # Render (a podobné hostingy) nastavujú premennú PORT a spúšťajú appku
    # inak ako pri lokálnom vývoji - tu appka pozná rozdiel a prispôsobí sa.
    is_hosted = "PORT" in os.environ or "RENDER" in os.environ

    if is_hosted:
        port = int(os.environ.get("PORT", 5000))
        app.run(host="0.0.0.0", port=port, debug=False)
    else:
        import threading, time, webbrowser

        def open_browser():
            time.sleep(1.5)
            webbrowser.open("http://127.0.0.1:5000/demo")

        if os.environ.get("WERKZEUG_RUN_MAIN") == "true" or not app.debug:
            threading.Thread(target=open_browser, daemon=True).start()

        app.run(debug=True, port=5000)
