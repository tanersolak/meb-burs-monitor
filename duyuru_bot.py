#!/usr/bin/env python3
"""MEB AB ve Dış İlişkiler Genel Müdürlüğü duyurularını izleyip Telegram'a bildirir.

Kaynak: sitenin RSS beslemesi (duyuru tablosu JS ile yüklendiği için HTML scraping yerine RSS).
Alıcılar: TELEGRAM_CHAT_ID (sabit liste) + bota /start yazan kullanıcılar (state/subscribers.json).
Yalnızca standart kütüphane kullanır, ek paket gerekmez.
"""
import argparse
import html
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

FEED_URL = os.getenv("FEED_URL", "https://abdigm.meb.gov.tr/meb_iys_dosyalar/xml/rss_duyurular.xml")
CATEGORY = os.getenv("CATEGORY", "2")  # 2 = Duyurular (/www/duyurular/kategori/2), 1 = Haberler
STATE_FILE = Path(os.getenv("STATE_FILE", "state/seen.json"))
SUBS_FILE = Path(os.getenv("SUBS_FILE", "state/subscribers.json"))
SUBSCRIBE_PASSWORD = os.getenv("SUBSCRIBE_PASSWORD", "")  # doluysa /start <parola> gerekir
USER_AGENT = "Mozilla/5.0 (compatible; abdigm-duyuru-bot/1.2)"
TR_TZ = timezone(timedelta(hours=3))
MAX_SEEN = 500


class TelegramError(RuntimeError):
    def __init__(self, code: int, msg: str):
        super().__init__(msg)
        self.code = code


class CertError(RuntimeError):
    """Sunucunun sertifika zinciri doğrulanamadı."""


def parse_chat_ids(value: str) -> list[str]:
    """'111, -100222;333' -> ['111', '-100222', '333'] (virgül, noktalı virgül veya boşlukla ayrılabilir)."""
    ids = [x for x in re.split(r"[,\s;]+", value) if x]
    return list(dict.fromkeys(ids))  # tekrarları at, sırayı koru


def log(msg: str) -> None:
    print(f"[{datetime.now(TR_TZ):%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def http_get(url: str, retries: int = 3, context: ssl.SSLContext | None = None) -> bytes:
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=30, context=context) as resp:
                return resp.read()
        except (urllib.error.URLError, TimeoutError) as e:
            if isinstance(getattr(e, "reason", None), ssl.SSLCertVerificationError):
                raise CertError(str(e.reason)) from e  # tekrar denemek işe yaramaz
            last_err = e
            log(f"İstek başarısız ({attempt}/{retries}): {e}")
            time.sleep(3 * attempt)
    raise RuntimeError(f"Besleme alınamadı: {last_err}")


def fetch_feed() -> bytes:
    """RSS'i indirir. Site ara sertifikayı göndermediği için doğrulama başarısız olursa,
    YALNIZCA bu genel RSS isteği için doğrulamayı atlayarak yeniden dener.
    (Telegram istekleri her zaman tam doğrulamayla yapılır.)

    FEED_CA_FILE=<pem>   : eksik ara sertifikayı verirseniz doğrulama atlanmaz (en güvenlisi)
    FEED_VERIFY_SSL=1    : hiçbir durumda atlama, hata ver
    """
    cafile = os.getenv("FEED_CA_FILE")
    ctx = ssl.create_default_context()
    if cafile:
        ctx.load_verify_locations(cafile=cafile)
    try:
        return http_get(FEED_URL, context=ctx)
    except CertError as e:
        if cafile or os.getenv("FEED_VERIFY_SSL", "").lower() in ("1", "true", "yes"):
            raise
        log(f"UYARI: Sertifika zinciri doğrulanamadı ({e}). Yalnızca RSS için doğrulama atlanıyor.")
        loose = ssl.create_default_context()
        loose.check_hostname = False
        loose.verify_mode = ssl.CERT_NONE
        return http_get(FEED_URL, context=loose)


def parse_feed(raw: bytes) -> list[dict]:
    """RSS'teki tüm kayıtları döndürür (kategori filtresi çağıran tarafta)."""
    root = ET.fromstring(raw)
    items = []
    for it in root.iter("item"):
        link = (it.findtext("link") or "").strip()
        m = re.search(r"/icerik/(\d+)", link)
        host = urllib.parse.urlparse(link).hostname or ""
        if not m or not (host == "meb.gov.tr" or host.endswith(".meb.gov.tr")):
            continue  # beklenmeyen alan adlarını yok say
        pub = None
        try:
            pub = parsedate_to_datetime(it.findtext("pubDate") or "").astimezone(TR_TZ)
        except (TypeError, ValueError):
            pass
        items.append({
            "id": int(m.group(1)),
            "title": " ".join((it.findtext("title") or "").split()),
            "link": link,
            "date": pub,
            "category": (it.findtext("category") or "").strip(),
        })
    return items


