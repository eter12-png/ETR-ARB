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
from telegram.error import BadRequest

# --- LOGGING ---
logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# --- AYARLAR ---
API_KEY = os.getenv("CSFLOAT_API_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
MIN_VOLUME_LIMIT = 50

# --- PROFESYONEL PROGRESS BAR ---
def generate_premium_bar(current, total, baslik="İŞLEM"):
    oran = current / total if total > 0 else 0
    bar_uzunlugu = 10
    dolu = int(oran * bar_uzunlugu)
    
    if oran < 0.33: renk = "🟧"
    elif oran < 0.66: renk = "🟦"
    else: renk = "🟩"
        
    bar = renk * dolu + "⬜" * (bar_uzunlugu - dolu)
    yuzde = round(oran * 100, 1)
    
    return (
        f"💠 **{baslik}**\n"
        f"┣ {bar}  `%{yuzde}`\n"
        f"┗━━━━━━━━━━━━━━"
    )

async def durdurulabilir_bekleme(saniye, context):
    adimlar = int(saniye / 0.5)
    for _ in range(adimlar):
        if context.user_data.get('stop_scan'): return True
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
        s_kom = max(0.01, math.floor(satici_alir * 0.05 * 100 + 0.000001) / 100)
        g_kom = max(0.01, math.floor(satici_alir * 0.10 * 100 + 0.000001) / 100)
        if round(satici_alir + s_kom + g_kom, 2) <= alici_oder:
            sonraki = round(satici_alir + 0.01, 2)
            if round(sonraki + max(0.01, math.floor(sonraki * 0.05 * 100 + 0.000001) / 100) + max(0.01, math.floor(sonraki * 0.10 * 100 + 0.000001) / 100), 2) <= alici_oder:
                satici_alir = sonraki
            else: break
        else: satici_alir = round(satici_alir - 0.01, 2)
    return satici_alir

async def veri_cek(session, isim):
    headers = {'User-Agent': 'Mozilla/5.0'}
    safe_name = urllib.parse.quote(isim)
    s_url = f"https://steamcommunity.com/market/priceoverview/?appid=730&currency=1&market_hash_name={safe_name}"
    f_url = f"https://csfloat.com/api/v1/listings?market_hash_name={safe_name}&limit=5&sort_by=lowest_price&type=buy_now"

    try:
        # Steam Verisi
        async with session.get(s_url, headers=headers, timeout=10) as r:
            if r.status != 200: return ("ATLA", r.status)
            s_data = await r.json()
            if not s_data or "lowest_price" not in s_data: return ("ATLA", "FiyatYok")
            s_fiyat = float(s_data["lowest_price"].replace("$", "").replace(",", ""))
            hacim = int(str(s_data.get("volume", "0")).replace(",", "")) if str(s_data.get("volume", "0")).replace(",", "").isdigit() else 0
            if hacim < MIN_VOLUME_LIMIT: return ("ATLA", "HacimAz")

        await asyncio.sleep(0.5)

        # CSFloat Verisi
        f_headers = {"Authorization": API_KEY} if API_KEY else {}
        async with session.get(f_url, headers=f_headers, timeout=10) as r:
            if r.status != 200: return ("ATLA", r.status)
            f_data = await r.json()
            ilanlar = f_data if isinstance(f_data, list) else f_data.get('data', [])
            if not ilanlar: return ("ATLA", "İlanYok")
            f_fiyat = round(ilanlar[0]['price']/100, 2)

        return {"isim": isim, "s": s_fiyat, "f": f_fiyat, "v": hacim}
    except Exception as e:
        logger.error(f"Hata: {isim} -> {str(e)}")
        return ("HATA", str(e))

# --- TELEGRAM ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    kb = [['🔄 CSFloat -> Steam', '🔄 Steam -> CSFloat']]
    await update.message.reply_text("🚀 **Market Analiz Botu Hazır**\nYön seçin:", 
                                   reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True), parse_mode="Markdown")

