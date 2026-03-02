import os
import sqlite3
import asyncio
import logging
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

# =========================
# CONFIG
# =========================
DB_PATH = "bacbo.db"

WINDOW_N = 30

MAX_GALE = 2  # ✅ como pediste
STOP_LOSS_PCT = 10
TAKE_PROFIT_PCT = 5
BASE_BET_PCT = 2.0

ALLOWED_BETS = [5000, 10000, 25000, 125000, 500000, 2500000]

TIE_AVOID_THRESHOLD = 3
DANGER_COOLDOWN_ROUNDS = 3

# ⏳ Tiempo de "POSIBLE ENTRADA" antes de confirmar automáticamente
POSSIBLE_DELAY_SECONDS = 12

# =========================
# LOG
# =========================
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("bacbo")


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    err = context.error
    if isinstance(err, (TimedOut, NetworkError)):
        logger.warning("Telegram timeout/network error (ignorado): %s", err)
        return
    logger.exception("Exception while handling an update:", exc_info=err)


# =========================
# DB INIT
# =========================
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
        confirm_msg_id INTEGER DEFAULT NULL
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
    last_reco: Optional[str]
    pending_side: Optional[str]
    pending_bet: float
    awaiting_outcome: bool
    danger_cooldown: int
    possible_msg_id: Optional[int]
    confirm_msg_id: Optional[int]


