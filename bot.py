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

# ===== Logging =====
logging.basicConfig(level=logging.INFO)

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    err = context.error
    if isinstance(err, (TimedOut, NetworkError)):
        logging.warning("Telegram timeout/network error (se ignora): %s", err)
        return
    logging.exception("Exception while handling an update:", exc_info=err)

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
ALERT_DELAY_SECONDS = 8  # "posible entrada" dura esto antes de confirmar

# ====== Helpers Emojis ======
def side_label(side: str) -> str:
    return "🔵 PLAYER" if side == "P" else "🔴 BANKER"

def result_ball(result: str) -> str:
    if result == "P":
        return "🔵"
    if result == "B":
        return "🔴"
    return "🟠"  # TIE

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
    """
    Devuelve:
      - sess actualizado
      - outcome_txt para usuario
      - outcome_kind: "GREEN" | "RED" | "TIE" | None
    """
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

# ====== Métricas mesa ======
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
        return "NO_BET", f"🚨 Mesa peligrosa detectada: {why} (bloqueo {DANGER_COOLDOWN_ROUNDS} rondas)"

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
        return ("BET_P" if streak_side == "P" else "BET_B"), f"Mesa RACHA: {streak_side} x{streak_len}. Seguir racha."

    if cr >= 0.65:
        if last == "P":
            return "BET_B", f"Mesa CHOP (ChopRate {cr:.2f}). Ir CONTRARIO al último: B."
        if last == "B":
            return "BET_P", f"Mesa CHOP (ChopRate {cr:.2f}). Ir CONTRARIO al último: P."

    filtered = [x for x in seq if x in ("P", "B")]
    if len(filtered) >= 2 and filtered[-1] == filtered[-2]:
        side = filtered[-1]
        return ("BET_P" if side == "P" else "BET_B"), f"Confirmación {side}{side}. Seguir {side}."

    return "NO_BET", "Sin confirmación clara. Mejor no entrar."

# ====== Telegram channel publishing (AnaPrime style) ======
async def safe_delete(bot, chat_id: int, msg_id: int):
    try:
        await bot.delete_message(chat_id=chat_id, message_id=msg_id)
    except BadRequest:
        return
    except (TimedOut, NetworkError):
        return
    except Forbidden:
        logging.warning("Sin permisos para borrar mensajes en el canal.")
        return

async def send_possible_entry(app: Application, channel_id: int) -> Optional[int]:
    text = (
        "🚨<b>ATENCIÓN POSIBLE ENTRADA</b>🚨\n"
        "🎰 <b>Juego:</b> <b>Bac Bo - Evolution</b>\n"
    )
    try:
        m: Message = await app.bot.send_message(
            chat_id=channel_id,
            text=text,
            parse_mode=ParseMode.HTML
        )
        return m.message_id
    except Exception as e:
        logging.exception("No pude enviar POSIBLE ENTRADA al canal: %s", e)
        return None

async def send_confirmed_entry(app: Application, channel_id: int, side: str) -> Optional[int]:
    # Nota: AnaPrime suele mostrar ingresar después (bolita contraria) y apuesta en (bolita del lado).
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
        m: Message = await app.bot.send_message(
            chat_id=channel_id,
            text=text,
            parse_mode=ParseMode.HTML
        )
        return m.message_id
    except Exception as e:
        logging.exception("No pude enviar ENTRADA CONFIRMADA al canal: %s", e)
        return None

async def send_result_reply(app: Application, channel_id: int, reply_to_id: Optional[int], outcome_kind: str, actual_result: str):
    # outcome_kind: GREEN/RED/TIE
    ball = result_ball(actual_result)

    if outcome_kind == "GREEN":
        txt = f"🍀🍀🍀 <b>GREEN!!!</b> 🍀🍀🍀\n\n✅ <b>RESULTADO:</b> {ball}"
        sticker_id = os.getenv("GREEN_STICKER_ID")
    elif outcome_kind == "RED":
        txt = f"❌❌❌ <b>RED!!!</b> ❌❌❌\n\n✅ <b>RESULTADO:</b> {ball}"
        sticker_id = os.getenv("RED_STICKER_ID")
    else:
        txt = f"🟠 <b>TIE</b> 🟠\n\n✅ <b>RESULTADO:</b> {ball}"
        sticker_id = os.getenv("TIE_STICKER_ID")

    # 2.1: responder al mensaje ENTRADA CONFIRMADA
    try:
        await app.bot.send_message(
            chat_id=channel_id,
            text=txt,
            parse_mode=ParseMode.HTML,
            reply_to_message_id=reply_to_id if reply_to_id else None,
            allow_sending_without_reply=True
        )
    except Exception as e:
        logging.exception("No pude enviar RESULTADO al canal: %s", e)

    # 2.2: sticker (si existe)
    if sticker_id:
        try:
            await app.bot.send_sticker(
                chat_id=channel_id,
                sticker=sticker_id,
                reply_to_message_id=reply_to_id if reply_to_id else None,
                allow_sending_without_reply=True
            )
        except Exception as e:
            logging.warning("No pude enviar sticker (config opcional): %s", e)

