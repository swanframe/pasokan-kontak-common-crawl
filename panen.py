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

Hanya pustaka bawaan Python 3.9 atau lebih baru. Panduan lengkap di README.md.
"""

import csv
import gzip
import html
import http.client
import json
import re
import sys
import time
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
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
NOT_EMAIL_TLD = {"png", "jpg", "jpeg", "gif", "svg", "webp", "css", "js", "ico"}
FREE_MAIL = {
    "gmail.com", "yahoo.com", "yahoo.co.id", "hotmail.com", "outlook.com",
    "live.com", "ymail.com", "icloud.com", "rocketmail.com", "aol.com",
}
JUNK_DOMAINS = (
    "example.com", "domain.com", "email.com", "yourdomain.com", "mysite.com",
    "sentry.io", "wixpress.com", "sentry-next.wixpress.com",
)
FIELDS = ["email", "domain_situs", "halaman", "url", "jenis"]

retry_count = 0


class ConnectionLost(Exception):
    """Terlalu banyak yang gagal sekaligus: kemungkinan internet putus."""


def http_get(url, byte_range=None):
    global retry_count
    headers = {"User-Agent": "pasokan-kontak-common-crawl"}
    if byte_range:
        headers["Range"] = f"bytes={byte_range[0]}-{byte_range[1]}"
    for attempt in range(HTTP_TRIES):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=60) as resp:
                return resp.read()
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
        tmp.write_bytes(http_get(f"{index_url}/cluster.idx"))
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
    return found


def jenis(email):
    return "gratis" if email.split("@", 1)[1] in FREE_MAIL else "bukan_gratis"


def harvest(month):
    months = available_months()
    crawl = dict(months).get(month)
    if not crawl:
        print(f"Arsip bulan {month} tidak ada. Lihat daftar dengan: python panen.py --daftar")
        return
    newest = months[0][0]
    age = (int(newest[:4]) - int(month[:4])) * 12 + int(newest[5:]) - int(month[5:])
    if age >= MAX_AGE_MONTHS:
        print(f"Peringatan: arsip ini {age} bulan lebih tua dari yang terbaru ({newest}). "
              f"Email lama lebih berisiko sudah mati; lihat README bagian Memilih Bulan.\n")

    RESULTS.mkdir(exist_ok=True)
    CACHE.mkdir(exist_ok=True)
    out_file = RESULTS / f"{month}.csv"
    progress_file = CACHE / f"{month}.progres"
    blocks = load_blocks(crawl)

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
            return item, set(), exc

    next_report = started + REPORT_EVERY
    stop_reason = None
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
                for (domain, kind, rec), found, _ in results:
                    for email in sorted(found - emails):
                        emails.add(email)
                        w.writerow([email, domain, kind, rec["url"], jenis(email)])
                f.flush()
                progress_file.write_text(str(i + 1))
                blocks_this_run += 1
                if time.time() >= next_report:
                    report(time.strftime("[%H:%M]"))
                    next_report += REPORT_EVERY
        except KeyboardInterrupt:
            stop_reason = "Dihentikan dengan Ctrl+C."
        except ConnectionLost as exc:
            stop_reason = (f"Berhenti karena koneksi bermasalah ({exc}). Periksa internet, "
                           f"lalu jalankan perintah yang sama untuk melanjutkan.")

    print()
    report("=== RINGKASAN ===\n ")
    print(stop_reason or f"Arsip {month_label(month)} selesai dipanen.")
    print(f"Hasil: {out_file}")


def list_months():
    months = available_months()
    print("Bulan arsip yang tersedia, terbaru di atas:")
    for i, (month, crawl) in enumerate(months[:24]):
        note = "" if i < MAX_AGE_MONTHS else "   (lebih dari 12 bulan)"
        done = "   [sudah ada di hasil/]" if (RESULTS / f"{month}.csv").exists() else ""
        print(f"  {month}   {month_label(month):<16} {crawl}{note}{done}")


def merge():
    files = sorted(p for p in RESULTS.glob("????-??.csv"))[::-1]  # terbaru dulu
    if not files:
        print("Belum ada hasil/<bulan>.csv. Kumpulkan dulu hasil semua orang ke folder hasil/.")
        return
    first_seen, per_site = {}, Counter()
    print("Email baru yang disumbang tiap bulan (bulan terbaru dihitung lebih dulu):")
    for path in files:
        month = path.stem
        total = added = 0
        with path.open(encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f):
                total += 1
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
        w.writerow(["email", "domain_situs", "jenis", "bulan", "alamat_di_situs_ini"])
        for email, (domain, kind, month) in sorted(first_seen.items()):
            w.writerow([email, domain, kind, month, per_site[domain]])
    free = sum(1 for _, kind, _ in first_seen.values() if kind == "gratis")
    crowded = sum(1 for n in per_site.values() if n > 20)
    print(f"\nTotal email unik: {len(first_seen):,} dari {len(per_site):,} situs, "
          f"Gmail {free / max(len(first_seen), 1):.0%}.")
    print(f"Situs dengan lebih dari 20 alamat: {crowded} (lihat kolom alamat_di_situs_ini).")
    print(f"Hasil: {out_file}")


def main():
    args = sys.argv[1:]
    if "--daftar" in args:
        return list_months()
    if "--gabung" in args:
        return merge()
    if "--bulan" in args and args.index("--bulan") + 1 < len(args):
        month = args[args.index("--bulan") + 1]
        if re.fullmatch(r"\d{4}-\d{2}", month):
            return harvest(month)
        print("Format bulan: TAHUN-BULAN, misalnya 2026-09.")
        return
    print(__doc__)


if __name__ == "__main__":
    sys.exit(main())
