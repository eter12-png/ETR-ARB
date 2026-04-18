import asyncio
import aiohttp
import math
import urllib.parse
import os
import random
import logging
from datetime import datetime
from collections import Counter
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# --- LOGGING ---
logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# --- AYARLAR ---
API_KEY = os.getenv("CSFLOAT_API_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
MIN_VOLUME_LIMIT = 50

# --- PROFESYONEL VE RENKLİ PROGRESS BAR ---
def generate_premium_bar(current, total, baslik="İŞLEM"):
    if total <= 0: return f"🌀 **{baslik}**\n`Hazırlanıyor...`"
    
    bar_uzunlugu = 10
    oran = current / total
    dolu = int(oran * bar_uzunlugu)
    
    # Renk paleti seçimi
    if oran < 0.33:
        renk = "🟧"
    elif oran < 0.66:
        renk = "🟦"
    else:
        renk = "🟩"
        
    bar = renk * dolu + "⬜" * (bar_uzunlugu - dolu)
    yuzde = round(oran * 100, 1)
    
    return (
        f"💠 **{baslik}**\n"
        f"┣ {bar}  `%{yuzde}`\n"
        f"┗━━━━━━━━━━━━━━"
    )

async def durdurulabilir_bekleme(saniye, context):
    """0.5 saniyelik adımlarla durdurma kontrolü yapar."""
    adimlar = int(saniye / 0.5)
    for _ in range(adimlar):
        if context.user_data.get('stop_scan'):
            return True
        await asyncio.sleep(0.5)
    return False

def item_listesini_yukle():
    if os.path.exists("items.txt"):
        with open("items.txt", "r", encoding="utf-8") as f:
            return [line.strip() for line in f.readlines() if line.strip()]
    return []

def steam_net_hesapla(alici_oder):
    if alici_oder < 0.03: return 0
    satici_alir = math.floor(alici_oder / 1.15 * 100) / 100
    while True:
        s_komisyon = max(0.01, math.floor(satici_alir * 0.05 * 100 + 0.000001) / 100)
        g_komisyon = max(0.01, math.floor(satici_alir * 0.10 * 100 + 0.000001) / 100)
        if round(satici_alir + s_komisyon + g_komisyon, 2) <= alici_oder:
            sonraki = round(satici_alir + 0.01, 2)
            if round(sonraki + max(0.01, math.floor(sonraki * 0.05 * 100 + 0.000001) / 100) + max(0.01, math.floor(sonraki * 0.10 * 100 + 0.000001) / 100), 2) <= alici_oder:
                satici_alir = sonraki
            else: break
        else: satici_alir = round(satici_alir - 0.01, 2)
    return satici_alir

async def veri_cek(session, isim, sira, toplam):
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0 Safari/537.36'}
    guvenli_isim = urllib.parse.quote(isim)
    s_url = f"https://steamcommunity.com/market/priceoverview/?appid=730&currency=1&market_hash_name={guvenli_isim}"
    f_url = f"https://csfloat.com/api/v1/listings?market_hash_name={guvenli_isim}&limit=50&sort_by=lowest_price&type=buy_now"

    try:
        async with session.get(s_url, headers=headers, timeout=15) as r_s:
            if r_s.status == 429: return ("TEKRAR", "429")
            if r_s.status != 200: return ("ATLA", "Hata")
            s_data = await r_s.json()
            if not s_data or "lowest_price" not in s_data: return ("ATLA", "FiyatYok")
            hacim_ham = str(s_data.get("volume", "0")).replace(",", "")
            hacim = int(hacim_ham) if hacim_ham.isdigit() else 0
            if hacim < MIN_VOLUME_LIMIT: return ("ATLA", "DüşükHacim")
            s_fiyat = float(s_data["lowest_price"].replace("$", "").replace(",", ""))

        await asyncio.sleep(0.5)

        async with session.get(f_url, headers={"Authorization": API_KEY}, timeout=15) as r_f:
            f_data = await r_f.json()
            ilanlar = f_data if isinstance(f_data, list) else f_data.get('data', [])
            if not ilanlar: return ("ATLA", "İlanYok")
            fiyatlar = [round(l['price']/100, 2) for l in ilanlar]
            f_fiyat = max(Counter(fiyatlar), key=Counter(fiyatlar).get)

        return {"isim": isim, "s": s_fiyat, "f": f_fiyat, "v": hacim}
    except:
        return ("TEKRAR", "Hata")

# --- TELEGRAM ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    kb = [['🔄 CSFloat -> Steam', '🔄 Steam -> CSFloat']]
    await update.message.reply_text("🚀 **Market Analiz Botu**\nLütfen işlem yönü seçin:", 
                                   reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True), parse_mode="Markdown")

