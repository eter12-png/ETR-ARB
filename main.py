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
    if total <= 0:
        return "⬜⬜⬜⬜⬜⬜⬜⬜⬜⬜ %0"
    bar_length = 10
    fraction = current / total
    filled = int(fraction * bar_length)
    color_block = "🟩" if fraction < 0.99 else "✅"
    bar = color_block * filled + "⬜" * (bar_length - filled)
    percent = int(fraction * 100)
    return f"┣ {bar} %{percent}"

def load_items():
    if os.path.exists("items.txt"):
        with open("items.txt", "r", encoding="utf-8") as f:
            lines = [line.strip() for line in f.readlines() if line.strip()]
            logger.info(f"Dosyadan {len(lines)} item yüklendi.")
            return lines
    logger.error("items.txt dosyası bulunamadı!")
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
            if round(
                next_s
                + max(0.01, math.floor(next_s * 0.05 * 100 + 0.000001) / 100)
                + max(0.01, math.floor(next_s * 0.10 * 100 + 0.000001) / 100),
                2
            ) <= buyer_pays:
                seller_gets = next_s
            else:
                break
        else:
            seller_gets = round(seller_gets - 0.01, 2)
    return seller_gets

def create_balanced_basket(final_list, total_balance):
    basket = []
    remaining_balance = total_balance
    sorted_items = sorted(final_list, key=lambda x: x['roi'], reverse=True)
    for item in sorted_items:
        if remaining_balance <= 0.05:
            break
        max_qty_by_vol = math.floor(item['vol'] * VOLUME_LIMIT_PERCENT)
        max_budget_for_this_item = total_balance * MAX_BUDGET_PER_ITEM
        max_qty_by_budget = math.floor(min(remaining_balance, max_budget_for_this_item) / item['buy'])
        final_qty = min(max_qty_by_vol, max_qty_by_budget)
        
        if final_qty > 0:
            cost = round(final_qty * item['buy'], 2)
            profit = round((item['net'] - item['buy']) * final_qty, 2)
            basket.append({**item, 'final_qty': final_qty, 'total_profit': profit, 'total_cost': cost})
            remaining_balance -= cost
        else:
            logger.debug(f"ℹ️ {item['name']} atlandı: Bütçe/Hacim limiti yetersiz.")
            
    return basket, round(total_balance - remaining_balance, 2)

# --- VERİ ÇEKME ---
async def fetch_item(session, name):
    # 403 Hatalarını engellemek için User-Agent Rotasyonu
    user_agents = [
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36',
        'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:122.0) Gecko/20100101 Firefox/122.0',
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Edge/121.0.0.0 Safari/537.36'
    ]
    
    headers = {'User-Agent': random.choice(user_agents)}
    safe_name = urllib.parse.quote(name)
    s_url = f"https://steamcommunity.com/market/priceoverview/?appid=730&currency=1&market_hash_name={safe_name}"
    f_url = f"https://csfloat.com/api/v1/listings?market_hash_name={safe_name}&limit=50&sort_by=lowest_price&type=buy_now"

    try:
        # --- STEAM ---
        async with session.get(s_url, headers=headers, timeout=15) as r_s:
            # Sadece 429 değil, 403 ve 500'lü hataları da RETRY'a düşür
            if r_s.status in [429, 403, 500, 502, 503, 504]:
                return ("RETRY", f"Steam hata/limit ({r_s.status})")
            elif r_s.status != 200:
                return ("SKIP", f"Steam HTTP hata: {r_s.status}")
            
            s_data = await r_s.json()
            if not s_data or "lowest_price" not in s_data:
                return ("SKIP", "Steam lowest_price yok")

            raw_vol = str(s_data.get("volume", "0")).replace(",", "")
            vol = int(raw_vol) if raw_vol.isdigit() else 0
            if vol < MIN_VOLUME_LIMIT:
                return ("SKIP", f"Düşük volume: {vol}")

            s_price = float(s_data["lowest_price"].replace("$", "").replace(",", ""))

        # Steam çekildikten sonra kısa bir nefes (CSFloat'a geçmeden önce)
        await asyncio.sleep(random.uniform(1.5, 3.0))

        # --- CSFLOAT ---
        async with session.get(f_url, headers={"Authorization": API_KEY, "User-Agent": headers['User-Agent']}, timeout=15) as r_f:
            if r_f.status in [429, 403, 500, 502, 503, 504]:
                return ("RETRY", f"CSFloat hata/limit ({r_f.status})")
            elif r_f.status != 200:
                return ("SKIP", f"CSFloat HTTP hata: {r_f.status}")
            
            f_data = await r_f.json()
            listings = f_data if isinstance(f_data, list) else f_data.get('data', [])
            if not listings:
                return ("SKIP", "CSFloat boş listing")

            prices = [round(l['price']/100, 2) for l in listings]
            f_price = max(Counter(prices), key=Counter(prices).get)

        return {"name": name, "s": s_price, "f": f_price, "vol": vol}
    
    except Exception as e:
        return ("RETRY", f"Exception: {str(e)}")