# ---------- duyuru state ----------

def load_state() -> dict | None:
    if not STATE_FILE.exists():
        return None
    return json.loads(STATE_FILE.read_text(encoding="utf-8"))


def save_state(seen: set[int]) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "last_check": datetime.now(TR_TZ).isoformat(timespec="seconds"),
        "seen": sorted(seen)[-MAX_SEEN:],
    }
    STATE_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


# ---------- abone state ----------

def load_subs() -> dict:
    if SUBS_FILE.exists():
        return json.loads(SUBS_FILE.read_text(encoding="utf-8"))
    return {"offset": 0, "chats": {}}


def save_subs(subs: dict) -> None:
    SUBS_FILE.parent.mkdir(parents=True, exist_ok=True)
    SUBS_FILE.write_text(json.dumps(subs, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


# ---------- Telegram ----------

def send_telegram(token: str, chat_id: str, text: str) -> None:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = urllib.parse.urlencode({
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }).encode()
    for attempt in range(1, 4):
        try:
            req = urllib.request.Request(url, data=payload, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=30) as resp:
                if json.load(resp).get("ok"):
                    return
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")
            if e.code == 429:  # Telegram hız sınırı
                wait = json.loads(body).get("parameters", {}).get("retry_after", 5)
                time.sleep(wait + 1)
                continue
            raise TelegramError(e.code, f"Telegram hatası {e.code}: {body}") from e
        except urllib.error.URLError:
            time.sleep(3 * attempt)
    raise TelegramError(0, "Telegram mesajı gönderilemedi")


def get_updates(token: str, offset: int) -> list[dict]:
    q = urllib.parse.urlencode({
        "offset": offset,
        "timeout": 0,
        "allowed_updates": json.dumps(["message"]),
    })
    raw = http_get(f"https://api.telegram.org/bot{token}/getUpdates?{q}")
    data = json.loads(raw)
    if not data.get("ok"):
        raise RuntimeError(f"getUpdates başarısız: {data}")
    return data["result"]


def safe_reply(token: str, chat_id: str, text: str) -> None:
    try:
        send_telegram(token, chat_id, text)
    except RuntimeError as e:
        log(f"Yanıt gönderilemedi (chat {chat_id}): {e}")


def sync_subscribers(token: str) -> dict:
    """Bota gelen /start ve /stop mesajlarını işleyip abone listesini günceller."""
    subs = load_subs()
    updates = get_updates(token, subs["offset"])
    if not updates:
        log("Yeni abonelik mesajı yok.")
        return subs

    for u in updates:
        subs["offset"] = max(subs["offset"], u["update_id"] + 1)
        msg = u.get("message")
        if not msg or msg.get("chat", {}).get("type") != "private":
            continue  # gruplar yalnızca TELEGRAM_CHAT_ID ile elle eklenir
        cid = str(msg["chat"]["id"])
        cmd, _, arg = (msg.get("text") or "").strip().partition(" ")
        cmd = cmd.split("@")[0].lower()

        if cmd == "/start":
            if SUBSCRIBE_PASSWORD and arg.strip() != SUBSCRIBE_PASSWORD:
                log(f"Yanlış/eksik parola ile kayıt denemesi: {cid}")
                safe_reply(token, cid, "⛔ Kayıt için size verilen özel bağlantıyı kullanmalısınız.")
            elif cid in subs["chats"]:
                safe_reply(token, cid, "Zaten kayıtlısınız. Ayrılmak için /stop yazın.")
            else:
                subs["chats"][cid] = {"joined": datetime.now(TR_TZ).isoformat(timespec="seconds")}
                log(f"Yeni abone: {cid}")
                safe_reply(token, cid, "✅ Kayıt oldunuz. Yeni duyurular size iletilecek.\nAyrılmak için /stop yazın.")
        elif cmd == "/stop":
            if subs["chats"].pop(cid, None) is not None:
                log(f"Abonelikten çıkış: {cid}")
                safe_reply(token, cid, "👋 Kaydınız silindi. Tekrar katılmak için /start yazabilirsiniz.")

    save_subs(subs)
    return subs


def broadcast(token: str, chat_ids: list[str], text: str,
              removable: set[str] = frozenset(), dead: set[str] | None = None) -> tuple[int, int]:
    """Mesajı tüm chat id'lere gönderir. (başarılı, başarısız) sayısını döndürür.

    Botu engellemiş (403) bir *abone* hata sayılmaz; `dead` kümesine eklenip listeden silinir.
    """
    ok = bad = 0
    for cid in chat_ids:
        try:
            send_telegram(token, cid, text)
            ok += 1
        except TelegramError as e:
            if e.code == 403 and cid in removable and dead is not None:
                dead.add(cid)
                log(f"Abone botu engellemiş, listeden çıkarılacak: {cid}")
            else:
                bad += 1
                log(f"HATA: chat {cid} için gönderilemedi: {e}")
        time.sleep(0.5)
    return ok, bad


def format_message(item: dict) -> str:
    date = f"{item['date']:%d.%m.%Y %H:%M}" if item["date"] else ""
    lines = ["📢 <b>Yeni Duyuru</b>", "", html.escape(item["title"])]
    if date:
        lines.append(f"🗓 {date}")
    lines += ["", f'🔗 <a href="{html.escape(item["link"], quote=True)}">Duyuruyu aç</a>']
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="ABDİGM duyuru → Telegram")
    ap.add_argument("--dry-run", action="store_true", help="Telegram'a göndermez, ekrana yazar; state'i değiştirmez")
    ap.add_argument("--feed-file", help="Test için canlı site yerine yerel RSS dosyası kullan")
    ap.add_argument("--sync-only", action="store_true", help="Sadece yeni abonelikleri işle, duyuru kontrol etme")
    args = ap.parse_args()

    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    static_ids = parse_chat_ids(os.getenv("TELEGRAM_CHAT_ID", ""))
    if not args.dry_run and not token:
        log("HATA: TELEGRAM_BOT_TOKEN tanımlı olmalı.")
        return 2

    # 1) Yeni /start, /stop mesajlarını işle
    subs = {"chats": {}}
    if not args.dry_run:
        try:
            subs = sync_subscribers(token)
        except Exception as e:  # abonelik hatası duyuru göndermeyi engellemesin
            log(f"UYARI: abonelikler işlenemedi: {e}")
            subs = load_subs()
    if args.sync_only:
        return 0

    sub_ids = list(subs["chats"].keys())
    chat_ids = list(dict.fromkeys(static_ids + sub_ids))
    removable = set(sub_ids) - set(static_ids)
    log(f"Alıcılar: {len(static_ids)} sabit + {len(sub_ids)} abone = {len(chat_ids)} sohbet.")
    if not args.dry_run and not chat_ids:
        log("HATA: Hiç alıcı yok. TELEGRAM_CHAT_ID girin veya bota /start yazdırın.")
        return 2

    # 2) Duyuruları kontrol et
    raw = Path(args.feed_file).read_bytes() if args.feed_file else fetch_feed()
    all_items = parse_feed(raw)
    if not all_items:
        log("HATA: Beslemede hiç kayıt bulunamadı (format değişmiş olabilir).")
        return 1

    items = [i for i in all_items if i["category"] == CATEGORY]
    log(f"Beslemede {len(all_items)} kayıt, kategori {CATEGORY}: {len(items)} kayıt.")

    state = load_state()
    first_run = state is None
    seen = set(state["seen"]) if state else set()
    new_items = sorted((i for i in items if i["id"] not in seen), key=lambda i: i["id"])
    dead: set[str] = set()
    failed = False

    if first_run:
        # İlk çalıştırmada eski duyuruları yağdırmamak için sadece kaydet.
        log(f"İlk çalıştırma: {len(items)} mevcut duyuru kaydedildi, bildirim gönderilmedi.")
        if not args.dry_run:
            seen.update(i["id"] for i in items)
            save_state(seen)
            broadcast(token, chat_ids,
                      f"✅ ABDİGM duyuru botu aktif. Şu an {len(items)} duyuru kayıtlı; "
                      "bundan sonra yenileri buraya gelecek.", removable, dead)
        new_items = []
    elif not new_items:
        log("Yeni duyuru yok.")

    for item in new_items:
        msg = format_message(item)
        if args.dry_run:
            print("-" * 40, msg, sep="\n")
            continue
        ok, bad = broadcast(token, chat_ids, msg, removable, dead)
        if bad:
            failed = True  # çıkış kodu 1 → Actions'ta kırmızı görünür
        if ok:
            seen.add(item["id"])
            log(f"Gönderildi ({ok}/{len(chat_ids)} sohbet): #{item['id']} {item['title'][:60]}")
        else:
            # Hiçbir sohbete gönderilemediyse 'görüldü' sayılmaz, yarın tekrar denenir.
            log(f"HATA: #{item['id']} hiçbir sohbete gönderilemedi.")
            failed = True
            break

    if not args.dry_run:
        if not first_run:
            save_state(seen)
        if dead:  # botu engelleyen aboneleri temizle
            fresh = load_subs()
            for cid in dead:
                fresh["chats"].pop(cid, None)
            save_subs(fresh)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
