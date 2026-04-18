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

# --- PREMIUM PROGRESS BAR SİSTEMİ ---
def generate_premium_bar(current, total, title="İŞLEM"):
    if total <= 0: return f"🌀 **{title}**\n`Hazırlanıyor...`"
    
    bar_length = 10
    fraction = current / total
    filled = int(fraction * bar_length)
    
    # Renk paleti: %33'e kadar turuncu, %66'ya kadar mavi, sonrası yeşil
    if fraction < 0.33:
        color_char = "🟧"
    elif fraction < 0.66:
        color_char = "🟦"
    else:
        color_char = "🟩"
        
    bar = color_char * filled + "⬜" * (bar_length - filled)
    percent = round(fraction * 100, 1)
    
    return (
        f"💠 **{title}**\n"
        f"┣ {bar}  `%{percent}`\n"
        f"┗━━━━━━━━━━━━━━"
    )

async def interruptible_sleep(seconds, context):
    """0.5 saniyelik adımlarla durdurma kontrolü yapar."""
    steps = int(seconds / 0.5)
    for _ in range(steps):
        if context.user_data.get('stop_scan'):
            return True
        await asyncio.sleep(0.5)
    return False

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

async def fetch_item(session, name, idx, total):
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0 Safari/537.36'}
    safe_name = urllib.parse.quote(name)
    s_url = f"https://steamcommunity.com/market/priceoverview/?appid=730&currency=1&market_hash_name={safe_name}"
    f_url = f"https://csfloat.com/api/v1/listings?market_hash_name={safe_name}&limit=50&sort_by=lowest_price&type=buy_now"

    try:
        async with session.get(s_url, headers=headers, timeout=15) as r_s:
            if r_s.status == 429: return ("RETRY", "429")
            if r_s.status != 200: return ("SKIP", "Err")
            s_data = await r_s.json()
            if not s_data or "lowest_price" not in s_data: return ("SKIP", "NoPrice")
            vol_raw = str(s_data.get("volume", "0")).replace(",", "")
            vol = int(vol_raw) if vol_raw.isdigit() else 0
            if vol < MIN_VOLUME_LIMIT: return ("SKIP", "LowVol")
            s_price = float(s_data["lowest_price"].replace("$", "").replace(",", ""))

        await asyncio.sleep(1)

        async with session.get(f_url, headers={"Authorization": API_KEY}, timeout=15) as r_f:
            f_data = await r_f.json()
            listings = f_data if isinstance(f_data, list) else f_data.get('data', [])
            if not listings: return ("SKIP", "NoList")
            prices = [round(l['price']/100, 2) for l in listings]
            f_price = max(Counter(prices), key=Counter(prices).get)

        return {"name": name, "s": s_price, "f": f_price, "vol": vol}
    except:
        return ("RETRY", "Err")

# --- TELEGRAM HANDLERS ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    kb = [['🔄 CSFloat -> Steam', '🔄 Steam -> CSFloat']]
    await update.message.reply_text("🚀 **Market Arbitraj Botu Aktif**\nLütfen işlem yönü seçin:", 
                                   reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True), parse_mode="Markdown")

