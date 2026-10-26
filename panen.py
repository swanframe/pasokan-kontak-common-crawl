"""Panen email perusahaan .id dari arsip Common Crawl, satu bulan arsip sampai habis.

    python panen.py --daftar
        Bulan arsip yang tersedia, terbaru di atas.

    python panen.py --bulan 2026-09
        Memanen arsip September 2026, sekitar 8 jam. Boleh berhenti kapan saja
        (Ctrl+C, laptop mati, internet putus): jalankan perintah yang sama lagi
        dan ia melanjutkan dari blok terakhir. Hasil: hasil/2026-09.csv

    python panen.py --gabung
        Menyatukan semua hasil/<bulan>.csv menjadi hasil/gabungan.csv, membuang
        email ganda, dan mencetak berapa email baru yang disumbang tiap bulan.

    python panen.py --bersihkan
        Membersihkan hasil/gabungan.csv sebelum dikirimi email: merapikan alamat
        yang tertempel sisa teks, membuang alamat contoh dan alamat sistem, dan
        memeriksa apakah domainnya masih punya server email. Hasil:
        hasil/bersih.csv (siap pakai) dan hasil/dibuang.csv (beserta alasannya).

Hanya pustaka bawaan Python 3.9 atau lebih baru. Panduan lengkap di README.md.
"""

import csv
import gzip
import html
import http.client
import json
import random
import re
import socket
import struct
import sys
import threading
import time
import urllib.parse
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

BASE = "https://data.commoncrawl.org"
COLLINFO = "https://index.commoncrawl.org/collinfo.json"
WORKERS = 4  # sengaja kecil: server arsip dipakai bersama banyak orang
HTTP_TRIES = 6
REPORT_EVERY = 300
MAX_AGE_MONTHS = 12  # patokan README: arsip lebih tua lebih berisiko berisi email mati

HERE = Path(__file__).resolve().parent
RESULTS = HERE / "hasil"
CACHE = HERE / "cache"

MONTHS = ["January", "February", "March", "April", "May", "June", "July",
          "August", "September", "October", "November", "December"]
MONTHS_ID = ["Januari", "Februari", "Maret", "April", "Mei", "Juni", "Juli",
             "Agustus", "September", "Oktober", "November", "Desember"]

# Second-level .id yang bukan perusahaan: kampus, pemerintah, sekolah, dst.
NON_COMPANY = {"ac", "go", "sch", "or", "mil", "desa", "ponpes", "net", "web", "my"}
# Harus di awal segmen path: "/tentang-kami" ya, "/artikel-semua-tentang-x" tidak.
CONTACT_PATH = re.compile(r"/(contact|kontak|hubungi|about|tentang)", re.I)
# Panjang tiap bagian dibatasi sesuai batas alamat email (64 sebelum @). Tanpa
# batas, deret karakter panjang tanpa @ (gambar yang ditanam di halaman,
# megabagus.id: 2,6 juta karakter) membuat pencarian mencoba tiap posisi sampai
# ujung deret, dan panen menggantung berjam-jam di satu halaman.
EMAIL = re.compile(r"[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]{1,253}\.[A-Za-z]{2,24}")
NOT_EMAIL_TLD = {"png", "jpg", "jpeg", "gif", "svg", "webp", "css", "js", "ico"}
FREE_MAIL = {
    "gmail.com", "yahoo.com", "yahoo.co.id", "hotmail.com", "outlook.com",
    "live.com", "ymail.com", "icloud.com", "rocketmail.com", "aol.com",
}
JUNK_DOMAINS = (
    "example.com", "domain.com", "email.com", "yourdomain.com", "mysite.com",
    "sentry.io", "wixpress.com", "sentry-next.wixpress.com",
)
# Penanda badan usaha di isi halaman. Huruf besar disengaja: "PT Maju Jaya" ya,
# kata "pt" di tengah kalimat tidak. Membantu memilah situs .id biasa, yang
# bisa milik siapa saja, dari situs perusahaan.
ENTITY = re.compile(r"\b(?:PT|CV)(?:\.\s*|\s+)[A-Z]|\bTbk\b|\(Persero\)")

