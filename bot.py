import os
import sqlite3
import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional, Tuple

from flask import Flask
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, Message
from telegram.constants import ParseMode
from telegram.error import BadRequest, TimedOut, NetworkError, Forbidden
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
from telegram.request import HTTPXRequest

DB_PATH = "bacbo.db"
logging.basicConfig(level=logging.INFO)

# ====== Configuración ======
WINDOW_N = 30
MAX_GALE = 1
STOP_LOSS_PCT = 10
TAKE_PROFIT_PCT = 5
BASE_BET_PCT = 2.0
ALLOWED_BETS = [5000, 10000, 25000, 125000, 500000, 2500000]
TIE_AVOID_THRESHOLD = 3
DANGER_COOLDOWN_ROUNDS = 3

# Estilo AnaPrime
ALERT_DELAY_SECONDS = 8  # segundos que dura "POSIBLE ENTRADA" antes de confirmar

# ===== Helpers =====
def side_label(side: str) -> str:
    return "🔵 PLAYER" if side == "P" else "🔴 BANKER"

def result_ball(result: str) -> str:
    if result == "P":
        return "🔵"
    if result == "B":
        return "🔴"
    return "🟠"  # TIE

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    err = context.error
    if isinstance(err, (TimedOut, NetworkError)):
        logging.warning("Telegram timeout/network error (ignorado): %s", err)
        return
    logging.exception("Exception while handling an update:", exc_info=err)

