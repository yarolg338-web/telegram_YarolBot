 import sqlite3
import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional, Tuple
import os
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest, TimedOut, NetworkError
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
from telegram.request import HTTPXRequest

DB_PATH = "bacbo.db"

# ===== Logging =====
logging.basicConfig(level=logging.INFO)

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    err = context.error
    # Timeouts/red: no son bugs, solo internet/telegram lento
    if isinstance(err, (TimedOut, NetworkError)):
        logging.warning("Telegram timeout/network error (se ignora): %s", err)
        return
    logging.exception("Exception while handling an update:", exc_info=err)

# ====== Configuración ======
WINDOW_N = 30               # ventana para lectura
MAX_GALE = 1                # máximo 1 gale
STOP_LOSS_PCT = 10          # -10% cierra sesión
TAKE_PROFIT_PCT = 5         # +5% cierra sesión

# % de banca que quieres arriesgar por señal (se redondea a ficha Evolution)
BASE_BET_PCT = 2.0

# Evolution chips (según tu imagen)
ALLOWED_BETS = [5000, 10000, 25000, 125000, 500000, 2500000]

TIE_AVOID_THRESHOLD = 3     # si hay >=3 ties en ventana, no apostar

# Anti-tilt
DANGER_COOLDOWN_ROUNDS = 3  # cuando la mesa es peligrosa, bloquea X rondas

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
        last_reco TEXT DEFAULT NULL
    )
    """)
    cur.execute("INSERT OR IGNORE INTO session (id) VALUES (1)")
    con.commit()

    # --- Migración suave: agregar columnas si no existen ---
    cur.execute("PRAGMA table_info(session)")
    cols = {row[1] for row in cur.fetchall()}

    def add_col_if_missing(name: str, ddl: str):
        if name not in cols:
            cur.execute(ddl)

    add_col_if_missing("pending_side", "ALTER TABLE session ADD COLUMN pending_side TEXT DEFAULT NULL")
    add_col_if_missing("pending_bet", "ALTER TABLE session ADD COLUMN pending_bet REAL NOT NULL DEFAULT 0")
    add_col_if_missing("awaiting_outcome", "ALTER TABLE session ADD COLUMN awaiting_outcome INTEGER NOT NULL DEFAULT 0")
    add_col_if_missing("danger_cooldown", "ALTER TABLE session ADD COLUMN danger_cooldown INTEGER NOT NULL DEFAULT 0")

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

def get_session() -> SessionState:
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("""
        SELECT is_active, bank_start, bank_current, base_bet, gale_level, last_reco,
               pending_side, pending_bet, awaiting_outcome,
               COALESCE(danger_cooldown, 0)
        FROM session WHERE id=1
    """)
    row = cur.fetchone()
    con.close()
    return SessionState(
        bool(row[0]), float(row[1]), float(row[2]), float(row[3]), int(row[4]), row[5],
        row[6], float(row[7]), bool(row[8]), int(row[9])
    )

def set_session(**kwargs):
    keys = []
    vals = []
    for k, v in kwargs.items():
        keys.append(f"{k}=?")
        vals.append(v)
    if not keys:
        return
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute(f"UPDATE session SET {', '.join(keys)} WHERE id=1", vals)
    con.commit()
    con.close()

# ====== Chips / Redondeo Evolution ======
def round_to_allowed(amount: float) -> float:
    """
    Redondea HACIA ARRIBA al siguiente chip permitido.
    Ej: 6400 -> 10000
    """
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
        danger_cooldown=0
    )

def stop_session():
    set_session(
        is_active=0,
        gale_level=0,
        last_reco=None,
        pending_side=None,
        pending_bet=0,
        awaiting_outcome=0,
        danger_cooldown=0
    )

def calc_next_bet(sess: SessionState) -> float:
    """
    Gale por escalón (Evolution):
    - gale 0: base_bet
    - gale 1: siguiente chip
    """
    base = float(sess.base_bet)
    # por si acaso, si base no está exacta en lista, la ajustamos
    base = round_to_allowed(base)

    if base not in ALLOWED_BETS:
        # seguridad extra
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
    """
    Resuelve la apuesta pendiente con el resultado real que el usuario registra.
    """
    if not (sess.is_active and sess.awaiting_outcome and sess.pending_side in ("P", "B") and sess.pending_bet > 0):
        return sess, ""

    side = sess.pending_side
    bet = float(sess.pending_bet)
    bank = float(sess.bank_current)
    gale = int(sess.gale_level)

    if actual_result == "T":
        outcome_txt = "🟡 Resultado fue TIE. Push (banca no cambia)."
        # banca y gale igual
    elif actual_result == side:
        bank += bet
        gale = 0
        outcome_txt = f"✅ WIN automático ({'🔵 P' if side=='P' else '🔴 B'}). +{bet:.0f}"
    else:
        bank -= bet
        if gale < MAX_GALE:
            gale += 1
        else:
            gale = 0
        outcome_txt = f"❌ LOSE automático ({'🔵 P' if side=='P' else '🔴 B'}). -{bet:.0f}"

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

# ====== Detector mesa peligrosa (ANTI-TILT) ======
def is_danger_table(seq: List[str]) -> Tuple[bool, str]:
    """
    Detecta caos estadístico en la ventana:
    - ties altos
    - chop muy alto + rachas cortas
    - chop "ruidoso" (cerca a 0.5) + sin racha clara
    Devuelve (danger, motivo)
    """
    if len(seq) < 12:
        return False, ""

    win = seq[-WINDOW_N:] if len(seq) >= WINDOW_N else seq[:]
    ties = count_ties(win)
    cr = chop_rate(win)
    streak_side, streak_len = current_streak(win)

    # 1) Muchos ties
    if ties >= 4:
        return True, f"Muchos TIE en ventana ({ties}/{len(win)})."

    # 2) Chop extremo + sin racha
    if cr >= 0.75 and streak_len <= 2:
        return True, f"Chop extremo (ChopRate {cr:.2f}) y sin racha clara."

    # 3) Mesa ruidosa (0.45-0.55) y racha corta => “random”
    if 0.45 <= cr <= 0.55 and streak_len <= 2 and ties >= 2:
        return True, f"Mesa muy ruidosa (ChopRate {cr:.2f}) con TIE frecuentes."

    return False, ""

def decide_action(seq: List[str], sess: SessionState) -> Tuple[str, str]:
    """
    Devuelve (accion, detalle)
    accion: NO_BET / BET_P / BET_B
    """
    if len(seq) < 10:
        return "NO_BET", "Aún hay pocas rondas (mínimo 10) para lectura."

    # Anti-tilt cooldown activo
    if sess.danger_cooldown > 0:
        return "NO_BET", f"ANTI-TILT activo: espera {sess.danger_cooldown} ronda(s) para re-evaluar."

    # Detector mesa peligrosa
    danger, why = is_danger_table(seq)
    if danger:
        # bloquea X rondas y limpia apuesta pendiente
        set_session(danger_cooldown=DANGER_COOLDOWN_ROUNDS, pending_side=None, pending_bet=0, awaiting_outcome=0)
        return "NO_BET", f"🚨 Mesa peligrosa detectada: {why} (bloqueo {DANGER_COOLDOWN_ROUNDS} rondas)"

    last = seq[-1]
    win = seq[-WINDOW_N:]
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

def format_reco_and_arm_next_bet(seq: List[str], sess: SessionState) -> str:
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

    side = "P" if action == "BET_P" else "B"
    side_emoji = "🔵 PLAYER" if side == "P" else "🔴 BANKER"

    if sess.is_active:
        bet = calc_next_bet(sess)
        set_session(pending_side=side, pending_bet=bet, awaiting_outcome=1, last_reco=side)
        bet_line = f"\n💵 Próxima apuesta: {bet:.0f} a {side_emoji} (pendiente)"
    else:
        bet_line = "\nℹ️ Inicia sesión con /bank <monto> para controlar banca."

    return (
        f"✅ APOSTAR\n"
        f"➡️ Recomendación próxima ronda: {side_emoji}\n"
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
        [InlineKeyboardButton("🟡 TIE (T)", callback_data="add_T")],
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
        f"🟡 Tie: {t} ({t/total*100:.1f}%)\n"
        f"🧠 Ventana {len(win)}: ChopRate {chop_rate(win):.2f} | Ties {count_ties(win)}/{len(win)}\n"
    )

    if sess.is_active:
        txt += (
            f"\n🏦 Sesión activa | Banca: {sess.bank_current:.0f} | Base: {sess.base_bet:.0f} | Gale: {sess.gale_level}/{MAX_GALE}\n"
            f"🎯 {session_limits_text(sess)}\n"
            f"🧯 Anti-tilt cooldown: {sess.danger_cooldown}"
        )
        if sess.awaiting_outcome and sess.pending_side in ("P", "B"):
            side_emoji = "🔵 P" if sess.pending_side == "P" else "🔴 B"
            txt += f"\n⏳ Apuesta pendiente próxima ronda: {side_emoji} por {sess.pending_bet:.0f}"
    return txt

# ===== Handlers =====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sess = get_session()
    await update.message.reply_text(
        "🤖 Bac Bo Bot (Modo PRO)\n✅ Registras resultados y yo mantengo el ritmo.\n"
        "🎰 Fichas Evolution: 5k/10k/25k/125k/500k/2.5M\n"
        "🧯 Anti-tilt: bloquea mesa peligrosa automáticamente.",
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
        result = data.split("_", 1)[1]  # P/B/T
        add_round(result)

        # bajar cooldown anti-tilt en cada ronda registrada
        sess_now = get_session()
        if sess_now.danger_cooldown > 0:
            set_session(danger_cooldown=max(0, sess_now.danger_cooldown - 1))

        # resolver apuesta pendiente (si había)
        sess_before = get_session()
        sess_after, outcome_txt = settle_pending_bet(sess_before, result)

        # stop loss / take profit
        stop_hit = check_stop_take(sess_after)
        if stop_hit == "STOP_LOSS":
            final_bank = sess_after.bank_current
            stop_session()
            txt = f"⛔ STOP LOSS alcanzado. Sesión cerrada.\nBanca final: {final_bank:.0f}\n\nRegistra resultados si quieres seguir observando."
            await safe_edit(q, txt, reply_markup=home_menu(get_session()))
            return
        if stop_hit == "TAKE_PROFIT":
            final_bank = sess_after.bank_current
            stop_session()
            txt = f"✅ TAKE PROFIT alcanzado. Sesión cerrada.\nBanca final: {final_bank:.0f}\n\nRegistra resultados si quieres seguir observando."
            await safe_edit(q, txt, reply_markup=home_menu(get_session()))
            return

        # recomendación próxima ronda
        seq = get_last_results(300)
        sess_now2 = get_session()
        reco_txt = format_reco_and_arm_next_bet(seq, sess_now2)

        header = f"✅ Guardado: {result}"
        parts = [header]
        if outcome_txt:
            parts.append(outcome_txt)
        parts.append(reco_txt)

        await safe_edit(q, "\n\n".join(parts), reply_markup=home_menu(get_session()))
        return

    if data == "menu_reco":
        seq = get_last_results(300)
        sess = get_session()
        txt = format_reco_and_arm_next_bet(seq, sess)
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
        f"Max gale: {MAX_GALE} (por escalón)\n"
        f"🎯 {session_limits_text(sess)}",
        reply_markup=home_menu(sess),
    )


async def run():
    init_db()

    TOKEN = os.getenv("BOT_TOKEN")
    if not TOKEN:
        raise RuntimeError("Falta BOT_TOKEN en Render")

    request = HTTPXRequest(
        connect_timeout=20.0,
        read_timeout=20.0,
        write_timeout=20.0,
        pool_timeout=20.0,
    )

    app = Application.builder().token(TOKEN).request(request).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("bank", bank_cmd))
    app.add_handler(CallbackQueryHandler(on_click))
    app.add_error_handler(error_handler)

    print("✅ Bot iniciado correctamente...")

    await app.run_polling(drop_pending_updates=True)
    
    if __name__ == "__main__":
    import asyncio

    loop = asyncio.get_event_loop()
    loop.create_task(run())
    loop.run_forever()