async def handle_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    ud = context.user_data

    if text == "🛑 Taramayı Durdur":
        ud['stop_scan'] = True
        return

    if text in ['🔄 CSFloat -> Steam', '🔄 Steam -> CSFloat']:
        ud['mode'] = text
        await update.message.reply_text("💰 **Bakiye ($):**", reply_markup=ReplyKeyboardRemove())
        return

    if 'mode' in ud and 'balance' not in ud:
        ud['balance'] = float(text.replace(",", "."))
        await update.message.reply_text("📉 **Min Ürün Fiyatı ($):**")
        return

    if 'balance' in ud and not ud.get('analyzing'):
        ud['min_price'] = float(text.replace(",", "."))
        ud['analyzing'] = True
        ud['stop_scan'] = False

        item_listesi = item_listesini_yukle()
        toplam = len(item_listesi)
        
        # İlk mesajı gönder
        status_msg = await update.message.reply_text(
            f"{generate_premium_bar(0, toplam, 'TARAMA BAŞLIYOR')}\n\nLütfen bekleyin...",
            reply_markup=ReplyKeyboardMarkup([['🛑 Taramayı Durdur']], resize_keyboard=True),
            parse_mode="Markdown"
        )

        sonuclar, basarili, atlanan, son_metin = [], 0, 0, ""

        async with aiohttp.ClientSession() as session:
            for i, item in enumerate(item_listesi, 1):
                if ud.get('stop_scan'):
                    await status_msg.edit_text("🛑 **İŞLEM DURDURULDU**")
                    ud['analyzing'] = False
                    return

                res = await veri_cek(session, item)
                
                if isinstance(res, dict):
                    fiyat = res['f'] if 'CSFloat -> Steam' in ud['mode'] else res['s']
                    if fiyat >= ud['min_price']:
                        sonuclar.append(res)
                        basarili += 1
                    else: atlanan += 1
                else: atlanan += 1

                # Her 3 itemda bir veya sonda güncelle (400 Bad Request önleyici)
                if i % 3 == 0 or i == toplam:
                    yeni_metin = (
                        f"{generate_premium_bar(i, toplam, 'MARKET ANALİZİ')}\n\n"
                        f"✅ Başarılı: `{basarili}`\n"
                        f"⏭️ Atlanan: `{atlanan}`\n"
                        f"📦 İlerleme: `{i}/{toplam}`"
                    )
                    if yeni_metin != son_metin:
                        try:
                            await status_msg.edit_text(yeni_metin, parse_mode="Markdown")
                            son_metin = yeni_metin
                        except BadRequest: pass
                
                if await durdurulabilir_bekleme(random.uniform(4, 6), context): break

        # --- RAPORLAMA ---
        final = []
        for d in sonuclar:
            if 'CSFloat -> Steam' in ud['mode']:
                alis, satis = d['f'], d['s']
                net = steam_net_hesapla(satis)
            else:
                alis, satis = d['s'], d['f']
                net = round(satis * 0.98, 2)
            
            adet = math.floor(ud['balance'] / alis) if alis > 0 else 0
            if adet > 0:
                kar = round((net - alis) * adet, 2)
                if kar > 0:
                    final.append({'isim': d['isim'], 'kar': kar, 'alis': alis, 'satis': satis, 'roi': round(((net-alis)/alis)*100, 1)})

        en_iyi = sorted(final, key=lambda x: x['kar'], reverse=True)[:5]
        if en_iyi:
            rapor = "🔥 **EN KÂRLI FIRSATLAR**\n\n"
            for it in en_iyi:
                rapor += f"▫️ **{it['isim']}**\n💰 Kâr: `${it['kar']}` | ROI: `%{it['roi']}`\n🛒 Alış: `${it['alis']}`\n\n"
            await update.message.reply_text(rapor, parse_mode="Markdown")
        else:
            await update.message.reply_text("⚠️ Kârlı fırsat bulunamadı.")

        ud['analyzing'] = False
        kb = [['🔄 Yeniden Başlat', '🏠 Ana Menü']]
        await update.message.reply_text("✅ İşlem tamamlandı.", reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True))

    elif text == '🔄 Yeniden Başlat':
        ud['analyzing'] = False
        await handle_msg(update, context)
    elif text == '🏠 Ana Menü':
        await start(update, context)

if __name__ == "__main__":
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_msg))
    app.run_polling()
