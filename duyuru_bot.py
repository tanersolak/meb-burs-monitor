#!/usr/bin/env python3
"""MEB AB ve Dış İlişkiler Genel Müdürlüğü duyurularını izleyip Telegram'a bildirir.

Kaynak: sitenin RSS beslemesi (duyuru tablosu JS ile yüklendiği için HTML scraping yerine RSS).
Yalnızca standart kütüphane kullanır, ek paket gerekmez.
"""
import argparse
import html
import json
import os
import re
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
USER_AGENT = "Mozilla/5.0 (compatible; abdigm-duyuru-bot/1.0)"
TR_TZ = timezone(timedelta(hours=3))
MAX_SEEN = 500


def parse_chat_ids(value: str) -> list[str]:
    """'111, -100222;333' -> ['111', '-100222', '333'] (virgül, noktalı virgül veya boşlukla ayrılabilir)."""
    ids = [x for x in re.split(r"[,\s;]+", value) if x]
    return list(dict.fromkeys(ids))  # tekrarları at, sırayı koru


def log(msg: str) -> None:
    print(f"[{datetime.now(TR_TZ):%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def http_get(url: str, retries: int = 3) -> bytes:
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.read()
        except (urllib.error.URLError, TimeoutError) as e:
            last_err = e
            log(f"İstek başarısız ({attempt}/{retries}): {e}")
            time.sleep(3 * attempt)
    raise RuntimeError(f"Besleme alınamadı: {last_err}")


def parse_feed(raw: bytes) -> list[dict]:
    """RSS'teki tüm kayıtları döndürür (kategori filtresi çağıran tarafta)."""
    root = ET.fromstring(raw)
    items = []
    for it in root.iter("item"):
        link = (it.findtext("link") or "").strip()
        m = re.search(r"/icerik/(\d+)", link)
        if not m:
            continue
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


def send_telegram(token: str, chat_id: str, text: str) -> None:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = urllib.parse.urlencode({
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
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
            raise RuntimeError(f"Telegram hatası {e.code}: {body}") from e
        except urllib.error.URLError:
            time.sleep(3 * attempt)
    raise RuntimeError("Telegram mesajı gönderilemedi")


def broadcast(token: str, chat_ids: list[str], text: str) -> tuple[int, int]:
    """Mesajı tüm chat id'lere gönderir. (başarılı, başarısız) sayısını döndürür.

    Bir sohbet (ör. botu engelleyen kullanıcı) hata verse bile diğerlerine gönderim sürer.
    """
    ok = bad = 0
    for cid in chat_ids:
        try:
            send_telegram(token, cid, text)
            ok += 1
        except RuntimeError as e:
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
    args = ap.parse_args()

    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_ids = parse_chat_ids(os.getenv("TELEGRAM_CHAT_ID", ""))
    if not args.dry_run and not (token and chat_ids):
        log("HATA: TELEGRAM_BOT_TOKEN ve TELEGRAM_CHAT_ID tanımlı olmalı.")
        return 2

    raw = Path(args.feed_file).read_bytes() if args.feed_file else http_get(FEED_URL)
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

    if first_run:
        # İlk çalıştırmada eski duyuruları yağdırmamak için sadece kaydet.
        log(f"İlk çalıştırma: {len(items)} mevcut duyuru kaydedildi, bildirim gönderilmedi.")
        if not args.dry_run:
            seen.update(i["id"] for i in items)
            save_state(seen)
            broadcast(token, chat_ids,
                      f"✅ ABDİGM duyuru botu aktif. Şu an {len(items)} duyuru kayıtlı; "
                      "bundan sonra yenileri buraya gelecek.")
        return 0

    if not new_items:
        log("Yeni duyuru yok.")
    failed = False
    for item in new_items:
        msg = format_message(item)
        if args.dry_run:
            print("-" * 40, msg, sep="\n")
            continue
        ok, bad = broadcast(token, chat_ids, msg)
        if bad:
            failed = True  # çıkış kodu 1 → Actions'ta kırmızı görünür
        if ok:
            seen.add(item["id"])
            log(f"Gönderildi ({ok}/{len(chat_ids)} sohbet): #{item['id']} {item['title'][:60]}")
        else:
            # Hiçbir sohbete gönderilemediyse 'görüldü' sayılmaz, yarın tekrar denenir.
            log(f"HATA: #{item['id']} hiçbir sohbete gönderilemedi.")
            break

    if not args.dry_run:
        save_state(seen)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
