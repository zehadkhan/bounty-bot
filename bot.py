"""
Bounty Watch - live bug bounty monitor that messages you on Telegram.

Watches HackerOne, Bugcrowd, Intigriti, YesWeHack (via arkadiyt/bounty-targets-data,
refreshed by its maintainer roughly every 30 min) and self-hosted programs
(disclose.io diodb). Sends you alerts for:
  - new programs
  - new in-scope assets on existing programs
  - max payout increases
  - programs that closed / disappeared

Telegram commands (only answered in your own chat):
  /status        what is being watched, last check
  /check         run a check right now
  /new [days]    programs first seen in the last N days (default 7)
  /top [n]       highest-paying programs (default 10)
  /search text   find programs by name, domain or asset
  /p name        full details + apply checklist for one program
"""
import hashlib, html, json, logging, os, sqlite3, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import requests

# ---------- config ----------
TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
INTERVAL_MIN = int(os.environ.get("CHECK_INTERVAL_MINUTES", "30"))
MIN_PAYOUT = float(os.environ.get("MIN_PAYOUT", "0"))
ONLY_PAID = os.environ.get("ONLY_PAID", "true").lower() == "true"
PLATFORMS = {p.strip().lower() for p in os.environ.get(
    "PLATFORMS", "hackerone,bugcrowd,intigriti,yeswehack,self-hosted").split(",") if p.strip()}
KEYWORDS = [k.strip().lower() for k in os.environ.get("KEYWORDS", "").split(",") if k.strip()]
ALERT_SCOPE = os.environ.get("ALERT_SCOPE_CHANGES", "true").lower() == "true"
ALERT_CLOSED = os.environ.get("ALERT_CLOSED", "false").lower() == "true"
H1_USER = os.environ.get("HACKERONE_USERNAME", "").strip()
DB_PATH = os.environ.get("DB_PATH", "/data/bounty.db")

BASE = "https://raw.githubusercontent.com/arkadiyt/bounty-targets-data/main/data/"
DIODB = "https://raw.githubusercontent.com/disclose/diodb/master/program-list.json"
API = f"https://api.telegram.org/bot{TOKEN}/"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("bounty")
lock = threading.Lock()
last_check = {"at": None, "count": 0, "error": None}

# ---------- fetch + normalize ----------
def _get(url):
    r = requests.get(url, timeout=120)
    r.raise_for_status()
    return r.json()

def _scope(items, key):
    out = []
    for t in items or []:
        a = t.get(key) or t.get("target") or t.get("endpoint") or t.get("uri") or t.get("name")
        if a:
            out.append({"a": str(a)[:200], "t": str(t.get("asset_type") or t.get("type") or ""),
                        "b": bool(t.get("eligible_for_bounty", True))})
    return out

def _num(v):
    try:
        v = float(v)
        return v if v > 0 else None
    except (TypeError, ValueError):
        return None

