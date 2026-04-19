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
VOLUME_LIMIT_PERCENT = 0.15
MAX_BUDGET_PER_ITEM = 0.30

# --- YARDIMCI FONKSİYONLAR ---
def generate_progress_bar(current, total):
    if total <= 0: return "⬜⬜⬜⬜⬜⬜⬜⬜⬜⬜ %0"
    bar_length = 10
    fraction = current / total
    filled = int(fraction * bar_length)
    color_block = "🟩" if fraction < 0.99 else "✅"
    bar = color_block * filled + "⬜" * (bar_length - filled)
    percent = int(fraction * 100)
    return f"┣ {bar}  `%{percent}`"

def load_items():
    if os.path.exists("items.txt"):
        with open("items.txt", "r", encoding="utf-8") as f:
            lines = [line.strip() for line in f.readlines() if line.strip()]
            logger.info(f"Dosyadan {len(lines)} item yüklendi.")
            return lines
    logger.error("items.txt dosyası bulunamadı!")
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
        max_qty_by_vol = math.floor(item['vol'] * VOLUME_LIMIT_PERCENT)
        max_budget_for_this_item = total_balance * MAX_BUDGET_PER_ITEM
        max_qty_by_budget = math.floor(min(remaining_balance, max_budget_for_this_item) / item['buy'])
        final_qty = min(max_qty_by_vol, max_qty_by_budget)
        if final_qty > 0:
            cost = round(final_qty * item['buy'], 2)
            profit = round((item['net'] - item['buy']) * final_qty, 2)
            basket.append({**item, 'final_qty': final_qty, 'total_profit': profit, 'total_cost': cost})
            remaining_balance -= cost
    return basket, round(total_balance - remaining_balance, 2)

# --- VERİ ÇEKME ---
async def fetch_item(session, name):
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0 Safari/537.36'}
    safe_name = urllib.parse.quote(name)
    s_url = f"https://steamcommunity.com/market/priceoverview/?appid=730&currency=1&market_hash_name={safe_name}"
    f_url = f"https://csfloat.com/api/v1/listings?market_hash_name={safe_name}&limit=50&sort_by=lowest_price&type=buy_now"

    try:
        # Steam Çekimi
        async with session.get(s_url, headers=headers, timeout=10) as r_s:
            if r_s.status == 429: return ("RETRY", "429")
            s_data = await r_s.json()
            if not s_data or "lowest_price" not in s_data: return ("SKIP", "Yok")
            vol = int(str(s_data.get("volume", "0")).replace(",", "")) if str(s_data.get("volume", "0")).replace(",", "").isdigit() else 0
            if vol < MIN_VOLUME_LIMIT: return ("SKIP", "Düşük Hacim")
            s_price = float(s_data["lowest_price"].replace("$", "").replace(",", ""))

        # CSFloat Çekimi
        async with session.get(f_url, headers={"Authorization": API_KEY}, timeout=10) as r_f:
            f_data = await r_f.json()
            listings = f_data if isinstance(f_data, list) else f_data.get('data', [])
            if not listings: return ("SKIP", "İlan Yok")
            prices = [round(l['price']/100, 2) for l in listings]
            f_price = max(Counter(prices), key=Counter(prices).get)

        return {"name": name, "s": s_price, "f": f_price, "vol": vol}

    # DEĞİŞİKLİK 1: Hataları artık logluyoruz, sessizce yutmuyoruz
    except Exception as e:
        logger.error(f"HATA: {name} | {e}")
        return ("RETRY", "Hata")

# --- TELEGRAM ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = [['🔄 CSFloat -> Steam', '🔄 Steam -> CSFloat']]
    await update.message.reply_text("🚀 İşlem yönü seçin:", reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True))

