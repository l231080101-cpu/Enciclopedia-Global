from flask import Flask, render_template, jsonify, request, g
from flask_cors import CORS
import requests
import sqlite3
import json
import os
import random
import time
import bcrypt
import jwt
from functools import wraps
from datetime import datetime, timedelta

app = Flask(__name__)
CORS(app)

# Configuración
REST_COUNTRIES_API = "https://restcountries.com/v3.1"
EXCHANGE_API = "https://open.er-api.com/v6/latest/USD"
DB_NAME = "geocultural.db"
CACHE_EXCHANGE_TTL = 1800  # 30 minutos

# JWT Config
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'clave-super-secreta-para-jwt')
JWT_EXPIRATION_DELTA = timedelta(days=1)

exchange_cache = {"data": None, "timestamp": 0}

# --- Funciones de autenticación ---
def token_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        token = None
        auth_header = request.headers.get('Authorization')
        if auth_header and auth_header.startswith('Bearer '):
            token = auth_header.split(' ')[1]
        if not token:
            return jsonify({'message': 'Token is missing!'}), 401
        try:
            data = jwt.decode(token, app.config['SECRET_KEY'], algorithms=['HS256'])
            current_user_id = data['user_id']
        except:
            return jsonify({'message': 'Token is invalid!'}), 401
        g.user_id = current_user_id
        return f(*args, **kwargs)
    return decorated

def get_current_user_id():
    return g.get('user_id')

# --- Inicialización de Base de Datos con migración ---
def init_db():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    
    # Tabla consultas_frecuentes
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS consultas_frecuentes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pais_nombre TEXT UNIQUE,
            conteo INTEGER DEFAULT 1,
            ultima_consulta TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # Tabla usuarios
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS usuarios (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            fecha_registro TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # Tabla favoritos original (global) - la mantenemos por compatibilidad, pero ya no la usaremos para usuarios autenticados.
    # Creamos tabla favoritos_usuarios
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS favoritos_usuarios (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            usuario_id INTEGER NOT NULL,
            pais_nombre TEXT NOT NULL,
            pais_data TEXT NOT NULL,
            fecha_agregado TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(usuario_id, pais_nombre),
            FOREIGN KEY (usuario_id) REFERENCES usuarios (id) ON DELETE CASCADE
        )
    ''')
    
    # Verificar si existe tabla favoritos antigua y migrar datos si es necesario (opcional)
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='favoritos'")
    old_fav_table = cursor.fetchone()
    if old_fav_table:
        # Para usuarios existentes, no podemos migrar porque no tenemos relación usuario. Dejamos la tabla global para usuarios anónimos? Mejor creamos una tabla global y luego migramos.
        # Vamos a mantener la tabla favoritos para usuarios no autenticados, pero la lógica de la API cambiará para que los endpoints requieran token.
        # Por simplicidad, no migramos y solo usaremos favoritos_usuarios.
        pass
    
    # Tabla paises_cache
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS paises_cache (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nombre_comun TEXT UNIQUE NOT NULL,
            data TEXT NOT NULL,
            ultima_actualizacion TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    conn.commit()
    conn.close()

init_db()

# --- Funciones auxiliares (sin cambios) ---
def get_exchange_rates():
    global exchange_cache
    now = time.time()
    if exchange_cache["data"] is None or (now - exchange_cache["timestamp"]) > CACHE_EXCHANGE_TTL:
        try:
            res = requests.get(EXCHANGE_API, timeout=5)
            data = res.json()
            if data["result"] == "success":
                exchange_cache["data"] = data["rates"]
                exchange_cache["timestamp"] = now
        except Exception as e:
            print(f"Error obteniendo tasas: {e}")
    return exchange_cache["data"]

def get_country_from_cache_or_api(name):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT data FROM paises_cache WHERE nombre_comun = ?", (name,))
    row = cursor.fetchone()
    if row:
        cursor.execute("UPDATE paises_cache SET ultima_actualizacion = CURRENT_TIMESTAMP WHERE nombre_comun = ?", (name,))
        conn.commit()
        conn.close()
        return json.loads(row[0])
    try:
        response = requests.get(f"{REST_COUNTRIES_API}/name/{name}", timeout=5)
        response.raise_for_status()
        data = response.json()
        if data:
            cursor.execute("INSERT OR REPLACE INTO paises_cache (nombre_comun, data) VALUES (?, ?)",
                           (name, json.dumps(data[0])))
            conn.commit()
        conn.close()
        return data[0] if data else None
    except Exception as e:
        conn.close()
        raise e

def get_all_countries_names():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM paises_cache")
    count = cursor.fetchone()[0]
    if count < 200:
        try:
            response = requests.get(f"{REST_COUNTRIES_API}/all?fields=name,flags", timeout=10)
            response.raise_for_status()
            all_countries = response.json()
            for country in all_countries:
                nombre = country['name']['common']
                cursor.execute("INSERT OR REPLACE INTO paises_cache (nombre_comun, data) VALUES (?, ?)",
                               (nombre, json.dumps(country)))
            conn.commit()
        except Exception as e:
            print(f"Error al cargar lista de países: {e}")
    cursor.execute("SELECT nombre_comun FROM paises_cache ORDER BY nombre_comun")
    rows = cursor.fetchall()
    conn.close()
    return [row[0] for row in rows]

# --- Rutas públicas ---
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/paises')
def lista_paises():
    try:
        nombres = get_all_countries_names()
        return jsonify(nombres)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/buscar/<name>')
def buscar_pais(name):
    try:
        pais = get_country_from_cache_or_api(name)
        if not pais:
            return jsonify({"error": "País no encontrado"}), 404
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO consultas_frecuentes (pais_nombre) 
            VALUES (?) 
            ON CONFLICT(pais_nombre) DO UPDATE SET 
            conteo = conteo + 1, 
            ultima_consulta = CURRENT_TIMESTAMP
        ''', (pais['name']['common'],))
        conn.commit()
        conn.close()
        return jsonify([pais])
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/region/<region>')
def buscar_por_region(region):
    try:
        response = requests.get(f"{REST_COUNTRIES_API}/region/{region}", timeout=5)
        response.raise_for_status()
        paises = response.json()
        return jsonify(paises[:12])
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/cambio/<moneda_codigo>')
def obtener_cambio(moneda_codigo):
    rates = get_exchange_rates()
    if rates:
        tasa = rates.get(moneda_codigo.upper())
        if tasa:
            return jsonify({"tasa": tasa, "base": "USD"})
        return jsonify({"error": "Moneda no soportada"}), 404
    return jsonify({"error": "Error en API de cambio"}), 500