# Pembersihan (--bersihkan). Situs dengan alamat sebanyak ini biasanya daftar
# peserta atau pelanggan, bukan alamat perusahaan itu sendiri.
CROWDED = 20
DNS_WORKERS = 8
DNS_HTTPS = "https://dns.google/resolve"
DNS_SERVERS = ("8.8.8.8", "1.1.1.1")
LOCAL_OK = re.compile(r"[a-z0-9_+-]+(?:\.[a-z0-9_+-]+)*")
DOMAIN_OK = re.compile(r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,24}")
GMAIL_TYPO = {"gmail.co", "gmail.con", "gmail.cm", "gmail.om", "gmail.co.id", "gmail.comm",
              "gmai.com", "gmial.com", "gamil.com", "gmal.com", "gmil.com", "gmaill.com"}
# Contoh isian formulir dan templat situs: abc@abc.com, contoh@gmail.com.
PLACEHOLDER_LOCAL = re.compile(r"example|contoh|nama|namaanda|emailanda|name|yourname|youremail|"
                               r"your-?email|johndoe|john\.doe|janedoe|someone|test|x{3,}")
PLACEHOLDER_DOMAINS = {
    "mail.com", "abc.com", "def.com", "xyz.com", "test.com", "company.com", "perusahaan.com",
    "website.com", "yourwebsite.com", "your-website.com", "your-domain.com", "address.com",
    "contoh.com", "google.com", "cloudflare.com", "wordpress.org", "w3.org", "schema.org",
}
NOREPLY = re.compile(r"no-?reply|do-?not-?reply|mailer-daemon|postmaster|hostmaster|^abuse$")
# Pelacak galat (Sentry, GlitchTip) dan kalender yang tertanam di kode halaman.
SYSTEM_DOMAIN = re.compile(r"sentry|glitchtip|calendar\.google\.com$|\.local$|\.teams\.ms$")
HEX_LOCAL = re.compile(r"(?=.*\d)[0-9a-f]{16,}")
OFFICIAL = re.compile(r"\.(?:go|ac|sch|mil|desa)\.id$|\.(?:edu|gov)(?:\.[a-z]{2})?$")
FIELDS = ["email", "domain_situs", "jenis_domain", "halaman", "url", "jenis",
          "sebut_badan_usaha"]

retry_count = 0


class ConnectionLost(Exception):
    """Terlalu banyak yang gagal sekaligus: kemungkinan internet putus."""


def http_get(url, byte_range=None, max_secs=120):
    global retry_count
    headers = {"User-Agent": "pasokan-kontak-common-crawl"}
    if byte_range:
        headers["Range"] = f"bytes={byte_range[0]}-{byte_range[1]}"
    for attempt in range(HTTP_TRIES):
        try:
            req = urllib.request.Request(url, headers=headers)
            # timeout urlopen hanya berlaku bila server diam total; server yang
            # mengirim sedikit demi sedikit bisa menggantung tanpa batas. Batas
            # total ini yang membuat unduhan seperti itu gagal lalu diulang.
            deadline = time.monotonic() + max_secs
            chunks = []
            with urllib.request.urlopen(req, timeout=30) as resp:
                while chunk := resp.read(65536):
                    if time.monotonic() > deadline:
                        raise TimeoutError(f"unduhan lebih dari {max_secs} dtk")
                    chunks.append(chunk)
            return b"".join(chunks)
        except (OSError, http.client.HTTPException):
            if attempt == HTTP_TRIES - 1:
                raise
            retry_count += 1
            time.sleep(2 ** (attempt + 1))


def available_months():
    """[(YYYY-MM, kode arsip)], terbaru dulu. Arsip lama berformat lain dilewati."""
    months = []
    for c in json.loads(http_get(COLLINFO)):
        parts = c["name"].split()  # mis. "September 2026 Index"
        if len(parts) >= 2 and parts[0] in MONTHS and parts[1].isdigit():
            months.append((f"{parts[1]}-{MONTHS.index(parts[0]) + 1:02d}", c["id"]))
    return months


