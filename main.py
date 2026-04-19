import asyncio
import aiohttp
import math
import urllib.parse
import os
import random
import logging
import time
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
VOLUME_LIMIT_PERCENT = 0.15
MAX_BUDGET_PER_ITEM = 0.30

# --- PROFESYONEL PROGRESS BAR ---
def generate_progress_bar(current, total):
    if total <= 0: return "⬜⬜⬜⬜⬜⬜⬜⬜⬜⬜ %0"
    bar_length = 10 
    fraction = current / total
    filled = int(fraction * bar_length)
    
    if fraction < 0.30: filled_char = "🟥"
    elif fraction < 0.70: filled_char = "🟧"
    elif fraction < 0.99: filled_char = "🟩"
    else: filled_char = "✅"
        
    bar = filled_char * filled + "⬜" * (bar_length - filled)
    percent = int(fraction * 100)
    return f"💠 **İlerleme:** {bar} `%{percent}`"

def load_items():
    if os.path.exists("items.txt"):
        with open("items.txt", "r", encoding="utf-8") as f:
            return [line.strip() for line in f.readlines() if line.strip()]
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

def create_balanced_basket(final_list, total_balance):
    basket = []
    remaining_balance = total_balance
    sorted_items = sorted(final_list, key=lambda x: x['roi'], reverse=True)
    for item in sorted_items:
        if remaining_balance <= 0.05: break
        max_qty = math.floor(min(item['vol'] * VOLUME_LIMIT_PERCENT, (total_balance * MAX_BUDGET_PER_ITEM) / item['buy'], remaining_balance / item['buy']))
        if max_qty > 0:
            cost = round(max_qty * item['buy'], 2)
            profit = round((item['net'] - item['buy']) * max_qty, 2)
            basket.append({**item, 'final_qty': max_qty, 'total_profit': profit, 'total_cost': cost})
            remaining_balance -= cost
    return basket, round(total_balance - remaining_balance, 2)

async def fetch_item(session, name):
    headers = {'User-Agent': 'Mozilla/5.0'}
    safe_name = urllib.parse.quote(name)
    s_url = f"https://steamcommunity.com/market/priceoverview/?appid=730&currency=1&market_hash_name={safe_name}"
    f_url = f"https://csfloat.com/api/v1/listings?market_hash_name={safe_name}&limit=50&sort_by=lowest_price&type=buy_now"
    try:
        async with session.get(s_url, headers=headers, timeout=8) as r_s:
            if r_s.status == 429: return ("RETRY", "429")
            s_data = await r_s.json()
            if not s_data or "lowest_price" not in s_data: return ("SKIP", "Yok")
            vol = int(str(s_data.get("volume", "0")).replace(",", "")) if str(s_data.get("volume", "0")).replace(",", "").isdigit() else 0
            if vol < MIN_VOLUME_LIMIT: return ("SKIP", "Hacim")
            s_p = float(s_data["lowest_price"].replace("$", "").replace(",", ""))
        async with session.get(f_url, headers={"Authorization": API_KEY}, timeout=8) as r_f:
            f_data = await r_f.json()
            listings = f_data if isinstance(f_data, list) else f_data.get('data', [])
            if not listings: return ("SKIP", "İlan")
            f_p = max(Counter([round(l['price']/100, 2) for l in listings]), key=Counter([round(l['price']/100, 2) for l in listings]).get)
        return {"name": name, "s": s_p, "f": f_p, "vol": vol}
    except: return ("RETRY", "Hata")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['analyzing'] = False
    kb = [['🔄 CSFloat -> Steam', '🔄 Steam -> CSFloat']]
    await update.message.reply_text("🚀 İşlem yönü seçin:", reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True))

