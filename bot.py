import os
import sqlite3
import logging
import time
import threading
import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional, Tuple

from flask import Flask, request

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.error import BadRequest, TimedOut, NetworkError
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ======================
# CONFIG
# ======================
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("bacbo")

DB_PATH = "bacbo.db"

# Lógica / estrategia (conservador)
WINDOW_SHORT = 12
WINDOW_LONG = 30
MIN_PB_FOR_STATS = 10
TIE_AVOID_THRESHOLD = 2

STOP_LOSS_PCT = 10
TAKE_PROFIT_PCT = 5
BASE_BET_PCT = 2.0

ALLOWED_BETS = [5000, 10000, 25000, 125000, 500000, 2500000]

# Dashboard
INACTIVITY_SECONDS = 30 * 60
ROADMAP_ROWS = 6
ROADMAP_MAX_COLS_TO_RENDER = 60

# Señales por score (AnaPrime-like)
POSSIBLE_SCORE = 60
CONFIRMED_SCORE = 75
CONFIRM_SAME_SIDE_REQUIRED = True

MAX_GALE = 2  # máximo 2 gales

# ======================
# ENV
# ======================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHANNEL_ID = os.getenv("CHANNEL_ID", "").strip()
PUBLIC_URL = os.getenv("PUBLIC_URL", "").strip().rstrip("/")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "secret").strip()

if not BOT_TOKEN:
    raise RuntimeError("Falta BOT_TOKEN en Environment Variables (Render).")
if not CHANNEL_ID:
    raise RuntimeError("Falta CHANNEL_ID (tu -100...) en Environment Variables (Render).")
if not PUBLIC_URL:
    raise RuntimeError("Falta PUBLIC_URL (https://tu-servicio.onrender.com) en Environment Variables (Render).")

WEBHOOK_PATH = f"/webhook/{WEBHOOK_SECRET}"
WEBHOOK_URL = f"{PUBLIC_URL}{WEBHOOK_PATH}"


# ======================
# DB
# ======================
def db() -> sqlite3.Connection:
    return sqlite3.connect(DB_PATH, check_same_thread=False)

def init_db():
    con = db()
    cur = con.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS rounds (
        user_id INTEGER NOT NULL,
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT NOT NULL,
        result TEXT NOT NULL CHECK(result IN ('P','B','T'))
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS session (
        user_id INTEGER PRIMARY KEY,
        is_active INTEGER NOT NULL DEFAULT 0,
        bank_start REAL NOT NULL DEFAULT 0,
        bank_current REAL NOT NULL DEFAULT 0,
        base_bet REAL NOT NULL DEFAULT 0,
        gale_level INTEGER NOT NULL DEFAULT 0,

        pending_side TEXT DEFAULT NULL,
        pending_bet REAL NOT NULL DEFAULT 0,
        awaiting_outcome INTEGER NOT NULL DEFAULT 0,
        danger_cooldown INTEGER NOT NULL DEFAULT 0,

        awaiting_bank INTEGER NOT NULL DEFAULT 0,

        dashboard_chat_id INTEGER DEFAULT NULL,
        dashboard_msg_id INTEGER DEFAULT NULL,

        possible_msg_id INTEGER DEFAULT NULL,
        confirmed_msg_id INTEGER DEFAULT NULL,

        last_candidate_side TEXT DEFAULT NULL,
        candidate_score INTEGER NOT NULL DEFAULT 0,
        candidate_ts INTEGER NOT NULL DEFAULT 0,

        last_activity_ts INTEGER NOT NULL DEFAULT 0
    )
    """)

    # MIGRACIÓN columnas
    cur.execute("PRAGMA table_info(session)")
    cols = {r[1] for r in cur.fetchall()}

    def add_col(name: str, ddl: str):
        if name not in cols:
            cur.execute(ddl)

    add_col("last_candidate_side", "ALTER TABLE session ADD COLUMN last_candidate_side TEXT DEFAULT NULL")
    add_col("candidate_score", "ALTER TABLE session ADD COLUMN candidate_score INTEGER NOT NULL DEFAULT 0")
    add_col("candidate_ts", "ALTER TABLE session ADD COLUMN candidate_ts INTEGER NOT NULL DEFAULT 0")

    con.commit()
    con.close()


@dataclass
class SessionState:
    user_id: int
    is_active: bool
    bank_start: float
    bank_current: float
    base_bet: float
    gale_level: int

    pending_side: Optional[str]
    pending_bet: float
    awaiting_outcome: bool
    danger_cooldown: int

    awaiting_bank: bool

    dashboard_chat_id: Optional[int]
    dashboard_msg_id: Optional[int]

    possible_msg_id: Optional[int]
    confirmed_msg_id: Optional[int]

    last_candidate_side: Optional[str]
    candidate_score: int
    candidate_ts: int

    last_activity_ts: int


def get_session(user_id: int) -> SessionState:
    con = db()
    cur = con.cursor()
    cur.execute("SELECT * FROM session WHERE user_id=?", (user_id,))
    row = cur.fetchone()
    if not row:
        cur.execute("INSERT INTO session (user_id, last_activity_ts) VALUES (?,?)", (user_id, int(time.time())))
        con.commit()
        cur.execute("SELECT * FROM session WHERE user_id=?", (user_id,))
        row = cur.fetchone()
    con.close()

    return SessionState(
        user_id=row[0],
        is_active=bool(row[1]),
        bank_start=float(row[2]),
        bank_current=float(row[3]),
        base_bet=float(row[4]),
        gale_level=int(row[5]),

        pending_side=row[6],
        pending_bet=float(row[7]),
        awaiting_outcome=bool(row[8]),
        danger_cooldown=int(row[9]),

        awaiting_bank=bool(row[10]),

        dashboard_chat_id=row[11],
        dashboard_msg_id=row[12],

        possible_msg_id=row[13],
        confirmed_msg_id=row[14],

        last_candidate_side=row[15],
        candidate_score=int(row[16]),
        candidate_ts=int(row[17]),

        last_activity_ts=int(row[18]),
    )


def set_session(user_id: int, **kwargs):
    if not kwargs:
        return
    keys, vals = [], []
    for k, v in kwargs.items():
        keys.append(f"{k}=?")
        vals.append(v)
    vals.append(user_id)

    con = db()
    cur = con.cursor()
    cur.execute(f"UPDATE session SET {', '.join(keys)} WHERE user_id=?", vals)
    con.commit()
    con.close()


def add_round(user_id: int, result: str):
    con = db()
    cur = con.cursor()
    cur.execute(
        "INSERT INTO rounds (user_id, ts, result) VALUES (?,?,?)",
        (user_id, datetime.now(timezone.utc).isoformat(), result),
    )
    con.commit()
    con.close()


def get_last_results(user_id: int, n: int) -> List[str]:
    con = db()
    cur = con.cursor()
    cur.execute("SELECT result FROM rounds WHERE user_id=? ORDER BY id DESC LIMIT ?", (user_id, n))
    rows = [r[0] for r in cur.fetchall()]
    con.close()
    return list(reversed(rows))


def clear_rounds(user_id: int):
    con = db()
    cur = con.cursor()
    cur.execute("DELETE FROM rounds WHERE user_id=?", (user_id,))
    con.commit()
    con.close()


# ======================
# UTILS
# ======================
def side_to_ball(side: str) -> str:
    if side == "P":
        return "🔵"
    if side == "B":
        return "🔴"
    return "🟠"

def opposite_side(side: str) -> str:
    return "B" if side == "P" else "P"

def round_to_allowed(amount: float) -> float:
    for v in ALLOWED_BETS:
        if amount <= v:
            return float(v)
    return float(ALLOWED_BETS[-1])


def start_session(user_id: int, bank: float):
    raw = bank * (BASE_BET_PCT / 100.0)
    base = round_to_allowed(max(ALLOWED_BETS[0], raw))
    set_session(
        user_id,
        is_active=1,
        bank_start=bank,
        bank_current=bank,
        base_bet=base,
        gale_level=0,
        pending_side=None,
        pending_bet=0,
        awaiting_outcome=0,
        danger_cooldown=0,
        awaiting_bank=0,
        possible_msg_id=None,
        confirmed_msg_id=None,
        last_candidate_side=None,
        candidate_score=0,
        candidate_ts=0,
    )


def stop_session(user_id: int):
    set_session(
        user_id,
        is_active=0,
        gale_level=0,
        pending_side=None,
        pending_bet=0,
        awaiting_outcome=0,
        danger_cooldown=0,
        awaiting_bank=0,
        possible_msg_id=None,
        confirmed_msg_id=None,
        last_candidate_side=None,
        candidate_score=0,
        candidate_ts=0,
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


def session_limits(sess: SessionState) -> Tuple[float, float]:
    sl = sess.bank_start * (1 - STOP_LOSS_PCT / 100.0)
    tp = sess.bank_start * (1 + TAKE_PROFIT_PCT / 100.0)
    return sl, tp


def check_stop_take(sess: SessionState) -> Optional[str]:
    if not sess.is_active or sess.bank_start <= 0:
        return None
    sl, tp = session_limits(sess)
    if sess.bank_current <= sl:
        return "STOP_LOSS"
    if sess.bank_current >= tp:
        return "TAKE_PROFIT"
    return None


# ======================
# STRATEGY
# ======================
def chop_rate(seq: List[str]) -> float:
    filtered = [x for x in seq if x in ("P", "B")]
    if len(filtered) < 8:
        return 0.0
    changes = sum(1 for i in range(1, len(filtered)) if filtered[i] != filtered[i - 1])
    return changes / (len(filtered) - 1)


def current_streak(seq: List[str]) -> Tuple[Optional[str], int]:
    if not seq:
        return None, 0

    last = seq[-1]
    if last == "T":
        return None, 0

    k = 1
    for i in range(len(seq) - 2, -1, -1):
        if seq[i] == "T":
            break  # ✅ el TIE corta la racha
        if seq[i] == last:
            k += 1
        else:
            break

    return last, k


def count_ties(seq: List[str]) -> int:
    return sum(1 for x in seq if x == "T")


def is_danger_table(seq: List[str]) -> Tuple[bool, str]:
    if len(seq) < 12:
        return False, ""

    win = seq[-WINDOW_LONG:] if len(seq) >= WINDOW_LONG else seq[:]
    ties = count_ties(win)
    tie_ratio = ties / len(win) if win else 0.0
    cr = chop_rate(win)
    _, streak_len = current_streak(win)

    if len(win) >= 20 and tie_ratio >= 0.20:
        return True, f"TIE ratio alto ({tie_ratio:.0%})."
    if ties >= 4:
        return True, f"Muchos TIE ({ties}/{len(win)})."
    if cr >= 0.75 and streak_len <= 2:
        return True, f"Chop extremo (ChopRate {cr:.2f}) sin racha."
    if 0.45 <= cr <= 0.55 and streak_len <= 2 and ties >= 2:
        return True, f"Mesa ruidosa (ChopRate {cr:.2f}) con TIE frecuentes."
    return False, ""


def compute_signal_score(seq: List[str], sess: SessionState) -> Tuple[int, Optional[str], str]:
    if len(seq) < 12:
        return 0, None, "Pocas rondas (min 12)."
    if seq[-1] == "T":
        return 0, None, "Último fue TIE."

    win_s = seq[-WINDOW_SHORT:] if len(seq) >= WINDOW_SHORT else seq[:]
    win_l = seq[-WINDOW_LONG:] if len(seq) >= WINDOW_LONG else seq[:]

    pb_l = [x for x in win_l if x in ("P", "B")]
    if len(pb_l) < MIN_PB_FOR_STATS:
        return 0, None, "Pocos P/B para lectura sólida."

    danger, why = is_danger_table(seq)
    if danger:
        # ✅ ENCIENDE anti-tilt si aún no estaba activo
        if sess.danger_cooldown == 0:
            set_session(sess.user_id, danger_cooldown=2)
        return 0, None, f"Mesa peligrosa: {why}"

    ties_s = count_ties(win_s)
    ties_l = count_ties(win_l)
    cr_s = chop_rate(win_s)
    cr_l = chop_rate(win_l)
    streak_side, streak_len = current_streak(seq)  # 👈 CAMBIO IMPORTANTE

    # 🔒 FILTRO ANTI-RACHA LARGA (GLOBAL)
    if streak_len >= 6:
        return 0, None, "Racha extendida (riesgo de ruptura)."
    score = 0
    reasons = []

    if ties_s >= 2:
        score -= 25
        reasons.append("ties corto")
    if ties_l >= 4:
        score -= 20
        reasons.append("ties largo")

    if 0.45 <= cr_s <= 0.55 and 0.45 <= cr_l <= 0.55 and streak_len < 4:
        score -= 35
        reasons.append("chop medio")

    if (cr_s >= 0.70 and 0.40 <= cr_l <= 0.60) or (0.40 <= cr_s <= 0.60 and cr_l >= 0.70):
        score -= 25
        reasons.append("ventanas no concuerdan")

    side = None

    if streak_side in ("P", "B") and streak_len >= 3 and cr_l < 0.65:
        side = streak_side
        score += min(85 + (streak_len - 3) * 2, 95)
        reasons.append(f"racha {streak_side}x{streak_len}")

    if side is None and cr_s >= 0.70 and cr_l >= 0.65:
        last_pb = next((x for x in reversed(win_s) if x in ("P", "B")), None)
        if last_pb in ("P", "B"):
            side = "B" if last_pb == "P" else "P"
            score += 78
            reasons.append(f"chop {cr_s:.2f}/{cr_l:.2f} contraria")

    if side is None:
        filtered = [x for x in seq if x in ("P", "B")]
        if len(filtered) >= 2 and filtered[-1] == filtered[-2] and ties_s == 0 and not (0.40 <= cr_s <= 0.60):
            side = filtered[-1]
            score += 65
            reasons.append(f"doble {side}{side}")

    if side is None:
        score += 5
        reasons.append("sin señal fuerte")

    score = max(0, min(100, score))
    return score, side, ", ".join(reasons)


def decide_with_score(seq: List[str], sess: SessionState) -> Tuple[str, Optional[str], int, str]:
    if sess.danger_cooldown > 0:
        return "NONE", None, 0, f"ANTI-TILT activo: {sess.danger_cooldown} ronda(s)."

    score, side, detail = compute_signal_score(seq, sess)

    if side is None or score < POSSIBLE_SCORE:
        return "NONE", None, score, detail

    if score >= CONFIRMED_SCORE:
        return "CONFIRMED", side, score, detail

    return "POSSIBLE", side, score, detail


# ======================
# ROADMAP 6xN
# ======================
def render_roadmap_6xn(seq: List[str]) -> str:
    if not seq:
        return ""

    max_items = ROADMAP_ROWS * ROADMAP_MAX_COLS_TO_RENDER
    trimmed = seq[-max_items:]
    truncated = len(seq) > len(trimmed)

    cols = (len(trimmed) + ROADMAP_ROWS - 1) // ROADMAP_ROWS
    grid = [["  " for _ in range(cols)] for _ in range(ROADMAP_ROWS)]

    for i, r in enumerate(trimmed):
        row = i % ROADMAP_ROWS
        col = i // ROADMAP_ROWS
        grid[row][col] = side_to_ball(r)

    lines = []
    for row in range(ROADMAP_ROWS):
        lines.append(" ".join(grid[row]).rstrip())

    header = "🧾 <b>ROADMAP (6xN)</b>"
    if truncated:
        header += f"\n<i>Mostrando últimos {len(trimmed)} resultados (recortado).</i>"
    return header + "\n" + "\n".join(lines)


# ======================
# DASHBOARD UI
# ======================
def dashboard_keyboard() -> InlineKeyboardMarkup:
    kb = [
        [InlineKeyboardButton("➕ REGISTRAR RESULTADO", callback_data="dash_add")],
        [InlineKeyboardButton("🏦 INICIAR APUESTA", callback_data="dash_bank")],
        [InlineKeyboardButton("🧹 ELIMINAR HISTORIAL", callback_data="dash_reset")],
    ]
    return InlineKeyboardMarkup(kb)


def result_keyboard() -> InlineKeyboardMarkup:
    kb = [
        [InlineKeyboardButton("🔵 PLAYER", callback_data="add_P")],
        [InlineKeyboardButton("🔴 BANKER", callback_data="add_B")],
        [InlineKeyboardButton("🟠 TIE", callback_data="add_T")],
        [InlineKeyboardButton("⬅️ VOLVER", callback_data="dash_back")],
    ]
    return InlineKeyboardMarkup(kb)


def stats_block(seq: List[str]) -> str:
    total = len(seq)
    if total == 0:
        return "📊 <b>ESTADÍSTICAS</b>\n<i>Sin datos (historial vacío).</i>"

    p = seq.count("P")
    b = seq.count("B")
    t = seq.count("T")

    return (
        "📊 <b>ESTADÍSTICAS</b>\n"
        f"🔵 PLAYER: {p} ({p/total*100:.1f}%)\n"
        f"🔴 BANKER: {b} ({b/total*100:.1f}%)\n"
        f"🟠 TIE: {t} ({t/total*100:.1f}%)"
    )


def reco_block(seq: List[str], sess: SessionState) -> str:
    state, side, score, detail = decide_with_score(seq, sess)

    # ✅ Mantener dashboard 1:1 con la lógica del canal
    # (No mostramos CONFIRMADA si todavía no existe un mensaje POSIBLE previo)
    if state == "CONFIRMED" and sess.possible_msg_id is None:
        state = "POSSIBLE"

    if state == "NONE" or side is None:
        return "🧠 <b>RECOMENDACIONES</b>\n• <b>NO BET</b>\n• " + detail

    return (
        "🧠 <b>RECOMENDACIONES</b>\n"
        f"• <b>{'CONFIRMADA' if state=='CONFIRMED' else 'POSIBLE'}</b>\n"
        f"• Próxima: {side_to_ball(side)}\n"
        f"• Score: <b>{score}</b>\n"
        f"• {detail}"
    )


def bet_block(sess: SessionState) -> str:
    if not sess.is_active:
        return "🏦 <b>APUESTA</b>\n• <i>No iniciada</i>"

    sl, tp = session_limits(sess)
    return (
        "🏦 <b>APUESTA</b>\n"
        f"• Banca: <b>{sess.bank_current:.0f}</b>\n"
        f"• Base: <b>{sess.base_bet:.0f}</b>\n"
        f"• Gale: <b>{sess.gale_level}/{MAX_GALE}</b>\n"
        f"• 🎯 SL: <b>{sl:.0f}</b> | TP: <b>{tp:.0f}</b>"
    )


def build_dashboard_text(seq: List[str], sess: SessionState) -> str:
    parts = [
        stats_block(seq),
        "",
        reco_block(seq, sess),
        "",
        bet_block(sess),
        "",
        render_roadmap_6xn(seq),
    ]
    return "\n".join([p for p in parts if p is not None])


async def safe_delete_message(bot, chat_id: int, msg_id: int):
    try:
        await bot.delete_message(chat_id=chat_id, message_id=msg_id)
    except Exception:
        return


# ✅ DASHBOARD ÚNICO
async def ensure_dashboard(update: Optional[Update], context: ContextTypes.DEFAULT_TYPE, user_id: int):
    sess = get_session(user_id)
    seq = get_last_results(user_id, 300)
    text = build_dashboard_text(seq, sess)

    chat_id = None
    if update and update.effective_chat:
        chat_id = update.effective_chat.id
    elif sess.dashboard_chat_id:
        chat_id = sess.dashboard_chat_id

    if not chat_id:
        return

    if sess.dashboard_chat_id == chat_id and sess.dashboard_msg_id:
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=sess.dashboard_msg_id,
                text=text,
                reply_markup=dashboard_keyboard(),
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
            return
        except BadRequest:
            set_session(user_id, dashboard_msg_id=None)

    msg = await context.bot.send_message(
        chat_id=chat_id,
        text=text,
        reply_markup=dashboard_keyboard(),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )
    set_session(user_id, dashboard_chat_id=chat_id, dashboard_msg_id=msg.message_id)


# ======================
# CHANNEL MESSAGES
# ======================
def text_posible_entrada() -> str:
    return (
        "🚨<b>ATENCIÓN POSIBLE ENTRADA</b>🚨\n"
        "🎰<b>Juego:</b> Bac Bo - Evolution"
    )


def text_entrada_confirmada(bet_side: str, last_result: str) -> str:
    ingresar = side_to_ball(last_result)   # <-- usar el último resultado real
    apuesta = side_to_ball(bet_side)

    return (
        "✅ <b>ENTRADA CONFIRMADA</b> ✅\n\n"
        "🎰 <b>Juego:</b> Bac Bo - Evolution\n"
        f"🧨<b>INGRESAR DESPUÉS:</b> {ingresar}\n"
        f"🔥 <b>APUESTA EN:</b> {apuesta}\n\n"
        "🔒 <b>PROTEGER EMPATE</b> con 10% (Opcional)\n\n"
        f"🔁 <b>MÁXIMO {MAX_GALE} GALE</b>"
    )


def text_green(result_side: str) -> str:
    return (
        "🍀🍀🍀 <b>GREEN!!!</b> 🍀🍀🍀\n\n"
        f"✅<b>RESULTADO:</b> {side_to_ball(result_side)}"
    )


def text_red() -> str:
    return (
        "❌ <b>RED</b>\n\n"
        "A veces puede suceder, ¡pero basta con gestionar tu banca!"
    )


def text_tie() -> str:
    return (
        "🟠 <b>EMPATE (TIE)</b>\n\n"
        "✅<b>RESULTADO:</b> 🟠"
    )


async def channel_send(context: ContextTypes.DEFAULT_TYPE, text: str, reply_to: Optional[int] = None) -> int:
    if reply_to:
        try:
            msg = await context.bot.send_message(
                chat_id=int(CHANNEL_ID),
                text=text,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
                reply_to_message_id=int(reply_to),
            )
            return msg.message_id
        except Exception:
            pass

    msg = await context.bot.send_message(
        chat_id=int(CHANNEL_ID),
        text=text,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )
    return msg.message_id


async def channel_delete(context: ContextTypes.DEFAULT_TYPE, msg_id: Optional[int]):
    if not msg_id:
        return
    await safe_delete_message(context.bot, int(CHANNEL_ID), int(msg_id))


# ======================
# BET SETTLEMENT
# ======================
def settle_pending(sess: SessionState, actual_result: str) -> Tuple[SessionState, str]:
    if not (sess.is_active and sess.awaiting_outcome and sess.pending_side in ("P", "B") and sess.pending_bet > 0):
        return sess, "NONE"

    side = sess.pending_side
    bet = float(sess.pending_bet)
    bank = float(sess.bank_current)
    gale = int(sess.gale_level)

    if actual_result == "T":
        outcome = "TIE"
    elif actual_result == side:
        outcome = "WIN"
        bank += bet
        gale = 0
    else:
        outcome = "LOSE"
        bank -= bet
        if gale < MAX_GALE:
            gale += 1
        else:
            gale = 0

    set_session(
        sess.user_id,
        bank_current=bank,
        gale_level=gale,
        pending_side=None,
        pending_bet=0,
        awaiting_outcome=0,
    )
    return get_session(sess.user_id), outcome


# ======================
# INACTIVITY RESET
# ======================
def touch_activity(user_id: int):
    set_session(user_id, last_activity_ts=int(time.time()))

def is_inactive(sess: SessionState) -> bool:
    if not sess.last_activity_ts:
        return False
    return (time.time() - sess.last_activity_ts) > INACTIVITY_SECONDS

def reset_for_inactivity(user_id: int):
    clear_rounds(user_id)
    stop_session(user_id)


# ======================
# HANDLERS
# ======================
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    err = context.error
    if isinstance(err, (TimedOut, NetworkError)):
        log.warning("Network/Timeout (ignorado): %s", err)
        return
    log.exception("Unhandled exception:", exc_info=err)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    touch_activity(user_id)

    try:
        await update.message.delete()
    except Exception:
        pass

    sess = get_session(user_id)
    if is_inactive(sess):
        reset_for_inactivity(user_id)

    await ensure_dashboard(update, context, user_id)


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    touch_activity(user_id)

    sess = get_session(user_id)
    if is_inactive(sess):
        reset_for_inactivity(user_id)
        sess = get_session(user_id)

    text = (update.message.text or "").strip()

    if sess.awaiting_bank:
        if text.lower().startswith("/bank"):
            parts = text.split()
            if len(parts) >= 2:
                text = parts[1].strip()
            else:
                await update.message.reply_text("Envía el monto así: 33500 (o /bank 33500)")
                return

        try:
            bank = float(text)
            if bank <= 0:
                raise ValueError
        except ValueError:
            await update.message.reply_text("Monto inválido. Ej: 33500")
            return

        start_session(user_id, bank)

        try:
            await update.message.delete()
        except Exception:
            pass

        await ensure_dashboard(update, context, user_id)
        return

    try:
        await update.message.delete()
    except Exception:
        pass

    await ensure_dashboard(update, context, user_id)


async def on_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    user_id = q.from_user.id
    touch_activity(user_id)

    try:
        await q.answer()
    except Exception:
        pass

    sess = get_session(user_id)
    if is_inactive(sess):
        reset_for_inactivity(user_id)
        sess = get_session(user_id)

    data = q.data

    if data == "dash_add":
        seq = get_last_results(user_id, 300)
        text = build_dashboard_text(seq, sess)

        # ✅✅✅ BLOQUEO: si no hay banca iniciada, NO permitir registrar resultados
        if not sess.is_active:
            try:
                await q.edit_message_text(
                    text=text + "\n\n⚠️ <b>Primero INICIA APUESTA</b> (pon tu bank) para poder registrar resultados.",
                    reply_markup=dashboard_keyboard(),
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                )
            except BadRequest:
                await ensure_dashboard(update, context, user_id)
            return

        # si sí está activa, abrir teclado de resultados
        try:
            await q.edit_message_text(
                text=text + "\n\n<b>➕ Registrar resultado:</b>",
                reply_markup=result_keyboard(),
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
        except BadRequest:
            await ensure_dashboard(update, context, user_id)
        return

    if data == "dash_bank":
        set_session(user_id, awaiting_bank=1)
        seq = get_last_results(user_id, 300)
        text = build_dashboard_text(seq, sess)
        try:
            await q.edit_message_text(
                text=text + "\n\n<b>🏦 INICIAR APUESTA</b>\nEnvía el monto así:\n<b>33500</b>\n(o <b>/bank 33500</b>)",
                reply_markup=dashboard_keyboard(),
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
        except BadRequest:
            await ensure_dashboard(update, context, user_id)
        return

    if data == "dash_reset":
        clear_rounds(user_id)
        stop_session(user_id)

        sess_now = get_session(user_id)
        await channel_delete(context, sess_now.possible_msg_id)
        set_session(
            user_id,
            possible_msg_id=None,
            confirmed_msg_id=None,
            last_candidate_side=None,
            candidate_score=0,
            candidate_ts=0
        )

        await ensure_dashboard(update, context, user_id)
        return

    if data == "dash_back":
        await ensure_dashboard(update, context, user_id)
        return

    # ============ RESULT ADD ============
    if data.startswith("add_"):
        # ✅✅✅ BLOQUEO extra: si por alguna razón llega aquí sin apuesta iniciada
        sess_check = get_session(user_id)
        if not sess_check.is_active:
            seq = get_last_results(user_id, 300)
            text = build_dashboard_text(seq, sess_check)
            try:
                await q.edit_message_text(
                    text=text + "\n\n⚠️ <b>No puedes registrar resultados</b> sin iniciar la apuesta (bank).",
                    reply_markup=dashboard_keyboard(),
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                )
            except BadRequest:
                await ensure_dashboard(update, context, user_id)
            return

        result = data.split("_", 1)[1]  # P/B/T
        add_round(user_id, result)
        # ✅ bajar cooldown 1 por ronda registrada
sess_cd = get_session(user_id)
if sess_cd.danger_cooldown > 0:
    set_session(user_id, danger_cooldown=max(0, sess_cd.danger_cooldown - 1))
        sess_before = get_session(user_id)
        sess_after, outcome = settle_pending(sess_before, result)

        log.info("OUTCOME=%s pending_side=%s result=%s confirmed_msg_id=%s gale_after=%s",
                 outcome, sess_before.pending_side, result, sess_before.confirmed_msg_id, sess_after.gale_level)

        if outcome in ("WIN", "LOSE", "TIE") and sess_before.confirmed_msg_id:
            if outcome == "WIN":
                await channel_send(context, text_green(result_side=result), reply_to=sess_before.confirmed_msg_id)
            elif outcome == "LOSE":
                await channel_send(context, text_red(), reply_to=sess_before.confirmed_msg_id)
            else:
                await channel_send(context, text_tie(), reply_to=sess_before.confirmed_msg_id)

        if outcome in ("WIN", "TIE"):
            set_session(
                user_id,
                possible_msg_id=None,
                confirmed_msg_id=None,
                last_candidate_side=None,
                candidate_score=0,
                candidate_ts=0
            )

        if outcome == "LOSE":
            sess_now = get_session(user_id)
            if sess_now.is_active and sess_now.gale_level > 0 and sess_now.gale_level <= MAX_GALE:
                gale_side = sess_before.pending_side
                if gale_side in ("P", "B") and sess_before.confirmed_msg_id:
                    gale_text = text_entrada_confirmada(gale_side, last_result=result) + f"\n\n🔁 <b>GALE {sess_now.gale_level}/{MAX_GALE}</b>"
                    gale_confirmed_id = await channel_send(context, gale_text, reply_to=sess_before.confirmed_msg_id)

                    bet_amount = calc_next_bet(sess_now)
                    set_session(
                        user_id,
                        confirmed_msg_id=gale_confirmed_id,
                        pending_side=gale_side,
                        pending_bet=bet_amount,
                        awaiting_outcome=1,
                        possible_msg_id=None,
                        last_candidate_side=None,
                        candidate_score=0,
                        candidate_ts=0
                    )

                    await ensure_dashboard(update, context, user_id)
                    return

            if sess_after.gale_level == 0:
                set_session(
                    user_id,
                    possible_msg_id=None,
                    confirmed_msg_id=None,
                    last_candidate_side=None,
                    candidate_score=0,
                    candidate_ts=0
                )

        stop_hit = check_stop_take(sess_after)
        if stop_hit in ("STOP_LOSS", "TAKE_PROFIT"):
            stop_session(user_id)

        sess_guard = get_session(user_id)
        if sess_guard.awaiting_outcome:
            await ensure_dashboard(update, context, user_id)
            return

        seq = get_last_results(user_id, 300)
        sess = get_session(user_id)
        seq = get_last_results(user_id, 300)

     # ✅ armar anti-tilt SOLO si está apagado
        sess_guard = get_session(user_id)
        if sess_guard.danger_cooldown == 0:
            danger, why = is_danger_table(seq)
            if danger:
                set_session(user_id, danger_cooldown=2)
                # refrescar sess para que el dashboard muestre ANTI-TILT inmediato
                sess_guard = get_session(user_id)

        state, side, score, detail = decide_with_score(seq, sess_guard)
        now_ts = int(time.time())

        if state == "CONFIRMED" and sess.possible_msg_id is None:
            state = "POSSIBLE"

        if state == "NONE" or side is None:
            if sess.possible_msg_id is not None:
                await channel_delete(context, sess.possible_msg_id)
            set_session(user_id, possible_msg_id=None, last_candidate_side=None, candidate_score=0, candidate_ts=0)

        elif state == "POSSIBLE":
            if sess.possible_msg_id is not None and sess.last_candidate_side and side != sess.last_candidate_side:
                await channel_delete(context, sess.possible_msg_id)
                set_session(user_id, possible_msg_id=None)

            sess2 = get_session(user_id)
            if sess2.possible_msg_id is None:
                possible_id = await channel_send(context, text_posible_entrada())
                set_session(user_id, possible_msg_id=possible_id)

            set_session(user_id, last_candidate_side=side, candidate_score=score, candidate_ts=now_ts)

        else:  # CONFIRMED
            can_confirm = True
            if CONFIRM_SAME_SIDE_REQUIRED and sess.last_candidate_side and side != sess.last_candidate_side:
                can_confirm = False

            if can_confirm and sess.possible_msg_id is not None and sess.candidate_score >= POSSIBLE_SCORE:
                await channel_delete(context, sess.possible_msg_id)

                last_result = seq[-1] if seq else side
                confirmed_id = await channel_send(
                    context,
                    text_entrada_confirmada(side, last_result)
                )

                set_session(
                    user_id,
                    possible_msg_id=None,
                    confirmed_msg_id=confirmed_id,
                    last_candidate_side=None,
                    candidate_score=0,
                    candidate_ts=0
                )

                sess3 = get_session(user_id)
                if sess3.is_active:
                    bet = calc_next_bet(sess3)
                    set_session(user_id, pending_side=side, pending_bet=bet, awaiting_outcome=1)
            else:
                if sess.possible_msg_id is None:
                    possible_id = await channel_send(context, text_posible_entrada())
                    set_session(user_id, possible_msg_id=possible_id)

                set_session(user_id, last_candidate_side=side, candidate_score=score, candidate_ts=now_ts)

        await ensure_dashboard(update, context, user_id)
        return


# ======================
# WEBHOOK + FLASK
# ======================
flask_app = Flask(__name__)
tg_app: Optional[Application] = None
tg_loop: Optional[asyncio.AbstractEventLoop] = None


@flask_app.get("/")
def root_home():
    return "BOT ONLINE", 200


@flask_app.get("/health")
def health():
    return "OK", 200


@flask_app.post(WEBHOOK_PATH)
def webhook():
    global tg_app, tg_loop
    if tg_app is None or tg_loop is None:
        return "Bot not ready", 503

    data = request.get_json(force=True, silent=True) or {}
    upd = Update.de_json(data, tg_app.bot)

    fut = asyncio.run_coroutine_threadsafe(tg_app.update_queue.put(upd), tg_loop)
    try:
        fut.result(timeout=2)
    except Exception:
        return "Queue error", 500

    return "OK", 200


async def setup_webhook(application: Application):
    await application.bot.delete_webhook(drop_pending_updates=True)
    ok = await application.bot.set_webhook(url=WEBHOOK_URL, drop_pending_updates=True)
    log.info("Webhook set: %s", ok)


def start_telegram_in_thread():
    global tg_app, tg_loop

    tg_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(tg_loop)

    tg_app = Application.builder().token(BOT_TOKEN).build()

    tg_app.add_error_handler(error_handler)
    tg_app.add_handler(CommandHandler("start", cmd_start))
    tg_app.add_handler(CallbackQueryHandler(on_click))
    tg_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    tg_app.add_handler(MessageHandler(filters.COMMAND, on_text))

    async def runner():
        await tg_app.initialize()
        await setup_webhook(tg_app)
        await tg_app.start()
        log.info("✅ Bot iniciado (WEBHOOK).")

    # ✅ FIX: runner() es corutina
    tg_loop.create_task(runner())
    tg_loop.run_forever()


def main():
    init_db()

    t = threading.Thread(target=start_telegram_in_thread, daemon=True)
    t.start()

    port = int(os.getenv("PORT", "10000"))
    flask_app.run(host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()