"""Menyatukan hasil Common Crawl dengan data Kemenperin dan kunjungan situs dari
repo id-company-domains (riset SCRUM-10) menjadi satu daftar kirim.

    python satukan.py <folder data id-company-domains>

Butuh hasil/bersih.csv (python panen.py --bersihkan) dan, di folder data,
indonesian_companies.csv serta indonesian_company_emails.csv hasil ekspor repo
id-company-domains. Hasil: hasil/pasokan.csv (siap pakai) dan
hasil/pasokan_dibuang.csv (beserta alasannya).

Hanya pustaka bawaan Python 3.9 atau lebih baru. Panduan di README.md.
"""

import csv
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

import panen

# Webmail dan ISP rumahan: alamat orang, bukan alamat kantor. Di luar
# panen.FREE_MAIL karena di Common Crawl jumlahnya tidak berarti, di Kemenperin
# ratusan.
PUBLIC_MAIL = panen.FREE_MAIL | {
    "googlemail.com", "yahoo.co.uk", "yahoo.com.sg", "hotmail.co.id", "outlook.co.id",
    "msn.com", "me.com", "mac.com", "zoho.com", "proton.me", "protonmail.com",
    "tutanota.com", "yandex.com", "gmx.com", "163.com", "126.com", "qq.com", "sina.com",
    "naver.com", "telkom.net", "telkom.net.id", "cbn.net.id", "centrin.net.id",
    "rad.net.id", "indosat.net.id", "indo.net.id", "pacific.net.id", "biznet.net.id",
}
# Kotak surat sekali pakai: pendaftar SIINas memakainya untuk lolos isian wajib.
# Tidak ada yang membacanya.
DISPOSABLE = {
    "yopmail.com", "yopmail.fr", "yopmail.net", "mailnesia.com", "getnada.com",
    "atomicmail.io", "mailto.plus", "macr2.com", "mailtemp.biz.id", "tempmail.com",
    "guerrillamail.com", "mailinator.com", "sharklasers.com", "dispostable.com",
    "10minutemail.com", "throwawaymail.com", "trashmail.com", "mohmal.com",
    "inboxkitten.com", "dropmail.me",
}
# Satu kotak surat yang terdaftar untuk sebanyak ini usaha milik dinas,
# pendamping UMKM, atau jasa perizinan yang mendaftarkan mereka, bukan milik
# usahanya: industri.kabsbb@gmail.com tercatat untuk 245 usaha. Kebanyakan
# memakai trik titik Gmail supaya lolos syarat satu alamat satu usaha:
# diskopda.g.r.in.si.i.nas@ dan 749 varian lainnya satu kotak surat.
SHARED = 3
# Domain kantor yang dipakai sebanyak ini usaha, hampir semuanya perusahaan
# rumahan, adalah alamat buatan balai atau pendamping halal
# (abdulhaya@bspjimanado.com). Domain grup perusahaan (japfa.com) tidak
# terkena karena usahanya PT.
FACILITATOR_MIN, FACILITATOR_SHARE = 10, 0.8
ADDRESS = re.compile(r"[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,24}")
ENTITY_NAME = re.compile(r"^(?:PT|CV)\b|\bTbk\b|\bPersero\b", re.I)
FIELDS = ["prioritas", "email", "nama_usaha", "kbli", "domain_situs", "jenis_domain",
          "jenis", "sebut_badan_usaha", "sumber"]


def mailbox(email):
    """Kunci satu kotak surat: Gmail mengabaikan titik dan +label, jadi
    bantuanijin@, bantuan.ijin@, dan bant.uani.jin@ satu kotak yang sama."""
    local, domain = email.split("@")
    if domain in ("gmail.com", "googlemail.com"):
        return local.split("+")[0].replace(".", "") + "@gmail.com"
    return email


def domain_kind(domain):
    kind = panen.domain_category(domain)
    return kind if kind != "id" or domain.endswith(".id") else "lain"


def read_csv(path):
    with path.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def kemenperin_companies(data):
    """Kotak surat -> usaha yang mendaftarkannya di SIINas."""
    companies = defaultdict(list)
    for row in read_csv(data / "indonesian_companies.csv"):
        if row["source"] != "kemenperin":
            continue
        # Sebagian isian memuat lebih dari satu alamat.
        for email in ADDRESS.findall(row["email"].lower()):
            companies[mailbox(email)].append(row)
    return companies


def facilitator_domains(companies):
    names = defaultdict(list)
    for key, rows in companies.items():
        domain = key.split("@")[1]
        if domain not in PUBLIC_MAIL:
            names[domain] += [r["name"] for r in rows]
    return {d for d, n in names.items() if len(n) >= FACILITATOR_MIN
            and sum(x.startswith("PR ") for x in n) / len(n) >= FACILITATOR_SHARE}