async def handle_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text

    if text == "🛑 Taramayı Durdur":
        context.user_data['stop_scan'] = True
        await update.message.reply_text("⚠️ **Durdurma komutu algılandı!** İşlem sonlandırılıyor...", reply_markup=ReplyKeyboardRemove())
        return

    if text in ['🔄 CSFloat -> Steam', '🔄 Steam -> CSFloat']:
        context.user_data['mode'] = text
        await update.message.reply_text("💰 **Cüzdan bakiyenizi girin ($):**", reply_markup=ReplyKeyboardRemove())
        return

    if 'mode' in context.user_data and 'balance' not in context.user_data:
        try:
            context.user_data['balance'] = float(text.replace(",", "."))
            await update.message.reply_text("📉 **Minimum ürün fiyatı limitini girin ($):**")
            return
        except:
            await update.message.reply_text("❌ Hata: Geçerli bir sayı girin.")
            return

    if 'balance' in context.user_data and not context.user_data.get('analyzing'):
        try:
            context.user_data['min_price'] = float(text.replace(",", "."))
            context.user_data['analyzing'] = True
            context.user_data['stop_scan'] = False
        except:
            await update.message.reply_text("❌ Hata: Geçerli bir sayı girin.")
            return

        # --- AŞAMA 1: TELEGRAM BAĞLANTI ---
        conn_msg = await update.message.reply_text(generate_premium_bar(100, 100, "TELEGRAM API CONNECTION"), parse_mode="Markdown")
        await asyncio.sleep(0.5)
        
        # --- AŞAMA 2: LİSTE VE ENVANTER ---
        items_list = load_items()
        total = len(items_list)
        await conn_msg.edit_text(generate_premium_bar(100, 100, f"DATABASE: {total} ITEMS LOADED"), parse_mode="Markdown")
        
        # --- AŞAMA 3: CANLI TARAMA PANELİ ---
        status_msg = await update.message.reply_text(
            f"{generate_premium_bar(0, total, 'ANALYZING MARKET DATA')}\n\n"
            f"✅ Başarılı: `0` | ⏭️ Atlanan: `0`",
            reply_markup=ReplyKeyboardMarkup([['🛑 Taramayı Durdur']], resize_keyboard=True),
            parse_mode="Markdown"
        )

        all_results, success_count, errors_count = [], 0, 0
        async with aiohttp.ClientSession() as session:
            for i, item in enumerate(items_list, 1):
                if context.user_data.get('stop_scan'):
                    await status_msg.edit_text("🛑 **İŞLEM KULLANICI TARAFINDAN DURDURULDU**", parse_mode="Markdown")
                    context.user_data['analyzing'] = False
                    return

                res = await fetch_item(session, item, i, total)
                
                if isinstance(res, tuple) and res[0] == "RETRY":
                    if await interruptible_sleep(15, context): break
                    res = await fetch_item(session, item, i, total)

                if isinstance(res, dict):
                    check_p = res['f'] if 'CSFloat -> Steam' in context.user_data['mode'] else res['s']
                    if check_p >= context.user_data['min_price']:
                        all_results.append(res)
                        success_count += 1
                    else:
                        errors_count += 1
                else:
                    errors_count += 1

                if i % 2 == 0 or i == total:
                    bar_view = generate_premium_bar(i, total, "MARKET SCANNING")
                    try:
                        await status_msg.edit_text(
                            f"{bar_view}\n\n"
                            f"✅ Başarılı Bulunan: `{success_count}`\n"
                            f"⏭️ Kriter Dışı/Hata: `{errors_count}`\n"
                            f"📦 İşlenen: `{i}/{total}`",
                            parse_mode="Markdown"
                        )
                    except: pass
                
                if await interruptible_sleep(random.uniform(5, 8), context):
                    break

        # --- RAPORLAMA ---
        final_list = []
        user_balance = context.user_data['balance']
        mode = context.user_data['mode']
        for d in all_results:
            if 'CSFloat -> Steam' in mode:
                buy_p, sell_p = d['f'], d['s']
                net_sell = steam_net_hesapla(sell_p)
                b_f, s_t = "CSFloat", "Steam"
            else:
                buy_p, sell_p = d['s'], d['f']
                net_sell = round(sell_p * 0.98, 2)
                b_f, s_t = "Steam", "CSFloat"
            
            qty = math.floor(user_balance / buy_p) if buy_p > 0 else 0
            if qty > 0:
                profit_per = round(net_sell - buy_p, 2)
                total_p = round(profit_per * qty, 2)
                roi = round((profit_per / buy_p) * 100, 1)
                if total_p > 0:
                    final_list.append({'name': d['name'], 'qty': qty, 'profit': total_p, 'buy': buy_p, 'sell': sell_p, 'net': net_sell, 'roi': roi, 'b_f': b_f, 's_t': s_t})

        sorted_res = sorted(final_list, key=lambda x: x['profit'], reverse=True)[:5]
        
        if sorted_res:
            report = f"🔥 **GÜNCEL FIRSAT ANALİZİ**\n`Bakiye: ${user_balance}`\n\n"
            for idx, it in enumerate(sorted_res, 1):
                report += (f"{idx}. **{it['name']}**\n"
                           f"🛒 Alış: `${it['buy']}` ({it['b_f']})\n"
                           f"💰 Satış: `${it['sell']}` ({it['s_t']})\n"
                           f"📈 Kâr: `${it['profit']}` | ROI: `%{it['roi']}`\n\n")
            await update.message.reply_text(report, parse_mode="Markdown")
        else:
            await update.message.reply_text("⚠️ Seçilen bakiye ve filtrelere uygun kârlı ürün bulunamadı.")

        context.user_data['analyzing'] = False
        kb = [['🔄 Yeniden Başlat', '💰 Bakiye Değiştir'], ['🏠 Ana Menü']]
        await update.message.reply_text("✅ Analiz başarıyla tamamlandı.", reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True))

    elif text == '🔄 Yeniden Başlat':
        context.user_data['analyzing'] = False
        await handle_msg(update, context)
    elif text == '💰 Bakiye Değiştir':
        mode = context.user_data.get('mode')
        context.user_data.clear()
        context.user_data['mode'] = mode
        await update.message.reply_text("💰 Yeni bakiye girin ($):")
    elif text == '🏠 Ana Menü':
        await start(update, context)

if __name__ == "__main__":
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_msg))
    app.run_polling()