def fetch_all():
    progs = []
    for p in _get(BASE + "hackerone_data.json"):
        if p.get("submission_state") != "open":
            continue
        progs.append(dict(name=p["name"], platform="HackerOne", url=p["url"], website=p.get("website") or "",
                          paid=bool(p.get("offers_bounties")), swag=bool(p.get("offers_swag")),
                          min=None, max=None, cur="USD", safe_harbor="",
                          response=p.get("response_efficiency_percentage"),
                          scope=_scope(p["targets"].get("in_scope"), "asset_identifier")))
    for p in _get(BASE + "bugcrowd_data.json"):
        mx = _num(p.get("max_payout"))
        progs.append(dict(name=p["name"].strip(), platform="Bugcrowd", url=p["url"], website="",
                          paid=bool(mx), swag=False, min=None, max=mx, cur="USD",
                          safe_harbor=p.get("safe_harbor") or "", response=None,
                          scope=_scope(p["targets"].get("in_scope"), "target")))
    for p in _get(BASE + "intigriti_data.json"):
        if p.get("status") != "open":
            continue
        mx = _num((p.get("max_bounty") or {}).get("value"))
        progs.append(dict(name=p["name"], platform="Intigriti", url=p["url"], website="",
                          paid=bool(mx), swag=False, min=_num((p.get("min_bounty") or {}).get("value")),
                          max=mx, cur=(p.get("max_bounty") or {}).get("currency", "EUR"),
                          safe_harbor="", response=None,
                          scope=_scope(p["targets"].get("in_scope"), "endpoint")))
    for p in _get(BASE + "yeswehack_data.json"):
        if p.get("disabled"):
            continue
        mx = _num(p.get("max_bounty"))
        progs.append(dict(name=p["name"], platform="YesWeHack", url="https://yeswehack.com/programs/" + p["id"],
                          website="", paid=bool(mx), swag=False, min=_num(p.get("min_bounty")), max=mx,
                          cur="EUR", safe_harbor="", response=None,
                          scope=_scope(p["targets"].get("in_scope"), "target")))
    try:
        for p in _get(DIODB):
            if p.get("policy_url_status") not in ("alive", ""):
                continue
            progs.append(dict(name=p["program_name"], platform="Self-hosted", url=p.get("policy_url") or "",
                              website="", paid=p.get("offers_bounty") in ("yes", "partial"),
                              swag=bool(p.get("offers_swag")), min=None, max=None, cur="",
                              safe_harbor=p.get("safe_harbor") or "", response=None, scope=[],
                              email=p.get("contact_email") or "", hof=p.get("hall_of_fame") or ""))
    except Exception as e:  # diodb is optional
        log.warning("diodb fetch failed: %s", e)
    for p in progs:
        p["key"] = p["platform"] + "|" + p["name"]
    return {p["key"]: p for p in progs}

# ---------- filters ----------
def wanted(p):
    if p["platform"].lower() not in PLATFORMS:
        return False
    if ONLY_PAID and not p["paid"]:
        return False
    if MIN_PAYOUT and (p["max"] or 0) < MIN_PAYOUT and p["max"] is not None:
        return False
    if KEYWORDS:
        hay = (p["name"] + " " + p["website"] + " " + " ".join(s["a"] for s in p["scope"])).lower()
        if not any(k in hay for k in KEYWORDS):
            return False
    return True

# ---------- storage ----------
def db():
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    c = sqlite3.connect(DB_PATH, check_same_thread=False)
    c.execute("""create table if not exists programs(
        key text primary key, data text, first_seen real, last_seen real, active int)""")
    c.execute("create table if not exists meta(k text primary key, v text)")
    return c

CONN = db()

def meta(k, v=None):
    if v is None:
        r = CONN.execute("select v from meta where k=?", (k,)).fetchone()
        return r[0] if r else None
    CONN.execute("insert or replace into meta values(?,?)", (k, v))
    CONN.commit()

# ---------- telegram ----------
def send(text, chat=None):
    chat = chat or CHAT_ID
    if not chat:
        log.info("No TELEGRAM_CHAT_ID yet; message not sent")
        return
    while text:
        chunk = text[:4000]
        if len(text) > 4000:
            cut = chunk.rfind("\n")
            chunk = text[:cut if cut > 1000 else 4000]
        text = text[len(chunk):]
        for attempt in range(3):
            try:
                r = requests.post(API + "sendMessage", timeout=30, json={
                    "chat_id": chat, "text": chunk, "parse_mode": "HTML",
                    "disable_web_page_preview": True})
                if r.status_code == 429:
                    time.sleep(r.json().get("parameters", {}).get("retry_after", 5) + 1)
                    continue
                if not r.ok:
                    log.error("telegram error %s %s", r.status_code, r.text[:200])
                break
            except requests.RequestException as e:
                log.error("telegram send failed: %s", e)
                time.sleep(3)
        time.sleep(0.4)

e = html.escape