# ====== DB ======
def init_db():
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS rounds (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT NOT NULL,
        result TEXT NOT NULL CHECK(result IN ('P','B','T'))
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS session (
        id INTEGER PRIMARY KEY CHECK(id=1),
        is_active INTEGER NOT NULL DEFAULT 0,
        bank_start REAL NOT NULL DEFAULT 0,
        bank_current REAL NOT NULL DEFAULT 0,
        base_bet REAL NOT NULL DEFAULT 0,
        gale_level INTEGER NOT NULL DEFAULT 0,
        last_reco TEXT DEFAULT NULL,
        pending_side TEXT DEFAULT NULL,
        pending_bet REAL NOT NULL DEFAULT 0,
        awaiting_outcome INTEGER NOT NULL DEFAULT 0,
        danger_cooldown INTEGER NOT NULL DEFAULT 0,
        last_alert_msg_id INTEGER DEFAULT NULL,
        last_confirm_msg_id INTEGER DEFAULT NULL
    )
    """)
    cur.execute("INSERT OR IGNORE INTO session (id) VALUES (1)")
    con.commit()
    con.close()

def add_round(result: str):
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute(
        "INSERT INTO rounds (ts, result) VALUES (?, ?)",
        (datetime.now(timezone.utc).isoformat(), result),
    )
    con.commit()
    con.close()

def get_last_results(n: int) -> List[str]:
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("SELECT result FROM rounds ORDER BY id DESC LIMIT ?", (n,))
    rows = [r[0] for r in cur.fetchall()]
    con.close()
    return list(reversed(rows))

def clear_rounds():
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("DELETE FROM rounds")
    con.commit()
    con.close()

# ====== Sesión ======
@dataclass
class SessionState:
    is_active: bool
    bank_start: float
    bank_current: float
    base_bet: float
    gale_level: int
    last_reco: Optional[str]
    pending_side: Optional[str]
    pending_bet: float
    awaiting_outcome: bool
    danger_cooldown: int
    last_alert_msg_id: Optional[int]
    last_confirm_msg_id: Optional[int]

def get_session() -> SessionState:
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("""
        SELECT
          is_active, bank_start, bank_current, base_bet, gale_level, last_reco,
          pending_side, pending_bet, awaiting_outcome, danger_cooldown,
          last_alert_msg_id, last_confirm_msg_id
        FROM session WHERE id=1
    """)
    row = cur.fetchone()
    con.close()
    return SessionState(
        bool(row[0]),
        float(row[1]),
        float(row[2]),
        float(row[3]),
        int(row[4]),
        row[5],
        row[6],
        float(row[7]),
        bool(row[8]),
        int(row[9]),
        row[10],
        row[11],
    )

def set_session(**kwargs):
    if not kwargs:
        return
    keys, vals = [], []
    for k, v in kwargs.items():
        keys.append(f"{k}=?")
        vals.append(v)
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute(f"UPDATE session SET {', '.join(keys)} WHERE id=1", vals)
    con.commit()
    con.close()

# ====== Cálculos banca ======
def round_to_allowed(amount: float) -> float:
    for v in ALLOWED_BETS:
        if amount <= v:
            return float(v)
    return float(ALLOWED_BETS[-1])

def start_session(bank: float):
    raw = bank * (BASE_BET_PCT / 100.0)
    base = round_to_allowed(max(ALLOWED_BETS[0], raw))
    set_session(
        is_active=1,
        bank_start=bank,
        bank_current=bank,
        base_bet=base,
        gale_level=0,
        last_reco=None,
        pending_side=None,
        pending_bet=0,
        awaiting_outcome=0,
        danger_cooldown=0,
        last_alert_msg_id=None,
        last_confirm_msg_id=None
    )

def stop_session():
    set_session(
        is_active=0,
        gale_level=0,
        last_reco=None,
        pending_side=None,
        pending_bet=0,
        awaiting_outcome=0,
        danger_cooldown=0,
        last_alert_msg_id=None,
        last_confirm_msg_id=None
    )

def calc_next_bet(sess: SessionState) -> float:
    base = round_to_allowed(float(sess.base_bet))
    if int(base) not in ALLOWED_BETS:
        base = float(ALLOWED_BETS[0])

    idx = ALLOWED_BETS.index(int(base))
    if sess.gale_level == 0:
        return float(ALLOWED_BETS[idx])

    next_idx = min(idx + 1, len(ALLOWED_BETS) - 1)
    return float(ALLOWED_BETS[next_idx])

def session_limits_text(sess: SessionState) -> str:
    if not sess.is_active or sess.bank_start <= 0:
        return ""
    sl = sess.bank_start * (1 - STOP_LOSS_PCT / 100.0)
    tp = sess.bank_start * (1 + TAKE_PROFIT_PCT / 100.0)
    return f"SL: {sl:.0f} | TP: {tp:.0f}"

def check_stop_take(sess: SessionState) -> Optional[str]:
    if not sess.is_active or sess.bank_start <= 0:
        return None
    sl = sess.bank_start * (1 - STOP_LOSS_PCT / 100.0)
    tp = sess.bank_start * (1 + TAKE_PROFIT_PCT / 100.0)
    if sess.bank_current <= sl:
        return "STOP_LOSS"
    if sess.bank_current >= tp:
        return "TAKE_PROFIT"
    return None

def settle_pending_bet(sess: SessionState, actual_result: str) -> Tuple[SessionState, str, Optional[str]]:
    if not (sess.is_active and sess.awaiting_outcome and sess.pending_side in ("P", "B") and sess.pending_bet > 0):
        return sess, "", None

    side = sess.pending_side
    bet = float(sess.pending_bet)
    bank = float(sess.bank_current)
    gale = int(sess.gale_level)

    if actual_result == "T":
        outcome_txt = "🟠 Resultado fue TIE. Push (banca no cambia)."
        outcome_kind = "TIE"
    elif actual_result == side:
        bank += bet
        gale = 0
        outcome_txt = f"✅ WIN automático ({side_label(side)}). +{bet:.0f}"
        outcome_kind = "GREEN"
    else:
        bank -= bet
        if gale < MAX_GALE:
            gale += 1
        else:
            gale = 0
        outcome_txt = f"❌ LOSE automático ({side_label(side)}). -{bet:.0f}"
        outcome_kind = "RED"

    set_session(
        bank_current=bank,
        gale_level=gale,
        pending_side=None,
        pending_bet=0,
        awaiting_outcome=0
    )

    return get_session(), outcome_txt, outcome_kind

# ====== Métricas ======
def chop_rate(seq: List[str]) -> float:
    filtered = [x for x in seq if x in ("P", "B")]
    if len(filtered) < 2:
        return 0.0
    changes = sum(1 for i in range(1, len(filtered)) if filtered[i] != filtered[i-1])
    return changes / (len(filtered) - 1)

def current_streak(seq: List[str]) -> Tuple[Optional[str], int]:
    if not seq:
        return None, 0
    last = seq[-1]
    if last == "T":
        return None, 0
    k = 1
    for i in range(len(seq)-2, -1, -1):
        if seq[i] == last:
            k += 1
        elif seq[i] == "T":
            continue
        else:
            break
    return last, k

def count_ties(seq: List[str]) -> int:
    return sum(1 for x in seq if x == "T")

def is_danger_table(seq: List[str]) -> Tuple[bool, str]:
    if len(seq) < 12:
        return False, ""

    win = seq[-WINDOW_N:] if len(seq) >= WINDOW_N else seq[:]
    ties = count_ties(win)
    cr = chop_rate(win)
    streak_side, streak_len = current_streak(win)

    if ties >= 4:
        return True, f"Muchos TIE en ventana ({ties}/{len(win)})."
    if cr >= 0.75 and streak_len <= 2:
        return True, f"Chop extremo (ChopRate {cr:.2f}) y sin racha clara."
    if 0.45 <= cr <= 0.55 and streak_len <= 2 and ties >= 2:
        return True, f"Mesa ruidosa (ChopRate {cr:.2f}) con TIE frecuentes."
    return False, ""

def decide_action(seq: List[str], sess: SessionState) -> Tuple[str, str]:
    if len(seq) < 10:
        return "NO_BET", "Aún hay pocas rondas (mínimo 10) para lectura."

    if sess.danger_cooldown > 0:
        return "NO_BET", f"ANTI-TILT activo: espera {sess.danger_cooldown} ronda(s)."

    danger, why = is_danger_table(seq)
    if danger:
        set_session(danger_cooldown=DANGER_COOLDOWN_ROUNDS, pending_side=None, pending_bet=0, awaiting_outcome=0)
        return "NO_BET", f"🚨 Mesa peligrosa: {why} (bloqueo {DANGER_COOLDOWN_ROUNDS} rondas)"

    last = seq[-1]
    win = seq[-WINDOW_N:] if len(seq) >= WINDOW_N else seq[:]
    ties = count_ties(win)
    cr = chop_rate(win)
    streak_side, streak_len = current_streak(win)

    if ties >= TIE_AVOID_THRESHOLD:
        return "NO_BET", f"Muchos TIE recientes ({ties}/{len(win)})."
    if last == "T":
        return "NO_BET", "Último resultado fue TIE. Espera 1 ronda."
    if 0.40 <= cr <= 0.60 and streak_len < 3:
        return "NO_BET", f"Mesa indecisa (ChopRate {cr:.2f})."

    if streak_len >= 3 and streak_side in ("P", "B"):
        return ("BET_P" if streak_side == "P" else "BET_B"), f"RACHA: {streak_side} x{streak_len}. Seguir racha."

    if cr >= 0.65:
        if last == "P":
            return "BET_B", f"CHOP (ChopRate {cr:.2f}). Contrario al último: B."
        if last == "B":
            return "BET_P", f"CHOP (ChopRate {cr:.2f}). Contrario al último: P."

    filtered = [x for x in seq if x in ("P", "B")]
    if len(filtered) >= 2 and filtered[-1] == filtered[-2]:
        side = filtered[-1]
        return ("BET_P" if side == "P" else "BET_B"), f"Confirmación {side}{side}. Seguir {side}."

    return "NO_BET", "Sin confirmación clara."

# ===== Canal (estilo AnaPrime) =====
async def safe_delete(bot, chat_id: int, msg_id: int):
    try:
        await bot.delete_message(chat_id=chat_id, message_id=msg_id)
    except (BadRequest, TimedOut, NetworkError, Forbidden):
        return

async def send_possible_entry(app: Application, channel_id: int) -> Optional[int]:
    text = (
        "🚨<b>ATENCIÓN POSIBLE ENTRADA</b>🚨\n"
        "🎰 <b>Juego:</b> <b>Bac Bo - Evolution</b>\n"
    )
    try:
        m: Message = await app.bot.send_message(chat_id=channel_id, text=text, parse_mode=ParseMode.HTML)
        return m.message_id
    except Exception as e:
        logging.exception("No pude enviar POSIBLE ENTRADA: %s", e)
        return None

async def send_confirmed_entry(app: Application, channel_id: int, side: str) -> Optional[int]:
    ingresar = "🔴" if side == "P" else "🔵"
    apostar = "🔵" if side == "P" else "🔴"

    text = (
        "✅ <b>ENTRADA CONFIRMADA</b> ✅\n\n"
        "🎰 <b>Juego:</b> <b>Bac Bo - Evolution</b>\n"
        f"🕒 <b>INGRESAR DESPUÉS:</b> {ingresar}\n"
        f"🔥 <b>APUESTA EN:</b> {apostar}  {side_label(side)}\n\n"
        "🔒 <b>PROTEGER EMPATE</b> con 10% (Opcional)\n"
        f"🔁 <b>MÁXIMO {MAX_GALE} GALE</b>\n"
    )
    try:
        m: Message = await app.bot.send_message(chat_id=channel_id, text=text, parse_mode=ParseMode.HTML)
        return m.message_id
    except Exception as e:
        logging.exception("No pude enviar ENTRADA CONFIRMADA: %s", e)
        return None

async def send_result_reply(app: Application, channel_id: int, reply_to_id: Optional[int], outcome_kind: str, actual_result: str):
    ball = result_ball(actual_result)

    if outcome_kind == "GREEN":
        txt = f"🍀🍀🍀 <b>GREEN!!!</b> 🍀🍀🍀\n\n✅ <b>RESULTADO:</b> {ball}"
        photo_id = os.getenv("GREEN_PHOTO_ID")
    elif outcome_kind == "RED":
        txt = f"❌❌❌ <b>RED!!!</b> ❌❌❌\n\n✅ <b>RESULTADO:</b> {ball}"
        photo_id = os.getenv("RED_PHOTO_ID")
    else:
        txt = f"🟠 <b>TIE</b> 🟠\n\n✅ <b>RESULTADO:</b> {ball}"
        photo_id = os.getenv("TIE_PHOTO_ID")

    try:
        await app.bot.send_message(
            chat_id=channel_id,
            text=txt,
            parse_mode=ParseMode.HTML,
            reply_to_message_id=reply_to_id if reply_to_id else None,
            allow_sending_without_reply=True
        )
    except Exception as e:
        logging.exception("No pude enviar resultado al canal: %s", e)

    if photo_id:
        try:
            await app.bot.send_photo(
                chat_id=channel_id,
                photo=photo_id,
                reply_to_message_id=reply_to_id if reply_to_id else None,
                allow_sending_without_reply=True
            )
        except Exception as e:
            logging.warning("No pude enviar foto GREEN/RED/TIE (opcional): %s", e)

async def schedule_entry_flow(app: Application, channel_id: int):
    possible_id = await send_possible_entry(app, channel_id)
    if possible_id:
        set_session(last_alert_msg_id=possible_id)

    await asyncio.sleep(ALERT_DELAY_SECONDS)

    seq = get_last_results(300)
    sess = get_session()
    action, _detail = decide_action(seq, sess)

    if action == "NO_BET" or sess.awaiting_outcome:
        if possible_id:
            await safe_delete(app.bot, channel_id, possible_id)
        set_session(last_alert_msg_id=None)
        return

    side = "P" if action == "BET_P" else "B"

    if possible_id:
        await safe_delete(app.bot, channel_id, possible_id)
    set_session(last_alert_msg_id=None)

    confirm_id = await send_confirmed_entry(app, channel_id, side)
    if confirm_id:
        set_session(last_confirm_msg_id=confirm_id)

    sess2 = get_session()
    if sess2.is_active:
        bet = calc_next_bet(sess2)
        set_session(pending_side=side, pending_bet=bet, awaiting_outcome=1, last_reco=side)

# ====== UI ======
async def safe_edit(q, text: str, reply_markup=None):
    try:
        await q.edit_message_text(text, reply_markup=reply_markup)
    except BadRequest as e:
        if "Message is not modified" in str(e):
            return
        raise
    except (TimedOut, NetworkError):
        return

def home_menu(sess: SessionState):
    kb = [
        [InlineKeyboardButton("➕ Registrar resultado", callback_data="menu_add")],
        [InlineKeyboardButton("🧠 Recomendación ahora", callback_data="menu_reco")],
        [InlineKeyboardButton("📊 Estadísticas", callback_data="menu_stats")],
    ]
    if sess.is_active:
        kb.append([InlineKeyboardButton("🏦 Sesión: Cerrar", callback_data="menu_session_stop")])
    else:
        kb.append([InlineKeyboardButton("🏦 Sesión: Iniciar", callback_data="menu_session_start")])

    kb.append([InlineKeyboardButton("🧹 Reset historial", callback_data="menu_reset_confirm")])
    return InlineKeyboardMarkup(kb)

def result_menu():
    kb = [
        [InlineKeyboardButton("🔵 PLAYER (P)", callback_data="add_P")],
        [InlineKeyboardButton("🔴 BANKER (B)", callback_data="add_B")],
        [InlineKeyboardButton("🟠 TIE (T)", callback_data="add_T")],
        [InlineKeyboardButton("⬅️ Menú", callback_data="back_home")],
    ]
    return InlineKeyboardMarkup(kb)

def stats_text(seq: List[str], sess: SessionState) -> str:
    total = len(seq)
    if total == 0:
        base = f"\n🏦 Sesión activa | Banca: {sess.bank_current:.0f}" if sess.is_active else ""
        return "Aún no hay rondas registradas." + base

    p = seq.count("P")
    b = seq.count("B")
    t = seq.count("T")

    win = seq[-WINDOW_N:] if len(seq) >= WINDOW_N else seq[:]
    txt = (
        f"📊 Estadísticas (total {total})\n"
        f"🔵 Player: {p} ({p/total*100:.1f}%)\n"
        f"🔴 Banker: {b} ({b/total*100:.1f}%)\n"
        f"🟠 Tie: {t} ({t/total*100:.1f}%)\n"
        f"🧠 Ventana {len(win)}: ChopRate {chop_rate(win):.2f} | Ties {count_ties(win)}/{len(win)}\n"
    )
    if sess.is_active:
        txt += (
            f"\n🏦 Sesión activa | Banca: {sess.bank_current:.0f} | Base: {sess.base_bet:.0f} | Gale: {sess.gale_level}/{MAX_GALE}\n"
            f"🎯 {session_limits_text(sess)}\n"
            f"🧯 Anti-tilt cooldown: {sess.danger_cooldown}"
        )
    return txt

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sess = get_session()
    await update.message.reply_text(
        "🤖 Bac Bo Bot\n✅ Tú registras resultados y el bot publica señales en tu canal.\n",
        reply_markup=home_menu(sess),
    )

async def handle_reco(context: ContextTypes.DEFAULT_TYPE) -> str:
    seq = get_last_results(300)
    sess = get_session()

    action, detail = decide_action(seq, sess)

    if action == "NO_BET":
        return f"🚫 NO APOSTAR\n🧠 {detail}"

    side = "P" if action == "BET_P" else "B"

    if sess.is_active and not sess.awaiting_outcome:
        channel_id = int(os.getenv("CHANNEL_ID", "0"))
        if channel_id:
            context.application.create_task(schedule_entry_flow(context.application, channel_id))

    return f"✅ POSIBLE APOSTAR\n➡️ Recomendación: {side_label(side)}\n🧠 {detail}"

async def on_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    try:
        await q.answer()
    except (TimedOut, NetworkError):
        return

    sess = get_session()
    data = q.data

    if data == "menu_add":
        await safe_edit(q, "Selecciona el resultado real de la ronda:", reply_markup=result_menu())
        return

    if data.startswith("add_"):
        result = data.split("_", 1)[1]
        add_round(result)

        sess_before = get_session()
        sess_after, outcome_txt, outcome_kind = settle_pending_bet(sess_before, result)

        channel_id = int(os.getenv("CHANNEL_ID", "0"))
        if channel_id and outcome_kind:
            reply_to = sess_before.last_confirm_msg_id
            context.application.create_task(
                send_result_reply(context.application, channel_id, reply_to, outcome_kind, result)
            )

        reco_txt = await handle_reco(context)
        await safe_edit(q, f"✅ Guardado: {result_ball(result)} ({result})\n\n{outcome_txt}\n\n{reco_txt}",
                        reply_markup=home_menu(get_session()))
        return

    if data == "menu_reco":
        txt = await handle_reco(context)
        await safe_edit(q, txt, reply_markup=home_menu(get_session()))
        return

    if data == "menu_stats":
        seq = get_last_results(800)
        sess = get_session()
        await safe_edit(q, stats_text(seq, sess), reply_markup=home_menu(sess))
        return

    if data == "menu_session_start":
        await safe_edit(q, "Para iniciar sesión envía: /bank <monto>\nEj: /bank 300000",
                        reply_markup=home_menu(sess))
        return

    if data == "menu_session_stop":
        stop_session()
        await safe_edit(q, "✅ Sesión cerrada.\nMenú:", reply_markup=home_menu(get_session()))
        return

    if data == "menu_reset_confirm":
        kb = [
            [InlineKeyboardButton("⚠️ Sí, borrar historial", callback_data="menu_reset_yes")],
            [InlineKeyboardButton("No", callback_data="back_home")],
        ]
        await safe_edit(q, "¿Seguro que quieres borrar el historial?", reply_markup=InlineKeyboardMarkup(kb))
        return

    if data == "menu_reset_yes":
        clear_rounds()
        await safe_edit(q, "🧹 Historial borrado.\nMenú:", reply_markup=home_menu(get_session()))
        return

    if data == "back_home":
        await safe_edit(q, "Menú:", reply_markup=home_menu(get_session()))
        return

async def bank_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Uso: /bank <monto>\nEj: /bank 300000")
        return
    try:
        bank = float(context.args[0])
        if bank <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Monto inválido. Ej: /bank 300000")
        return

    start_session(bank)
    sess = get_session()
    await update.message.reply_text(
        f"🏦 Sesión iniciada.\nBanca: {sess.bank_current:.0f}\n"
        f"Riesgo: {BASE_BET_PCT:.1f}%\n"
        f"Apuesta base: {sess.base_bet:.0f}\n"
        f"Max gale: {MAX_GALE}\n"
        f"🎯 {session_limits_text(sess)}",
        reply_markup=home_menu(sess),
    )

# ====== WEB (Render ping) ======
flask_app = Flask(__name__)

@flask_app.get("/")
def home():
    return "OK - Bacbo bot activo ✅", 200

# ====== Arranque correcto (SIN asyncio.run + run_polling) ======
def start_bot_sync():
    init_db()

    token = os.getenv("BOT_TOKEN")
    if not token:
        raise RuntimeError("Falta BOT_TOKEN en Render (Environment Variables).")

    request = HTTPXRequest(
        connect_timeout=20.0,
        read_timeout=20.0,
        write_timeout=20.0,
        pool_timeout=20.0,
    )

    app = Application.builder().token(token).request(request).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("bank", bank_cmd))
    app.add_handler(CallbackQueryHandler(on_click))
    app.add_error_handler(error_handler)

    logging.info("✅ Bot iniciado (polling)...")
    app.run_polling(drop_pending_updates=True)

def main():
    port = int(os.getenv("PORT", "10000"))

    from threading import Thread

    def run_web():
        flask_app.run(host="0.0.0.0", port=port)

    Thread(target=run_web, daemon=True).start()

    # Telegram EN MAIN THREAD
    start_bot_sync()

if __name__ == "__main__":
    main()