def month_label(month):
    year, mon = month.split("-")
    return f"{MONTHS_ID[int(mon) - 1]} {year}"


def last_month():
    """Bulan terbaru yang dipakai: bulan lalu, dihitung dari tanggal laptop.
    Arsip bulan berjalan tidak dipakai walau sudah terbit, supaya aturannya
    tidak bergantung pada kapan arsip itu muncul (README, Memilih Bulan)."""
    now = time.localtime()
    year, mon = (now.tm_year, now.tm_mon - 1) if now.tm_mon > 1 else (now.tm_year - 1, 12)
    return f"{year}-{mon:02d}"


def months_before(month, reference):
    """Berapa bulan `month` lebih tua dari `reference`; negatif bila lebih baru."""
    return ((int(reference[:4]) - int(month[:4])) * 12
            + int(reference[5:]) - int(month[5:]))


def month_note(month):
    age = months_before(month, last_month())
    if age < 0:
        return "bulan berjalan, belum dipakai"
    if age >= MAX_AGE_MONTHS:
        return "lebih dari 12 bulan"
    return ""


def company_domain(surt_key):
    """'id,co,akr,www)/x' -> 'akr.co.id'; None bila bukan domain perusahaan."""
    host = surt_key.split(")", 1)[0].split(",")
    if len(host) < 2 or host[0] != "id":
        return None
    if host[1] in ("co", "biz"):
        return f"{host[2]}.{host[1]}.id" if len(host) > 2 else None
    if host[1] in NON_COMPANY:
        return None
    return f"{host[1]}.id"


def domain_category(domain):
    """co.id hanya untuk badan usaha berbadan hukum; biz.id usaha dengan syarat
    longgar; id biasa boleh milik siapa saja."""
    if domain.endswith(".co.id"):
        return "co.id"
    if domain.endswith(".biz.id"):
        return "biz.id"
    return "id"


def page_kind(surt_key):
    path = surt_key.split(")", 1)[1] if ")" in surt_key else ""
    if path in ("", "/"):
        return "beranda"
    if CONTACT_PATH.search(path):
        return "kontak"
    return None


def load_blocks(crawl):
    index_url = f"{BASE}/cc-index/collections/{crawl}/indexes"
    cluster = CACHE / f"{crawl}.idx"
    if not cluster.exists():
        print("Mengunduh peta indeks arsip ini (sekitar 105 MB, sekali saja)...", flush=True)
        tmp = cluster.with_suffix(".tmp")
        tmp.write_bytes(http_get(f"{index_url}/cluster.idx", max_secs=1800))
        tmp.replace(cluster)
    blocks = []
    with cluster.open(encoding="utf-8") as f:
        for line in f:
            if line.startswith("id,"):
                _, cdx_file, offset, length, _ = line.rstrip("\n").split("\t")
                blocks.append((f"{index_url}/{cdx_file}", int(offset), int(length)))
    return blocks


def candidates_from_block(block):
    url, offset, length = block
    raw = http_get(url, (offset, offset + length - 1))
    picked = {}
    for line in gzip.decompress(raw).decode("utf-8", "ignore").splitlines():
        key, _, payload = line.split(" ", 2)
        domain = company_domain(key)
        kind = page_kind(key)
        if not domain or not kind:
            continue
        rec = json.loads(payload)
        if rec.get("status") != "200" or "html" not in rec.get("mime", ""):
            continue
        # Satu beranda dan paling banyak dua halaman kontak per domain.
        slot = picked.setdefault(domain, {"beranda": [], "kontak": []})
        if len(slot[kind]) < (1 if kind == "beranda" else 2):
            slot[kind].append(rec)
    return picked


def emails_from_page(rec):
    start = int(rec["offset"])
    raw = http_get(f"{BASE}/{rec['filename']}", (start, start + int(rec["length"]) - 1))
    text = html.unescape(gzip.decompress(raw).decode("utf-8", "ignore"))
    found = set()
    for email in EMAIL.findall(text):
        email = email.lower().strip(".")
        domain = email.split("@", 1)[1]
        if domain.rsplit(".", 1)[-1] in NOT_EMAIL_TLD or domain.endswith(JUNK_DOMAINS):
            continue
        found.add(email)
    return found, bool(ENTITY.search(text))


