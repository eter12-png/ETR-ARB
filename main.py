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

logging.basicConfig(
    format='%(asctime)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

API_KEY = os.getenv("CSFLOAT_API_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
MIN_VOLUME_LIMIT = 50
VOLUME_LIMIT_PERCENT = 0.15
MAX_BUDGET_PER_ITEM = 0.30

def generate_progress_bar(current, total, found=0):
    if total <= 0:
        return "⬜⬜⬜⬜⬜⬜⬜⬜⬜⬜ %0"
    bar_length = 10
    fraction = current / total
    filled = int(fraction * bar_length)
    if fraction >= 1.0:
        bar = "✅" * bar_length
    else:
        bar = "🟩" * filled + "⬜" * (bar_length - filled)
    percent = int(fraction * 100)
    return f"┣ {bar}  `%{percent}`\n┣ `{current}/{total}` item  •  `{found}` fırsat bulundu"

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
        else:
            seller_gets = round(seller_gets - 0.01, 2)
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

async def fetch_item(session, name):
    headers = {'User-Agent': 'Mozilla/5.0'}
    safe_name = urllib.parse.quote(name)
    s_url = f"https://steamcommunity.com/market/priceoverview/?appid=730&currency=1&market_hash_name={safe_name}"
    f_url = f"https://csfloat.com/api/v1/listings?market_hash_name={safe_name}&limit=50&sort_by=lowest_price&type=buy_now"

    try:
        async with session.get(s_url, headers=headers) as r_s:
            s_data = await r_s.json()
            if not s_data or "lowest_price" not in s_data:
                return None
            vol = int(str(s_data.get("volume", "0")).replace(",", "")) if str(s_data.get("volume", "0")).replace(",", "").isdigit() else 0
            if vol < MIN_VOLUME_LIMIT:
                return None
            s_price = float(s_data["lowest_price"].replace("$", "").replace(",", ""))

        async with session.get(f_url, headers={"Authorization": API_KEY}) as r_f:
            f_data = await r_f.json()
            listings = f_data if isinstance(f_data, list) else f_data.get('data', [])
            if not listings:
                return None
            prices = [round(l['price']/100, 2) for l in listings]
            f_price = max(Counter(prices), key=Counter(prices).get)

        return {"name": name, "s": s_price, "f": f_price, "vol": vol}
    except:
        return None

async def scan_items(update, context, user_balance, items_list):
    total = len(items_list)
    all_results = []
    found_count = 0

    # Tek progress mesajı gönder, sonra edit_text ile güncelle
    progress_msg = await update.message.reply_text(
        f"🔎 *{total} item taranıyor...*\n{generate_progress_bar(0, total, 0)}",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup([['🛑 Taramayı Durdur']], resize_keyboard=True)
    )
    context.user_data['progress_msg'] = progress_msg

    try:
        timeout = aiohttp.ClientTimeout(total=5)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            for i, item in enumerate(items_list, 1):
                # Her mesaj kontrolünde durdurma bayrağını kontrol et
                if context.user_data.get('stop_scan'):
                    context.user_data['stop_scan'] = False
                    raise asyncio.CancelledError()

                logger.info(f"[{i}/{total}] {item}")
                res = await fetch_item(session, item)
                if res:
                    all_results.append(res)
                    found_count += 1

                # Her 5 itemda bir progress mesajını güncelle
                if i % 5 == 0 or i == total:
                    try:
                        await progress_msg.edit_text(
                            f"🔎 *{total} item taranıyor...*\n{generate_progress_bar(i, total, found_count)}",
                            parse_mode="Markdown"
                        )
                    except Exception:
                        pass  # Edit başarısız olursa sessizce devam et

                await asyncio.sleep(random.uniform(1, 2))

    except asyncio.CancelledError:
        try:
            await progress_msg.edit_text(
                f"🛑 *Tarama durduruldu*\n{generate_progress_bar(len(all_results), total, found_count)}\n\n`{len(all_results)}/{total}` tamamlandı.",
                parse_mode="Markdown"
            )
        except Exception:
            pass
        await update.message.reply_text(
            "Ne yapmak istersin?",
            reply_markup=ReplyKeyboardMarkup(
                [['🔄 CSFloat -> Steam', '🔄 Steam -> CSFloat']],
                resize_keyboard=True
            )
        )
        context.user_data['analyzing'] = False
        return None

    # Tarama tamamlandı — progress mesajını güncelle
    try:
        await progress_msg.edit_text(
            f"✅ *Tarama tamamlandı!*\n{generate_progress_bar(total, total, found_count)}",
            parse_mode="Markdown"
        )
    except Exception:
        pass

    return all_results

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = [['🔄 CSFloat -> Steam', '🔄 Steam -> CSFloat']]
    await update.message.reply_text("🚀 İşlem seç:", reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True))

async def handle_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text

    if text == "🛑 Taramayı Durdur":
        if context.user_data.get('analyzing'):
            context.user_data['stop_scan'] = True
        return

    if text in ['🔄 CSFloat -> Steam', '🔄 Steam -> CSFloat']:
        context.user_data['mode'] = text
        await update.message.reply_text("💰 Bakiye gir ($):", reply_markup=ReplyKeyboardRemove())
        return

    if 'mode' in context.user_data and not context.user_data.get('analyzing'):
        try:
            user_balance = float(text.replace(",", "."))
            context.user_data['analyzing'] = True
        except:
            await update.message.reply_text("❌ sayı gir")
            return

        items_list = load_items()
        if not items_list:
            await update.message.reply_text("❌ items.txt yok")
            context.user_data['analyzing'] = False
            return

        context.user_data['stop_scan'] = False

        all_results = await scan_items(update, context, user_balance, items_list)
        if all_results is None:
            return

        final_list = []
        for d in all_results:
            if 'CSFloat -> Steam' in context.user_data['mode']:
                buy_p, sell_p, net_sell = d['f'], d['s'], steam_net_hesapla(d['s'])
            else:
                buy_p, sell_p, net_sell = d['s'], d['f'], round(d['f'] * 0.98, 2)

            roi = round(((net_sell - buy_p) / buy_p) * 100, 1) if buy_p > 0 else 0
            if net_sell > buy_p:
                final_list.append({'name': d['name'], 'buy': buy_p, 'sell': sell_p, 'net': net_sell, 'roi': roi, 'vol': d['vol']})

        sepet, harcanan = create_balanced_basket(final_list, user_balance)

        if sepet:
            report = f"⚖️ SEPET\nBakiye: ${harcanan}/${user_balance}\n\n"
            for i, item in enumerate(sepet, 1):
                report += f"{i}. {item['name']}\n{item['final_qty']} adet | +${item['total_profit']} (%{item['roi']})\n"
            report += f"\nToplam: ${round(sum(i['total_profit'] for i in sepet), 2)}"
            await update.message.reply_text(report)
        else:
            await update.message.reply_text("❌ fırsat yok")

        context.user_data['analyzing'] = False
        await update.message.reply_text(
            "✅ bitti",
            reply_markup=ReplyKeyboardMarkup(
                [['🔄 CSFloat -> Steam', '🔄 Steam -> CSFloat']],
                resize_keyboard=True
            )
        )

if __name__ == "__main__":
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_msg))
    app.run_polling()
