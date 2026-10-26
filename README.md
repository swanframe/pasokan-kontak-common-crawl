# Pasokan Kontak dari Common Crawl

Mengumpulkan alamat email perusahaan Indonesia dari arsip Common Crawl, untuk
dikirimi campaign oleh Sales Agent.

Common Crawl adalah lembaga nirlaba yang menerbitkan salinan miliaran halaman
web hampir tiap bulan, gratis. Halaman situs perusahaan `.co.id`, `.biz.id`,
dan `.id` sudah ada di arsip itu, jadi email dicari di salinannya. Situs
perusahaannya tidak dikunjungi satu per satu.

Asal metode ini: riset SCRUM-19 di Jira, Metode 1.

## Yang dibutuhkan

- Python 3.9 atau lebih baru. Tidak ada yang perlu dipasang.
- Internet. Satu bulan arsip mengunduh beberapa GB.

## Memanen satu bulan

```
python panen.py --bulan 2026-09
```

- Satu bulan arsip butuh **sekitar 8 jam**. Tiap 5 menit muncul baris progres
  berisi perkiraan sisa waktu.
- **Boleh berhenti kapan saja**: Ctrl+C, laptop mati, atau internet putus.
  Jalankan perintah yang sama lagi, dan ia melanjutkan dari blok terakhir.
- Selama berjalan, atur laptop supaya tidak tidur (Windows: Settings, System,
  Power, Sleep: Never) dan colokkan charger.
- Hasilnya `hasil/2026-09.csv`. Nama berkasnya bulan arsip, jadi siapa pun yang
  memanen, berkasnya langsung terbaca isinya.

## Memilih bulan

Lihat bulan yang tersedia:

```
python panen.py --daftar
```

Patokannya, urut dari yang paling penting:

1. **Mulai dari bulan lalu.** Di bulan Oktober, yang dipakai arsip September;
   di bulan November, arsip Oktober. Arsip bulan berjalan tidak dipakai walau
   sudah terbit, supaya aturannya tidak bergantung pada kapan arsip itu muncul.
2. **Terbaru dulu, mundur dari bulan lalu.** Makin baru arsipnya, makin besar
   kemungkinan emailnya masih aktif. Email mati menjadi bounce, dan bounce yang
   banyak merusak reputasi domain pengirim.
3. **Paling jauh 12 bulan ke belakang.** Arsip yang lebih tua tetap bisa
   dipanen, tetapi skrip akan memberi peringatan. Angka 12 bulan ini patokan
   awal, bukan hasil ukur.
4. **Gabungkan dulu sebelum menambah bulan.** Bulan yang berdekatan kemungkinan
   banyak memuat situs yang sama. Bila `--gabung` menunjukkan bulan kedua hanya
   menyumbang sedikit email baru, bulan berikutnya sebaiknya diambil yang lebih
   jauh, bukan yang bersebelahan.

Contoh di bulan Oktober: September menghasilkan 20 ribu email. Agustus juga 20
ribu, tetapi 16 ribu di antaranya sudah ada di September, jadi Agustus hanya
menambah 4 ribu. Bulan ketiga sebaiknya Maret, bukan Juli.

`--daftar` menandai bulan berjalan dan bulan yang lebih dari 12 bulan, jadi
aturan di atas tidak perlu dihitung sendiri.

## Membagi kerja

- **Satu orang, satu bulan.** Sepakati dulu siapa mengambil bulan apa, supaya
  tidak ada bulan yang dipanen dua kali.
- **Satu laptop, satu jaringan.** Server arsip membatasi permintaan per alamat
  internet. Dua laptop di jaringan yang sama berbagi batas itu dan sama-sama
  melambat.

## Menggabungkan

1. Kumpulkan berkas `hasil/<bulan>.csv` dari semua orang ke folder `hasil/` di
   satu laptop.
2. Jalankan:

```
python panen.py --gabung
```

Hasilnya `hasil/gabungan.csv`, bisa dibuka di Excel. Email ganda sudah dibuang.
Di layar tercetak berapa email **baru** yang disumbang tiap bulan, yaitu angka
yang dipakai untuk memilih bulan berikutnya.

Dua kolom membantu memilah situs perusahaan dari yang bukan:

- `jenis_domain`: `co.id` hanya boleh didaftarkan badan usaha berbadan hukum,
  jadi paling bisa dipegang. `biz.id` untuk usaha, tetapi syaratnya longgar.
  `id` biasa boleh milik siapa saja: perusahaan, blog, portal berita.