def jenis(email):
    return "gratis" if email.split("@", 1)[1] in FREE_MAIL else "bukan_gratis"


def harvest(month):
    months = available_months()
    crawl = dict(months).get(month)
    if not crawl:
        print(f"Arsip bulan {month} tidak ada. Lihat daftar dengan: python panen.py --daftar")
        return
    note = month_note(month)
    if note:
        print(f"Peringatan: {month_label(month)} {note}. Yang dipakai paling baru bulan lalu "
              f"({month_label(last_month())}), paling jauh 12 bulan ke belakang. "
              f"Lihat README bagian Memilih Bulan.\n")

    RESULTS.mkdir(exist_ok=True)
    CACHE.mkdir(exist_ok=True)
    out_file = RESULTS / f"{month}.csv"
    progress_file = CACHE / f"{month}.progres"
    blocks = load_blocks(crawl)
    # Urutan abjad menumpuk situs sejenis: blok awal jauh lebih cepat dari rata-
    # rata, sehingga perkiraan sisa waktu meleset dan hasil yang berhenti di
    # tengah tidak mewakili arsip. Diacak dengan benih nama arsip, jadi urutannya
    # sama tiap kali dijalankan dan catatan progres tetap berlaku.
    random.Random(crawl).shuffle(blocks)

    # Melanjutkan: blok yang sudah selesai dilewati, email yang sudah tercatat
    # tidak ditulis dua kali.
    start_block = int(progress_file.read_text()) if progress_file.exists() else 0
    emails, seen_domains = set(), set()
    if out_file.exists():
        with out_file.open(encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f):
                emails.add(row["email"])
                seen_domains.add(row["domain_situs"])
    if start_block >= len(blocks):
        print(f"Arsip {month_label(month)} sudah selesai dipanen: {len(emails):,} email di "
              f"hasil/{out_file.name}.")
        return

    started = time.time()
    emails_at_start, blocks_this_run, failed = len(emails), 0, 0
    print(f"Arsip {month_label(month)} ({crawl}): {len(blocks)} blok, mulai dari blok "
          f"{start_block + 1}, {len(emails):,} email sudah tercatat. Mulai {time.strftime('%H:%M')}.\n"
          f"Progres dicetak tiap 5 menit. Ctrl+C untuk berhenti; jalankan perintah yang sama "
          f"untuk melanjutkan.", flush=True)

    def report(head):
        elapsed = time.time() - started
        done = start_block + blocks_this_run
        speed = blocks_this_run / max(elapsed, 1)
        eta = (len(blocks) - done) / speed / 3600 if speed else 0
        n = len(emails) or 1
        free = sum(1 for e in emails if jenis(e) == "gratis")
        print(f"{head} blok {done}/{len(blocks)} ({done / len(blocks):.0%}) | "
              f"email {len(emails):,} (+{len(emails) - emails_at_start:,} sesi ini) | "
              f"Gmail {free / n:.0%} | sisa sekitar {eta:.1f} jam | "
              f"diulang {retry_count} | gagal {failed}", flush=True)

    def fetch(item):
        try:
            return item, emails_from_page(item[2]), None
        except Exception as exc:
            return item, (set(), False), exc

    next_report = started + REPORT_EVERY
    stop_reason = None
    last_block_done = [time.time()]
    finished = threading.Event()

    def heartbeat():
        # Baris progres hanya tercetak saat blok selesai. Tanpa ini, server yang
        # sangat lambat tidak bisa dibedakan dari skrip yang macet.
        while not finished.wait(REPORT_EVERY):
            idle = time.time() - last_block_done[0]
            if idle >= REPORT_EVERY:
                print(f"{time.strftime('[%H:%M]')} masih menunggu server: blok terakhir selesai "
                      f"{idle / 60:.0f} menit lalu | diulang {retry_count}", flush=True)

    threading.Thread(target=heartbeat, daemon=True).start()
    with out_file.open("a", newline="", encoding="utf-8") as f, ThreadPoolExecutor(WORKERS) as pool:
        w = csv.writer(f)
        if f.tell() == 0:
            w.writerow(FIELDS)
        try:
            for i in range(start_block, len(blocks)):
                try:
                    picked = candidates_from_block(blocks[i])
                except Exception as exc:
                    raise ConnectionLost(exc)
                pages = [(d, kind, rec) for d, slot in picked.items() if d not in seen_domains
                         for kind in ("beranda", "kontak") for rec in slot[kind]]
                results = list(pool.map(fetch, pages))
                errors = sum(1 for _, _, exc in results if exc)
                # Lebih dari separuh gagal berarti masalahnya koneksi, bukan
                # halamannya: blok ini tidak ditandai selesai, supaya diulang.
                if pages and errors * 2 > len(pages):
                    raise ConnectionLost(f"{errors} dari {len(pages)} halaman gagal")
                failed += errors
                seen_domains.update(d for d, _, _ in pages)
                for (domain, kind, rec), (found, entity), _ in results:
                    for email in sorted(found - emails):
                        emails.add(email)
                        w.writerow([email, domain, domain_category(domain), kind, rec["url"],
                                    jenis(email), "ya" if entity else "tidak"])
                f.flush()
                progress_file.write_text(str(i + 1))
                blocks_this_run += 1
                last_block_done[0] = time.time()
                if time.time() >= next_report:
                    report(time.strftime("[%H:%M]"))
                    next_report += REPORT_EVERY
        except KeyboardInterrupt:
            stop_reason = "Dihentikan dengan Ctrl+C."
        except ConnectionLost as exc:
            stop_reason = (f"Berhenti karena koneksi bermasalah ({exc}). Periksa internet, "
                           f"lalu jalankan perintah yang sama untuk melanjutkan.")
    finished.set()

    print()
    report("=== RINGKASAN ===\n ")
    print(stop_reason or f"Arsip {month_label(month)} selesai dipanen.")
    print(f"Hasil: {out_file}")


