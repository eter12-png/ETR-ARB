import asyncio
import aiohttp
import math
import urllib.parse
import os
import random
from datetime import datetime
from collections import Counter
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# --- AYARLAR (RAILWAY VARIABLES) ---
API_KEY = os.getenv("CSFLOAT_API_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

MIN_PRICE_LIMIT = 0.20
MIN_VOLUME_LIMIT = 50

# --- YARDIMCI FONKSİYONLAR ---
def load_items():
    if os.path.exists("items.txt"):
        with open("items.txt", "r", encoding="utf-8") as f:
            items = [line.strip() for line in f.readlines() if line.strip()]
            print(f"📦 {len(items)} adet item dosyadan yüklendi.")
            return items
    print("❌ HATA: items.txt dosyası ana dizinde bulunamadı!")
    return []

def steam_net_hesapla(buyer_pays):
    if buyer_pays < 0.03: return 0
    seller_gets = math.floor(buyer_pays / 1.15 * 100) / 100
    while True:
        s_fee = max(0.01, math.floor(seller_gets * 0.05 * 100 + 0.000001) / 100)
        g_fee = max(0.01, math.floor(seller_gets * 0.10 * 100 + 0.000001) / 100)
        if round(seller_gets + s_fee + g_fee, 2) <= buyer_pays:
            next_s = round(seller_gets + 0.01, 2)
            if round(next_s + max(0.01, math.floor(next_s * 0.05 * 100 + 0.000001) / 100) + max(0.01, math.floor(next_s * 0.10 * 100 + 0.000001) / 100), 2) <= buyer_pays:
                seller_gets = next_s
            else: break
        else: seller_gets = round(seller_gets - 0.01, 2)
    return seller_gets

# --- VERİ ÇEKME ---
async def fetch_item(session, name, idx, total):
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'}
    safe_name = urllib.parse.quote(name)
    s_url = f"https://steamcommunity.com/market/priceoverview/?appid=730&currency=1&market_hash_name={safe_name}"
    f_url = f"https://csfloat.com/api/v1/listings?market_hash_name={safe_name}&limit=50&sort_by=lowest_price&type=buy_now"

    try:
        async with session.get(s_url, headers=headers, timeout=15) as r_s:
            if r_s.status == 429: return ("RETRY", "Steam Rate Limit (429)")
            if r_s.status != 200: return ("SKIP", f"Steam Error {r_s.status}")
            s_data = await r_s.json()
            if not s_data or "lowest_price" not in s_data: return ("SKIP", "No Price")
            
            vol = int(str(s_data.get("volume", "0")).replace(",", "")) if str(s_data.get("volume", "0")).replace(",", "").isdigit() else 0
            if vol < MIN_VOLUME_LIMIT: return ("SKIP", f"Low Vol: {vol}")
            s_price = float(s_data["lowest_price"].replace("$", "").replace(",", ""))

        await asyncio.sleep(random.uniform(1.5, 3.0))

        async with session.get(f_url, headers={"Authorization": API_KEY}, timeout=15) as r_f:
            if r_f.status == 429: return ("RETRY", "CSFloat Rate Limit (429)")
            f_data = await r_f.json()
            listings = f_data if isinstance(f_data, list) else f_data.get('data', [])
            if not listings: return ("SKIP", "No Float Listings")
            prices = [round(l['price']/100, 2) for l in listings]
            f_price = max(Counter(prices), key=Counter(prices).get)

        print(f"✅ [{idx}/{total}] {name} OK.")
        return {"name": name, "s": s_price, "f": f_price, "vol": vol}
    except Exception as e:
        return ("RETRY", str(e))

# --- TELEGRAM ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = [['🔄 CSFloat -> Steam', '🔄 Steam -> CSFloat']]
    await update.message.reply_text("🚀 Yön seçin:", reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True))

async def handle_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if text in ['🔄 CSFloat -> Steam', '🔄 Steam -> CSFloat']:
        context.user_data['mode'] = text
        await update.message.reply_text("💰 Bakiye gir (Örn: 500):", reply_markup=ReplyKeyboardRemove())
        return

    if 'mode' in context.user_data and 'analyzing' not in context.user_data:
        try:
            user_balance = float(text.replace(",", "."))
            context.user_data['analyzing'] = True
            mode = context.user_data['mode']
        except:
            await update.message.reply_text("❌ Sayı girin.")
            return

        items_list = load_items()
        if not items_list:
            await update.message.reply_text("❌ HATA: items.txt bulunamadı veya boş!")
            context.user_data.clear()
            return

        total = len(items_list)
        all_results = []
        status_msg = await update.message.reply_text(f"🛰 {total} item için analiz başladı...")

        async with aiohttp.ClientSession() as session:
            for i, item in enumerate(items_list, 1):
                if i % 3 == 0:
                    try: await status_msg.edit_text(f"📡 İlerleme: {i}/{total}\nBulunan: {len(all_results)}")
                    except: pass

                res = await fetch_item(session, item, i, total)
                
                # Retry mekanizması
                retry_count = 0
                while isinstance(res, tuple) and res[0] == "RETRY" and retry_count < 2:
                    await asyncio.sleep(30)
                    res = await fetch_item(session, item, i, total)
                    retry_count += 1

                if isinstance(res, dict):
                    all_results.append(res)
                
                await asyncio.sleep(random.uniform(5, 10)) # Railway'de ban yememek için biraz hızlandık

        # --- HESAPLAMA ---
        final_list = []
        for data in all_results:
            buy = data['f'] if 'CSFloat -> Steam' in mode else data['s']
            sell = data['s'] if 'CSFloat -> Steam' in mode else data['f']
            net_sell = steam_net_hesapla(sell) if 'CSFloat -> Steam' in mode else round(sell * 0.98, 2)
            
            qty = math.floor(user_balance / buy) if buy > 0 else 0
            if qty > 0:
                profit = round((qty * net_sell) - (qty * buy), 2)
                final_list.append({'name': data['name'], 'qty': qty, 'profit': profit})

        sorted_res = sorted(final_list, key=lambda x: x['profit'], reverse=True)[:5]
        if not sorted_res:
            await update.message.reply_text("❌ Kârlı bir fırsat bulunamadı.")
        else:
            report = f"🏆 EN İYİ 5 ({mode})\n\n"
            for i, item in enumerate(sorted_res, 1):
                report += f"{i}. {item['name']}\nAdet: {item['qty']} | Kâr: +${item['profit']}\n\n"
            await update.message.reply_text(report)
        
        context.user_data.clear()

if __name__ == "__main__":
    if not TELEGRAM_TOKEN or not API_KEY:
        print("❌ Değişkenler eksik!")
    else:
        app = Application.builder().token(TELEGRAM_TOKEN).build()
        app.add_handler(CommandHandler("start", start))
        app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_msg))
        app.run_polling()