def get_session() -> SessionState:
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("""
        SELECT
            is_active, bank_start, bank_current, base_bet, gale_level, last_reco,
            pending_side, pending_bet, awaiting_outcome, danger_cooldown,
            possible_msg_id, confirm_msg_id
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


# =========================
# HELPERS
# =========================
def ball(res: str) -> str:
    if res == "P":
        return "🔵"
    if res == "B":
        return "🔴"
    return "🟠"  # TIE


def last_non_tie(seq: List[str]) -> Optional[str]:
    for x in reversed(seq):
        if x in ("P", "B"):
            return x
    return None


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
        confirm_msg_id=None,
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
        confirm_msg_id=None,
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
# TABLE METRICS
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
        return "NO_BET", f"ANTI-TILT activo: espera {sess.danger_cooldown} ronda(s) para re-evaluar."

    danger, why = is_danger_table(seq)
    if danger:
        set_session(danger_cooldown=DANGER_COOLDOWN_ROUNDS)
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

    # seguir racha
    if streak_len >= 3 and streak_side in ("P", "B"):
        return ("BET_P" if streak_side == "P" else "BET_B"), f"Mesa RACHA: {streak_side} x{streak_len}. Seguir racha."

    # contraria si chop fuerte
    if cr >= 0.65:
        if last == "P":
            return "BET_B", f"Mesa CHOP (ChopRate {cr:.2f}). Ir CONTRARIO al último: B."
        if last == "B":
            return "BET_P", f"Mesa CHOP (ChopRate {cr:.2f}). Ir CONTRARIO al último: P."

    # confirmación simple: dos iguales seguidas
    filtered = [x for x in seq if x in ("P", "B")]
    if len(filtered) >= 2 and filtered[-1] == filtered[-2]:
        side = filtered[-1]
        return ("BET_P" if side == "P" else "BET_B"), f"Confirmación {side}{side}. Seguir {side}."

    return "NO_BET", "Sin confirmación clara. Mejor no entrar."


# =========================
# ROADMAP 6xN
# =========================
def render_roadmap(seq: List[str], rows: int = 6) -> str:
    if not seq:
        return "🧾 Roadmap (6xN)\n(Aún vacío)"

    cols = (len(seq) + rows - 1) // rows
    grid = [["  " for _ in range(cols)] for __ in range(rows)]

    for i, r in enumerate(seq):
        row = i % rows
        col = i // rows
        grid[row][col] = ball(r)

    lines = ["🧾 Roadmap (6xN)"]
    for r in range(rows):
        lines.append(" ".join(grid[r]))
    return "\n".join(lines)


# =========================
# TELEGRAM SEND/EDIT HELPERS
# =========================
async def safe_edit(q, text: str, reply_markup=None):
    try:
        await q.edit_message_text(text, reply_markup=reply_markup)
    except BadRequest as e:
        if "Message is not modified" in str(e):
            return
        raise
    except (TimedOut, NetworkError):
        return


async def delete_message_safe(bot, chat_id: int, message_id: int):
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except BadRequest:
        pass
    except (TimedOut, NetworkError):
        pass


# =========================
# ANA-STYLE MESSAGES
# =========================
def build_possible_text() -> str:
    return (
        "🚨ATENCIÓN POSIBLE ENTRADA🚨\n"
        "🎰Juego: Bac Bo - Evolution"
    )


def build_confirm_text(last_ball: str, bet_ball: str) -> str:
    # ✅ EXACTO como lo pediste (solo bolitas en esas líneas)
    return (
        "✅ ENTRADA CONFIRMADA ✅\n\n"
        "🎰 Juego: Bac Bo - Evolution\n"
        f"🧨INGRESAR DESPUÉS: {last_ball}\n"
        f"🔥 APUESTA EN: {bet_ball}\n\n"
        "🔒 PROTEGER EMPATE con 10% (Opcional)\n\n"
        f"🔁 MÁXIMO {MAX_GALE} GALE"
    )


def build_green_text(result_ball: str) -> str:
    return (
        "🍀🍀🍀 GREEN!!! 🍀🍀🍀\n\n"
        f"✅RESULTADO: {result_ball}"
    )


def build_red_text() -> str:
    return (
        "❌ RED\n\n"
        "A veces puede suceder, ipero basta con gestionar tu banca!"
    )


# =========================
# POSSIBLE -> CONFIRM WORKFLOW
# =========================
async def cancel_possible_task(app: Application):
    t: Optional[asyncio.Task] = app.bot_data.get("possible_task")
    if t and not t.done():
        t.cancel()
    app.bot_data["possible_task"] = None


async def maybe_send_possible(app: Application):
    """
    Se llama cuando el bot detecta que "hay posible entrada"
    y programa confirmación después de X segundos si se mantiene.
    """
    channel_id = app.bot_data["CHANNEL_ID"]
    bot = app.bot

    await cancel_possible_task(app)

    sess = get_session()
    # Si ya hay posible, no duplicar; la vamos a reemplazar por seguridad
    if sess.possible_msg_id:
        await delete_message_safe(bot, channel_id, sess.possible_msg_id)
        set_session(possible_msg_id=None)

    msg = await bot.send_message(chat_id=channel_id, text=build_possible_text())
    set_session(possible_msg_id=msg.message_id)

    async def worker():
        try:
            await asyncio.sleep(POSSIBLE_DELAY_SECONDS)

            seq = get_last_results(300)
            sess_now = get_session()

            action, _detail = decide_action(seq, sess_now)

            # Si cambió a NO_BET, borrar posible
            if action == "NO_BET":
                sess2 = get_session()
                if sess2.possible_msg_id:
                    await delete_message_safe(bot, channel_id, sess2.possible_msg_id)
                    set_session(possible_msg_id=None)
                return

            # Si sigue siendo BET, confirmamos y borramos posible
            side = "P" if action == "BET_P" else "B"
            last_res = seq[-1] if seq else None

            # INGRESAR DESPUÉS = última bolita real (si fue T, usamos última no-tie; si no hay, usamos 🟠)
            last_for_ingresar = last_res if last_res in ("P", "B", "T") else None
            if last_for_ingresar == "T":
                ln = last_non_tie(seq)
                last_ball = ball(ln) if ln else "🟠"
            elif last_for_ingresar in ("P", "B"):
                last_ball = ball(last_for_ingresar)
            else:
                last_ball = "🟠"

            bet_ball = ball(side)

            # borrar posible
            sess3 = get_session()
            if sess3.possible_msg_id:
                await delete_message_safe(bot, channel_id, sess3.possible_msg_id)
                set_session(possible_msg_id=None)

            # enviar confirm y guardar msg_id
            confirm_msg = await bot.send_message(
                chat_id=channel_id,
                text=build_confirm_text(last_ball=last_ball, bet_ball=bet_ball),
            )
            set_session(confirm_msg_id=confirm_msg.message_id)

            # preparar apuesta pendiente (para que al registrar resultado mande GREEN/RED/TIE)
            sess_after = get_session()
            if sess_after.is_active:
                bet_amount = calc_next_bet(sess_after)
                set_session(pending_side=side, pending_bet=bet_amount, awaiting_outcome=1, last_reco=side)
            else:
                # igual dejamos pendiente_side para poder evaluar win/lose (opcional)
                set_session(pending_side=side, pending_bet=0, awaiting_outcome=1, last_reco=side)

        except asyncio.CancelledError:
            return
        except Exception as e:
            logger.exception("Error en worker de posible->confirm: %s", e)

    app.bot_data["possible_task"] = asyncio.create_task(worker())


async def maybe_clear_possible_if_needed(app: Application):
    """
    Si ya existe mensaje de posible entrada pero el cálculo cambió a NO_BET,
    lo borra inmediatamente.
    """
    channel_id = app.bot_data["CHANNEL_ID"]
    bot = app.bot

    sess = get_session()
    if not sess.possible_msg_id:
        return

    seq = get_last_results(300)
    action, _detail = decide_action(seq, sess)

    if action == "NO_BET":
        await delete_message_safe(bot, channel_id, sess.possible_msg_id)
        set_session(possible_msg_id=None)
        await cancel_possible_task(app)


# =========================
# SETTLEMENT (GREEN/RED/TIE) -> REPLY TO CONFIRM
# =========================
async def settle_and_post(app: Application, actual_result: str) -> str:
    """
    Se llama cuando el usuario registra un resultado.
    - Ajusta banca si hay sesión activa y apuesta pendiente
    - Envía GREEN/RED/TIE al canal respondiendo al confirm_msg_id
    """
    channel_id = app.bot_data["CHANNEL_ID"]
    bot = app.bot

    sess = get_session()
    if not (sess.awaiting_outcome and sess.pending_side in ("P", "B")):
        return ""

    side = sess.pending_side
    bet = float(sess.pending_bet)
    bank_now = float(sess.bank_current)
    gale = int(sess.gale_level)

    result_ball = ball(actual_result)

    if actual_result == "T":
        # Empate: push (no cambia banca) pero sí avisamos 🟠
        txt = build_green_text(result_ball="🟠")  # empate lo tratamos como aviso “resultado”
        if sess.confirm_msg_id:
            await bot.send_message(
                chat_id=channel_id,
                text=txt,
                reply_to_message_id=sess.confirm_msg_id,
            )
        # no cambiamos gale por push
        set_session(awaiting_outcome=0)
        return "🟠 TIE (push)."

    if actual_result == side:
        # WIN
        if sess.is_active and bet > 0:
            bank_now += bet
        gale = 0
        if sess.confirm_msg_id:
            await bot.send_message(
                chat_id=channel_id,
                text=build_green_text(result_ball=result_ball),
                reply_to_message_id=sess.confirm_msg_id,
            )
        outcome_txt = "✅ GREEN (WIN)."
    else:
        # LOSE
        if sess.is_active and bet > 0:
            bank_now -= bet
        if gale < MAX_GALE:
            gale += 1
        else:
            gale = 0

        if sess.confirm_msg_id:
            await bot.send_message(
                chat_id=channel_id,
                text=build_red_text(),
                reply_to_message_id=sess.confirm_msg_id,
            )
        outcome_txt = "❌ RED (LOSE)."

    set_session(
        bank_current=bank_now,
        gale_level=gale,
        awaiting_outcome=0,
    )
    return outcome_txt


# =========================
# UI MENUS
# =========================
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
        f"🧠 Ventana {len(win)}: ChopRate {chop_rate(win):.2f} | Ties {count_ties(win)}/{len(win)}\n\n"
        f"{render_roadmap(seq)}\n"
    )

    if sess.is_active:
        txt += (
            f"\n\n🏦 Sesión activa | Banca: {sess.bank_current:.0f} | Base: {sess.base_bet:.0f} | Gale: {sess.gale_level}/{MAX_GALE}\n"
            f"🎯 {session_limits_text(sess)}\n"
            f"🧯 Anti-tilt cooldown: {sess.danger_cooldown}"
        )
    return txt


# =========================
# HANDLERS
# =========================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sess = get_session()
    await update.message.reply_text(
        "🤖 Bac Bo Bot (Estilo AnaPrime)\n"
        "✅ Registras resultados y el bot maneja:\n"
        "- Posible entrada automática\n"
        "- Entrada confirmada\n"
        "- GREEN/RED/TIE respondiendo a la confirmada\n"
        "- Roadmap 6xN\n\n"
        "🎰 Fichas Evolution: 5k/10k/25k/125k/500k/2.5M",
        reply_markup=home_menu(sess),
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

        # bajar anti-tilt cooldown si aplica
        sess_now = get_session()
        if sess_now.danger_cooldown > 0:
            set_session(danger_cooldown=max(0, sess_now.danger_cooldown - 1))

        # ✅ resolver apuesta pendiente y mandar GREEN/RED/TIE al canal (reply)
        outcome_txt = await settle_and_post(context.application, result)

        # stop/take
        sess_after = get_session()
        stop_hit = check_stop_take(sess_after)
        if stop_hit == "STOP_LOSS":
            final_bank = sess_after.bank_current
            stop_session()
            txt = f"⛔ STOP LOSS alcanzado. Sesión cerrada.\nBanca final: {final_bank:.0f}"
            await safe_edit(q, txt, reply_markup=home_menu(get_session()))
            return
        if stop_hit == "TAKE_PROFIT":
            final_bank = sess_after.bank_current
            stop_session()
            txt = f"✅ TAKE PROFIT alcanzado. Sesión cerrada.\nBanca final: {final_bank:.0f}"
            await safe_edit(q, txt, reply_markup=home_menu(get_session()))
            return

        # ahora evaluamos recomendación y manejamos posible/confirm
        seq = get_last_results(300)
        sess2 = get_session()
        action, detail = decide_action(seq, sess2)

        # Si hay posible y ya no aplica, borrarla
        await maybe_clear_possible_if_needed(context.application)

        # Si hay apuesta recomendada, disparamos posible automática
        if action in ("BET_P", "BET_B"):
            await maybe_send_possible(context.application)

        roadmap = render_roadmap(seq)
        header = f"✅ Guardado: {ball(result)} ({result})"
        parts = [header]
        if outcome_txt:
            parts.append(outcome_txt)
        parts.append(f"🧠 {detail}")
        parts.append(roadmap)

        await safe_edit(q, "\n\n".join(parts), reply_markup=home_menu(get_session()))
        return

    if data == "menu_reco":
        seq = get_last_results(300)
        sess = get_session()
        action, detail = decide_action(seq, sess)

        # mostrar en el bot interno + roadmap
        txt = f"🧠 {detail}\n\n{render_roadmap(seq)}"

        # también maneja posible automática
        await maybe_clear_possible_if_needed(context.application)
        if action in ("BET_P", "BET_B"):
            await maybe_send_possible(context.application)

        await safe_edit(q, txt, reply_markup=home_menu(get_session()))
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
            "Para iniciar sesión envía: /bank <monto>\nEj: /bank 300000\n\n"
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
        f"Riesgo: {BASE_BET_PCT:.1f}% (redondeado a ficha)\n"
        f"Apuesta base: {sess.base_bet:.0f}\n"
        f"Max gale: {MAX_GALE}\n"
        f"🎯 {session_limits_text(sess)}",
        reply_markup=home_menu(sess),
    )


# =========================
# FLASK (KEEP-ALIVE ENDPOINT)
# =========================
flask_app = Flask(__name__)


@flask_app.get("/")
def home():
    return "OK - Bacbo bot activo ✅", 200


def run_flask():
    port = int(os.getenv("PORT", "10000"))
    flask_app.run(host="0.0.0.0", port=port)


# =========================
# MAIN ASYNC (NO LOOP ERRORS)
# =========================
async def main_async():
    init_db()

    token = os.getenv("BOT_TOKEN")
    channel_id = os.getenv("CHANNEL_ID")

    if not token:
        raise RuntimeError("Falta BOT_TOKEN en Render (Environment Variables).")
    if not channel_id:
        raise RuntimeError("Falta CHANNEL_ID en Render (Environment Variables).")

    # Telegram necesita int para chat_id
    channel_id_int = int(channel_id)

    # Flask en hilo aparte
    from threading import Thread
    Thread(target=run_flask, daemon=True).start()

    request = HTTPXRequest(
        connect_timeout=20.0,
        read_timeout=20.0,
        write_timeout=20.0,
        pool_timeout=20.0,
    )

    app = Application.builder().token(token).request(request).build()

    # Guardamos channel_id para usarlo en cualquier parte
    app.bot_data["CHANNEL_ID"] = channel_id_int
    app.bot_data["possible_task"] = None

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("bank", bank_cmd))
    app.add_handler(CallbackQueryHandler(on_click))
    app.add_error_handler(error_handler)

    logger.info("✅ Bot iniciado (polling) ...")

    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)

    # mantener vivo el proceso
    await asyncio.Event().wait()


def main():
    asyncio.run(main_async())


if __name__ == "__main__":
    main()