def list_months():
    months = available_months()
    print("Bulan arsip yang tersedia, terbaru di atas:")
    for month, crawl in months[:24]:
        note = month_note(month)
        note = f"   ({note})" if note else ""
        done = "   [sudah ada di hasil/]" if (RESULTS / f"{month}.csv").exists() else ""
        print(f"  {month}   {month_label(month):<16} {crawl}{note}{done}")


def merge():
    files = sorted(p for p in RESULTS.glob("????-??.csv"))[::-1]  # terbaru dulu
    if not files:
        print("Belum ada hasil/<bulan>.csv. Kumpulkan dulu hasil semua orang ke folder hasil/.")
        return
    # Ditandai per situs, bukan per halaman: cukup satu halaman situs itu yang
    # menyebut badan usaha.
    first_seen, per_site, entity_sites = {}, Counter(), set()
    print("Email baru yang disumbang tiap bulan (bulan terbaru dihitung lebih dulu):")
    for path in files:
        month = path.stem
        total = added = 0
        with path.open(encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f):
                total += 1
                if row.get("sebut_badan_usaha") == "ya":
                    entity_sites.add(row["domain_situs"])
                if row["email"] not in first_seen:
                    first_seen[row["email"]] = (row["domain_situs"], row["jenis"], month)
                    per_site[row["domain_situs"]] += 1
                    added += 1
        print(f"  {month} {month_label(month):<16} {total:>7,} email, {added:>7,} baru "
              f"({added / max(total, 1):.0%})")

    out_file = RESULTS / "gabungan.csv"
    # utf-8-sig supaya Excel membaca hurufnya dengan benar.
    with out_file.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["email", "domain_situs", "jenis_domain", "jenis", "sebut_badan_usaha",
                    "bulan", "alamat_di_situs_ini"])
        for email, (domain, kind, month) in sorted(first_seen.items()):
            w.writerow([email, domain, domain_category(domain), kind,
                        "ya" if domain in entity_sites else "tidak", month, per_site[domain]])
    free = sum(1 for _, kind, _ in first_seen.values() if kind == "gratis")
    crowded = sum(1 for n in per_site.values() if n > CROWDED)
    print(f"\nTotal email unik: {len(first_seen):,} dari {len(per_site):,} situs, "
          f"Gmail {free / max(len(first_seen), 1):.0%}.")
    print("Menurut jenis domain:")
    for category in ("co.id", "biz.id", "id"):
        domains = [d for d, _, _ in first_seen.values() if domain_category(d) == category]
        entity = sum(1 for d in domains if d in entity_sites)
        print(f"  {category:<7} {len(domains):>7,} email, {entity:>7,} dari situs yang "
              f"menyebut badan usaha ({entity / max(len(domains), 1):.0%})")
    print(f"Situs dengan lebih dari {CROWDED} alamat: {crowded} (lihat kolom alamat_di_situs_ini).")
    print(f"Hasil: {out_file}")
    print("Sebelum dipakai, bersihkan dulu dengan: python panen.py --bersihkan")