async def handle_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text

    if text == "🛑 Taramayı Durdur":
        context.user_data['stop_scan'] = True
        await update.message.reply_text("⚠️ **Durdurma sinyali alındı!**", reply_markup=ReplyKeyboardRemove())
        return

    if text in ['🔄 CSFloat -> Steam', '🔄 Steam -> CSFloat']:
        context.user_data['mode'] = text
        await update.message.reply_text("💰 **Cüzdan bakiyesi ($):**")
        return

    if 'mode' in context.user_data and 'balance' not in context.user_data:
        try:
            context.user_data['balance'] = float(text.replace(",", "."))
            await update.message.reply_text("📉 **Minimum ürün fiyat limiti ($):**")
            return
        except:
            await update.message.reply_text("❌ Geçerli bir bakiye girin.")
            return

    if 'balance' in context.user_data and not context.user_data.get('analyzing'):
        try:
            context.user_data['min_price'] = float(text.replace(",", "."))
            context.user_data['analyzing'] = True
            context.user_data['stop_scan'] = False
        except:
            await update.message.reply_text("❌ Geçerli bir fiyat girin.")
            return

        # --- AŞAMA 1: BAĞLANTI ---
        conn_msg = await update.message.reply_text(generate_premium_bar(100, 100, "TELEGRAM BAĞLANTISI"), parse_mode="Markdown")
        
        # --- AŞAMA 2: LİSTE ---
        item_listesi = item_listesini_yukle()
        toplam = len(item_listesi)
        await conn_msg.edit_text(generate_premium_bar(100, 100, f"LİSTE YÜKLENDİ: {toplam} ÜRÜN"), parse_mode="Markdown")
        
        # --- AŞAMA 3: ANALİZ ---
        panel_metni = f"{generate_premium_bar(0, toplam, 'MARKET VERİLERİ ANALİZ EDİLİYOR')}\n\n✅ Başarılı: `0` | ⏭️ Atlanan: `0`"
        status_msg = await update.message.reply_text(
            panel_metni,
            reply_markup=ReplyKeyboardMarkup([['🛑 Taramayı Durdur']], resize_keyboard=True),
            parse_mode="Markdown"
        )

        sonuclar, basarili, hatalar = [], 0, 0
        async with aiohttp.ClientSession() as session:
            for i, item in enumerate(item_listesi, 1):
                if context.user_data.get('stop_scan'):
                    await status_msg.edit_text("🛑 **İŞLEM DURDURULDU**", parse_mode="Markdown")
                    context.user_data['analyzing'] = False
                    return

                res = await veri_cek(session, item, i, toplam)
                
                if isinstance(res, tuple) and res[0] == "TEKRAR":
                    if await durdurulabilir_bekleme(10, context): break
                    res = await veri_cek(session, item, i, toplam)

                if isinstance(res, dict):
                    kontrol_fiyat = res['f'] if 'CSFloat -> Steam' in context.user_data['mode'] else res['s']
                    if kontrol_fiyat >= context.user_data['min_price']:
                        sonuclar.append(res)
                        basarili += 1
                    else:
                        hatalar += 1
                else:
                    hatalar += 1

                if i % 2 == 0 or i == toplam:
                    yeni_bar = generate_premium_bar(i, toplam, "FİYATLAR TARANIYOR")
                    yeni_metin = (
                        f"{yeni_bar}\n\n"
                        f"✅ Başarılı: `{basarili}`\n"
                        f"⏭️ Atlanan: `{hatalar}`\n"
                        f"📦 İlerleme: `{i}/{toplam}`"
                    )
                    try:
                        # Sadece içerik değiştiyse ve durdurulmadıysa güncelle
                        await status_msg.edit_text(yeni_metin, parse_mode="Markdown")
                    except: pass
                
                if await durdurulabilir_bekleme(random.uniform(5, 7), context):
                    break

        # --- RAPOR ---
        final_list = []
        bakiye = context.user_data['balance']
        mod = context.user_data['mode']
        for d in sonuclar:
            if 'CSFloat -> Steam' in mod:
                alis, satis = d['f'], d['s']
                net_satis = steam_net_hesapla(satis)
                nereden, nereye = "CSFloat", "Steam"
            else:
                alis, satis = d['s'], d['f']
                net_satis = round(satis * 0.98, 2)
                nereden, nereye = "Steam", "CSFloat"
            
            adet = math.floor(bakiye / alis) if alis > 0 else 0
            if adet > 0:
                kar_tek = round(net_satis - alis, 2)
                toplam_kar = round(kar_tek * adet, 2)
                roi = round((kar_tek / alis) * 100, 1)
                if toplam_kar > 0:
                    final_list.append({'isim': d['isim'], 'adet': adet, 'kar': toplam_kar, 'alis': alis, 'satis': satis, 'net': net_satis, 'roi': roi, 'nereden': nereden, 'nereye': nereye})

        en_iyi = sorted(final_list, key=lambda x: x['kar'], reverse=True)[:5]
        
        if en_iyi:
            rapor = f"🔥 **ANALİZ SONUÇLARI (EN İYİ 5)**\n`Bakiye: ${bakiye}`\n\n"
            for s, it in enumerate(en_iyi, 1):
                rapor += (f"{s}. **{it['isim']}**\n"
                          f"🛒 Alış: `${it['alis']}` ({it['nereden']})\n"
                          f"💰 Satış: `${it['satis']}` ({it['nereye']})\n"
                          f"📈 Kâr: `${it['kar']}` | ROI: `%{it['roi']}`\n\n")
            await update.message.reply_text(rapor, parse_mode="Markdown")
        else:
            await update.message.reply_text("⚠️ Kârlı ürün bulunamadı.")

        context.user_data['analyzing'] = False
        kb = [['🔄 Yeniden Başlat', '💰 Bakiye Değiştir'], ['🏠 Ana Menü']]
        await update.message.reply_text("✅ İşlem bitti.", reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True))

    elif text == '🔄 Yeniden Başlat':
        context.user_data['analyzing'] = False
        await handle_msg(update, context)
    elif text == '💰 Bakiye Değiştir':
        mod = context.user_data.get('mode')
        context.user_data.clear()
        context.user_data['mode'] = mod
        await update.message.reply_text("💰 Yeni bakiye ($):")
    elif text == '🏠 Ana Menü':
        await start(update, context)

if __name__ == "__main__":
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_msg))
    app.run_polling()
