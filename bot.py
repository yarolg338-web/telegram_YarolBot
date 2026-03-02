import os
import sqlite3
import asyncio
import logging
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional, Tuple

from flask import Flask
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest, TimedOut, NetworkError
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)
from telegram.request import HTTPXRequest

DB_PATH = "bacbo.db"

# ===== Logging =====
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("bacbo")

# ====== Configuración ======
WINDOW_N = 30
MAX_GALE = 1
STOP_LOSS_PCT = 10
TAKE_PROFIT_PCT = 5
BASE_BET_PCT = 2.0

# Evolution chips
ALLOWED_BETS = [5000, 10000, 25000, 125000, 500000, 2500000]

TIE_AVOID_THRESHOLD = 3
DANGER_COOLDOWN_ROUNDS = 3

# Canal estilo AnaPrime
POSSIBLE_TTL_SECONDS = 8  # “posible entrada” se borra si no confirmas en este tiempo

# Tablero
BOARD_ROWS = 6
MAX_COLS_DISPLAY = 50
CELL_EMPTY = "⬜️"

# ============ UTIL =============
def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    err = context.error
    if isinstance(err, (TimedOut, NetworkError)):
        log.warning("Telegram timeout/network error (se ignora): %s", err)
        return
    log.exception("Exception while handling an update:", exc_info=err)

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

        possible_msg_id INTEGER DEFAULT NULL,
        confirmed_msg_id INTEGER DEFAULT NULL
    )
    """)
    cur.execute("INSERT OR IGNORE INTO session (id) VALUES (1)")
    con.commit()
    con.close()

def add_round(result: str):
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("INSERT INTO rounds (ts, result) VALUES (?, ?)", (now_utc(), result))
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

# ====== Sesión / banca ======
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
    possible_msg_id: Optional[int]
    confirmed_msg_id: Optional[int]

def get_session() -> SessionState:
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("""
        SELECT
            is_active, bank_start, bank_current, base_bet, gale_level, last_reco,
            pending_side, pending_bet, awaiting_outcome, danger_cooldown,
            possible_msg_id, confirmed_msg_id
        FROM session WHERE id=1
    """)
    row = cur.fetchone()
    con.close()
    return SessionState(
        bool(row[0]), float(row[1]), float(row[2]), float(row[3]), int(row[4]), row[5],
        row[6], float(row[7]), bool(row[8]), int(row[9]),
        row[10], row[11]
    )

def set_session(**kwargs):
    if not kwargs:
        return
    keys = []
    vals = []
    for k, v in kwargs.items():
        keys.append(f"{k}=?")
        vals.append(v)
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute(f"UPDATE session SET {', '.join(keys)} WHERE id=1", vals)
    con.commit()
    con.close()

# ====== Chips / Redondeo Evolution ======
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
        possible_msg_id=None,
        confirmed_msg_id=None
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
        possible_msg_id=None,
        confirmed_msg_id=None
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

def settle_pending_bet(sess: SessionState, actual_result: str) -> Tuple[SessionState, str]:
    # Solo liquida si había una apuesta confirmada pendiente
    if not (sess.is_active and sess.awaiting_outcome and sess.pending_side in ("P", "B") and sess.pending_bet > 0):
        return sess, ""

    side = sess.pending_side
    bet = float(sess.pending_bet)
    bank = float(sess.bank_current)
    gale = int(sess.gale_level)

    if actual_result == "T":
        outcome_txt = "🟠 TIE. Push (banca no cambia)."
    elif actual_result == side:
        bank += bet
        gale = 0
        outcome_txt = f"🍀🍀🍀 GREEN!!! 🍀🍀🍀\n✅ RESULTADO: {'🔵' if actual_result=='P' else '🔴'}"
    else:
        bank -= bet
        if gale < MAX_GALE:
            gale += 1
        else:
            gale = 0
        outcome_txt = f"❌❌❌ RED ❌❌❌\n✅ RESULTADO: {'🔵' if actual_result=='P' else '🔴'}"

    set_session(
        bank_current=bank,
        gale_level=gale,
        pending_side=None,
        pending_bet=0,
        awaiting_outcome=0
    )
    return get_session(), outcome_txt

# ====== Métricas mesa ======
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
        return "NO_BET", f"ANTI-TILT activo: espera {sess.danger_cooldown} ronda(s) para re-evaluar."

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
        return "NO_BET", "Último resultado fue TIE. Espera 1 ronda y reevalúa."
    if 0.40 <= cr <= 0.60 and streak_len < 3:
        return "NO_BET", f"Mesa indecisa (ChopRate {cr:.2f})."

    if streak_len >= 3 and streak_side in ("P", "B"):
        return ("BET_P" if streak_side == "P" else "BET_B"), f"RACHA: {streak_side} x{streak_len}. Seguir racha."

    if cr >= 0.65:
        if last == "P":
            return "BET_B", f"Mesa CHOP (ChopRate {cr:.2f}). Contrario al último: B."
        if last == "B":
            return "BET_P", f"Mesa CHOP (ChopRate {cr:.2f}). Contrario al último: P."

    filtered = [x for x in seq if x in ("P", "B")]
    if len(filtered) >= 2 and filtered[-1] == filtered[-2]:
        side = filtered[-1]
        return ("BET_P" if side == "P" else "BET_B"), f"Confirmación {side}{side}. Seguir {side}."

    return "NO_BET", "Sin confirmación clara. Mejor no entrar."

# ====== TABLERO 6xN ======
def render_bead_board(seq: list[str], rows: int = BOARD_ROWS, max_cols: int = MAX_COLS_DISPLAY) -> tuple[str, int, int]:
    m = {"P": "🔵", "B": "🔴", "T": "🟠"}
    seq = [x for x in seq if x in ("P", "B", "T")]

    if not seq:
        grid = [[CELL_EMPTY for _ in range(1)] for _ in range(rows)]
        text = "\n".join("".join(r) for r in grid) + "\nLeyenda: 🔵P  🔴B  🟠T"
        return text, 1, 1

    total_cols = math.ceil(len(seq) / rows)
    start_col = max(0, total_cols - max_cols)
    shown_cols = total_cols - start_col

    grid = [[CELL_EMPTY for _ in range(shown_cols)] for _ in range(rows)]

    for c in range(start_col, total_cols):
        for r in range(rows):
            idx = c * rows + r
            if idx < len(seq):
                grid[r][c - start_col] = m.get(seq[idx], CELL_EMPTY)

    lines = ["".join(row) for row in grid]
    legend = "Leyenda: 🔵P  🔴B  🟠T"
    return "\n".join(lines) + "\n" + legend, total_cols, shown_cols

async def update_board_message(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    seq = get_last_results(999999)
    board_text, total_cols, shown_cols = render_bead_board(seq)

    header = f"📌 <b>TABLERO Bac Bo</b> (6x{total_cols} | mostrando últimas {shown_cols})"
    if total_cols > shown_cols:
        header += f"\nℹ️ Hay más historial. Usa /board <página> (ej: /board 2)."

    text = f"{header}\n<pre>{board_text}</pre>"

    key = f"board_msg_id:{chat_id}"
    msg_id = context.application.bot_data.get(key)

    try:
        if msg_id:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=msg_id,
                text=text,
                parse_mode="HTML",
            )
            return
    except Exception:
        pass

    msg = await context.bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML")
    context.application.bot_data[key] = msg.message_id

async def board_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    page = 1
    if context.args:
        try:
            page = max(1, int(context.args[0]))
        except:
            page = 1

    seq = get_last_results(999999)
    seq = [x for x in seq if x in ("P", "B", "T")]

    total_cols = max(1, math.ceil(len(seq) / BOARD_ROWS))
    cols_per_page = MAX_COLS_DISPLAY
    total_pages = max(1, math.ceil(total_cols / cols_per_page))
    if page > total_pages:
        page = total_pages

    end_col = total_cols - (page - 1) * cols_per_page
    start_col = max(0, end_col - cols_per_page)
    shown_cols = end_col - start_col

    m = {"P": "🔵", "B": "🔴", "T": "🟠"}
    grid = [[CELL_EMPTY for _ in range(shown_cols)] for _ in range(BOARD_ROWS)]

    for c in range(start_col, end_col):
        for r in range(BOARD_ROWS):
            idx = c * BOARD_ROWS + r
            if idx < len(seq):
                grid[r][c - start_col] = m.get(seq[idx], CELL_EMPTY)

    board_text = "\n".join("".join(row) for row in grid) + "\nLeyenda: 🔵P  🔴B  🟠T"
    header = f"📌 <b>TABLERO</b> página {page}/{total_pages} (cols {start_col+1}-{end_col} de {total_cols})"
    text = f"{header}\n<pre>{board_text}</pre>"
    await update.message.reply_text(text, parse_mode="HTML")

# ====== CANAL (AnaPrime style) ======
def side_text(side: str) -> str:
    return "🔵 PLAYER" if side == "P" else "🔴 BANKER"

def ingresar_despues_ball(side: str) -> str:
    # ENTRAR DESPUÉS: contrario al lado recomendado
    return "🔴" if side == "P" else "🔵"

def apuesta_ball(side: str) -> str:
    return "🔵" if side == "P" else "🔴"

async def send_possible_entry(context: ContextTypes.DEFAULT_TYPE, side: str, detail: str) -> Optional[int]:
    channel_id = context.application.bot_data.get("CHANNEL_ID")
    if not channel_id:
        return None

    text = (
        "🚨🚨 <b>ATENCIÓN POSIBLE ENTRADA</b> 🚨🚨\n"
        "🎰 <b>Juego:</b> Bac Bo - Evolution\n\n"
        f"🧠 {detail}\n"
        f"➡️ Posible: <b>{side_text(side)}</b>"
    )
    msg = await context.bot.send_message(chat_id=channel_id, text=text, parse_mode="HTML")
    return msg.message_id

async def delete_channel_message_safe(context: ContextTypes.DEFAULT_TYPE, msg_id: int):
    channel_id = context.application.bot_data.get("CHANNEL_ID")
    if not channel_id or not msg_id:
        return
    try:
        await context.bot.delete_message(chat_id=channel_id, message_id=msg_id)
    except Exception:
        return

async def auto_delete_possible_job(context: ContextTypes.DEFAULT_TYPE):
    sess = get_session()
    # si todavía existe (no confirmaste), bórralo
    if sess.possible_msg_id:
        await delete_channel_message_safe(context, sess.possible_msg_id)
        set_session(possible_msg_id=None)

async def send_confirmed_entry(context: ContextTypes.DEFAULT_TYPE, side: str) -> Optional[int]:
    channel_id = context.application.bot_data.get("CHANNEL_ID")
    if not channel_id:
        return None

    text = (
        "✅ <b>ENTRADA CONFIRMADA</b> ✅\n\n"
        "🎰 <b>Juego:</b> Bac Bo - Evolution\n"
        f"🕒 <b>INGRESAR DESPUÉS:</b> {ingresar_despues_ball(side)}\n"
        f"🔥 <b>APUESTA EN:</b> {apuesta_ball(side)} {side_text(side)}\n\n"
        "🔒 <b>PROTEGER EMPATE</b> con 10% (Opcional)\n"
        f"🔁 <b>MÁXIMO {MAX_GALE} GALE</b>\n"
    )
    msg = await context.bot.send_message(chat_id=channel_id, text=text, parse_mode="HTML")
    return msg.message_id

async def send_result_reply(context: ContextTypes.DEFAULT_TYPE, result: str, reply_to_message_id: Optional[int]):
    channel_id = context.application.bot_data.get("CHANNEL_ID")
    if not channel_id:
        return

    # Stickers opcionales
    green_sticker = context.application.bot_data.get("GREEN_STICKER_FILE_ID")
    red_sticker = context.application.bot_data.get("RED_STICKER_FILE_ID")
    tie_sticker = context.application.bot_data.get("TIE_STICKER_FILE_ID")

    if result == "T":
        title = "🟠🟠🟠 TIE 🟠🟠🟠"
        ball = "🟠"
        sticker = tie_sticker
    elif result in ("P", "B"):
        # GREEN/RED se decide cuando se liquida; aquí solo mostramos el resultado real.
        # El texto final GREEN/RED lo arma settle_pending_bet()
        title = None
        ball = "🔵" if result == "P" else "🔴"
        sticker = None
    else:
        return

    # Si hay sticker para tie, envíalo primero (opcional)
    if sticker:
        try:
            await context.bot.send_sticker(chat_id=channel_id, sticker=sticker)
        except Exception:
            pass

    if title:
        text = f"{title}\n✅ RESULTADO: {ball}"
        await context.bot.send_message(
            chat_id=channel_id,
            text=text,
            reply_to_message_id=reply_to_message_id if reply_to_message_id else None,
        )

# ====== SAFE EDIT ======
async def safe_edit(q, text: str, reply_markup=None):
    try:
        await q.edit_message_text(text, reply_markup=reply_markup, parse_mode="HTML")
    except BadRequest as e:
        if "Message is not modified" in str(e):
            return
        raise
    except (TimedOut, NetworkError):
        return

# ====== UI ======
def home_menu(sess: SessionState):
    kb = [
        [InlineKeyboardButton("➕ Registrar resultado", callback_data="menu_add")],
        [InlineKeyboardButton("🧠 Recomendación ahora", callback_data="menu_reco")],
        [InlineKeyboardButton("📌 Ver tablero (actualiza)", callback_data="menu_board")],
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

def confirm_menu():
    kb = [
        [InlineKeyboardButton("✅ Confirmar entrada (manda al canal)", callback_data="confirm_entry")],
        [InlineKeyboardButton("❌ Cancelar (borra posible entrada)", callback_data="cancel_possible")],
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
        f"📊 <b>Estadísticas</b> (total {total})\n"
        f"🔵 Player: {p} ({p/total*100:.1f}%)\n"
        f"🔴 Banker: {b} ({b/total*100:.1f}%)\n"
        f"🟠 Tie: {t} ({t/total*100:.1f}%)\n"
        f"🧠 Ventana {len(win)}: ChopRate {chop_rate(win):.2f} | Ties {count_ties(win)}/{len(win)}\n"
    )

    if sess.is_active:
        txt += (
            f"\n🏦 <b>Sesión activa</b>\n"
            f"• Banca: {sess.bank_current:.0f}\n"
            f"• Base: {sess.base_bet:.0f}\n"
            f"• Gale: {sess.gale_level}/{MAX_GALE}\n"
            f"• 🎯 {session_limits_text(sess)}\n"
            f"• 🧯 Anti-tilt: {sess.danger_cooldown}\n"
        )
        if sess.awaiting_outcome and sess.pending_side in ("P", "B"):
            side_emoji = "🔵 P" if sess.pending_side == "P" else "🔴 B"
            txt += f"\n⏳ Apuesta pendiente: {side_emoji} por {sess.pending_bet:.0f}"
    return txt

# ===== Handlers =====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sess = get_session()
    await update.message.reply_text(
        "🤖 <b>Bac Bo Bot</b>\n"
        "✅ Registras resultados y yo recomiendo.\n"
        "🎰 Fichas Evolution: 5k/10k/25k/125k/500k/2.5M\n"
        "📌 Usa /board para ver tablero por páginas.\n",
        reply_markup=home_menu(sess),
        parse_mode="HTML",
    )
    await update_board_message(context, update.effective_chat.id)

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
        f"🏦 <b>Sesión iniciada</b>\n"
        f"Banca: {sess.bank_current:.0f}\n"
        f"Riesgo: {BASE_BET_PCT:.1f}% (redondeado a ficha)\n"
        f"Apuesta base: {sess.base_bet:.0f}\n"
        f"Max gale: {MAX_GALE} (por escalón)\n"
        f"🎯 {session_limits_text(sess)}",
        reply_markup=home_menu(sess),
        parse_mode="HTML",
    )

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

        # baja cooldown
        sess_now = get_session()
        if sess_now.danger_cooldown > 0:
            set_session(danger_cooldown=max(0, sess_now.danger_cooldown - 1))

        # Liquida apuesta pendiente (si existía)
        sess_before = get_session()
        sess_after, outcome_txt = settle_pending_bet(sess_before, result)

        # stop/take
        stop_hit = check_stop_take(sess_after)
        if stop_hit in ("STOP_LOSS", "TAKE_PROFIT"):
            final_bank = sess_after.bank_current
            stop_session()
            txt = ("⛔ STOP LOSS alcanzado. Sesión cerrada."
                   if stop_hit == "STOP_LOSS" else
                   "✅ TAKE PROFIT alcanzado. Sesión cerrada.")
            await safe_edit(q, f"{txt}\nBanca final: {final_bank:.0f}\n\nMenú:", reply_markup=home_menu(get_session()))
            await update_board_message(context, q.message.chat_id)
            return

        # Si hubo outcome, envíalo al canal respondiendo a la ENTRADA CONFIRMADA
        sess_after2 = get_session()
        if outcome_txt and sess_after2.confirmed_msg_id:
            # sticker opcional según GREEN/RED/TIE
            if "GREEN" in outcome_txt:
                sticker = context.application.bot_data.get("GREEN_STICKER_FILE_ID")
                if sticker:
                    try:
                        await context.bot.send_sticker(chat_id=context.application.bot_data.get("CHANNEL_ID"), sticker=sticker)
                    except Exception:
                        pass

            if "RED" in outcome_txt:
                sticker = context.application.bot_data.get("RED_STICKER_FILE_ID")
                if sticker:
                    try:
                        await context.bot.send_sticker(chat_id=context.application.bot_data.get("CHANNEL_ID"), sticker=sticker)
                    except Exception:
                        pass

            if "TIE" in outcome_txt:
                sticker = context.application.bot_data.get("TIE_STICKER_FILE_ID")
                if sticker:
                    try:
                        await context.bot.send_sticker(chat_id=context.application.bot_data.get("CHANNEL_ID"), sticker=sticker)
                    except Exception:
                        pass

            try:
                await context.bot.send_message(
                    chat_id=context.application.bot_data.get("CHANNEL_ID"),
                    text=outcome_txt,
                    reply_to_message_id=sess_after2.confirmed_msg_id,
                )
            except Exception:
                pass

            # NO borrar el resultado (se queda fijo como pediste)
            # Reiniciamos confirmed_msg_id para no responder dos veces
            set_session(confirmed_msg_id=None)

        # Recomendación (solo interna)
        seq = get_last_results(300)
        sess_now2 = get_session()
        action, detail = decide_action(seq, sess_now2)

        header = f"✅ Guardado: {('🔵' if result=='P' else '🔴' if result=='B' else '🟠')} ({result})"
        parts = [header]
        if outcome_txt:
            parts.append(outcome_txt)

        if action == "NO_BET":
            parts.append(f"🚫 NO APOSTAR\n🧠 {detail}")
        else:
            side = "P" if action == "BET_P" else "B"
            parts.append(f"✅ POSIBLE APOSTAR\n➡️ Recomendación: {side_text(side)}\n🧠 {detail}\n\n"
                         f"👉 Si realmente se confirma, entra a <b>Recomendación ahora</b> y presiona <b>Confirmar</b>.")

        await safe_edit(q, "\n\n".join(parts), reply_markup=home_menu(get_session()))
        await update_board_message(context, q.message.chat_id)
        return

    if data == "menu_board":
        await update_board_message(context, q.message.chat_id)
        await safe_edit(q, "📌 Tablero actualizado.\nMenú:", reply_markup=home_menu(get_session()))
        return

    if data == "menu_reco":
        seq = get_last_results(300)
        sess = get_session()
        action, detail = decide_action(seq, sess)

        if action == "NO_BET":
            set_session(pending_side=None, pending_bet=0, awaiting_outcome=0)
            txt = f"🚫 <b>NO APOSTAR</b>\n🧠 {detail}"
            await safe_edit(q, txt, reply_markup=home_menu(get_session()))
            return

        side = "P" if action == "BET_P" else "B"

        # Calcula apuesta para sesión (si está activa)
        sess2 = get_session()
        bet_line = ""
        if sess2.is_active:
            bet = calc_next_bet(sess2)
            set_session(pending_side=side, pending_bet=bet, awaiting_outcome=1, last_reco=side)
            bet_line = f"\n💵 Próxima apuesta: <b>{bet:.0f}</b> a {side_text(side)}"
        else:
            bet_line = "\nℹ️ Inicia sesión con /bank <monto> para control de banca."

        # 1) manda “posible entrada” al canal
        possible_id = await send_possible_entry(context, side, detail)
        if possible_id:
            set_session(possible_msg_id=possible_id)

            # programa borrado automático si no confirmas
            context.job_queue.run_once(
                lambda ctx: auto_delete_possible_job(ctx),
                when=POSSIBLE_TTL_SECONDS,
                name=f"auto_del_possible_{possible_id}",
            )

        txt = (
            f"🚨 <b>ATENCIÓN POSIBLE ENTRADA</b>\n"
            f"🎰 Bac Bo - Evolution\n"
            f"➡️ Posible: <b>{side_text(side)}</b>\n"
            f"🧠 {detail}"
            f"{bet_line}\n\n"
            f"✅ Si se confirma, presiona <b>Confirmar entrada</b>.\n"
            f"❌ Si se cae la entrada, presiona <b>Cancelar</b>."
        )

        await safe_edit(q, txt, reply_markup=confirm_menu())
        return

    if data == "confirm_entry":
        sess = get_session()
        if not sess.pending_side:
            await safe_edit(q, "No hay entrada pendiente para confirmar.\nMenú:", reply_markup=home_menu(get_session()))
            return

        # borra “posible” si existe
        if sess.possible_msg_id:
            await delete_channel_message_safe(context, sess.possible_msg_id)
            set_session(possible_msg_id=None)

        # manda confirmada y guarda message_id
        confirmed_id = await send_confirmed_entry(context, sess.pending_side)
        if confirmed_id:
            set_session(confirmed_msg_id=confirmed_id)

        await safe_edit(q, "✅ Entrada confirmada y enviada al canal.\nAhora registra el resultado real cuando salga.\nMenú:",
                        reply_markup=home_menu(get_session()))
        return

    if data == "cancel_possible":
        sess = get_session()
        if sess.possible_msg_id:
            await delete_channel_message_safe(context, sess.possible_msg_id)
        set_session(possible_msg_id=None)

        # si cancelas, también quitamos apuesta pendiente (opcional)
        set_session(pending_side=None, pending_bet=0, awaiting_outcome=0)

        await safe_edit(q, "❌ Posible entrada cancelada (borrada del canal).\nMenú:", reply_markup=home_menu(get_session()))
        return

    if data == "menu_stats":
        seq = get_last_results(800)
        sess = get_session()
        txt = stats_text(seq, sess)
        await safe_edit(q, txt, reply_markup=home_menu(sess))
        return

    if data == "menu_session_start":
        await safe_edit(
            q,
            "Para iniciar sesión envía: <b>/bank &lt;monto&gt;</b>\nEj: <b>/bank 300000</b>\n\n"
            f"📌 El bot calcula {BASE_BET_PCT:.1f}% y lo redondea a fichas Evolution.",
            reply_markup=home_menu(sess),
        )
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
        await update_board_message(context, q.message.chat_id)
        return

    if data == "back_home":
        await safe_edit(q, "Menú:", reply_markup=home_menu(get_session()))
        return

# ====== WEB (para Render/UptimeRobot) ======
flask_app = Flask(__name__)

@flask_app.get("/")
def home():
    return "OK - Bacbo bot activo ✅", 200

# ====== MAIN ======
async def start_bot():
    init_db()

    TOKEN = os.getenv("BOT_TOKEN")
    CHANNEL_ID = os.getenv("CHANNEL_ID")  # debe ser -100xxxxxxxxxx
    if not TOKEN:
        raise RuntimeError("Falta BOT_TOKEN en Render (Environment Variables).")
    if not CHANNEL_ID:
        log.warning("⚠️ Falta CHANNEL_ID. El bot funcionará pero NO enviará al canal.")
    else:
        try:
            CHANNEL_ID = int(CHANNEL_ID)
        except:
            raise RuntimeError("CHANNEL_ID debe ser numérico, ejemplo: -1001234567890")

    request = HTTPXRequest(
        connect_timeout=20.0,
        read_timeout=20.0,
        write_timeout=20.0,
        pool_timeout=20.0,
    )

    app = Application.builder().token(TOKEN).request(request).build()

    # Guardamos config en bot_data para usar en cualquier función
    app.bot_data["CHANNEL_ID"] = CHANNEL_ID

    # Stickers opcionales (si no los pones, solo manda texto)
    app.bot_data["GREEN_STICKER_FILE_ID"] = os.getenv("GREEN_STICKER_FILE_ID")
    app.bot_data["RED_STICKER_FILE_ID"] = os.getenv("RED_STICKER_FILE_ID")
    app.bot_data["TIE_STICKER_FILE_ID"] = os.getenv("TIE_STICKER_FILE_ID")

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("bank", bank_cmd))
    app.add_handler(CommandHandler("board", board_cmd))
    app.add_handler(CallbackQueryHandler(on_click))
    app.add_error_handler(error_handler)

    log.info("✅ Bot iniciado (polling)...")
    await app.run_polling(drop_pending_updates=True)

def main():
    port = int(os.getenv("PORT", "10000"))

    # Flask en hilo separado (NO toca el loop principal)
    from threading import Thread

    def run_web():
        # dev server OK para ping; Render free lo aguanta
        flask_app.run(host="0.0.0.0", port=port)

    Thread(target=run_web, daemon=True).start()

    # Telegram en el hilo principal (evita errores de signals/event loop)
    asyncio.run(start_bot())

if __name__ == "__main__":
    main()