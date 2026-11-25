from flask import Flask, request, jsonify, render_template, session
from werkzeug.security import generate_password_hash, check_password_hash
import sqlite3
import json
import random
import os
import re
from datetime import datetime, timezone
from functools import wraps
from urllib.parse import urlparse

try:
    import psycopg2
    from psycopg2.extras import RealDictCursor
except ImportError:
    psycopg2 = None
    RealDictCursor = None

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "darkluck-secret-dev-key")
SPONSORS_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin123")
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()

# Sécurité cookie de session en production HTTPS
if os.environ.get("FLASK_ENV") == "production" or DATABASE_URL:
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    if os.environ.get("FORCE_HTTPS", "1") == "1":
        app.config["SESSION_COOKIE_SECURE"] = True

DB_PATH = "jeu.db"
MIN_NUM = 3
MAX_NUM = 65
MIN_NUM_TICKET = 2
MAX_NUM_TICKET = 7
MISE_MIN = 1.0
MISE_MAX = 15.0
FACTEUR = 100
NB_BOULES_TIREES = 5
TICKETS_REQUIS_PROMO = 15
BONUS_FILLEUL = 1.00
BONUS_PARRAIN = 0.34
MAX_PARRAINAGES_JOUR = 2


def is_postgres() -> bool:
    return bool(DATABASE_URL)


class DbCursorWrapper:
    def __init__(self, cur, postgres: bool):
        self._cur = cur
        self._postgres = postgres
        self.lastrowid = getattr(cur, "lastrowid", None)

    def fetchone(self):
        row = self._cur.fetchone()
        if row is None:
            return None
        return dict(row) if self._postgres else row

    def fetchall(self):
        rows = self._cur.fetchall()
        if not rows:
            return []
        return [dict(r) for r in rows] if self._postgres else rows


class DbConnectionWrapper:
    def __init__(self, conn, postgres: bool):
        self._conn = conn
        self._postgres = postgres

    def _adapt_sql(self, sql: str) -> str:
        if not self._postgres:
            return sql
        return sql.replace("?", "%s")

    def execute(self, sql: str, params=()):
        adapted = self._adapt_sql(sql)
        cur = self._conn.cursor(cursor_factory=RealDictCursor) if self._postgres else self._conn.cursor()
        cur.execute(adapted, params)
        wrapped = DbCursorWrapper(cur, self._postgres)
        if self._postgres and adapted.strip().upper().startswith("INSERT") and "RETURNING" not in adapted.upper():
            try:
                cur_id = self._conn.cursor()
                cur_id.execute("SELECT LASTVAL()")
                val = cur_id.fetchone()
                if val:
                    wrapped.lastrowid = val[0]
            except Exception:
                pass
        return wrapped

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type:
            self._conn.rollback()
        else:
            self._conn.commit()
        self._conn.close()


def get_db() -> DbConnectionWrapper:
    if is_postgres():
        if psycopg2 is None:
            raise RuntimeError("psycopg2-binary est requis pour PostgreSQL.")
        url = DATABASE_URL
        if url.startswith("postgres://"):
            url = url.replace("postgres://", "postgresql://", 1)
        parsed = urlparse(url)
        conn = psycopg2.connect(
            dbname=parsed.path.lstrip("/"),
            user=parsed.username,
            password=parsed.password,
            host=parsed.hostname,
            port=parsed.port or 5432,
            sslmode="require" if "localhost" not in (parsed.hostname or "") and "127.0.0.1" not in (parsed.hostname or "") else "prefer"
        )
        return DbConnectionWrapper(conn, True)

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return DbConnectionWrapper(conn, False)


def now_gmt() -> datetime:
    return datetime.now(timezone.utc)


def row_get(row, key, default=None):
    if row is None:
        return default
    try:
        return row[key]
    except (IndexError, KeyError, TypeError):
        return default


def init_db():
    pk = "SERIAL PRIMARY KEY" if is_postgres() else "INTEGER PRIMARY KEY AUTOINCREMENT"
    with get_db() as conn:
        conn.execute(f"""
            CREATE TABLE IF NOT EXISTS joueurs (
                id {pk},
                email TEXT NOT NULL UNIQUE,
                pseudo TEXT NOT NULL,
                password_hash TEXT NOT NULL DEFAULT '',
                solde_retirable REAL NOT NULL DEFAULT 0.0,
                solde_bonus REAL NOT NULL DEFAULT 0.0,
                code_promo TEXT UNIQUE,
                parraine_par_id INTEGER,
                created_at TEXT NOT NULL
            )
        """)
        if not is_postgres():
            cols = [c["name"] for c in conn.execute("PRAGMA table_info(joueurs)").fetchall()]
            if "password_hash" not in cols:
                conn.execute("ALTER TABLE joueurs ADD COLUMN password_hash TEXT NOT NULL DEFAULT ''")

        conn.execute(f"""
            CREATE TABLE IF NOT EXISTS parrainages (
                id {pk},
                parrain_id INTEGER NOT NULL,
                filleul_id INTEGER NOT NULL UNIQUE,
                bonus_parrain REAL NOT NULL,
                bonus_filleul REAL NOT NULL,
                date_jour TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        conn.execute(f"""
            CREATE TABLE IF NOT EXISTS transactions (
                id {pk},
                joueur_id INTEGER NOT NULL,
                type_tx TEXT NOT NULL,
                montant REAL NOT NULL,
                statut TEXT NOT NULL DEFAULT 'EN_ATTENTE',
                note TEXT DEFAULT '',
                created_at TEXT NOT NULL,
                processed_at TEXT
            )
        """)
        conn.execute(f"""
            CREATE TABLE IF NOT EXISTS tirages (
                id {pk},
                numeros_tirage TEXT NOT NULL,
                mode_tirage TEXT NOT NULL DEFAULT 'AUTO',
                pool_securite INTEGER NOT NULL,
                total_mises REAL NOT NULL,
                total_gains REAL NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        conn.execute(f"""
            CREATE TABLE IF NOT EXISTS tickets (
                id {pk},
                code_ticket TEXT NOT NULL,
                joueur_id INTEGER,
                joueur TEXT NOT NULL,
                numeros TEXT NOT NULL,
                mise REAL NOT NULL,
                part_bonus REAL NOT NULL DEFAULT 0.0,
                part_retirable REAL NOT NULL DEFAULT 0.0,
                liaisons_jouees INTEGER NOT NULL,
                liaisons_gagnantes INTEGER DEFAULT 0,
                bons_numeros TEXT DEFAULT '[]',
                gain REAL DEFAULT 0.0,
                statut TEXT NOT NULL DEFAULT 'EN_ATTENTE',
                tirage_id INTEGER,
                created_at TEXT NOT NULL
            )
        """)
        conn.commit()


def sponsors_requis(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("is_sponsor"):
            return jsonify({"error": "Accès réservé aux Sponsors."}), 401
        return f(*args, **kwargs)
    return wrapper


def joueur_requis(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("joueur_id"):
            return jsonify({"error": "Veuillez vous identifier par e-mail."}), 401
        return f(*args, **kwargs)
    return wrapper


def email_valide(email: str) -> bool:
    return bool(re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email))


def mot_de_passe_valide(mdp: str) -> tuple[bool, str]:
    if not isinstance(mdp, str) or len(mdp) < 8:
        return False, "Le mot de passe doit contenir au moins 8 caractères."
    if not re.search(r"[A-Z]", mdp):
        return False, "Le mot de passe doit contenir au moins une lettre majuscule."
    if not re.search(r"[a-z]", mdp):
        return False, "Le mot de passe doit contenir au moins une lettre minuscule."
    if not re.search(r"[0-9]", mdp):
        return False, "Le mot de passe doit contenir au moins un chiffre."
    if not re.search(r"[^A-Za-z0-9]", mdp):
        return False, "Le mot de passe doit contenir au moins un caractère spécial."
    return True, ""


def formater_date_gmt(raw_dt: str) -> str:
    if not raw_dt:
        return ""
    try:
        dt = datetime.fromisoformat(raw_dt)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = dt.astimezone(timezone.utc)
        return dt.strftime("%Y-%m-%d %H:%M:%S GMT")
    except Exception:
        return f"{raw_dt} GMT"


def compter_tickets_achetes(conn, joueur_id: int) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS cnt FROM tickets WHERE joueur_id = ? AND statut != 'ANNULE'",
        (joueur_id,)
    ).fetchone()
    return int(row_get(row, "cnt", 0))


def formater_joueur(conn, row) -> dict:
    jid = row_get(row, "id")
    nb_tickets = compter_tickets_achetes(conn, jid)
    today_gmt = now_gmt().strftime("%Y-%m-%d")
    parrainages_jour = conn.execute(
        "SELECT COUNT(*) AS cnt FROM parrainages WHERE parrain_id = ? AND date_jour = ?",
        (jid, today_gmt)
    ).fetchone()
    nb_parr_jour = int(row_get(parrainages_jour, "cnt", 0))
    solde_ret = round(float(row_get(row, "solde_retirable", 0.0)), 2)
    solde_bon = round(float(row_get(row, "solde_bonus", 0.0)), 2)
    code_promo = row_get(row, "code_promo")

    return {
        "id": jid,
        "email": row_get(row, "email", ""),
        "pseudo": row_get(row, "pseudo", ""),
        "solde_retirable": solde_ret,
        "solde_bonus": solde_bon,
        "solde_total": round(solde_ret + solde_bon, 2),
        "code_promo": code_promo,
        "tickets_achetes": nb_tickets,
        "peut_creer_promo": nb_tickets >= TICKETS_REQUIS_PROMO and not code_promo,
        "parrainages_aujourdhui": nb_parr_jour,
        "parrainages_restants_jour": max(0, MAX_PARRAINAGES_JOUR - nb_parr_jour)
    }


def calculer_liaisons(nb_numeros: int) -> int:
    if nb_numeros < 2:
        return 0
    return (nb_numeros * (nb_numeros - 1)) // 2


def valider_ticket(numeros: list[int], mise: float) -> tuple[bool, str]:
    if mise < MISE_MIN or mise > MISE_MAX:
        return False, f"La mise doit être comprise entre {MISE_MIN:g} et {MISE_MAX:g} USDT."
    if not (MIN_NUM_TICKET <= len(numeros) <= MAX_NUM_TICKET):
        return False, f"Il faut choisir entre {MIN_NUM_TICKET} et {MAX_NUM_TICKET} numéros."
    if len(set(numeros)) != len(numeros):
        return False, "Les numéros doivent être uniques."
    if not all(isinstance(n, int) and MIN_NUM <= n <= MAX_NUM for n in numeros):
        return False, f"Les numéros doivent être compris entre {MIN_NUM} et {MAX_NUM}."
    return True, ""


def valider_tirage_manuel(numeros: list[int]) -> tuple[bool, str]:
    if not isinstance(numeros, list) or len(numeros) != NB_BOULES_TIREES:
        return False, f"Il faut saisir exactement {NB_BOULES_TIREES} numéros gagnants."
    if len(set(numeros)) != NB_BOULES_TIREES:
        return False, "Les 5 numéros gagnants doivent être différents."
    if not all(isinstance(n, int) and MIN_NUM <= n <= MAX_NUM for n in numeros):
        return False, f"Chaque numéro doit être compris entre {MIN_NUM} et {MAX_NUM}."
    return True, ""


def generer_tirage_intelligent(tickets: list[dict], pool_securite: int = 10) -> list[int]:
    numeros_joues_set = set()
    exposition = {n: 0.0 for n in range(MIN_NUM, MAX_NUM + 1)}

    for ticket in tickets:
        nums = ticket["numeros"]
        mise = float(ticket["mise"])
        nb_liaisons = calculer_liaisons(len(nums))
        if nb_liaisons == 0:
            continue
        mise_par_liaison = mise / nb_liaisons
        poids_numero = mise_par_liaison * (len(nums) - 1)
        for n in nums:
            if MIN_NUM <= n <= MAX_NUM:
                numeros_joues_set.add(n)
                exposition[n] += poids_numero

    tous_les_numeros = list(range(MIN_NUM, MAX_NUM + 1))
    non_joues = [n for n in tous_les_numeros if n not in numeros_joues_set]

    tirage_set = set()
    if non_joues:
        tirage_set.add(random.choice(non_joues))

    restants_tries = [n for n in tous_les_numeros if n not in tirage_set]
    random.shuffle(restants_tries)
    restants_tries.sort(key=lambda n: exposition[n])

    taille_pool = max(NB_BOULES_TIREES - len(tirage_set), min(pool_securite, len(restants_tries)))
    candidats = restants_tries[:taille_pool]

    manquants = NB_BOULES_TIREES - len(tirage_set)
    for n in random.sample(candidats, manquants):
        tirage_set.add(n)

    return sorted(list(tirage_set))


def evaluer_ticket(ticket: dict, tirage: list[int]) -> dict:
    nums_joues = set(ticket["numeros"])
    bons_numeros = sorted(list(nums_joues.intersection(set(tirage))))
    m = len(bons_numeros)
    liaisons_jouees = calculer_liaisons(len(nums_joues))
    liaisons_gagnantes = calculer_liaisons(m)
    mise_unitaire = float(ticket["mise"]) / liaisons_jouees if liaisons_jouees else 0.0
    gain = round(mise_unitaire * liaisons_gagnantes * FACTEUR, 2)
    return {
        "id": ticket["id"],
        "joueur_id": ticket.get("joueur_id"),
        "code_ticket": ticket["code_ticket"],
        "joueur": ticket["joueur"],
        "numeros": ticket["numeros"],
        "mise": float(ticket["mise"]),
        "bons_numeros": bons_numeros,
        "liaisons_jouees": liaisons_jouees,
        "liaisons_gagnantes": liaisons_gagnantes,
        "gain_usdt": gain,
        "statut": "GAGNE" if gain > 0 else "PERDU"
    }


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


@app.route("/")
def accueil():
    return render_template("index.html")


@app.route("/api/grille-meta", methods=["GET"])
def grille_meta():
    jour_gmt = now_gmt().day
    compteur = {n: 0 for n in range(MIN_NUM, MAX_NUM + 1)}
    with get_db() as conn:
        rows = conn.execute("SELECT numeros_tirage FROM tirages").fetchall()
    for r in rows:
        try:
            nums = json.loads(row_get(r, "numeros_tirage", "[]"))
            for n in nums:
                if isinstance(n, int) and MIN_NUM <= n <= MAX_NUM:
                    compteur[n] += 1
        except Exception:
            pass

    max_occ = max(compteur.values()) if compteur else 0
    plus_tires = [n for n, c in compteur.items() if max_occ > 0 and c == max_occ]

    return jsonify({
        "jour_gmt": jour_gmt,
        "plus_tires": plus_tires,
        "max_occurrences": max_occ
    })


@app.route("/api/joueur/session", methods=["GET"])
def joueur_session():
    jid = session.get("joueur_id")
    if not jid:
        return jsonify({"joueur": None})
    with get_db() as conn:
        row = conn.execute("SELECT * FROM joueurs WHERE id = ?", (jid,)).fetchone()
        if not row:
            session.pop("joueur_id", None)
            return jsonify({"joueur": None})
        return jsonify({"joueur": formater_joueur(conn, row)})


@app.route("/api/joueur/login", methods=["POST"])
def joueur_login():
    data = request.get_json(silent=True) or {}
    email = str(data.get("email", "")).strip().lower()
    password = str(data.get("password", ""))

    if not email_valide(email):
        return jsonify({"error": "Adresse e-mail invalide."}), 400
    if not password:
        return jsonify({"error": "Veuillez saisir votre mot de passe."}), 400

    with get_db() as conn:
        joueur = conn.execute("SELECT * FROM joueurs WHERE email = ?", (email,)).fetchone()
        if not joueur:
            return jsonify({"error": "Aucun compte trouvé avec cet e-mail. Passez sur l'onglet Inscription."}), 404

        pw_hash = row_get(joueur, "password_hash", "")
        if not pw_hash or not check_password_hash(pw_hash, password):
            return jsonify({"error": "Mot de passe incorrect."}), 401

        session["joueur_id"] = row_get(joueur, "id")
        return jsonify({"joueur": formater_joueur(conn, joueur)})


@app.route("/api/joueur/register", methods=["POST"])
def joueur_register():
    data = request.get_json(silent=True) or {}
    email = str(data.get("email", "")).strip().lower()
    password = str(data.get("password", ""))
    pseudo = str(data.get("pseudo", "")).strip()
    code_promo_saisi = str(data.get("code_promo", "")).strip().upper()
    now_dt = now_gmt()
    now_iso = now_dt.isoformat()
    today_gmt = now_dt.strftime("%Y-%m-%d")

    if not email_valide(email):
        return jsonify({"error": "Adresse e-mail invalide."}), 400

    mdp_ok, mdp_err = mot_de_passe_valide(password)
    if not mdp_ok:
        return jsonify({"error": mdp_err}), 400

    with get_db() as conn:
        existant = conn.execute("SELECT id FROM joueurs WHERE email = ?", (email,)).fetchone()
        if existant:
            return jsonify({"error": "Cet e-mail est déjà inscrit. Utilisez l'onglet Connexion."}), 409

        parrain = None
        bonus_filleul = 0.0
        if code_promo_saisi:
            parrain = conn.execute(
                "SELECT * FROM joueurs WHERE UPPER(code_promo) = ?",
                (code_promo_saisi,)
            ).fetchone()
            if not parrain:
                return jsonify({"error": "Code promo introuvable."}), 400

            parrain_id = row_get(parrain, "id")
            parr_count = conn.execute(
                "SELECT COUNT(*) AS cnt FROM parrainages WHERE parrain_id = ? AND date_jour = ?",
                (parrain_id, today_gmt)
            ).fetchone()
            if int(row_get(parr_count, "cnt", 0)) >= MAX_PARRAINAGES_JOUR:
                return jsonify({"error": f"Ce parrain a déjà atteint la limite de {MAX_PARRAINAGES_JOUR} parrainages aujourd'hui."}), 400
            bonus_filleul = BONUS_FILLEUL

        pseudo_final = pseudo or email.split("@")[0]
        pw_hash = generate_password_hash(password)

        cur = conn.execute(
            """
            INSERT INTO joueurs (email, pseudo, password_hash, solde_retirable, solde_bonus, parraine_par_id, created_at)
            VALUES (?, ?, ?, 0.0, ?, ?, ?)
            """,
            (email, pseudo_final, pw_hash, bonus_filleul, row_get(parrain, "id"), now_iso)
        )
        filleul_id = cur.lastrowid

        if parrain:
            parrain_id = row_get(parrain, "id")
            conn.execute(
                "UPDATE joueurs SET solde_bonus = ROUND(solde_bonus + ?, 2) WHERE id = ?",
                (BONUS_PARRAIN, parrain_id)
            )
            conn.execute(
                """
                INSERT INTO parrainages (parrain_id, filleul_id, bonus_parrain, bonus_filleul, date_jour, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (parrain_id, filleul_id, BONUS_PARRAIN, BONUS_FILLEUL, today_gmt, now_iso)
            )

        conn.commit()
        nouveau_row = conn.execute("SELECT * FROM joueurs WHERE id = ?", (filleul_id,)).fetchone()
        session["joueur_id"] = filleul_id
        return jsonify({"joueur": formater_joueur(conn, nouveau_row)}), 201


@app.route("/api/joueur/logout", methods=["POST"])
def joueur_logout():
    session.pop("joueur_id", None)
    return jsonify({"ok": True})


@app.route("/api/joueur/creer-promo", methods=["POST"])
@joueur_requis
def creer_code_promo():
    jid = session["joueur_id"]
    data = request.get_json(silent=True) or {}
    code_perso = str(data.get("code_promo", "")).strip().upper()

    with get_db() as conn:
        joueur = conn.execute("SELECT * FROM joueurs WHERE id = ?", (jid,)).fetchone()
        if not joueur:
            return jsonify({"error": "Joueur introuvable."}), 404
        if row_get(joueur, "code_promo"):
            return jsonify({"error": f"Vous possédez déjà le code promo {row_get(joueur, 'code_promo')}."}), 400

        nb_tickets = compter_tickets_achetes(conn, jid)
        if nb_tickets < TICKETS_REQUIS_PROMO:
            return jsonify({
                "error": f"Il faut acheter au moins {TICKETS_REQUIS_PROMO} tickets pour créer un code promo (actuel : {nb_tickets})."
            }), 400

        if not code_perso:
            base_pseudo = str(row_get(joueur, "pseudo", "DARK")).upper()
            base = re.sub(r"[^A-Z0-9]", "", base_pseudo)[:6] or "DARK"
            code_perso = f"{base}{random.randint(100, 999)}"
        else:
            if not re.match(r"^[A-Z0-9]{4,15}$", code_perso):
                return jsonify({"error": "Le code promo doit contenir 4 à 15 caractères alphanumériques (A-Z, 0-9)."}), 400

        doublon = conn.execute("SELECT id FROM joueurs WHERE UPPER(code_promo) = ?", (code_perso,)).fetchone()
        if doublon:
            return jsonify({"error": "Ce code promo est déjà utilisé."}), 400

        conn.execute("UPDATE joueurs SET code_promo = ? WHERE id = ?", (code_perso, jid))
        conn.commit()
        updated = conn.execute("SELECT * FROM joueurs WHERE id = ?", (jid,)).fetchone()
        return jsonify({"joueur": formater_joueur(conn, updated)})


@app.route("/api/transactions", methods=["GET", "POST"])
@joueur_requis
def transactions_joueur():
    jid = session["joueur_id"]
    if request.method == "GET":
        with get_db() as conn:
            rows = conn.execute(
                "SELECT * FROM transactions WHERE joueur_id = ? ORDER BY id DESC LIMIT 20",
                (jid,)
            ).fetchall()
        return jsonify({
            "transactions": [
                {
                    "id": row_get(r, "id"),
                    "type_tx": row_get(r, "type_tx"),
                    "montant": row_get(r, "montant"),
                    "statut": row_get(r, "statut"),
                    "note": row_get(r, "note", ""),
                    "date_gmt": formater_date_gmt(row_get(r, "created_at", ""))
                }
                for r in rows
            ]
        })

    data = request.get_json(silent=True) or {}
    type_tx = str(data.get("type_tx", "")).upper()
    note = str(data.get("note", "")).strip()
    now_iso = now_gmt().isoformat()

    try:
        montant = round(float(data.get("montant", 0)), 2)
    except (TypeError, ValueError):
        return jsonify({"error": "Montant invalide."}), 400

    if type_tx not in ("DEPOT", "RETRAIT"):
        return jsonify({"error": "Type d'opération invalide."}), 400
    if montant <= 0:
        return jsonify({"error": "Le montant doit être supérieur à 0."}), 400

    with get_db() as conn:
        joueur = conn.execute("SELECT * FROM joueurs WHERE id = ?", (jid,)).fetchone()
        if not joueur:
            return jsonify({"error": "Joueur introuvable."}), 404

        if type_tx == "RETRAIT":
            if float(row_get(joueur, "solde_retirable", 0.0)) < montant:
                return jsonify({"error": "Solde retirable insuffisant (les bonus ne sont pas retirables)."}), 400
            conn.execute(
                "UPDATE joueurs SET solde_retirable = ROUND(solde_retirable - ?, 2) WHERE id = ?",
                (montant, jid)
            )

        cur = conn.execute(
            """
            INSERT INTO transactions (joueur_id, type_tx, montant, statut, note, created_at)
            VALUES (?, ?, ?, 'EN_ATTENTE', ?, ?)
            """,
            (jid, type_tx, montant, note, now_iso)
        )
        tx_id = cur.lastrowid
        conn.commit()
        updated = conn.execute("SELECT * FROM joueurs WHERE id = ?", (jid,)).fetchone()

    return jsonify({"id": tx_id, "joueur": formater_joueur(conn, updated)}), 201


@app.route("/api/tickets", methods=["GET", "POST"])
def gerer_tickets():
    if request.method == "GET":
        with get_db() as conn:
            rows = conn.execute(
                "SELECT * FROM tickets WHERE statut = 'EN_ATTENTE' ORDER BY id ASC"
            ).fetchall()
        tickets = [
            {
                "id": row_get(r, "id"),
                "code_ticket": row_get(r, "code_ticket"),
                "joueur_id": row_get(r, "joueur_id"),
                "joueur": row_get(r, "joueur"),
                "numeros": json.loads(row_get(r, "numeros", "[]")),
                "mise": row_get(r, "mise"),
                "liaisons_jouees": row_get(r, "liaisons_jouees"),
                "statut": row_get(r, "statut")
            }
            for r in rows
        ]
        return jsonify({"tickets": tickets})

    jid = session.get("joueur_id")
    if not jid:
        return jsonify({"error": "Connectez-vous avec votre adresse e-mail et mot de passe pour acheter un ticket."}), 401

    data = request.get_json(silent=True) or {}
    numeros = sorted(data.get("numeros", []))
    try:
        mise = round(float(data.get("mise", 0)), 2)
    except (TypeError, ValueError):
        return jsonify({"error": "Mise invalide."}), 400

    valide, message = valider_ticket(numeros, mise)
    if not valide:
        return jsonify({"error": message}), 400

    liaisons = calculer_liaisons(len(numeros))
    now_iso = now_gmt().isoformat()

    with get_db() as conn:
        joueur = conn.execute("SELECT * FROM joueurs WHERE id = ?", (jid,)).fetchone()
        if not joueur:
            return jsonify({"error": "Joueur introuvable."}), 404

        solde_bonus = float(row_get(joueur, "solde_bonus", 0.0))
        solde_retirable = float(row_get(joueur, "solde_retirable", 0.0))
        if round(solde_bonus + solde_retirable, 2) < mise:
            return jsonify({"error": "Solde insuffisant. Effectuez un dépôt ou utilisez un bonus."}), 400

        part_bonus = round(min(solde_bonus, mise), 2)
        part_retirable = round(mise - part_bonus, 2)

        conn.execute(
            """
            UPDATE joueurs
            SET solde_bonus = ROUND(solde_bonus - ?, 2),
                solde_retirable = ROUND(solde_retirable - ?, 2)
            WHERE id = ?
            """,
            (part_bonus, part_retirable, jid)
        )

        pseudo_joueur = row_get(joueur, "pseudo", "Joueur")
        cur = conn.execute(
            """
            INSERT INTO tickets (
                code_ticket, joueur_id, joueur, numeros, mise,
                part_bonus, part_retirable, liaisons_jouees, statut, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'EN_ATTENTE', ?)
            """,
            ("TEMP", jid, pseudo_joueur, json.dumps(numeros), mise, part_bonus, part_retirable, liaisons, now_iso)
        )
        ticket_id = cur.lastrowid
        code_ticket = f"T{ticket_id:03d}"
        conn.execute("UPDATE tickets SET code_ticket = ? WHERE id = ?", (code_ticket, ticket_id))
        conn.commit()

        updated_joueur = conn.execute("SELECT * FROM joueurs WHERE id = ?", (jid,)).fetchone()

    return jsonify({
        "ticket": {
            "id": ticket_id,
            "code_ticket": code_ticket,
            "joueur_id": jid,
            "joueur": pseudo_joueur,
            "numeros": numeros,
            "mise": mise,
            "liaisons_jouees": liaisons,
            "statut": "EN_ATTENTE"
        },
        "joueur": formater_joueur(conn, updated_joueur)
    }), 201


@app.route("/api/tickets/<int:ticket_id>/annuler", methods=["POST"])
@joueur_requis
def annuler_ticket(ticket_id: int):
    jid = session["joueur_id"]
    with get_db() as conn:
        ticket = conn.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
        if not ticket:
            return jsonify({"error": "Ticket introuvable."}), 404
        if row_get(ticket, "joueur_id") != jid:
            return jsonify({"error": "Vous ne pouvez annuler que vos propres tickets."}), 403
        if row_get(ticket, "statut") != "EN_ATTENTE":
            return jsonify({"error": "Seul un ticket en attente peut être annulé."}), 400

        remboursement = round(float(row_get(ticket, "mise", 0.0)) * 0.75, 2)
        conn.execute("UPDATE tickets SET statut = 'ANNULE' WHERE id = ?", (ticket_id,))
        conn.execute(
            "UPDATE joueurs SET solde_retirable = ROUND(solde_retirable + ?, 2) WHERE id = ?",
            (remboursement, jid)
        )
        conn.commit()
        updated_joueur = conn.execute("SELECT * FROM joueurs WHERE id = ?", (jid,)).fetchone()

    return jsonify({
        "remboursement": remboursement,
        "joueur": formater_joueur(conn, updated_joueur)
    })


@app.route("/api/sponsors/status", methods=["GET"])
def sponsors_status():
    return jsonify({"is_sponsor": bool(session.get("is_sponsor"))})


@app.route("/api/sponsors/login", methods=["POST"])
def sponsors_login():
    data = request.get_json(silent=True) or {}
    mot_de_passe = str(data.get("password", ""))
    if mot_de_passe == SPONSORS_PASSWORD:
        session["is_sponsor"] = True
        return jsonify({"is_sponsor": True})
    return jsonify({"error": "Code d'accès Sponsors incorrect."}), 401


@app.route("/api/sponsors/logout", methods=["POST"])
def sponsors_logout():
    session.pop("is_sponsor", None)
    return jsonify({"is_sponsor": False})


@app.route("/api/sponsors/dashboard", methods=["GET"])
@sponsors_requis
def sponsors_dashboard():
    today_gmt = now_gmt().strftime("%Y-%m-%d")
    with get_db() as conn:
        rows_tickets = conn.execute(
            "SELECT * FROM tickets WHERE statut = 'EN_ATTENTE' ORDER BY id ASC"
        ).fetchall()

        tickets = []
        compteur_numeros = {n: 0 for n in range(MIN_NUM, MAX_NUM + 1)}
        for r in rows_tickets:
            nums = json.loads(row_get(r, "numeros", "[]"))
            for n in nums:
                if MIN_NUM <= n <= MAX_NUM:
                    compteur_numeros[n] += 1
            tickets.append({
                "id": row_get(r, "id"),
                "code_ticket": row_get(r, "code_ticket"),
                "joueur": row_get(r, "joueur"),
                "numeros": nums,
                "mise": row_get(r, "mise"),
                "liaisons_jouees": row_get(r, "liaisons_jouees"),
                "statut": row_get(r, "statut")
            })

        top_numeros = sorted(
            [{"numero": n, "occurrences": c} for n, c in compteur_numeros.items() if c > 0],
            key=lambda x: (-x["occurrences"], x["numero"])
        )

        rows_nouveaux = conn.execute(
            "SELECT id, email, pseudo, code_promo, solde_retirable, solde_bonus, created_at FROM joueurs WHERE substr(created_at, 1, 10) = ? ORDER BY id DESC",
            (today_gmt,)
        ).fetchall()
        nouveaux_joueurs = [
            {
                "id": row_get(r, "id"),
                "email": row_get(r, "email"),
                "pseudo": row_get(r, "pseudo"),
                "code_promo": row_get(r, "code_promo"),
                "solde_retirable": row_get(r, "solde_retirable", 0.0),
                "solde_bonus": row_get(r, "solde_bonus", 0.0),
                "date_gmt": formater_date_gmt(row_get(r, "created_at", ""))
            }
            for r in rows_nouveaux
        ]

        rows_tx = conn.execute(
            """
            SELECT t.id, t.joueur_id, j.email, j.pseudo, t.type_tx, t.montant, t.statut, t.note, t.created_at
            FROM transactions t
            JOIN joueurs j ON j.id = t.joueur_id
            ORDER BY CASE WHEN t.statut = 'EN_ATTENTE' THEN 0 ELSE 1 END, t.id DESC
            LIMIT 50
            """
        ).fetchall()
        transactions_list = [
            {
                "id": row_get(r, "id"),
                "joueur_id": row_get(r, "joueur_id"),
                "email": row_get(r, "email"),
                "pseudo": row_get(r, "pseudo"),
                "type_tx": row_get(r, "type_tx"),
                "montant": row_get(r, "montant"),
                "statut": row_get(r, "statut"),
                "note": row_get(r, "note", ""),
                "date_gmt": formater_date_gmt(row_get(r, "created_at", ""))
            }
            for r in rows_tx
        ]

    return jsonify({
        "tickets": tickets,
        "top_numeros": top_numeros,
        "nouveaux_joueurs_jour": nouveaux_joueurs,
        "transactions": transactions_list
    })


@app.route("/api/sponsors/transactions/<int:tx_id>/<action>", methods=["POST"])
@sponsors_requis
def traiter_transaction(tx_id: int, action: str):
    action = action.upper()
    if action not in ("VALIDER", "REJETER"):
        return jsonify({"error": "Action invalide."}), 400

    now_iso = now_gmt().isoformat()
    with get_db() as conn:
        tx = conn.execute("SELECT * FROM transactions WHERE id = ?", (tx_id,)).fetchone()
        if not tx:
            return jsonify({"error": "Transaction introuvable."}), 404
        if row_get(tx, "statut") != "EN_ATTENTE":
            return jsonify({"error": "Cette transaction a déjà été traitée."}), 400

        montant = float(row_get(tx, "montant", 0.0))
        jid = row_get(tx, "joueur_id")
        type_tx = row_get(tx, "type_tx")

        if action == "VALIDER":
            if type_tx == "DEPOT":
                conn.execute(
                    "UPDATE joueurs SET solde_retirable = ROUND(solde_retirable + ?, 2) WHERE id = ?",
                    (montant, jid)
                )
            nouveau_statut = "VALIDE"
        else:
            if type_tx == "RETRAIT":
                conn.execute(
                    "UPDATE joueurs SET solde_retirable = ROUND(solde_retirable + ?, 2) WHERE id = ?",
                    (montant, jid)
                )
            nouveau_statut = "REJETE"

        conn.execute(
            "UPDATE transactions SET statut = ?, processed_at = ? WHERE id = ?",
            (nouveau_statut, now_iso, tx_id)
        )
        conn.commit()

    return jsonify({"id": tx_id, "statut": nouveau_statut})


@app.route("/tirage", methods=["POST"])
@sponsors_requis
def lancer_tirage():
    data = request.get_json(silent=True) or {}
    mode = str(data.get("mode", "AUTO")).upper()
    pool = int(data.get("pool_securite", 10))
    now_iso = now_gmt().isoformat()

    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM tickets WHERE statut = 'EN_ATTENTE' ORDER BY id ASC"
        ).fetchall()

        if not rows:
            return jsonify({"error": "Aucun ticket en attente pour le tirage."}), 400

        tickets = [
            {
                "id": row_get(r, "id"),
                "joueur_id": row_get(r, "joueur_id"),
                "code_ticket": row_get(r, "code_ticket"),
                "joueur": row_get(r, "joueur"),
                "numeros": json.loads(row_get(r, "numeros", "[]")),
                "mise": row_get(r, "mise", 0.0)
            }
            for r in rows
        ]

        if mode == "MANUEL":
            nums_manuels = data.get("numeros_manuels", [])
            valide, message = valider_tirage_manuel(nums_manuels)
            if not valide:
                return jsonify({"error": message}), 400
            tirage = sorted(nums_manuels)
            mode_enregistre = "MANUEL"
        else:
            tirage = generer_tirage_intelligent(tickets, pool_securite=pool)
            mode_enregistre = "AUTO"

        resultats = [evaluer_ticket(t, tirage) for t in tickets]

        total_mises = round(sum(float(t["mise"]) for t in tickets), 2)
        total_gains = round(sum(float(res["gain_usdt"]) for res in resultats), 2)

        cur_tirage = conn.execute(
            """
            INSERT INTO tirages (numeros_tirage, mode_tirage, pool_securite, total_mises, total_gains, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (json.dumps(tirage), mode_enregistre, pool, total_mises, total_gains, now_iso)
        )
        tirage_id = cur_tirage.lastrowid

        for res in resultats:
            conn.execute(
                """
                UPDATE tickets
                SET liaisons_gagnantes = ?, bons_numeros = ?, gain = ?, statut = ?, tirage_id = ?
                WHERE id = ?
                """,
                (
                    res["liaisons_gagnantes"],
                    json.dumps(res["bons_numeros"]),
                    res["gain_usdt"],
                    res["statut"],
                    tirage_id,
                    res["id"]
                )
            )
            if res["gain_usdt"] > 0 and res.get("joueur_id"):
                conn.execute(
                    "UPDATE joueurs SET solde_retirable = ROUND(solde_retirable + ?, 2) WHERE id = ?",
                    (res["gain_usdt"], res["joueur_id"])
                )

        conn.commit()

    return jsonify({
        "tirage_id": tirage_id,
        "mode": mode_enregistre,
        "tirage": tirage,
        "total_mises": total_mises,
        "total_gains": total_gains,
        "solde_net": round(total_mises - total_gains, 2),
        "resultats": resultats
    })


@app.route("/api/historique", methods=["GET"])
def historique_tirages():
    is_sponsor = bool(session.get("is_sponsor"))
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM tirages ORDER BY id DESC LIMIT 10"
        ).fetchall()
    historique = []
    for r in rows:
        item = {
            "id": row_get(r, "id"),
            "numeros_tirage": json.loads(row_get(r, "numeros_tirage", "[]")),
            "date_gmt": formater_date_gmt(row_get(r, "created_at", ""))
        }
        if is_sponsor:
            item["mode_tirage"] = row_get(r, "mode_tirage")
            item["pool_securite"] = row_get(r, "pool_securite")
            item["total_mises"] = row_get(r, "total_mises", 0.0)
            item["total_gains"] = row_get(r, "total_gains", 0.0)
        historique.append(item)
    return jsonify({"historique": historique})


init_db()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)