- `sebut_badan_usaha`: `ya` bila ada halaman situs itu yang menyebut PT, CV,
  Tbk, atau Persero. Ini penanda, bukan bukti: portal berita juga sering
  menyebut PT penerbitnya. Paling berguna untuk memilah situs `id` biasa.

Kolom `alamat_di_situs_ini` menunjukkan berapa alamat yang ditemukan di situs
yang sama. Situs dengan puluhan atau ratusan alamat biasanya halaman peserta
lomba atau daftar pelanggan, bukan alamat perusahaan itu sendiri.

`gabungan.csv` belum siap dikirimi email. Bersihkan dulu.

## Membersihkan

```
python panen.py --bersihkan
```

Gratis, tanpa layanan verifikasi berbayar. Sekitar 6 menit untuk 40 ribu
alamat. Bila terhenti, jalankan perintah yang sama dan ia melanjutkan.

Yang dikerjakan:

- **Merapikan** alamat yang tertempel sisa teks halaman: `%20%20sales@x.co.id`
  menjadi `sales@x.co.id`, `budi@gmail.comtelepon` menjadi `budi@gmail.com`,
  `budi@gmail.co` menjadi `budi@gmail.com`.
- **Membuang** alamat contoh (`contoh@email.id`, `abc@abc.com`), alamat sistem
  (`no-reply@`, pelacak galat Sentry), alamat pemerintah, kampus, dan sekolah,
  serta alamat dari situs dengan lebih dari 20 alamat.
- **Memeriksa server email** tiap domain lewat DNS Google. Domain yang sudah
  tidak ada atau tidak punya server email dibuang, karena email ke sana pasti
  bounce. Yang dikirim ke Google hanya nama domain, bukan alamat email. Di
  jaringan yang memblokir DNS lewat HTTPS, seperti jaringan kantor, skrip
  otomatis memakai DNS biasa ke 8.8.8.8 dan 1.1.1.1.

Hasilnya:

- `hasil/bersih.csv`, siap dipakai, berurutan menurut kolom `prioritas`:
  1. alamat kantor di situs `co.id`, paling bisa dipegang;
  2. alamat kantor lain, di situs yang menyebut badan usaha;
  3. alamat kantor lain;
  4. Gmail dan sejenisnya, data pribadi menurut UU PDP.
- `hasil/dibuang.csv`, yang dibuang beserta alasannya, untuk dicek bila ada
  yang terasa salah buang.

Yang **tidak** bisa diperiksa gratis: apakah kotak surat orangnya masih ada.
Domain bisa hidup sementara alamat `budi@` sudah dihapus. Itu baru ketahuan
saat dikirim, jadi mulailah dari prioritas 1 dalam jumlah kecil dan biarkan
pengirim (misalnya Mailgun) mencatat yang bounce supaya tidak dikirimi lagi.

Hasil pemeriksaan domain disimpan di `cache/mx.tsv`. Hapus berkas itu bila
ingin memeriksa ulang dari awal, misalnya setelah beberapa bulan.

## Hasil tidak masuk repo

Folder `hasil/` dan `cache/` ditolak `.gitignore`.

- `hasil/` berisi alamat email, sekitar 25% di antaranya Gmail pribadi yang
  termasuk data pribadi menurut UU PDP. Bagikan hanya lewat Drive privat.
- `cache/` berisi peta indeks arsip (sekitar 105 MB per bulan), catatan
  progres, dan hasil pemeriksaan domain. Semuanya bisa dibuat ulang.

## Angka acuan

Dari uji 21 dan 22 Oktober 2026 dengan arsip September 2026, termasuk jalan 3
jam tanpa henti yang mengolah 37% arsip:

- Sekitar 45% situs perusahaan mencantumkan email.
- Sekitar 75% email adalah alamat kantor, 25% Gmail dan sejenisnya.
- Sekitar 2.500 email per jam, stabil.
- Satu bulan arsip: sekitar 20 ribu email, sekitar 8 jam.

## Batasan

- `--bersihkan` memeriksa domain, bukan kotak surat. Sebagian alamat tetap bisa
  bounce.
- Situs `.id` yang bukan perusahaan, misalnya portal berita, ikut terambil.
- Perusahaan Indonesia yang memakai `.com` belum tercakup.