ESCAPE_PREFIX = re.compile(r"^(?:(?:u00|x)(?:22|26|27|3c|3e|a0))+")


def repair(email):
    """Merapikan alamat yang tertempel sisa teks halaman; None bila tidak ada
    alamat yang bisa diselamatkan.
    '%20%20sales@dutaint.co.id' -> 'sales@dutaint.co.id'
    'budi@gmail.comtelepon'     -> 'budi@gmail.com'"""
    text = urllib.parse.unquote(email).strip().lower()
    # Alamatnya di ujung; yang di depannya spasi, tanda kutip, atau tag HTML.
    m = re.search(r"[a-z0-9._+-]+@[a-z0-9.-]+$", text)
    if not m:
        return None
    local, domain = m.group().split("@")
    # Sisa kode escape di skrip halaman: ">info@" terbaca "u003einfo@",
    # "\x3einfo@" terbaca "x3einfo@". Hanya kode tanda kutip, <, >, &, dan spasi
    # tak terputus, supaya nama yang kebetulan berawalan x atau u tidak terpotong.
    local = ESCAPE_PREFIX.sub("", local)
    if not local:
        return None
    domain = re.sub(r"\.{2,}", ".", domain)
    if domain in GMAIL_TYPO:
        domain = "gmail.com"
    for free in sorted(FREE_MAIL, key=len, reverse=True):
        rest = domain[len(free):]
        if domain.startswith(free) and (rest.isalpha() or rest == ".com"):
            domain = free
            break
    return f"{local}@{domain}"


def problem(email, row):
    """Alasan alamat ini dibuang, atau None bila layak dipakai."""
    local, domain = email.split("@")
    if not LOCAL_OK.fullmatch(local) or not DOMAIN_OK.fullmatch(domain):
        return "format salah"
    if (PLACEHOLDER_LOCAL.fullmatch(local) or domain in PLACEHOLDER_DOMAINS
            or domain.endswith(JUNK_DOMAINS)):
        return "alamat contoh"
    if NOREPLY.search(local) or SYSTEM_DOMAIN.search(domain) or HEX_LOCAL.fullmatch(local):
        return "alamat sistem"
    if OFFICIAL.search(domain):
        return "pemerintah, kampus, atau sekolah"
    if int(row["alamat_di_situs_ini"]) > CROWDED:
        return f"situs dengan lebih dari {CROWDED} alamat"
    return None


def mail_status(rcode, hosts):
    """'ok' bila domain punya server email. `hosts` berisi nama server email
    dari catatan MX; '.' berarti domain itu menyatakan tidak menerima email."""
    if rcode == 3:
        return "domain tidak ada"
    if rcode != 0:
        return "dns domain rusak"
    if not hosts or hosts == ["."]:
        return "domain tanpa server email"
    return "ok"