def money(p):
    if not p.get("max"):
        return "Pays bounties" if p["paid"] else "No cash (disclosure / hall of fame)"
    c = "€" if p.get("cur") == "EUR" else "$"
    lo = f"{c}{p['min']:,.0f} – " if p.get("min") else "up to "
    return f"{lo}{c}{p['max']:,.0f}"

def short(p):
    return (f"<b>{e(p['name'])}</b> · {p['platform']}\n💰 {money(p)} · {len(p['scope'])} asset{'' if len(p['scope']) == 1 else 's'}\n"
            f"{e(p['url'])}")

def checklist(p):
    alias = f"{H1_USER}@wearehackerone.com" if H1_USER else "yourusername@wearehackerone.com"
    steps = []
    if p["platform"] == "Self-hosted":
        steps += ["Read the full policy page and note what is out of scope",
                  "Report to " + (p.get("email") or "the contact on the policy page"),
                  "Only test what the policy allows"]
    else:
        steps += [f"Open the program on {p['platform']} and read the full policy + out-of-scope list",
                  "Check the program's rules on automated scanning and rate limits"]
        if p["platform"] == "HackerOne":
            steps.append(f"Create test accounts with {alias} (or +tag variants)")
            steps.append("Add the X-HackerOne-Research header if the policy asks for it")
        else:
            steps.append(f"Use your {p['platform']} researcher email/alias for test accounts if the policy requires it")
        steps.append("Check if a VPN / test environment / credentials are provided")
    steps += ["Start recon on the in-scope assets below only",
              "Write the report: summary, steps to reproduce, impact, PoC, fix suggestion"]
    return "\n".join(f"☐ {s}" for s in steps)

def full(p):
    lines = [f"🎯 <b>{e(p['name'])}</b>", f"Platform: {p['platform']}", f"Payout: {money(p)}"]
    if p.get("safe_harbor"):
        lines.append(f"Safe harbor: {e(p['safe_harbor'])}")
    if p.get("response") not in (None, "None"):
        lines.append(f"Response rate: {p['response']}%")
    if p.get("swag"):
        lines.append("Swag: yes")
    lines.append(f"Apply / submit: {e(p['url'])}")
    if p.get("website"):
        lines.append(f"Website: {e(p['website'])}")
    if p.get("email"):
        lines.append(f"Contact: {e(p['email'])}")
    if p["scope"]:
        lines.append(f"\n<b>In scope ({len(p['scope'])})</b>")
        for s in p["scope"][:30]:
            nb = "" if s["b"] else " (no bounty)"
            lines.append(f"• <code>{e(s['a'])}</code> {e(s['t'].lower())}{nb}")
        if len(p["scope"]) > 30:
            lines.append(f"…and {len(p['scope']) - 30} more on the program page")
    lines.append("\n<b>Checklist</b>\n" + checklist(p))
    return "\n".join(lines)

# ---------- diff ----------
def scope_set(p):
    return {s["a"] for s in p["scope"]}

