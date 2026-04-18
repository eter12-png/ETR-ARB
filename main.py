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

# --- LOGGING YAPILANDIRMASI ---
logging.basicConfig(
    format='%(asctime)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- AYARLAR ---
API_KEY = os.getenv("CSFLOAT_API_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
MIN_VOLUME_LIMIT = 50

# --- YARDIMCI FONKSİYONLAR ---
def generate_progress_bar(current, total):
    if total <= 0: return "⬜⬜⬜⬜⬜⬜⬜⬜⬜⬜ %0"
    
    bar_length = 10 
    fraction = current / total
    filled = int(fraction * bar_length)
    
    # İlerleme durumuna göre renk değiştiren ikonlar
    if fraction < 0.33:
        color_block = "🟥" 
    elif fraction < 0.66:
        color_block = "🟧" 
    elif fraction < 0.99:
        color_block = "🟩" 
    else:
        color_block = "✅" 
        
    bar = color_block * filled + "⬜" * (bar_length - filled)
    percent = int(fraction * 100)
    
    return f"┣ {bar}  `%{percent}`\n┗━━━━━━━━━━━━━━"

def load_items():
    if os.path.exists("items.txt"):
        with open("items.txt", "r", encoding="utf-8") as f:
            items = [line.strip() for line in f.readlines() if line.strip()]
            return items
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
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0 Safari/537.36'}
    safe_name = urllib.parse.quote(name)
    s_url = f"https://steamcommunity.com/market/priceoverview/?appid=730&currency=1&market_hash_name={safe_name}"
    f_url = f"https://csfloat.com/api/v1/listings?market_hash_name={safe_name}&limit=50&sort_by=lowest_price&type=buy_now"

    try:
        async with session.get(s_url, headers=headers, timeout=15) as r_s:
            if r_s.status == 429: return ("RETRY", "Steam 429")
            if r_s.status != 200: return ("SKIP", f"Steam {r_s.status}")
            s_data = await r_s.json()
            if not s_data or "lowest_price" not in s_data: return ("SKIP", "Fiyat Yok")
            vol_raw = str(s_data.get("volume", "0")).replace(",", "")
            vol = int(vol_raw) if vol_raw.isdigit() else 0
            if vol < MIN_VOLUME_LIMIT: return ("SKIP", f"Düşük Hacim ({vol})")
            s_price = float(s_data["lowest_price"].replace("$", "").replace(",", ""))

        await asyncio.sleep(random.uniform(1.5, 3.0))

        async with session.get(f_url, headers={"Authorization": API_KEY}, timeout=15) as r_f:
            f_data = await r_f.json()
            listings = f_data if isinstance(f_data, list) else f_data.get('data', [])
            if not listings: return ("SKIP", "İlan Yok")
            prices = [round(l['price']/100, 2) for l in listings]
            f_price = max(Counter(prices), key=Counter(prices).get)

        logger.info(f"✅ [{idx}/{total}] {name} tarandı.")
        return {"name": name, "s": s_price, "f": f_price, "vol": vol}
    except Exception as e:
        return ("RETRY", str(e))

# --- TELEGRAM ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    kb = [['🔄 CSFloat -> Steam', '🔄 Steam -> CSFloat']]
    await update.message.reply_text("🚀 İşlem yönü seçin:", reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True))

async def handle_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text

    if text == "🛑 Taramayı Durdur":
        if context.user_data.get('analyzing'):
            context.user_data['stop_scan'] = True
            await update.message.reply_text("⏳ Mevcut item bitince tarama durdurulacak...")
            return

    if text in ['🔄 CSFloat -> Steam', '🔄 Steam -> CSFloat']:
        context.user_data['mode'] = text
        await update.message.reply_text("💰 Bakiye girin ($):", reply_markup=ReplyKeyboardRemove())
        return

    if 'mode' in context.user_data and not context.user_data.get('analyzing'):
        try:
            user_balance = float(text.replace(",", "."))
            context.user_data['analyzing'] = True
            context.user_data['balance'] = user_balance
            context.user_data['stop_scan'] = False
            mode = context.user_data['mode']
        except:
            await update.message.reply_text("❌ Geçerli bir sayı girin.")
            return

        items_list = load_items()
        total = len(items_list)
        all_results, errors_count, success_count = [], 0, 0
        
        # Profesyonel ve emoji tabanlı bar ile başlıyoruz
        prog_bar = generate_progress_bar(0, total)
        status_msg = await update.message.reply_text(
            f"💠 **MARKET ANALİZİ YAPILIYOR**\n"
            f"{prog_bar}\n\n"
            f"📡 Durum: `Hazırlanıyor...`",
            reply_markup=ReplyKeyboardMarkup([['🛑 Taramayı Durdur']], resize_keyboard=True),
            parse_mode="Markdown"
        )

        async with aiohttp.ClientSession() as session:
            for i, item in enumerate(items_list, 1):
                if context.user_data.get('stop_scan'):
                    await status_msg.edit_text("🛑 **Tarama kullanıcı tarafından durduruldu.**")
                    break

                try:
                    current_bar = generate_progress_bar(i-1, total)
                    await status_msg.edit_text(
                        f"💠 **MARKET ANALİZİ YAPILIYOR**\n"
                        f"{current_bar}\n\n"
                        f"📡 **İşlem:** `{item}`\n"
                        f"✨ Başarılı: `{success_count}` | ⚠️ Hata: `{errors_count}`",
                        parse_mode="Markdown"
                    )
                except: pass

                res = await fetch_item(session, item, i, total)
                if isinstance(res, tuple) and res[0] == "RETRY":
                    await asyncio.sleep(20)
                    res = await fetch_item(session, item, i, total)

                if isinstance(res, dict):
                    all_results.append(res)
                    success_count += 1
                else:
                    errors_count += 1
                
                await asyncio.sleep(random.uniform(5, 10))

        # --- SONUÇ RAPORU ---
        final_list = []
        for d in all_results:
            if 'CSFloat -> Steam' in mode:
                buy_from, buy_p, sell_to, sell_p = "CSFloat", d['f'], "Steam", d['s']
                net_sell = steam_net_hesapla(sell_p)
            else:
                buy_from, buy_p, sell_to, sell_p = "Steam", d['s'], "CSFloat", d['f']
                net_sell = round(sell_p * 0.98, 2)
            
            qty = math.floor(user_balance / buy_p) if buy_p > 0 else 0
            if qty > 0:
                profit_per = round(net_sell - buy_p, 2)
                total_profit = round(profit_per * qty, 2)
                roi = round((profit_per / buy_p) * 100, 1)
                if total_profit > 0:
                    final_list.append({
                        'name': d['name'], 'qty': qty, 'profit': total_profit,
                        'buy': buy_p, 'sell': sell_p, 'net': net_sell, 
                        'roi': roi, 'buy_from': buy_from, 'sell_to': sell_to
                    })

        sorted_res = sorted(final_list, key=lambda x: x['profit'], reverse=True)[:5]
        
        if sorted_res:
            report = f"🏆 **EN İYİ 5 FIRSAT**\n`{mode}`\n`Bakiye: ${user_balance}`\n\n"
            for idx, item in enumerate(sorted_res, 1):
                report += (
                    f"{idx}. **{item['name']}**\n"
                    f"📥 Alış ({item['buy_from']}): `${item['buy']}`\n"
                    f"📤 Satış ({item['sell_to']}): `${item['sell']}`\n"
                    f"💰 Net: `${item['net']}` | ROI: `%{item['roi']}`\n"
                    f"📦 Adet: `{item['qty']}` | **Kâr: +${item['profit']}**\n\n"
                )
            await update.message.reply_text(report, parse_mode="Markdown")
        else:
            await update.message.reply_text("❌ Kârlı fırsat bulunamadı.")

        context.user_data['analyzing'] = False
        post_scan_kb = [['🔄 Yeniden Başlat', '💰 Bakiye Değiştir'], ['🏠 Ana Menü']]
        await update.message.reply_text(
            "✅ İşlem tamamlandı. Şimdi ne yapalım?",
            reply_markup=ReplyKeyboardMarkup(post_scan_kb, resize_keyboard=True)
        )

    elif text == '🔄 Yeniden Başlat':
        await handle_msg(update, context)
    elif text == '💰 Bakiye Değiştir':
        context.user_data.pop('analyzing', None)
        await update.message.reply_text("💰 Yeni bakiye girin ($):", reply_markup=ReplyKeyboardRemove())
    elif text == '🏠 Ana Menü':
        await start(update, context)

if __name__ == "__main__":
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_msg))
    app.run_polling()