async def handle_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text

    if text == "🛑 Taramayı Durdur":
        context.user_data['stop_scan'] = True
        return

    if text in ['🔄 CSFloat -> Steam', '🔄 Steam -> CSFloat']:
        context.user_data['mode'] = text
        await update.message.reply_text("💰 Bakiye girin ($):", reply_markup=ReplyKeyboardRemove())
        return

    if 'mode' in context.user_data and not context.user_data.get('analyzing'):
        try:
            user_balance = float(text.replace(",", "."))
        except:
            await update.message.reply_text("❌ Sayı girin.")
            return

        # Eğer kullanıcı tarama başlamadan "Durdur" gönderdiyse iptal et
        if context.user_data.get('stop_scan'):
            context.user_data['stop_scan'] = False
            await update.message.reply_text("⚠️ Önceki durdurma komutu temizlendi. Tekrar bakiye girin.")
            return

        context.user_data['analyzing'] = True
        context.user_data['stop_scan'] = False

        items_list = load_items()
        if not items_list:
            await update.message.reply_text("❌ items.txt boş veya bulunamadı.")
            context.user_data['analyzing'] = False
            return

        total = len(items_list)
        all_results, success_count = [], 0
        skip_count = 0
        error_count = 0

        await update.message.reply_text(f"🔎 **{total}** item taranıyor... Lütfen bekleyin.", reply_markup=ReplyKeyboardMarkup([['🛑 Taramayı Durdur']], resize_keyboard=True))

        async with aiohttp.ClientSession() as session:
            for i, item in enumerate(items_list, 1):
                if context.user_data.get('stop_scan'):
                    await update.message.reply_text("🛑 Tarama durduruldu.")
                    break

                logger.info(f"[{i}/{total}] Taranıyor: {item}")

                res = await fetch_item(session, item)

                if isinstance(res, tuple) and res[0] == "RETRY":
                    # DEĞİŞİKLİK 2: Kısa bekleme sonrası tekrar dene
                    await asyncio.sleep(10)
                    res = await fetch_item(session, item)

                if isinstance(res, dict):
                    all_results.append(res)
                    success_count += 1
                elif isinstance(res, tuple):
                    skip_count += 1
                    logger.info(f"ATLANDI: {item} | Sebep: {res[1]}")

                if i % 10 == 0:
                    await update.message.reply_text(f"⏳ İlerleme: {generate_progress_bar(i, total)}", parse_mode="Markdown")

                # DEĞİŞİKLİK 3: Sleep'i parçalara böldük — "Durdur" komutu anında çalışsın
                sleep_duration = random.uniform(3, 5)
                steps = int(sleep_duration * 10)
                for _ in range(steps):
                    if context.user_data.get('stop_scan'):
                        break
                    await asyncio.sleep(0.1)

        # --- SEPET ANALİZİ ---
        final_list = []
        eliminated_count = 0

        for d in all_results:
            if 'CSFloat -> Steam' in context.user_data['mode']:
                buy_p, sell_p, net_sell = d['f'], d['s'], steam_net_hesapla(d['s'])
            else:
                buy_p, sell_p, net_sell = d['s'], d['f'], round(d['f'] * 0.98, 2)

            roi = round(((net_sell - buy_p) / buy_p) * 100, 1) if buy_p > 0 else 0

            if net_sell > buy_p:
                final_list.append({'name': d['name'], 'buy': buy_p, 'sell': sell_p, 'net': net_sell, 'roi': roi, 'vol': d['vol']})
            else:
                # DEĞİŞİKLİK 4: Neden elendi artık loglarda görünüyor
                eliminated_count += 1
                logger.info(f"ELENDİ: {d['name']} | Alış: {buy_p} | Net: {net_sell} | ROI: %{roi}")

        logger.info(f"Özet → Taranan: {success_count} | Kârlı: {len(final_list)} | Elenen: {eliminated_count} | Atlanan: {skip_count}")

        sepet, harcanan = create_balanced_basket(final_list, user_balance)

        if sepet:
            report = f"⚖️ **RİSK DENGELİ ALIM SEPETİ**\n`Bakiye: ${harcanan} / ${user_balance}`\n\n"
            for idx, item in enumerate(sepet, 1):
                report += f"{idx}. **{item['name']}**\n📦 `{item['final_qty']} Adet` | Kâr: `+${item['total_profit']}` (%{item['roi']})\n"
            report += f"\n📈 **TOPLAM KÂR: ${round(sum(i['total_profit'] for i in sepet), 2)}**"
            await update.message.reply_text(report, parse_mode="Markdown")
        else:
            # DEĞİŞİKLİK 5: Neden kârlı fırsat olmadığına dair özet Telegram'a da geliyor
            await update.message.reply_text(
                f"❌ Kârlı fırsat yok.\n\n"
                f"📊 *Tarama özeti:*\n"
                f"• Fiyat çekilen: `{success_count}` item\n"
                f"• Kârsız elenen: `{eliminated_count}` item\n"
                f"• Atlanan (hacim/ilan yok): `{skip_count}` item",
                parse_mode="Markdown"
            )

        context.user_data['analyzing'] = False
        await update.message.reply_text("✅ İşlem bitti.", reply_markup=ReplyKeyboardMarkup([['🔄 CSFloat -> Steam', '🔄 Steam -> CSFloat']], resize_keyboard=True))

if __name__ == "__main__":
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_msg))
    app.run_polling()
 