# --- ARKA PLAN TARAMA GÖREVİ ---
async def run_scan(update: Update, context: ContextTypes.DEFAULT_TYPE, items_list: list, user_balance: float):
    total = len(items_list)
    all_results = []

    await update.message.reply_text(
        f"🔎 **{total}** item taranıyor... Lütfen bekleyin.",
        reply_markup=ReplyKeyboardMarkup([['🛑 Taramayı Durdur']], resize_keyboard=True)
    )

    try:
        async with aiohttp.ClientSession() as session:
            for i, item in enumerate(items_list, 1):
                logger.info(f"[{i}/{total}] Taranıyor: {item}")
                
                res = await fetch_item(session, item)
                
                # --- RETRY MANTIĞI (AĞIR GERİ ÇEKİLME) ---
                retry_count = 0
                while isinstance(res, tuple) and res[0] == "RETRY" and retry_count < 3: # Şansımızı 3'e çıkardık
                    # Steam genellikle 1-2 dakikada ban kaldırmaz. 4-6 dakika arası bekletiyoruz.
                    bekleme_suresi = random.uniform(240, 360) 
                    logger.warning(f"⚠️ {item} için hata ({res[1]}). {retry_count+1}. deneme öncesi {int(bekleme_suresi)} saniye bekleniyor...")
                    await asyncio.sleep(bekleme_suresi)
                    res = await fetch_item(session, item)
                    retry_count += 1

                if isinstance(res, tuple) and res[0] == "SKIP":
                    continue

                if isinstance(res, dict):
                    all_results.append(res)

                if i % 10 == 0:
                    await update.message.reply_text(
                        f"⏳ İlerleme: {generate_progress_bar(i, total)}",
                        parse_mode="Markdown"
                    )
                
                # --- ITEMLAR ARASI BEKLEME (DAHA HIZLI) ---
                # Sorun yaşanmadığı sürece hızlıca taramaya devam et
                await asyncio.sleep(random.uniform(5, 8))
                
                # --- HER 20 ITEMDA BİR BLOK BEKLEME (KISA MOLA) ---
                if i % 20 == 0:
                    logger.info("⏸️ 20 item doldu, blok bekleme yapılıyor...")
                    await asyncio.sleep(random.uniform(60, 90))

        # --- SEPET ANALİZİ VE RAPORLAMA ---
        final_list = []
        for d in all_results:
            if 'CSFloat -> Steam' in context.user_data['mode']:
                buy_p, sell_p, net_sell = d['f'], d['s'], steam_net_hesapla(d['s'])
            else:
                buy_p, sell_p, net_sell = d['s'], d['f'], round(d['f'] * 0.98, 2)

            roi = round(((net_sell - buy_p) / buy_p) * 100, 1) if buy_p > 0 else 0
            if net_sell > buy_p:
                final_list.append({
                    'name': d['name'], 'buy': buy_p, 'sell': sell_p, 
                    'net': net_sell, 'roi': roi, 'vol': d['vol']
                })

        sepet, harcanan = create_balanced_basket(final_list, user_balance)

        if sepet:
            report = f"⚖️ **RİSK DENGELİ ALIM SEPETİ**\nBakiye: `${harcanan}` / `${user_balance}`\n\n"
            for idx, item in enumerate(sepet, 1):
                report += f"{idx}. **{item['name']}**\n"
                report += f"💰 Alış: `${item['buy']}` | Satış: `${item['sell']}`\n"
                report += f"📩 Net: `${item['net']}`\n"
                report += f"📦 {item['final_qty']} Adet | Kâr: +${item['total_profit']} (%{item['roi']})\n"
                report += f"---\n" 
                
            report += f"\n📈 **TOPLAM KÂR: ${round(sum(i['total_profit'] for i in sepet), 2)}**"
            await update.message.reply_text(report, parse_mode="Markdown")
        else:
            await update.message.reply_text("❌ Kârlı fırsat yok.")

        context.user_data['analyzing'] = False
        await update.message.reply_text(
            "✅ İşlem bitti. Şimdi ne yapalım?",
            reply_markup=ReplyKeyboardMarkup([['🔄 CSFloat -> Steam', '🔄 Steam -> CSFloat']], resize_keyboard=True)
        )

    except asyncio.CancelledError:
        pass
    except Exception as e:
        logger.error(f"Tarama sırasında hata: {e}")
        context.user_data['analyzing'] = False

# --- TELEGRAM ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = [['🔄 CSFloat -> Steam', '🔄 Steam -> CSFloat']]
    await update.message.reply_text("🚀 İşlem yönü seçin:", reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True))

async def handle_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text

    if text == "🛑 Taramayı Durdur":
        task = context.user_data.get('scan_task')
        if task and not task.done():
            task.cancel()
        context.user_data['analyzing'] = False
        kb = [['🔄 CSFloat -> Steam', '🔄 Steam -> CSFloat']]
        await update.message.reply_text("🛑 Tarama durduruldu.", reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True))
        return

    if text in ['🔄 CSFloat -> Steam', '🔄 Steam -> CSFloat']:
        context.user_data['mode'] = text
        await update.message.reply_text("💰 Bakiye girin ($):", reply_markup=ReplyKeyboardRemove())
        return

    if 'mode' in context.user_data and not context.user_data.get('analyzing'):
        try:
            user_balance = float(text.replace(",", "."))
            context.user_data['analyzing'] = True
        except ValueError:
            await update.message.reply_text("❌ Sayı girin.")
            return

        items_list = load_items()
        if not items_list:
            await update.message.reply_text("❌ items.txt bulunamadı.")
            context.user_data['analyzing'] = False
            return

        context.user_data['scan_task'] = asyncio.create_task(
            run_scan(update, context, items_list, user_balance)
        )

if __name__ == "__main__":
    if not TELEGRAM_TOKEN or not API_KEY:
        print("❌ HATA: API_KEY veya TELEGRAM_TOKEN sistem değişkeni bulunamadı!")
    else:
        app = Application.builder().token(TELEGRAM_TOKEN).build()
        app.add_handler(CommandHandler("start", start))
        app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_msg))
        logger.info("🚀 Bot aktif, Railway üzerinde çalışıyor...")
        app.run_polling()
