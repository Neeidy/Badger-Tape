# Badger Tape — Claude Code Talimatları

## Proje Nedir
Badger Tape bir lofi müzik YouTube kanalıdır.
Kanal: youtube.com/@BadgerTape
Maskot: Porsuk adlı bir kedi karakteri — kulaklıklı, gece hayvanı, yuva/in teması
Slogan: "drift away, one beat at a time"
Hedef: Tür lideri lofi kanallarıyla yarışabilen, özgün marka kimliğine sahip, AI destekli üretim pipeline'ı kurmak

## İçerik Stratejisi (tür benchmark analizine göre)
- Upload ritmi: Haftada 2-3 video
- Süre: 1 saat (çalışma) veya 8 saat (uyku)
- Başlık formatı: küçük harf, emoji, "beats to [verb] to" imzası
- POV başlıkları: "pov: you..." formatı yüksek performans
- Mevsimsel içerik: Özel günlere özel videolar

## Başlık Şablonları
- badger tape 🎧 beats to study/relax to
- midnight burrow 🌙 [1 hour] lofi beats to drift away to
- pov: it's 3am and you can't sleep 🌙 lofi beats for the quiet hours
- 8 hours of cozy lofi 🎧 beats to sleep to
- [sezon] lofi mix 🍂 beats to [aktivite] to

## Açıklama Şablonu
🎧 | Badger Tape on Spotify and all platforms
→ [link]

🌎 | Follow Badger Tape
→ [sosyal medya linkleri]

🦡 All music produced with AI tools and curated with care.
Thank you for listening. Stay cozy.

## Pipeline (Sıralı İş Akışı)
1. Tema belirle (mevsim, aktivite, zaman)
2. Suno/Udio ile müzik üret
3. Nano Banana Pro 2 ile Porsuk animasyon görseli üret
4. CapCut ile video birleştir
5. Başlık, açıklama, tag yaz
6. YouTube'a yükle

## Teknoloji Stack
- Müzik: Suno, Udio
- Görsel: Nano Banana Pro 2
- Video: CapCut
- Otomasyon: n8n
- Deploy: YouTube API
- Analiz: Firecrawl

## Kurallar
- İçerik kararlarında tür benchmark analiz notlarına (yerel referans) başvur
- Başlıklar küçük harf olsun
- Porsuk karakteri her videoda olsun
- Mevsimsel takvime uy
- Token tasarrufu için /compact kullan uzun oturumlarda

## fal.ai Harcama Limiti
- Her görsel üretiminden önce kullanıcıdan onay al
- Tek seferde maksimum 3 görsel üret
- Toplam harcama $2 limitini geçme, geçecekse dur ve sor
- Her işlem sonrası tahmini harcamayı bildir
- $2 limit aşılırsa tüm fal.ai işlemlerini durdur

## Müzik Pipeline Kuralları
- Her pipeline çalıştığında 10 adet MiniMax Music v2 parçası üret
- Model: fal-ai/minimax-music/v2
- Her parça için prompt şu master promptu baz alsın ama tempo, enstrüman ve mood parametrelerini rastgele değiştirsin:
  "Lofi hip hop, instrumental, no vocals, [65-85] BPM, [enstrüman kombinasyonu], melancholic nostalgic mood, late night rainy atmosphere, minimal vinyl crackle, subtle tape hiss, clean mix"
- lyrics_prompt: "[Intro]\n[Inst]\n[Verse]\n[Inst]\n[Bridge]\n[Inst]\n[Outro]\n[Inst]"
- 10 parça toplam 3600 saniyeyi tamamlamazsa baştan loop et
- Çıktılar music/ klasörüne kaydedilsin

## Güvenlik Kuralları
- .env dosyası asla GitHub'a yüklenmez
- API key'ler asla kod içine yazılmaz, her zaman .env'den okunur
- .gitignore'da .env olduğunu her zaman kontrol et
- Yeni API key eklendiğinde .env'e ekle, koda yazma
- FAL_KEY, YOUTUBE_API_KEY gibi tüm key'ler .env'de tutulur
