from flask import Flask, render_template, jsonify, request
import requests
import sqlite3
import json
import os
import random # <--- Nueva importación para el Radar
from datetime import datetime

app = Flask(__name__)

# Configuración de archivos y APIs
REST_COUNTRIES_API = "https://restcountries.com/v3.1"
EXCHANGE_API = "https://open.er-api.com/v6/latest/USD"
DB_NAME = "geocultural.db"
FAVS_FILE = "favoritos_paises.json"

# --- Inicialización de Persistencia ---
def init_db():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS consultas_frecuentes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pais_nombre TEXT UNIQUE,
            conteo INTEGER DEFAULT 1,
            ultima_consulta TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS usuarios (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            email TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            fecha_registro TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    conn.commit()
    conn.close()

    if not os.path.exists(FAVS_FILE):
        with open(FAVS_FILE, 'w', encoding='utf-8') as f:
            json.dump([], f)

init_db()

# --- Rutas de Navegación ---
@app.route('/')
def index():
    return render_template('index.html')

# --- Servicio Web REST (Endpoints) ---

@app.route('/api/buscar/<name>')
def buscar_pais(name):
    try:
        response = requests.get(f"{REST_COUNTRIES_API}/name/{name}")
        response.raise_for_status()
        paises = response.json()

        if paises:
            nombre_comun = paises[0]['name']['common']
            conn = sqlite3.connect(DB_NAME)
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO consultas_frecuentes (pais_nombre) 
                VALUES (?) 
                ON CONFLICT(pais_nombre) DO UPDATE SET 
                conteo = conteo + 1, 
                ultima_consulta = CURRENT_TIMESTAMP
            ''', (nombre_comun,))
            conn.commit()
            conn.close()

        return jsonify(paises)
    except Exception as e:
        return jsonify({"error": "No se encontró el país", "detalle": str(e)}), 404

# --- NUEVO: Endpoint para el Radar de Costo de Vida ---
@app.route('/api/costos/<pais>')
def obtener_costos(pais):
    """Genera datos simulados pero consistentes para el gráfico de radar."""
    random.seed(pais) # Asegura que el mismo país siempre devuelva los mismos datos
    datos = {
        "comida": random.randint(3, 10),
        "hospedaje": random.randint(2, 10),
        "transporte": random.randint(4, 10),
        "ocio": random.randint(3, 10),
        "seguridad": random.randint(5, 10)
    }
    return jsonify(datos)

@app.route('/api/cambio/<moneda_codigo>')
def obtener_cambio(moneda_codigo):
    try:
        res = requests.get(EXCHANGE_API)
        data = res.json()
        
        if data["result"] == "success":
            tasa = data["rates"].get(moneda_codigo.upper())
            if tasa:
                return jsonify({"tasa": tasa, "base": "USD"})
            return jsonify({"error": "Moneda no soportada"}), 404
        return jsonify({"error": "Error en API de cambio"}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/favoritos', methods=['GET', 'POST'])
def gestionar_favoritos():
    if request.method == 'POST':
        try:
            nuevo_favorito = request.json
            nombre_nuevo = nuevo_favorito['name']['common']
            with open(FAVS_FILE, 'r', encoding='utf-8') as f:
                favoritos = json.load(f)
            
            if not any(p['name']['common'] == nombre_nuevo for p in favoritos):
                favoritos.append(nuevo_favorito)
                with open(FAVS_FILE, 'w', encoding='utf-8') as f:
                    json.dump(favoritos, f, indent=4)
                return jsonify({"status": "success", "message": f"{nombre_nuevo} guardado"}), 201
            return jsonify({"status": "info", "message": "Ya es favorito"}), 200
        except Exception as e:
            return jsonify({"error": "Error al guardar", "detalle": str(e)}), 500

    try:
        with open(FAVS_FILE, 'r', encoding='utf-8') as f:
            return jsonify(json.load(f))
    except:
        return jsonify([])

@app.errorhandler(404)
def not_found(e):
    return jsonify({"error": "Recurso no encontrado"}), 404

if __name__ == '__main__':
    app.run(debug=True, port=5000)