@app.route('/api/costos/<pais>')
def obtener_costos(pais):
    random.seed(pais)
    datos = {
        "comida": random.randint(3, 10),
        "hospedaje": random.randint(2, 10),
        "transporte": random.randint(4, 10),
        "ocio": random.randint(3, 10),
        "seguridad": random.randint(5, 10)
    }
    return jsonify(datos)

# --- Rutas de autenticación ---
@app.route('/api/register', methods=['POST'])
def register():
    data = request.json
    username = data.get('username')
    email = data.get('email')
    password = data.get('password')

    if not username or not email or not password:
        return jsonify({"error": "Faltan campos"}), 400

    # Validar longitud de contraseña
    if len(password) < 6:
        return jsonify({"error": "La contraseña debe tener al menos 6 caracteres"}), 400

    # Hashear contraseña
    password_hash = bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')

    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    try:
        cursor.execute(
            "INSERT INTO usuarios (username, email, password_hash) VALUES (?, ?, ?)",
            (username, email, password_hash)
        )
        conn.commit()
        user_id = cursor.lastrowid
        # Generar token JWT para iniciar sesión automáticamente
        token = jwt.encode({
            'user_id': user_id,
            'exp': datetime.utcnow() + JWT_EXPIRATION_DELTA
        }, app.config['SECRET_KEY'], algorithm='HS256')
        return jsonify({"token": token, "user": {"id": user_id, "username": username, "email": email}}), 201
    except sqlite3.IntegrityError as e:
        if "username" in str(e):
            return jsonify({"error": "El nombre de usuario ya existe"}), 400
        elif "email" in str(e):
            return jsonify({"error": "El email ya está registrado"}), 400
        else:
            return jsonify({"error": "Error al registrar usuario"}), 500
    finally:
        conn.close()

@app.route('/api/login', methods=['POST'])
def login():
    data = request.json
    username = data.get('username')
    password = data.get('password')

    if not username or not password:
        return jsonify({"error": "Faltan campos"}), 400

    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT id, username, email, password_hash FROM usuarios WHERE username = ?", (username,))
    user = cursor.fetchone()
    conn.close()

    if not user:
        return jsonify({"error": "Usuario no encontrado"}), 401

    user_id, username_db, email, password_hash = user
    if bcrypt.checkpw(password.encode('utf-8'), password_hash.encode('utf-8')):
        token = jwt.encode({
            'user_id': user_id,
            'exp': datetime.utcnow() + JWT_EXPIRATION_DELTA
        }, app.config['SECRET_KEY'], algorithm='HS256')
        return jsonify({"token": token, "user": {"id": user_id, "username": username_db, "email": email}})
    else:
        return jsonify({"error": "Contraseña incorrecta"}), 401

@app.route('/api/me', methods=['GET'])
@token_required
def me():
    user_id = get_current_user_id()
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT id, username, email FROM usuarios WHERE id = ?", (user_id,))
    user = cursor.fetchone()
    conn.close()
    if user:
        return jsonify({"id": user[0], "username": user[1], "email": user[2]})
    return jsonify({"error": "Usuario no encontrado"}), 404

# --- Rutas de favoritos (protegidas) ---
@app.route('/api/favoritos', methods=['GET', 'POST'])
@token_required
def gestionar_favoritos():
    user_id = get_current_user_id()
    if request.method == 'POST':
        try:
            pais_data = request.json
            nombre = pais_data.get('name', {}).get('common')
            if not nombre:
                return jsonify({"error": "Datos inválidos"}), 400
            conn = sqlite3.connect(DB_NAME)
            cursor = conn.cursor()
            cursor.execute(
                "INSERT OR REPLACE INTO favoritos_usuarios (usuario_id, pais_nombre, pais_data) VALUES (?, ?, ?)",
                (user_id, nombre, json.dumps(pais_data))
            )
            conn.commit()
            conn.close()
            return jsonify({"status": "success", "message": f"{nombre} guardado en favoritos"}), 201
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    # GET
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT pais_data FROM favoritos_usuarios WHERE usuario_id = ? ORDER BY fecha_agregado DESC",
        (user_id,)
    )
    rows = cursor.fetchall()
    conn.close()
    favoritos = [json.loads(row[0]) for row in rows]
    return jsonify(favoritos)

@app.route('/api/favoritos/<nombre>', methods=['DELETE'])
@token_required
def eliminar_favorito(nombre):
    user_id = get_current_user_id()
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute(
        "DELETE FROM favoritos_usuarios WHERE usuario_id = ? AND pais_nombre = ?",
        (user_id, nombre)
    )
    conn.commit()
    conn.close()
    return jsonify({"status": "deleted"}), 200

# --- Manejo de errores ---
@app.errorhandler(404)
def not_found(e):
    return jsonify({"error": "Recurso no encontrado"}), 404

if __name__ == '__main__':
    app.run(debug=True, port=5000)