async def handle_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text

    if text == "🛑 Taramayı Durdur":
        if context.user_data.get('analyzing'):
            context.user_data['stop_scan'] = True
            await update.message.reply_text("⛔ Tarama durduruluyor...")
        return

    if text in ['🔄 CSFloat -> Steam', '🔄 Steam -> CSFloat']:
        context.user_data['mode'] = text
        await update.message.reply_text("💰 Bakiye girin ($):", reply_markup=ReplyKeyboardRemove())
        return

    if 'mode' in context.user_data and not context.user_data.get('analyzing'):
        try:
            user_balance = float(text.replace(",", "."))
            context.user_data.update({'analyzing': True, 'stop_scan': False, 'balance': user_balance})
        except:
            await update.message.reply_text("❌ Sayı girin.")
            return

        items_list = load_items()
        total = len(items_list)
        status_msg = await update.message.reply_text(
            f"💠 **TARAMA BAŞLADI**\n{generate_progress_bar(0, total)}",
            reply_markup=ReplyKeyboardMarkup([['🛑 Taramayı Durdur']], resize_keyboard=True),
            parse_mode="Markdown"
        )

        all_results, last_update_time, last_pct = [], 0, -1
        
        async with aiohttp.ClientSession() as session:
            for i, item in enumerate(items_list, 1):
                if context.user_data.get('stop_scan'): break
                
                pct = int((i / total) * 100)
                # Sadece her %5'lik dilimde ve en az 15 saniye geçtiyse edit yap
                if (pct % 5 == 0 and pct != last_pct) or (time.time() - last_update_time > 15):
                    try:
                        await status_msg.edit_text(
                            f"💠 **TARAMA YAPILIYOR**\n{generate_progress_bar(i, total)}\n"
                            f"📡 **Son İtem:** `{item}`\n📈 Bulunan: `{len(all_results)}`",
                            parse_mode="Markdown"
                        )
                        last_update_time = time.time()
                        last_pct = pct
                    except Exception as e:
                        logger.warning(f"Edit atlandı: {e}")

                res = await fetch_item(session, item)
                if isinstance(res, dict):
                    all_results.append(res)
                
                # Dinamik durdurma dinlemesi
                for _ in range(5):
                    if context.user_data.get('stop_scan'): break
                    await asyncio.sleep(1)

        if context.user_data.get('stop_scan'):
            context.user_data['analyzing'] = False
            await update.message.reply_text("🛑 Tarama durduruldu. Menüden seçim yapın.", reply_markup=ReplyKeyboardMarkup([['🔄 Yeniden Başlat', '🏠 Ana Menü']], resize_keyboard=True))
            return

        # Analiz ve Raporlama kısmı aynı kalıyor...
        final_list = []
        for d in all_results:
            if 'CSFloat -> Steam' in context.user_data['mode']:
                buy, sell, net = d['f'], d['s'], steam_net_hesapla(d['s'])
            else:
                buy, sell, net = d['s'], d['f'], round(d['f'] * 0.98, 2)
            roi = round(((net - buy) / buy) * 100, 1) if buy > 0 else 0
            if net > buy:
                final_list.append({'name': d['name'], 'buy': buy, 'sell': sell, 'net': net, 'roi': roi, 'vol': d['vol']})

        sepet, harcanan = create_balanced_basket(final_list, user_balance)
        if sepet:
            report = f"⚖️ **ALIM SEPETİ** (${harcanan})\n\n" + "\n".join([f"{idx+1}. **{it['name']}** - `{it['final_qty']} Adet` (%{it['roi']} Kar)" for idx, it in enumerate(sepet)])
            await update.message.reply_text(report, parse_mode="Markdown")
        else:
            await update.message.reply_text("❌ Uygun fırsat bulunamadı.")

        context.user_data['analyzing'] = False
        await update.message.reply_text("✅ İşlem bitti.", reply_markup=ReplyKeyboardMarkup([['🔄 CSFloat -> Steam', '🔄 Steam -> CSFloat']], resize_keyboard=True))

    elif text in ['🔄 Yeniden Başlat', '🏠 Ana Menü']:
        await start(update, context)

if __name__ == "__main__":
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_msg))
    app.run_polling()