def dzaky_rows(data):
    """Baris id-company-domains dalam bentuk baris panen, plus yang dibuang
    sebelum itu."""
    companies = kemenperin_companies(data)
    facilitators = facilitator_domains(companies)
    emails = read_csv(data / "indonesian_company_emails.csv")
    per_site = Counter(r["domain"] for r in emails if r["source"] == "contact_scrape")
    rows, dropped = [], []
    for e in emails:
        # verified_executive adalah tebakan alamat direksi (Metode 4), di luar
        # daftar ini.
        if e["source"] not in ("kemenperin", "contact_scrape"):
            continue
        email = e["email"].strip().lower()
        domain = email.split("@")[-1]
        if e["source"] == "kemenperin":
            owners = companies.get(mailbox(email), [])
            if domain in DISPOSABLE:
                dropped.append((email, "", "kotak surat sekali pakai"))
                continue
            if len(owners) >= SHARED:
                dropped.append((email, "", f"satu kotak surat untuk {SHARED} usaha atau lebih"))
                continue
            if domain in facilitators:
                dropped.append((email, "", "alamat buatan pendamping"))
                continue
            name = owners[0]["name"] if owners else e["company_name"]
            rows.append({"email": email, "nama_usaha": name,
                         "kbli": owners[0]["kbli"] if owners else "",
                         "domain_situs": "", "jenis_domain": domain_kind(domain),
                         "sebut_badan_usaha": "ya" if ENTITY_NAME.search(name) else "tidak",
                         "sumber": "kemenperin", "alamat_di_situs_ini": "1"})
        else:
            site = e["domain"]
            name = e["company_name"]
            rows.append({"email": email, "nama_usaha": name, "kbli": "",
                         "domain_situs": site, "jenis_domain": domain_kind(site),
                         "sebut_badan_usaha": "ya" if ENTITY_NAME.search(name) else "tidak",
                         "sumber": "kunjungan situs",
                         "alamat_di_situs_ini": str(per_site[site])})
    return rows, dropped


def main():
    if len(sys.argv) != 2:
        print(__doc__)
        return
    data = Path(sys.argv[1])
    cc_file = panen.RESULTS / "bersih.csv"
    if not cc_file.exists():
        print("Belum ada hasil/bersih.csv. Jalankan dulu: python panen.py --bersihkan")
        return
    if not (data / "indonesian_company_emails.csv").exists():
        print(f"Tidak ada indonesian_company_emails.csv di {data}.")
        return

    # Common Crawl lebih dulu: sudah dibersihkan dan diperiksa server emailnya.
    kept, cc = {}, {}
    for row in read_csv(cc_file):
        kept[row["email"]] = cc[mailbox(row["email"])] = dict(
            row, nama_usaha="", kbli="", sumber=f"common crawl {row['bulan']}")
    from_cc = len(kept)

    rows, dropped = dzaky_rows(data)
    total_dzaky = len(rows) + len(dropped)
    fresh, seen, overlap = {}, set(), 0
    for row in rows:
        email = panen.repair(row["email"])
        key = mailbox(email) if email else None
        if email is None:
            dropped.append((row["email"], row["domain_situs"], "bukan alamat email"))
            continue
        if key in cc:
            # Tetap satu baris, tetapi nama usaha dan KBLI ikut: berguna untuk
            # isi email, dan Common Crawl tidak punya keduanya.
            if not cc[key]["nama_usaha"]:
                cc[key].update(nama_usaha=row["nama_usaha"], kbli=row["kbli"])
            overlap += 1
            continue
        reason = "ganda" if key in seen else panen.problem(email, row)
        if reason:
            dropped.append((row["email"], row["domain_situs"], reason))
            continue
        seen.add(key)
        fresh[email] = dict(row, email=email,
                            jenis="gratis" if email.split("@")[1] in PUBLIC_MAIL else "bukan_gratis")

    status = panen.mail_servers({e.split("@")[1] for e in fresh} - panen.FREE_MAIL)
    if status is None:
        return
    for email in list(fresh):
        result = status.get(email.split("@")[1], "ok")
        if result != "ok":
            dropped.append((email, fresh.pop(email)["domain_situs"], result))
    kept.update(fresh)

    out_file = panen.RESULTS / "pasokan.csv"
    by_priority, by_source = Counter(), Counter()
    order = lambda r: (panen.priority(r), r["sebut_badan_usaha"] != "ya",
                       r["domain_situs"] or r["email"].split("@")[1], r["email"])
    with out_file.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, FIELDS, extrasaction="ignore")
        w.writeheader()
        for row in sorted(kept.values(), key=order):
            p = panen.priority(row)
            by_priority[p] += 1
            by_source[row["sumber"].split(" 20")[0]] += 1
            w.writerow(dict(row, prioritas=p))
    with (panen.RESULTS / "pasokan_dibuang.csv").open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["email", "domain_situs", "alasan"])
        w.writerows(sorted(dropped, key=lambda d: (d[2], d[0])))

    print(f"\nCommon Crawl: {from_cc:,} alamat bersih.")
    print(f"id-company-domains: {total_dzaky:,} alamat, {overlap:,} sudah ada di Common Crawl, "
          f"{len(fresh):,} baru, {len(dropped):,} dibuang.")
    print(f"Total: {len(kept):,} alamat.")
    print("Dibuang karena:")
    for reason, n in Counter(d[2] for d in dropped).most_common():
        print(f"  {reason:<38} {n:>7,}")
    print("Menurut sumber:")
    for source, n in by_source.most_common():
        print(f"  {source:<38} {n:>7,}")
    print("Menurut prioritas kirim:")
    labels = {1: "kantor, situs co.id", 2: "kantor, situs lain, ada badan usaha",
              3: "kantor, situs lain, tanpa badan usaha", 4: "Gmail dan sejenisnya"}
    for p in sorted(labels):
        print(f"  {p} {labels[p]:<40} {by_priority[p]:>7,}")
    print(f"Hasil: {out_file}\nYang dibuang beserta alasannya: "
          f"{panen.RESULTS / 'pasokan_dibuang.csv'}")


if __name__ == "__main__":
    sys.exit(main())