async def schedule_entry_flow(app: Application, channel_id: int, side: str):
    """
    1) envía POSIBLE ENTRADA
    2) espera ALERT_DELAY_SECONDS
    3) si aún aplica (sigue recomendado y no hay awaiting_outcome), manda ENTRADA CONFIRMADA
       y elimina posible entrada.
    """
    # Enviar posible entrada
    possible_id = await send_possible_entry(app, channel_id)
    if possible_id:
        set_session(last_alert_msg_id=possible_id)

    await asyncio.sleep(ALERT_DELAY_SECONDS)

    # Re-evaluar (por si cambió el contexto)
    seq = get_last_results(300)
    sess = get_session()
    action, _detail = decide_action(seq, sess)

    # Si ya no es apuesta o ya hay apuesta pendiente → borrar posible y salir
    if action == "NO_BET" or sess.awaiting_outcome:
        if possible_id:
            await safe_delete(app.bot, channel_id, possible_id)
            set_session(last_alert_msg_id=None)
        return

    # Confirmar según acción actual
    new_side = "P" if action == "BET_P" else "B"
    # Si cambió el lado, confirmamos el nuevo.
    if possible_id:
        await safe_delete(app.bot, channel_id, possible_id)
        set_session(last_alert_msg_id=None)

    confirm_id = await send_confirmed_entry(app, channel_id, new_side)
    if confirm_id:
        set_session(last_confirm_msg_id=confirm_id)

    # Armar apuesta pendiente (para que el próximo resultado se liquide)
    sess2 = get_session()
    if sess2.is_active:
        bet = calc_next_bet(sess2)
        set_session(pending_side=new_side, pending_bet=bet, awaiting_outcome=1, last_reco=new_side)

# ====== Texto de recomendación (para chat privado del bot) ======
async def reco_text_and_maybe_publish(context: ContextTypes.DEFAULT_TYPE) -> str:
    seq = get_last_results(300)
    sess = get_session()

    action, detail = decide_action(seq, sess)
    win = seq[-WINDOW_N:] if len(seq) >= WINDOW_N else seq[:]
    cr = chop_rate(win)
    streak_side, streak_len = current_streak(win)
    ties = count_ties(win)

    bank_line = ""
    if sess.is_active:
        bank_line = f"\n🏦 Banca: {sess.bank_current:.0f} | Gale: {sess.gale_level}/{MAX_GALE} | Anti-tilt: {sess.danger_cooldown}\n🎯 {session_limits_text(sess)}"

    if action == "NO_BET":
        if sess.is_active:
            set_session(pending_side=None, pending_bet=0, awaiting_outcome=0)
        return (
            f"🚫 NO APOSTAR\n"
            f"🧠 {detail}\n"
            f"📌 Ventana: {len(win)} | ChopRate: {cr:.2f} | Racha: {streak_side or '-'} x{streak_len} | Ties: {ties}/{len(win)}"
            f"{bank_line}"
        )

    # Si recomienda apostar:
    side = "P" if action == "BET_P" else "B"
    side_txt = side_label(side)

    # SOLO publicamos al canal si sesión está activa y no hay apuesta pendiente
    if sess.is_active and not sess.awaiting_outcome:
        channel_id = int(os.getenv("CHANNEL_ID", "0"))
        if channel_id:
            # Crear tarea sin bloquear el botón del usuario
            context.application.create_task(schedule_entry_flow(context.application, channel_id, side))

    if sess.is_active:
        bet_preview = calc_next_bet(sess)
        bet_line = f"\n💵 Próxima apuesta estimada: {bet_preview:.0f} a {side_txt} (se confirma en canal)"
    else:
        bet_line = "\nℹ️ Inicia sesión con /bank <monto> para controlar banca y publicar al canal."

    return (
        f"✅ POSIBLE APOSTAR (modo canal)\n"
        f"➡️ Recomendación: {side_txt}\n"
        f"🧠 {detail}\n"
        f"📌 Ventana: {len(win)} | ChopRate: {cr:.2f} | Racha: {streak_side or '-'} x{streak_len} | Ties: {ties}/{len(win)}"
        f"{bet_line}"
        f"{bank_line}"
    )

# ====== SAFE EDIT ======
async def safe_edit(q, text: str, reply_markup=None):
    try:
        await q.edit_message_text(text, reply_markup=reply_markup)
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

def stats_text(seq: