import os
import sqlite3
import logging
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional, Tuple

from flask import Flask
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.error import BadRequest, TimedOut, NetworkError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from telegram.request import HTTPXRequest

# =========================
# LOGGING
# =========================
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("bacbo")

# =========================
# ENV
# =========================
DB_PATH = "bacbo.db"
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHANNEL_ID = os.getenv("CHANNEL_ID", "").strip()  # -100xxxx
GREEN_STICKER_ID = os.getenv("GREEN_STICKER_ID", "").strip()
RED_STICKER_ID = os.getenv("RED_STICKER_ID", "").strip()

# =========================
# GAME CONFIG
# =========================
WINDOW_N = 30
MAX_GALE = 2

STOP_LOSS_PCT = 10
TAKE_PROFIT_PCT = 5
BASE_BET_PCT = 2.0
ALLOWED_BETS = [5000, 10000, 25000, 125000, 500000, 2500000]

TIE_AVOID_THRESHOLD = 3
DANGER_COOLDOWN_ROUNDS = 3

# ROADMAP 6xN
ROADMAP_ROWS = 6
ROADMAP_MAX_RESULTS = 240  # limitar para no pasarnos de tamaño de mensaje en Telegram

# POSIBLE ENTRADA delay (segundos)
POSSIBLE_CONFIRM_DELAY = 8

# =========================
# EMOJIS (como en tu ejemplo)
# PLAYER = 🔵, BANKER = 🔴, TIE = 🟠
# =========================
def ball(result: str) -> str:
    if result == "P":
        return "🔵"
    if result == "B":
        return "🔴"
    return "🟠"  # TIE


def side_name(side: str) -> str:
    return "PLAYER" if side == "P" else "BANKER"


def side_ball(side: str) -> str:
    return ball(side)


# =========================
# DB
# =========================
def db():
    return sqlite3.connect(DB_PATH)


def init_db():
    con = db()
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

        -- apuesta
        is_active INTEGER NOT NULL DEFAULT 0,
        bank_start REAL NOT NULL DEFAULT 0,
        bank_current REAL NOT NULL DEFAULT 0,
        base_bet REAL NOT NULL DEFAULT 0,
        gale_level INTEGER NOT NULL DEFAULT 0,

        -- pending bet
        pending_side TEXT DEFAULT NULL,
        pending_bet REAL NOT NULL DEFAULT 0,
        awaiting_outcome INTEGER NOT NULL DEFAULT 0,

        -- anti tilt
        danger_cooldown INTEGER NOT NULL DEFAULT 0,

        -- UI state
        awaiting_bank INTEGER NOT NULL DEFAULT 0,

        -- dashboard message in private chat
        dashboard_chat_id INTEGER DEFAULT NULL,
        dashboard_msg_id INTEGER DEFAULT NULL,
        panel_enabled INTEGER NOT NULL DEFAULT 0,

        -- channel messages tracking
        possible_msg_id INTEGER DEFAULT NULL,
        confirmed_msg_id INTEGER DEFAULT NULL,
        last_possible_side TEXT DEFAULT NULL
    )
    """)
    cur.execute("INSERT OR IGNORE INTO session (id) VALUES (1)")
    con.commit()
    con.close()


def add_round(result: str):
    con = db()
    cur = con.cursor()
    cur.execute(
        "INSERT INTO rounds (ts, result) VALUES (?, ?)",
        (datetime.now(timezone.utc).isoformat(), result),
    )
    con.commit()
    con.close()


def clear_rounds():
    con = db()
    cur = con.cursor()
    cur.execute("DELETE FROM rounds")
    con.commit()
    con.close()


def get_last_results(n: int) -> List[str]:
    con = db()
    cur = con.cursor()
    cur.execute("SELECT result FROM rounds ORDER BY id DESC LIMIT ?", (n,))
    rows = [r[0] for r in cur.fetchall()]
    con.close()
    return list(reversed(rows))


# =========================
# SESSION STATE
# =========================
@dataclass
class SessionState:
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
    panel_enabled: bool

    possible_msg_id: Optional[int]
    confirmed_msg_id: Optional[int]
    last_possible_side: Optional[str]


def get_session() -> SessionState:
    con = db()
    cur = con.cursor()
    cur.execute("""
        SELECT
          is_active, bank_start, bank_current, base_bet, gale_level,
          pending_side, pending_bet, awaiting_outcome,
          danger_cooldown,
          awaiting_bank,
          dashboard_chat_id, dashboard_msg_id, panel_enabled,
          possible_msg_id, confirmed_msg_id, last_possible_side
        FROM session WHERE id=1
    """)
    row = cur.fetchone()
    con.close()

    return SessionState(
        bool(row[0]), float(row[1]), float(row[2]), float(row[3]), int(row[4]),
        row[5], float(row[6]), bool(row[7]),
        int(row[8]),
        bool(row[9]),
        row[10], row[11], bool(row[12]),
        row[13], row[14], row[15]
    )


def set_session(**kwargs):
    if not kwargs:
        return
    con = db()
    cur = con.cursor()
    keys = []
    vals = []
    for k, v in kwargs.items():
        keys.append(f"{k}=?")
        vals.append(v)
    cur.execute(f"UPDATE session SET {', '.join(keys)} WHERE id=1", vals)
    con.commit()
    con.close()


# =========================
# BET / LIMITS
# =========================
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
        panel_enabled=1,
        bank_start=bank,
        bank_current=bank,
        base_bet=base,
        gale_level=0,
        pending_side=None,
        pending_bet=0,
        awaiting_outcome=0,
        danger_cooldown=0,
        awaiting_bank=0
    )


def stop_session():
    set_session(
        is_active=0,
        bank_start=0,
        bank_current=0,
        base_bet=0,
        gale_level=0,
        pending_side=None,
        pending_bet=0,
        awaiting_outcome=0,
        danger_cooldown=0,
        awaiting_bank=0
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


# =========================
# METRICS / DECISION
# =========================
def chop_rate(seq: List[str]) -> float:
    filtered = [x for x in seq if x in ("P", "B")]
    if len(filtered) < 2:
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
        return True, f"Mesa muy ruidosa (ChopRate {cr:.2f}) con TIE frecuentes."
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

    win = seq[-WINDOW_N:] if len(seq) >= WINDOW_N else seq[:]
    ties = count_ties(win)
    cr = chop_rate(win)
    streak_side, streak_len = current_streak(win)
    last = seq[-1]

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


# =========================
# ROADMAP (6xN)
# =========================
def build_roadmap(seq: List[str]) -> str:
    if not seq:
        return ""

    seq = seq[-ROADMAP_MAX_RESULTS:]  # limitar tamaño

    cols = math.ceil(len(seq) / ROADMAP_ROWS)
    grid = [["  " for _ in range(cols)] for _ in range(ROADMAP_ROWS)]

    for i, r in enumerate(seq):
        c = i // ROADMAP_ROWS
        rr = i % ROADMAP_ROWS
        grid[rr][c] = ball(r)

    lines = []
    for rr in range(ROADMAP_ROWS):
        lines.append(" ".join(grid[rr]).rstrip())

    return "\n".join(lines)


# =========================
# DASHBOARD (PRIVATE CHAT)
# =========================
def dashboard_text(seq: List[str], sess: SessionState) -> str:
    # Si historial eliminado y no se ha iniciado apuesta => panel vacío (sin stats/roadmap)
    if not sess.panel_enabled:
        return (
            "📌 <b>DASHBOARD</b>\n"
            "⚠️ <i>Sin apuesta activa.</i>\n"
            "Pulsa <b>🏦 INICIAR APUESTA</b> para comenzar."
        )

    total = len(seq)
    p = seq.count("P")
    b = seq.count("B")
    t = seq.count("T")

    pct = lambda x: (x / total * 100.0) if total else 0.0

    action, detail = decide_action(seq, sess)
    reco = "NO BET" if action == "NO_BET" else ("PLAYER (🔵)" if action == "BET_P" else "BANKER (🔴)")

    roadmap = build_roadmap(seq)

    stats_block = (
        "📊 <b>ESTADÍSTICAS</b>\n"
        f"🔵 PLAYER: <b>{pct(p):.1f}%</b>\n"
        f"🔴 BANKER: <b>{pct(b):.1f}%</b>\n"
        f"🟠 TIE: <b>{pct(t):.1f}%</b>\n"
    )

    reco_block = (
        "\n🧠 <b>RECOMENDACIONES</b>\n"
        f"• <b>{reco}</b>\n"
        f"• {detail}\n"
    )

    bank_block = ""
    if sess.is_active:
        bank_block = (
            "\n🏦 <b>APUESTA</b>\n"
            f"• Banca: <b>{sess.bank_current:.0f}</b>\n"
            f"• Base: <b>{sess.base_bet:.0f}</b>\n"
            f"• Gale: <b>{sess.gale_level}/{MAX_GALE}</b>\n"
            f"• 🎯 {session_limits_text(sess)}\n"
        )

    roadmap_block = "\n🧾 <b>ROADMAP (6xN)</b>\n" + (roadmap if roadmap else "—")

    return stats_block + reco_block + bank_block + roadmap_block


def main_menu_kb() -> InlineKeyboardMarkup:
    kb = [
        [InlineKeyboardButton("➕ REGISTRAR RESULTADO", callback_data="menu_add")],
        [InlineKeyboardButton("🏦 INICIAR APUESTA", callback_data="menu_start_bet")],
        [InlineKeyboardButton("🧹 ELIMINAR HISTORIAL", callback_data="menu_reset")],
    ]
    return InlineKeyboardMarkup(kb)


def result_kb() -> InlineKeyboardMarkup:
    kb = [
        [InlineKeyboardButton("🔵 PLAYER", callback_data="add_P")],
        [InlineKeyboardButton("🔴 BANKER", callback_data="add_B")],
        [InlineKeyboardButton("🟠 TIE", callback_data="add_T")],
        [InlineKeyboardButton("⬅️ VOLVER", callback_data="back_home")],
    ]
    return InlineKeyboardMarkup(kb)


async def safe_edit_message(bot, chat_id: int, msg_id: int, text: str, reply_markup=None):
    try:
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=msg_id,
            text=text,
            parse_mode=ParseMode.HTML,
            reply_markup=reply_markup
        )
    except BadRequest as e:
        if "Message is not modified" in str(e):
            return
        # Si el mensaje ya no existe, lo dejamos pasar
        logger.warning("Edit failed: %s", e)
    except (TimedOut, NetworkError):
        return


async def upsert_dashboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sess = get_session()
    seq = get_last_results(800)
    text = dashboard_text(seq, sess)

    chat_id = update.effective_chat.id

    # si no hay dashboard guardado, lo creamos
    if not sess.dashboard_chat_id or not sess.dashboard_msg_id or sess.dashboard_chat_id != chat_id:
        msg = await context.bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode=ParseMode.HTML,
            reply_markup=main_menu_kb()
        )
        set_session(dashboard_chat_id=chat_id, dashboard_msg_id=msg.message_id)
        return

    # editar dashboard existente
    await safe_edit_message(context.bot, sess.dashboard_chat_id, sess.dashboard_msg_id, text, reply_markup=main_menu_kb())


# =========================
# CHANNEL MESSAGES (AnaPrime style)
# =========================
async def delete_channel_message(context: ContextTypes.DEFAULT_TYPE, msg_id: Optional[int]):
    if not CHANNEL_ID or not msg_id:
        return
    try:
        await context.bot.delete_message(chat_id=int(CHANNEL_ID), message_id=int(msg_id))
    except Exception:
        pass


async def send_possible_entry(context: ContextTypes.DEFAULT_TYPE, side: str):
    if not CHANNEL_ID:
        return

    txt = (
        "🚨<b>ATENCIÓN POSIBLE ENTRADA</b>🚨\n"
        "🎰 <b>Juego:</b> Bac Bo - Evolution"
    )
    msg = await context.bot.send_message(
        chat_id=int(CHANNEL_ID),
        text=txt,
        parse_mode=ParseMode.HTML
    )
    set_session(possible_msg_id=msg.message_id, last_possible_side=side)


async def send_confirmed_entry(context: ContextTypes.DEFAULT_TYPE, last_seen_result: str, bet_side: str):
    """
    ENTRADA CONFIRMADA:
    - INGRESAR DESPUÉS: bola del resultado anterior (lo que "apareció antes")
    - APUESTA EN: bola del cálculo (bet_side)
    """
    if not CHANNEL_ID:
        return

    ingresar_despues_ball = ball(last_seen_result)
    apuesta_ball = side_ball(bet_side)

    txt = (
        "✅ <b>ENTRADA CONFIRMADA</b> ✅\n\n"
        "🎰 <b>Juego:</b> Bac Bo - Evolution\n"
        f"🧨<b>INGRESAR DESPUÉS:</b> {ingresar_despues_ball}\n"
        f"🔥 <b>APUESTA EN:</b> {apuesta_ball}\n\n"
        "🔒 <b>PROTEGER EMPATE</b> con 10% (Opcional)\n\n"
        f"🔁 <b>MÁXIMO {MAX_GALE} GALE</b>"
    )

    msg = await context.bot.send_message(
        chat_id=int(CHANNEL_ID),
        text=txt,
        parse_mode=ParseMode.HTML
    )
    set_session(confirmed_msg_id=msg.message_id)


async def reply_result_to_confirmed(context: ContextTypes.DEFAULT_TYPE, kind: str, actual_result: str):
    """
    kind: GREEN / RED / TIE
    Responde al mensaje de ENTRADA CONFIRMADA.
    """
    if not CHANNEL_ID:
        return

    sess = get_session()
    reply_to = sess.confirmed_msg_id
    if not reply_to:
        return

    res_ball = ball(actual_result)

    if kind == "GREEN":
        txt = (
            "🍀🍀🍀 <b>GREEN!!!</b> 🍀🍀🍀\n\n"
            f"✅<b>RESULTADO:</b> {res_ball}"
        )
        await context.bot.send_message(
            chat_id=int(CHANNEL_ID),
            text=txt,
            parse_mode=ParseMode.HTML,
            reply_to_message_id=int(reply_to)
        )
        if GREEN_STICKER_ID:
            try:
                await context.bot.send_sticker(chat_id=int(CHANNEL_ID), sticker=GREEN_STICKER_ID)
            except Exception:
                pass

    elif kind == "RED":
        txt = (
            "❌ <b>RED</b>\n\n"
            "A veces puede suceder, ¡pero basta con gestionar tu banca!"
        )
        await context.bot.send_message(
            chat_id=int(CHANNEL_ID),
            text=txt,
            parse_mode=ParseMode.HTML,
            reply_to_message_id=int(reply_to)
        )
        if RED_STICKER_ID:
            try:
                await context.bot.send_sticker(chat_id=int(CHANNEL_ID), sticker=RED_STICKER_ID)
            except Exception:
                pass

    else:  # TIE
        txt = (
            "🟠 <b>EMPATE</b>\n\n"
            f"✅<b>RESULTADO:</b> {res_ball}"
        )
        await context.bot.send_message(
            chat_id=int(CHANNEL_ID),
            text=txt,
            parse_mode=ParseMode.HTML,
            reply_to_message_id=int(reply_to)
        )


# =========================
# BET SETTLEMENT
# =========================
def settle_pending_bet(sess: SessionState, actual_result: str) -> Tuple[SessionState, str]:
    """
    Actualiza banca y gale basado en pending_side/pending_bet.
    Retorna (sess_updated, outcome_kind)
    outcome_kind: GREEN / RED / TIE / ""
    """
    if not (sess.is_active and sess.awaiting_outcome and sess.pending_side in ("P", "B") and sess.pending_bet > 0):
        return sess, ""

    side = sess.pending_side
    bet = float(sess.pending_bet)
    bank = float(sess.bank_current)
    gale = int(sess.gale_level)

    if actual_result == "T":
        # Push
        outcome_kind = "TIE"
        outcome_txt = "TIE"
        # banca no cambia, gale no cambia (podrías resetear si quisieras)
    elif actual_result == side:
        bank += bet
        gale = 0
        outcome_kind = "GREEN"
        outcome_txt = "WIN"
    else:
        bank -= bet
        if gale < MAX_GALE:
            gale += 1
        else:
            gale = 0
        outcome_kind = "RED"
        outcome_txt = "LOSE"

    set_session(
        bank_current=bank,
        gale_level=gale,
        pending_side=None,
        pending_bet=0,
        awaiting_outcome=0
    )

    return get_session(), outcome_kind


# =========================
# POSSIBLE ENTRY FLOW
# - se manda POSIBLE ENTRADA
# - luego de X segundos re-evalúa:
#   - si se mantiene => borra posible, manda confirmada
#   - si cambió/no bet => borra posible y listo
# =========================
async def possible_flow_job(context: ContextTypes.DEFAULT_TYPE):
    sess = get_session()
    seq = get_last_results(300)

    # si no hay resultados suficientes, borrar posible y listo
    if len(seq) < 10:
        await delete_channel_message(context, sess.possible_msg_id)
        set_session(possible_msg_id=None, last_possible_side=None)
        return

    action, _ = decide_action(seq, sess)
    if action not in ("BET_P", "BET_B"):
        # cambió => borrar posible
        await delete_channel_message(context, sess.possible_msg_id)
        set_session(possible_msg_id=None, last_possible_side=None)
        return

    side = "P" if action == "BET_P" else "B"

    # si cambió de lado
    if sess.last_possible_side and side != sess.last_possible_side:
        await delete_channel_message(context, sess.possible_msg_id)
        set_session(possible_msg_id=None, last_possible_side=None)
        return

    # confirmar
    last_seen = seq[-1]
    await delete_channel_message(context, sess.possible_msg_id)
    set_session(possible_msg_id=None, last_possible_side=None)

    await send_confirmed_entry(context, last_seen_result=last_seen, bet_side=side)

    # Armar apuesta pendiente internamente (para que al registrar siguiente resultado, calcule win/lose)
    # bet se calcula desde banca
    sess2 = get_session()
    if sess2.is_active:
        bet_amt = calc_next_bet(sess2)
        set_session(pending_side=side, pending_bet=bet_amt, awaiting_outcome=1)


def schedule_possible_flow(application: Application):
    # limpia job previo
    for j in application.job_queue.jobs():
        if j.name == "possible_flow":
            j.schedule_removal()

    application.job_queue.run_once(possible_flow_job, when=POSSIBLE_CONFIRM_DELAY, name="possible_flow")


# =========================
# HANDLERS
# =========================
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Registrar dashboard y mostrarlo
    await upsert_dashboard(update, context)


async def on_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    try:
        await q.answer()
    except (TimedOut, NetworkError):
        return

    data = q.data
    sess = get_session()

    if data == "menu_add":
        # Mostrar teclado de resultados (editando el dashboard si existe)
        if sess.dashboard_chat_id and sess.dashboard_msg_id:
            await safe_edit_message(
                context.bot,
                sess.dashboard_chat_id,
                sess.dashboard_msg_id,
                "Selecciona el resultado real:",
                reply_markup=result_kb()
            )
        else:
            await q.message.reply_text("Selecciona el resultado real:", reply_markup=result_kb())
        return

    if data in ("add_P", "add_B", "add_T"):
        result = data.split("_", 1)[1]

        # guardar ronda
        add_round(result)

        # bajar cooldown
        s = get_session()
        if s.danger_cooldown > 0:
            set_session(danger_cooldown=max(0, s.danger_cooldown - 1))

        # si había apuesta pendiente => liquidar
        s_before = get_session()
        s_after, outcome_kind = settle_pending_bet(s_before, result)

        # publicar GREEN/RED/TIE en canal (respondiendo a entrada confirmada)
        if outcome_kind in ("GREEN", "RED", "TIE"):
            await reply_result_to_confirmed(context, outcome_kind, result)

        # stop-loss / take-profit
        hit = check_stop_take(s_after)
        if hit:
            # si toca cerrar, cerramos sesión
            stop_session()

        # evaluar posible entrada automática (si hay panel activo y hay apuesta activa)
        seq = get_last_results(300)
        s_now = get_session()
        if s_now.is_active and len(seq) >= 10:
            action, _ = decide_action(seq, s_now)
            if action in ("BET_P", "BET_B"):
                side = "P" if action == "BET_P" else "B"

                # si ya hay posible, lo reemplazamos (pero realmente lo borraremos si cambia)
                if s_now.possible_msg_id:
                    await delete_channel_message(context, s_now.possible_msg_id)
                    set_session(possible_msg_id=None, last_possible_side=None)

                await send_possible_entry(context, side=side)
                schedule_possible_flow(context.application)

        # Volver al dashboard principal actualizado
        await upsert_dashboard(update, context)
        return

    if data == "menu_start_bet":
        # pedir monto por mensaje
        set_session(awaiting_bank=1)
        # volver al dashboard pero con aviso
        await q.message.reply_text("Envía el monto así:\n<b>33500</b>\n(o <code>/bank 33500</code>)", parse_mode=ParseMode.HTML)
        await upsert_dashboard(update, context)
        return

    if data == "menu_reset":
        # BORRAR HISTORIAL + dejar panel vacío hasta iniciar apuesta
        clear_rounds()
        stop_session()
        # panel vacío (esto cumple tu punto 5)
        set_session(panel_enabled=0)

        # borrar posible entrada si existía
        sess2 = get_session()
        if sess2.possible_msg_id:
            await delete_channel_message(context, sess2.possible_msg_id)
        set_session(possible_msg_id=None, last_possible_side=None)

        await upsert_dashboard(update, context)
        return

    if data == "back_home":
        await upsert_dashboard(update, context)
        return


async def bank_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # /bank 33500
    if not context.args:
        await update.message.reply_text("Uso: /bank <monto>\nEj: /bank 33500")
        return
    try:
        bank = float(context.args[0])
        if bank <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Monto inválido. Ej: /bank 33500")
        return

    start_session(bank)
    await update.message.reply_text("✅ Apuesta iniciada. Ya puedes registrar resultados.")
    await upsert_dashboard(update, context)


async def plain_amount_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # si awaiting_bank=1, aceptar número simple
    sess = get_session()
    if not sess.awaiting_bank:
        return

    text = (update.message.text or "").strip()
    # permitir "33.500" o "33,500"
    clean = text.replace(".", "").replace(",", "")
    if not clean.isdigit():
        await update.message.reply_text("Envíame solo el número. Ej: 33500")
        return

    bank = float(clean)
    if bank <= 0:
        await update.message.reply_text("Monto inválido. Ej: 33500")
        return

    start_session(bank)
    await update.message.reply_text("✅ Apuesta iniciada. Ya puedes registrar resultados.")
    await upsert_dashboard(update, context)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    err = context.error
    if isinstance(err, (TimedOut, NetworkError)):
        logger.warning("Telegram timeout/network error: %s", err)
        return
    logger.exception("Unhandled error:", exc_info=err)


# =========================
# HEALTHCHECK (Render ping)
# =========================
flask_app = Flask(__name__)

@flask_app.get("/")
def home():
    return "OK - Bacbo bot activo ✅", 200


def run_web():
    port = int(os.getenv("PORT", "10000"))
    flask_app.run(host="0.0.0.0", port=port)


# =========================
# BOOT
# =========================
async def post_init(app: Application):
    # evita conflicto con webhooks activos
    try:
        await app.bot.delete_webhook(drop_pending_updates=True)
    except Exception:
        pass


def main():
    if not BOT_TOKEN:
        raise RuntimeError("Falta BOT_TOKEN en Environment Variables.")

    init_db()

    # Iniciar servidor web en hilo aparte (no toca el event loop)
    from threading import Thread
    Thread(target=run_web, daemon=True).start()

    request = HTTPXRequest(
        connect_timeout=20.0,
        read_timeout=20.0,
        write_timeout=20.0,
        pool_timeout=20.0,
    )

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .request(request)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("bank", bank_cmd))
    app.add_handler(CallbackQueryHandler(on_click))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, plain_amount_handler))
    app.add_error_handler(error_handler)

    logger.info("✅ Bot iniciado (polling)...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()