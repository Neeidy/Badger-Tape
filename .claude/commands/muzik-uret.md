Suno veya Udio için lofi müzik promptu üret.

Kullanıcıdan şunları al (belirtilmemişse makul varsayılanlar kullan):
- **Tema**: (örn. yağmurlu gece, sonbahar, sabah kahvesi, kış, uzay)
- **Süre**: 1 saat veya 8 saat
- **Mood**: (örn. melankoli, cozy, odaklanma, uyku, nostaljik)

Ardından şu formatta iki ayrı prompt üret:

---

## Suno Promptu

```
[genre: lofi hip hop]
[mood: {mood}]
[instruments: lo-fi drums, vinyl crackle, mellow piano, soft bass, {temaya uygun ek enstrüman}]
[tempo: 70-85 bpm]
[atmosphere: {tema ve mood'a uygun 1-2 cümle atmosfer açıklaması}]
[no vocals]
[duration: ~3 minutes per track]
```

**Başlık önerisi (Suno için):** `{tema} lofi - track {numara}`

---

## Udio Promptu

```
lofi hip hop, {mood}, {tema atmosferi}, mellow beats, vinyl texture,
soft piano, gentle bass, {temaya uygun detaylar},
no vocals, instrumental, relaxing, {bpm} bpm
```

---

## Playlist Notları
- Bu tema için **{süreye göre track sayısı}** track üretilmeli (1 saat ≈ 18-20 track, 8 saat ≈ 144-160 track)
- Trackleri hafif varyasyonlarla üret: bazıları daha yavaş, bazıları biraz daha enerjik
- Vinyl crackle ve room tone detayları her track'te olsun
- Track isimleri: `{tema} lofi 01`, `{tema} lofi 02` şeklinde numaralandır
