# Octo-Replay · FRC Türkiye Maç Tekrarı ve Analizi

FRC Team #7682 **Fusion Robotics** için: Türkiye'deki FRC maçlarını (2026 REBUILT, off-season dahil) izle, AI analiz sonuçlarını videoyla senkron gör, scouting notu al.

- Ön yüz: sade HTML/CSS/JS (derleme yok)
- Veri: The Blue Alliance (TBA) — GitHub Actions ile `data/tba/` altına statik JSON olarak indirilir
- İsteğe bağlı PHP + MySQL katmanı: canlı TBA proxy'si ve scouting notları
- Yerel Python analizörü: YouTube maç videolarından robot hareket analizi (`data/{yıl}/{etkinlik}/{maç}.json`)

## GitHub Pages ile yayın (PHP gerekmez)

1. Repoyu GitHub'a yükleyin.
2. **Settings → Secrets and variables → Actions → New repository secret**: ad `TBA_API_KEY`, değer TBA anahtarınız (https://www.thebluealliance.com/account → Read API Keys).
3. **Actions → "Veri güncelle" → Run workflow**. Türkiye 2026 etkinlikleri `data/tba/2026/` altına yazılır; SEO sayfaları (`etkinlik/…`, `sitemap.xml`) otomatik üretilir. Sonra 6 saatte bir kendiliğinden güncellenir.
4. **Settings → Pages → Branch: `main`, klasör `/ (root)`**.
5. (İsteğe bağlı) Özel alan adı kullanıyorsanız **Settings → Variables → Actions** altına `SITE_URL` (örn. `https://replay.team7682.com.tr/`) ekleyin; canonical/sitemap ona göre üretilir.

> Statik yayında scouting notları kapalıdır (sunucu tarafı gerekir); diğer her şey çalışır.

## SEO

`tools/seo_build.py` (Actions içinde otomatik çalışır): `index.html` içindeki `<!--seo:start-->` bloğunu (canonical, Open Graph, Twitter, JSON-LD), `sitemap.xml`, `robots.txt` ve her etkinlik için taranabilir `etkinlik/{anahtar}/` sayfasını üretir. Elle: `python tools/seo_build.py --site-url https://alanadiniz/`. Search Console'a `sitemap.xml` adresini ekleyin.

## Yerel çalıştırma

- Windows: `baslat.bat`, macOS/Linux: `./baslat.sh` (tarayıcıda `http://localhost:…`). `index.html`'i çift tıklayarak (file://) açmayın; tarayıcı veri yüklemeyi engeller.
- Veri indirme: `TBA_API_KEY=... python tools/fetch_tba.py --year 2026`

## PHP + MySQL ile yayın (notlar dahil)

`api/config.local.example.php` dosyasını `api/config.local.php` olarak kopyalayıp TBA anahtarı ve MySQL bilgilerini doldurun, `schema.sql`'i içe aktarın. `api/tba-proxy.php` 10 dk önbellekli proxy'dir; PHP yoksa ön yüz otomatik olarak `data/tba/` dosyalarına döner. `config.local.php` `.gitignore` içindedir; **anahtarı asla depoya yüklemeyin**.

## AI analizi ve eğitim (yerel)

```
pip install -r requirements.txt
python tools/extract_frames.py --all-turkey --year 2026 --every 3   # eğitim kareleri
python tools/split_dataset.py                                        # YOLO veri seti
python analyzer.py all --year 2026 [--weights best.pt]               # maç analizi
```
Çıktılar `data/2026/{etkinlik}/{maç}.json` olarak kaydedilir; şema için `data/schema-example.json`. Analizi olmayan maçlar TBA verisi + "AI Analizi Bekleniyor" rozetiyle görünür.

## Dizin

`index.html`, `css/`, `js/`, `assets/` ön yüz · `data/tba/` TBA verisi · `data/{yıl}/` AI analizleri · `api/` PHP · `tools/` veri/SEO/eğitim araçları · `analyzer.py` analizör · `.github/workflows/` otomasyon.

Veriler The Blue Alliance'tan alınır. FIRST® ve FRC® FIRST'ün tescilli markalarıdır; bu proje resmî değildir.
