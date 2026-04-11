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

# --- AYARLAR ---
API_KEY = "burada api yazıyor paylaşmamak için sildim"
TELEGRAM_TOKEN = "burada api yazıyor paylaşmamak için sildim"
MIN_PRICE_LIMIT = 0.20
MIN_VOLUME_LIMIT = 50

# --- YARDIMCI FONKSİYONLAR ---
def load_items():
    if os.path.exists("items.txt"):
        with open("items.txt", "r", encoding="utf-8") as f:
            return [line.strip() for line in f.readlines() if line.strip()]
    return []

def steam_net_hesapla(buyer_pays):
    if buyer_pays < 0.03:
        return 0
    seller_gets = math.floor(buyer_pays / 1.15 * 100) / 100
    while True:
        s_fee = max(0.01, math.floor(seller_gets * 0.05 * 100 + 0.000001) / 100)
        g_fee = max(0.01, math.floor(seller_gets * 0.10 * 100 + 0.000001) / 100)
        if round(seller_gets + s_fee + g_fee, 2) <= buyer_pays:
            next_s = round(seller_gets + 0.01, 2)
            if round(next_s + max(0.01, math.floor(next_s * 0.05 * 100 + 0.000001) / 100) + max(0.01, math.floor(next_s * 0.10 * 100 + 0.000001) / 100), 2) <= buyer_pays:
                seller_gets = next_s
            else:
                break
        else:
            seller_gets = round(seller_gets - 0.01, 2)
    return seller_gets

# --- VERİ ÇEKME ---
async def fetch_item(session, name, idx, total):
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0'}
    safe_name = urllib.parse.quote(name)

    s_url = f"https://steamcommunity.com/market/priceoverview/?appid=730&currency=1&market_hash_name={safe_name}"
    f_url = f"https://csfloat.com/api/v1/listings?market_hash_name={safe_name}&limit=50&sort_by=lowest_price&type=buy_now"

    try:
        # --- STEAM ---
        async with session.get(s_url, headers=headers, timeout=15) as r_s:
            if r_s.status == 429:
                return ("RETRY", "Steam rate limit (429)")

            if r_s.status != 200:
                return ("SKIP", f"Steam HTTP hata: {r_s.status}")

            s_data = await r_s.json()

            if not s_data or "lowest_price" not in s_data:
                return ("SKIP", "Steam lowest_price yok")

            raw_vol = str(s_data.get("volume", "0")).replace(",", "")
            vol = int(raw_vol) if raw_vol.isdigit() else 0

            if vol < MIN_VOLUME_LIMIT:
                return ("SKIP", f"Düşük volume: {vol}")

            s_price = float(s_data["lowest_price"].replace("$", "").replace(",", ""))

            if s_price < MIN_PRICE_LIMIT:
                return ("SKIP", f"Düşük Steam fiyat: {s_price}")

        await asyncio.sleep(random.uniform(1.0, 2.0))

        # --- CSFLOAT ---
        async with session.get(f_url, headers={"Authorization": API_KEY}, timeout=15) as r_f:
            if r_f.status == 429:
                return ("RETRY", "CSFloat rate limit (429)")

            f_data = await r_f.json()
            listings = f_data if isinstance(f_data, list) else f_data.get('data', [])

            if not listings:
                return ("SKIP", "CSFloat boş listing")

            prices = [round(l['price']/100, 2) for l in listings]
            f_price = max(Counter(prices), key=Counter(prices).get)

            if f_price < MIN_PRICE_LIMIT:
                return ("SKIP", f"Düşük CSFloat fiyat: {f_price}")

        print(f"✅ [{idx}/{total}] {name} çekildi.")
        return {"name": name, "s": s_price, "f": f_price, "vol": vol}

    except Exception as e:
        return ("RETRY", f"Exception: {str(e)}")


# --- TELEGRAM ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = [['🔄 CSFloat -> Steam', '🔄 Steam -> CSFloat']]
    await update.message.reply_text(
        "🚀 Yön seçin:",
        reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True)
    )

async def handle_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text

    if text in ['🔄 CSFloat -> Steam', '🔄 Steam -> CSFloat']:
        context.user_data['mode'] = text
        await update.message.reply_text("💰 Bakiye gir (Örn: 787.09):", reply_markup=ReplyKeyboardRemove())
        return

    if 'mode' in context.user_data and 'analyzing' not in context.user_data:
        try:
            user_balance = float(text.replace(",", "."))
            context.user_data['analyzing'] = True
            mode = context.user_data['mode']
        except:
            await update.message.reply_text("❌ Geçerli sayı gir")
            return

        raw_items = load_items()
        total = len(raw_items)
        all_results = []

        filename = f"results_{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}.txt"

        status_msg = await update.message.reply_text(f"🛰 {user_balance}$ analiz başladı")

        async with aiohttp.ClientSession() as session:
            for i, item in enumerate(raw_items, 1):

                if i % 5 == 0:
                    try:
                        await status_msg.edit_text(f"📡 {i}/{total} | İşlenen item: {len(all_results)}")
                    except:
                        pass

                res = await fetch_item(session, item, i, total)

                retry_count = 0
                while isinstance(res, tuple) and res[0] == "RETRY" and retry_count < 2:
                    await asyncio.sleep(random.uniform(60, 120))
                    res = await fetch_item(session, item, i, total)
                    retry_count += 1

                # --- SKIP LOG (FIXED) ---
                if isinstance(res, tuple) and res[0] == "SKIP":
                    reason = res[1]
                    print(f"[{i}/{total}] {item} → SKIPPED | {reason}")
                    continue

                if isinstance(res, dict):
                    all_results.append(res)

                    with open(filename, "a", encoding="utf-8") as f:
                        f.write(f"{res['name']} | Steam:{res['s']} | CSFloat:{res['f']} | Vol:{res['vol']}\n")

                await asyncio.sleep(random.uniform(14, 20))

                if i % 20 == 0:
                    await asyncio.sleep(random.uniform(120, 180))

                if i % 100 == 0:
                    await asyncio.sleep(random.uniform(600, 900))

        # --- HESAPLAMA ---
        final_list = []
        for data in all_results:
            buy_price = data['f'] if 'CSFloat -> Steam' in mode else data['s']
            sell_price = data['s'] if 'CSFloat -> Steam' in mode else data['f']

            if 'CSFloat -> Steam' in mode:
                net_sell_price = steam_net_hesapla(sell_price)
            else:
                net_sell_price = round(sell_price * 0.98, 2)

            if buy_price > 0:
                quantity = math.floor(user_balance / buy_price)
                if quantity > 0:
                    total_cost = quantity * buy_price
                    total_revenue = quantity * net_sell_price
                    total_profit = round(total_revenue - total_cost, 2)

                    final_list.append({
                        'name': data['name'],
                        'qty': quantity,
                        'buy': buy_price,
                        'net_sell': net_sell_price,
                        'profit': total_profit,
                        'final_balance': round(user_balance + total_profit, 2),
                        'vol': data['vol']
                    })

        sorted_res = sorted(final_list, key=lambda x: x['profit'], reverse=True)[:5]

        if not sorted_res:
            await status_msg.edit_text("❌ Kârlı item yok")
        else:
            report = f"🏆 EN İYİ 5\n\n"
            for i, item in enumerate(sorted_res, 1):
                report += f"{i}. {item['name']}\n"
                report += f"Adet: {item['qty']}\n"
                report += f"Kâr: +${item['profit']}\n\n"

            await status_msg.edit_text(report)

        context.user_data.clear()


# --- MAIN ---
if __name__ == "__main__":
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_msg))

    print("🚀 Bot aktif")
    app.run_polling()