def check(manual_chat=None):
    with lock:
        try:
            cur = fetch_all()
        except Exception as ex:
            last_check.update(at=time.time(), error=str(ex))
            log.exception("fetch failed")
            if manual_chat:
                send(f"⚠️ Fetch failed: {e(str(ex))}", manual_chat)
            return
        now = time.time()
        old = {k: (json.loads(d), a) for k, d, a in CONN.execute("select key,data,active from programs")}
        first_run = not old
        new, scope_adds, raises, closed = [], [], [], []
        for k, p in cur.items():
            if k not in old:
                new.append(p)
                CONN.execute("insert into programs values(?,?,?,?,1)", (k, json.dumps(p), now, now))
                continue
            op, active = old[k]
            if not active:
                new.append(p)  # reopened
            added = scope_set(p) - scope_set(op)
            if added and op["scope"]:
                scope_adds.append((p, sorted(added)))
            if p.get("max") and op.get("max") and p["max"] > op["max"]:
                raises.append((p, op["max"]))
            CONN.execute("update programs set data=?, last_seen=?, active=1 where key=?", (json.dumps(p), now, k))
        for k, (op, active) in old.items():
            if k not in cur and active:
                closed.append(op)
                CONN.execute("update programs set active=0 where key=?", (k,))
        CONN.commit()
        last_check.update(at=now, count=len(cur), error=None)
        log.info("checked %d programs: %d new, %d scope, %d raises, %d closed",
                 len(cur), len(new), len(scope_adds), len(raises), len(closed))

        if first_run:
            meta("seeded_at", str(now))
            n = sum(1 for p in cur.values() if wanted(p))
            send(f"✅ Bounty Watch is live.\nTracking {len(cur):,} programs, {n:,} match your filters.\n"
                 f"I'll message you when something new shows up. Checking every {INTERVAL_MIN} min.\n"
                 f"Try /top or /search shopify", manual_chat)
            return

        new = [p for p in new if wanted(p)]
        new.sort(key=lambda p: -(p.get("max") or 0))
        for p in new[:15]:
            send("🆕 <b>New program</b>\n\n" + full(p))
        if len(new) > 15:
            send(f"…plus {len(new) - 15} more new programs. Use /new 1 to list them.")
        if ALERT_SCOPE:
            for p, added in [x for x in scope_adds if wanted(x[0])][:10]:
                body = "\n".join(f"• <code>{e(a)}</code>" for a in added[:20])
                more = f"\n…and {len(added) - 20} more" if len(added) > 20 else ""
                send(f"🔭 <b>New scope</b> on {short(p)}\n\n{body}{more}\n\nNew assets = fresh attack surface, check early.")
        for p, was in [x for x in raises if wanted(x[0])]:
            send(f"📈 <b>Payout raised</b>\n{short(p)}\nwas {was:,.0f} → now {money(p)}")
        if ALERT_CLOSED:
            for p in [x for x in closed if wanted(x)][:10]:
                send(f"🚪 <b>Closed / removed</b>\n{e(p['name'])} · {p['platform']}")
        if manual_chat and not (new or scope_adds or raises):
            send("Checked. Nothing new matching your filters.", manual_chat)

# ---------- commands ----------
def active_programs():
    return [json.loads(d) | {"_first": f} for d, f in
            CONN.execute("select data, first_seen from programs where active=1")]

def find(q, exact_first=False):
    q = q.lower()
    if exact_first:
        exact = [p for p in active_programs() if p["name"].lower() == q]
        if exact:
            return exact
    return [p for p in active_programs()
            if q in (p["name"] + " " + p.get("website", "") + " " + " ".join(s["a"] for s in p["scope"])).lower()]