def mail_server(domain):
    """Ditanyakan ke DNS Google lewat HTTPS karena pustaka bawaan Python tidak
    bisa menanyakan catatan MX."""
    answer = json.loads(http_get(f"{DNS_HTTPS}?name={domain}&type=MX"))
    hosts = [a["data"].split()[-1] for a in answer.get("Answer", []) if a.get("type") == 15]
    return mail_status(answer.get("Status"), hosts)


def skip_name(data, pos):
    while data[pos] and data[pos] < 0xC0:
        pos += data[pos] + 1
    return pos + (2 if data[pos] else 1)  # penunjuk nama 2 byte, akhir nama 1 byte


def dns_query(domain, tries=3):
    """Jawaban mentah DNS untuk catatan MX, lewat UDP port 53."""
    query = (struct.pack(">HHHHHH", random.getrandbits(16), 0x0100, 1, 0, 0, 0)
             + b"".join(bytes([len(p)]) + p.encode() for p in domain.split("."))
             + b"\x00" + struct.pack(">HH", 15, 1))
    for attempt in range(tries):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.settimeout(5)
                s.sendto(query, (DNS_SERVERS[attempt % len(DNS_SERVERS)], 53))
                reply = s.recv(4096)
            # Gagal sementara (SERVFAIL) sering hilang bila ditanyakan ke server
            # lain, jadi diulang dulu sebelum domainnya dianggap rusak.
            if reply[:2] == query[:2] and (reply[3] & 0xF != 2 or attempt == tries - 1):
                return reply
        except OSError:
            pass
    raise OSError(f"DNS tidak menjawab untuk {domain}")


def mail_server_udp(domain):
    """Sama dengan mail_server, tetapi lewat DNS biasa. Untuk jaringan yang
    memblokir DNS lewat HTTPS, misalnya jaringan kantor."""
    try:
        reply = dns_query(domain)
    except OSError:
        # Server nama domain yang mati membuat DNS tidak menjawab sama sekali.
        # Bila gmail.com juga tidak dijawab, yang putus jaringannya: berhenti.
        dns_query("gmail.com")
        return "dns domain rusak"
    flags, _, answers = struct.unpack(">HHH", reply[2:8])
    if flags & 0x0200 and not answers:
        return "ok"  # jawaban terpotong; tidak dibuang hanya karena itu
    pos = skip_name(reply, 12) + 4
    hosts = []
    for _ in range(answers):
        pos = skip_name(reply, pos)
        rtype, _, _, length = struct.unpack(">HHIH", reply[pos:pos + 10])
        pos += 10
        if rtype == 15:
            hosts.append("." if reply[pos + 2] == 0 else "mx")
        pos += length
    return mail_status(flags & 0xF, hosts)


def https_dns_reachable():
    try:
        urllib.request.urlopen(f"{DNS_HTTPS}?name=gmail.com&type=MX", timeout=10).close()
        return True
    except OSError:
        return False


def mail_servers(domains):
    """Status server email tiap domain, atau None bila terhenti di tengah jalan."""
    # Domain yang sudah diperiksa dicatat, jadi bila terhenti, menjalankan lagi
    # melanjutkan dari domain berikutnya. DNS rusak bisa hanya sementara, jadi
    # yang itu selalu diperiksa ulang.
    CACHE.mkdir(exist_ok=True)
    mx_file = CACHE / "mx.tsv"
    status = {}
    if mx_file.exists():
        with mx_file.open(encoding="utf-8") as f:
            status = dict(line.rstrip("\n").split("\t") for line in f if "\t" in line)
        status = {d: s for d, s in status.items() if s != "dns domain rusak"}
    todo = sorted(domains - status.keys())
    print(f"Memeriksa server email {len(todo):,} domain "
          f"({len(domains) - len(todo):,} sudah diperiksa sebelumnya)...", flush=True)
    check = mail_server
    if todo and not https_dns_reachable():
        print("DNS lewat HTTPS terblokir di jaringan ini; memakai DNS biasa.", flush=True)
        check = mail_server_udp
    pool = ThreadPoolExecutor(DNS_WORKERS)
    try:
        with mx_file.open("a", encoding="utf-8") as f:
            for i, (domain, result) in enumerate(zip(todo, pool.map(check, todo)), 1):
                status[domain] = result
                f.write(f"{domain}\t{result}\n")
                f.flush()
                if i % 1000 == 0:
                    print(f"{time.strftime('[%H:%M]')} {i:,}/{len(todo):,} domain", flush=True)
    except (OSError, http.client.HTTPException, KeyboardInterrupt) as exc:
        print(f"Berhenti ({str(exc) or 'Ctrl+C'}). Jalankan perintah yang sama untuk melanjutkan.")
        return None
    finally:
        pool.shutdown(cancel_futures=True)
    return status