def handle(msg):
    chat = str(msg["chat"]["id"])
    text = (msg.get("text") or "").strip()
    if not text.startswith("/"):
        return
    cmd, _, arg = text.partition(" ")
    cmd = cmd.split("@")[0].lower()
    arg = arg.strip()
    if cmd == "/start" and not CHAT_ID:
        send(f"Your chat ID is <code>{chat}</code>.\nPut it in TELEGRAM_CHAT_ID and restart the bot.", chat)
        return
    if chat != CHAT_ID:
        return  # ignore strangers
    if cmd in ("/start", "/help"):
        send(__doc__.split("Telegram commands")[1].split('"""')[0].replace("(only answered in your own chat):", "<b>Commands</b>"), chat)
    elif cmd == "/status":
        at = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(last_check["at"])) if last_check["at"] else "not yet"
        err = f"\n⚠️ Last error: {e(last_check['error'])}" if last_check["error"] else ""
        send(f"Tracking {last_check['count']:,} programs\nLast check: {at}\nEvery {INTERVAL_MIN} min\n"
             f"Filters: paid only={ONLY_PAID}, min payout={MIN_PAYOUT:,.0f}, "
             f"platforms={', '.join(sorted(PLATFORMS))}, keywords={', '.join(KEYWORDS) or 'none'}{err}", chat)
    elif cmd == "/check":
        send("Checking now…", chat)
        threading.Thread(target=check, args=(chat,), daemon=True).start()
    elif cmd == "/top":
        n = min(int(arg) if arg.isdigit() else 10, 40)
        ps = sorted([p for p in active_programs() if wanted(p) and p.get("max")], key=lambda p: -p["max"])[:n]
        send("<b>Top payouts</b>\n\n" + "\n\n".join(short(p) for p in ps) if ps else "Nothing yet.", chat)
    elif cmd == "/new":
        days = float(arg) if arg.replace(".", "", 1).isdigit() else 7
        since = time.time() - days * 86400
        seeded = float(meta("seeded_at") or 0)
        ps = [p for p in active_programs() if p["_first"] > max(since, seeded) and wanted(p)]
        ps.sort(key=lambda p: -p["_first"])
        send(f"<b>New in last {days:g} days ({len(ps)})</b>\n\n" + "\n\n".join(short(p) for p in ps[:30])
             if ps else f"No new programs in the last {days:g} days yet.", chat)
    elif cmd == "/search":
        if not arg:
            send("Usage: /search shopify", chat)
            return
        ps = sorted(find(arg), key=lambda p: -(p.get("max") or 0))
        send(f"<b>{len(ps)} results</b>\n\n" + "\n\n".join(short(p) for p in ps[:20]) +
             ("\n\nUse /p name for full details." if ps else ""), chat)
    elif cmd == "/p":
        ps = find(arg, exact_first=True) if arg else []
        if not ps:
            send("Not found. Try /search first.", chat)
        else:
            send(full(ps[0]), chat)
    else:
        send("Unknown command. /help", chat)

def poll_updates():
    offset = None
    while True:
        try:
            r = requests.get(API + "getUpdates", timeout=60,
                             params={"timeout": 50, "offset": offset, "allowed_updates": json.dumps(["message"])})
            for u in r.json().get("result", []):
                offset = u["update_id"] + 1
                if "message" in u:
                    try:
                        handle(u["message"])
                    except Exception:
                        log.exception("command failed")
        except Exception as ex:
            log.warning("getUpdates: %s", ex)
            time.sleep(5)

# ---------- status page ----------
STATUS_PORT = int(os.environ.get("STATUS_PORT", "8080"))

class StatusHandler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        at = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(last_check["at"])) if last_check["at"] else "not yet"
        err = f"<p style='color:#c0392b'>⚠️ Last error: {e(last_check['error'])}</p>" if last_check["error"] else ""
        body = f"""<!doctype html><meta charset="utf-8"><title>Bounty Watch</title>
<body style="font-family:system-ui,sans-serif;max-width:640px;margin:40px auto;line-height:1.6;color:#222">
<h1>🎯 Bounty Watch</h1>
<p>Tracking <b>{last_check['count']:,}</b> programs</p>
<p>Last check: {e(at)} &middot; every {INTERVAL_MIN} min</p>
<p>Filters: paid only={ONLY_PAID} &middot; min payout={MIN_PAYOUT:,.0f} &middot; platforms={e(', '.join(sorted(PLATFORMS)))} &middot; keywords={e(', '.join(KEYWORDS) or 'none')}</p>
{err}
<p><small>Controlled via Telegram: /status /check /new /top /search /p</small></p>
</body>"""
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body.encode())

def serve_status():
    ThreadingHTTPServer(("0.0.0.0", STATUS_PORT), StatusHandler).serve_forever()

def main():
    threading.Thread(target=poll_updates, daemon=True).start()
    threading.Thread(target=serve_status, daemon=True).start()
    if not CHAT_ID:
        log.warning("TELEGRAM_CHAT_ID not set. Send /start to your bot to get it.")
    while True:
        if CHAT_ID:
            check()
        time.sleep(INTERVAL_MIN * 60)

if __name__ == "__main__":
    main()