def priority(row):
    """1 kantor di situs co.id, 2 kantor di situs selain co.id yang mencantumkan
    badan usaha, 3 kantor di situs selain co.id tanpa badan usaha, 4 Gmail dan
    sejenisnya (data pribadi menurut UU PDP)."""
    if row["jenis"] == "gratis":
        return 4
    if row["jenis_domain"] == "co.id":
        return 1
    return 2 if row["sebut_badan_usaha"] == "ya" else 3


def clean():
    src = RESULTS / "gabungan.csv"
    if not src.exists():
        print("Belum ada hasil/gabungan.csv. Jalankan dulu: python panen.py --gabung")
        return
    with src.open(encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))

    kept, dropped, repaired = {}, [], 0
    for row in rows:
        email = repair(row["email"])
        if email is None:
            reason = "bukan alamat email"
        elif email in kept:
            reason = "ganda setelah dirapikan"
        else:
            reason = problem(email, row)
        if reason:
            dropped.append((row["email"], row["domain_situs"], reason))
            continue
        repaired += email != row["email"]
        kept[email] = dict(row, email=email, jenis=jenis(email))

    status = mail_servers({e.split("@")[1] for e in kept} - FREE_MAIL)
    if status is None:
        return
    for email in list(kept):
        result = status.get(email.split("@")[1], "ok")
        if result != "ok":
            dropped.append((email, kept.pop(email)["domain_situs"], result))

    out_file = RESULTS / "bersih.csv"
    fields = ["prioritas", "email", "domain_situs", "jenis_domain", "jenis",
              "sebut_badan_usaha", "bulan", "alamat_di_situs_ini"]
    by_priority = Counter()
    with out_file.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fields)
        w.writeheader()
        for row in sorted(kept.values(), key=lambda r: (priority(r), r["domain_situs"], r["email"])):
            by_priority[priority(row)] += 1
            w.writerow(dict(row, prioritas=priority(row)))
    with (RESULTS / "dibuang.csv").open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["email", "domain_situs", "alasan"])
        w.writerows(sorted(dropped, key=lambda d: (d[2], d[0])))

    print(f"\nDari {len(rows):,} alamat: {len(kept):,} layak dipakai "
          f"({repaired:,} di antaranya dirapikan), {len(dropped):,} dibuang.")
    print("Dibuang karena:")
    for reason, n in Counter(d[2] for d in dropped).most_common():
        print(f"  {reason:<38} {n:>6,}")
    print("Layak dipakai, menurut prioritas kirim:")
    labels = {1: "kantor, situs co.id", 2: "kantor, situs lain, ada badan usaha",
              3: "kantor, situs lain, tanpa badan usaha", 4: "Gmail dan sejenisnya"}
    for p in sorted(labels):
        print(f"  {p} {labels[p]:<40} {by_priority[p]:>6,}")
    print(f"Hasil: {out_file}\nYang dibuang beserta alasannya: {RESULTS / 'dibuang.csv'}")


def main():
    args = sys.argv[1:]
    if "--daftar" in args:
        return list_months()
    if "--gabung" in args:
        return merge()
    if "--bersihkan" in args:
        return clean()
    if "--bulan" in args and args.index("--bulan") + 1 < len(args):
        month = args[args.index("--bulan") + 1]
        if re.fullmatch(r"\d{4}-\d{2}", month):
            return harvest(month)
        print("Format bulan: TAHUN-BULAN, misalnya 2026-09.")
        return
    print(__doc__)


if __name__ == "__main__":
    sys.exit(main())
