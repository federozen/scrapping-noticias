"""
Monitor Deportivo Pro — Backend FastAPI (adaptado de monitor_core.py + app.py
del proyecto Streamlit "monito_noticias").

Versión standalone para desplegar en un hosting real (Render u otro), a
diferencia de la versión en cuaderno de Colab + ngrok. Sirve la API y el
frontend estático desde el mismo proceso, protegidos con autenticación
HTTP Basic (usuario/contraseña por variables de entorno).

Todas las funciones de scraping, clustering, agenda, prompts de IA, etc. son
las mismas que usa la versión Streamlit — acá expuestas como endpoints REST
que consume el frontend de frontend/index.html.

No incluye la integración con Google Sheets / Telegram / GitHub Actions
(sheets_memoria.py, vigia.py, parte.py, informe.py del repo original), porque
eso es infraestructura de automatización aparte de la app en sí. El momentum
de la Agenda funciona "solo por sesión" (memoria en RAM del proceso): en el
plan free de Render el proceso se reinicia cuando el servicio se "duerme" por
inactividad, así que la canasta y el momentum se resetean en ese momento —
igual que la app Streamlit cuando no tiene la planilla configurada. La
pre-carga automática al arrancar (ver más abajo) ayuda a que, apenas alguien
entra con el proceso ya despierto, encuentre todo cargado igual.
"""
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response
from pydantic import BaseModel
from typing import Optional
import os, base64, secrets
import re, json, math, random, unicodedata, threading
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import lru_cache

import requests
import anthropic
from bs4 import BeautifulSoup

app = FastAPI(title="Monitor Deportivo Pro API")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# ─── AUTENTICACIÓN HTTP BASIC ────────────────────────────────────────────────
# Usuario y contraseña se configuran como variables de entorno en Render (o
# donde sea que corra esto) — nunca quedan escritos en el código ni en el
# repositorio de GitHub. Si no están configuradas, la app bloquea todo el
# acceso (falla "cerrado", no "abierto") para que nunca quede desplegada sin
# querer sin protección.
BASIC_AUTH_USER = os.environ.get("BASIC_AUTH_USER", "")
BASIC_AUTH_PASS = os.environ.get("BASIC_AUTH_PASS", "")

class BasicAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if not BASIC_AUTH_USER or not BASIC_AUTH_PASS:
            return Response(
                content=(
                    "Configuración incompleta: faltan las variables de entorno "
                    "BASIC_AUTH_USER y BASIC_AUTH_PASS en el servicio de Render "
                    "(Settings → Environment)."
                ),
                status_code=500,
            )
        auth = request.headers.get("authorization", "")
        if auth.startswith("Basic "):
            try:
                decoded = base64.b64decode(auth[6:]).decode("utf-8")
                user, _, pwd = decoded.partition(":")
                if secrets.compare_digest(user, BASIC_AUTH_USER) and secrets.compare_digest(pwd, BASIC_AUTH_PASS):
                    return await call_next(request)
            except Exception:
                pass
        return Response(
            content="Acceso restringido.",
            status_code=401,
            headers={"WWW-Authenticate": 'Basic realm="Monitor Deportivo Pro"'},
        )

app.add_middleware(BasicAuthMiddleware)

MAX_ITEMS = 50
SIMILITUD_UMBRAL = 0.22
CORE_VERSION = "núcleo v19 · primicias v2 (standalone)"

# ─── FUENTES ──────────────────────────────────────────────────────────────────
# Nacionales + internacionales + primicias/instituciones. Incluye TODAS las
# fuentes de monitor_core.py (las que ya estaban en el cuaderno viejo y las
# que faltaban: 442, Cielosports, Diario Popular, Ámbito, AFA, Radar AR, NA
# Deportes, Gazzetta, Corriere, Record PT, CBS Sports, Sporting News, FIFA,
# Guardian, Sky Sports, Di Marzio, Calciomercato, TNT Sports, Foot Mercato,
# Fabrizio Romano, Kicker, The Athletic, Ovación UY, CONMEBOL, UEFA, GE Globo,
# La Tercera, A Bola, Bild, Sky Sport IT, y todo el grupo de Primicias.
FUENTES_NAC = [
    {"id": "ole",           "nombre": "Olé",           "url": "https://www.ole.com.ar/",                             "color": "#00a846", "es_ole": True},
    {"id": "espn",          "nombre": "ESPN AR",        "url": "https://www.espn.com.ar/",                            "color": "#cc0000", "es_espn": True},
    {"id": "tyc",           "nombre": "TyC Sports",     "url": "https://www.tycsports.com/",                          "color": "#1565c0", "gnews_extra": True},
    {"id": "infobae",       "nombre": "Infobae",        "url": "https://www.infobae.com/deportes/",                   "color": "#b00020", "gnews_extra": True},
    {"id": "lanacion",      "nombre": "La Nación",      "url": "https://www.lanacion.com.ar/deportes/",               "color": "#1565c0"},
    {"id": "tn",            "nombre": "TN Deportes",    "url": "https://tn.com.ar/deportes/",                         "color": "#cc2200"},
    {"id": "clarin",        "nombre": "Clarín Dep.",    "url": "https://www.clarin.com/deportes/",                    "color": "#c00000"},
    {"id": "elgrafico",   "nombre": "El Gráfico",     "url": "https://news.google.com/rss/search?q=%22El%20Gr%C3%A1fico%22%20(futbol%20OR%20river%20OR%20boca%20OR%20seleccion)&hl=es-419&gl=AR&ceid=AR:es-419", "color": "#b07800", "es_rss": True},
    {"id": "dobleamarilla","nombre": "Doble Amarilla", "url": "https://news.google.com/rss/search?q=%22Doble%20Amarilla%22&hl=es-419&gl=AR&ceid=AR:es-419", "color": "#a07800", "es_rss": True},
    {"id": "bolavip",       "nombre": "Bolavip",        "url": "https://bolavip.com/ar",                              "color": "#c04a00"},
    {"id": "lavoz",         "nombre": "La Voz",         "url": "https://www.lavoz.com.ar/deportes/",                  "color": "#8b0000"},
    {"id": "capital",    "nombre": "La Capital (Ovación)", "url": "https://news.google.com/rss/search?q=site:lacapital.com.ar%20futbol&hl=es-419&gl=AR&ceid=AR:es-419", "color": "#8e44ad", "es_rss": True},
    {"id": "na",         "nombre": "NA Deportes",      "url": "https://news.google.com/rss/search?q=site:noticiasargentinas.com%20(futbol%20OR%20deportes)&hl=es-419&gl=AR&ceid=AR:es-419", "color": "#2c3e50", "es_rss": True},
    # ── Nuevas nacionales (vía Google News) — antes faltaban en el cuaderno ──
    {"id": "cuatro42",   "nombre": "442",              "url": "https://news.google.com/rss/search?q=site:442.perfil.com&hl=es-419&gl=AR&ceid=AR:es-419",                       "color": "#7b2d8b", "es_rss": True},
    {"id": "cielosports","nombre": "Cielosports",      "url": "https://news.google.com/rss/search?q=site:infocielo.com%20futbol&hl=es-419&gl=AR&ceid=AR:es-419",                      "color": "#0090d0", "es_rss": True},
    {"id": "popular",    "nombre": "Diario Popular",   "url": "https://news.google.com/rss/search?q=site:diariopopular.com.ar%20futbol&hl=es-419&gl=AR&ceid=AR:es-419",        "color": "#d32f2f", "es_rss": True},
    {"id": "ambito",     "nombre": "Ámbito Deportes",  "url": "https://news.google.com/rss/search?q=site:ambito.com%20futbol&hl=es-419&gl=AR&ceid=AR:es-419",                  "color": "#00594e", "es_rss": True},
    {"id": "afa",        "nombre": "AFA (oficial)",    "url": "https://news.google.com/rss/search?q=site:afa.com.ar&hl=es-419&gl=AR&ceid=AR:es-419",                           "color": "#6cace4", "es_rss": True},
    {"id": "radar_ar",   "nombre": "Radar AR",         "url": "https://news.google.com/rss/search?q=%22f%C3%BAtbol%20argentino%22&hl=es-419&gl=AR&ceid=AR:es-419",             "color": "#444444", "es_rss": True},
]

FUENTES_INT = [
    {"id": "as",        "nombre": "AS",              "url": "https://as.com/futbol/",                          "color": "#b00020", "es_as": True},
    {"id": "marca",     "nombre": "Marca",            "url": "https://www.marca.com/",                          "color": "#267326"},
    {"id": "mundodep",  "nombre": "Mundo Deportivo",  "url": "https://www.mundodeportivo.com/",                 "color": "#1565c0"},
    {"id": "sport",     "nombre": "Sport",            "url": "https://www.sport.es/es/",                        "color": "#cc0020"},
    {"id": "globo",     "nombre": "Globoesporte",     "url": "https://ge.globo.com/",                           "color": "#007a2f", "gnews_extra": True},
    {"id": "gazzetta",  "nombre": "Gazzetta Sport",   "url": "https://www.gazzetta.it/Calcio/",                 "color": "#e8000a"},
    {"id": "corriere",  "nombre": "Corriere Sport",   "url": "https://www.corrieredellosport.it/calcio",        "color": "#e06000"},
    {"id": "record",    "nombre": "Record PT",        "url": "https://www.record.pt/futebol/",                  "color": "#c8000a"},
    {"id": "bbc",       "nombre": "BBC Sport",        "url": "https://feeds.bbci.co.uk/sport/football/rss.xml",      "color": "#bb1919", "es_rss": True},
    {"id": "goal",      "nombre": "Goal",             "url": "https://www.goal.com/es",                         "color": "#00a878"},
    {"id": "espnint",   "nombre": "ESPN INT",         "url": "https://www.espn.com/soccer/",                    "color": "#d00000"},
    {"id": "cbssport",  "nombre": "CBS Sports",       "url": "https://www.cbssports.com/rss/headlines/soccer/", "color": "#004b87", "es_rss": True},
    {"id": "sportnews", "nombre": "Sporting News",    "url": "https://www.sportingnews.com/us/soccer",          "color": "#cc3300"},
    {"id": "lequipe",   "nombre": "L'Equipe",         "url": "https://www.lequipe.fr/Football/",                "color": "#f5c400"},
    {"id": "fifa",      "nombre": "FIFA (RSS)",       "url": "https://www.fifa.com/rss-feeds/index.html",       "color": "#326295"},
    # ── Nuevas: inglés + especialistas de mercado — antes faltaban ──
    {"id": "guardian",   "nombre": "Guardian Fútbol",  "url": "https://www.theguardian.com/football/rss",        "color": "#052962", "es_rss": True},
    {"id": "skysports",  "nombre": "Sky Sports",       "url": "https://www.skysports.com/rss/12040",             "color": "#0072c9", "es_rss": True},
    {"id": "dimarzio",   "nombre": "Di Marzio",        "url": "https://www.gianlucadimarzio.com/it/rss",         "color": "#0a3d62", "es_rss": True},
    {"id": "calciomer",  "nombre": "Calciomercato",    "url": "https://www.calciomercato.com/rss",               "color": "#c8102e", "es_rss": True},
    # ── Vía Google News directo — antes faltaban ──
    {"id": "tntsports",  "nombre": "TNT Sports AR",    "url": "https://news.google.com/rss/search?q=%22TNT%20Sports%22%20(river%20OR%20boca%20OR%20futbol%20OR%20seleccion)&hl=es-419&gl=AR&ceid=AR:es-419",  "color": "#e4002b", "es_rss": True},
    {"id": "footmercato","nombre": "Foot Mercato",     "url": "https://news.google.com/rss/search?q=site:footmercato.net%20OR%20%22Foot%20Mercato%22&hl=fr&gl=FR&ceid=FR:fr",    "color": "#0a5c36", "es_rss": True},
    {"id": "fabrizio",   "nombre": "Fabrizio Romano",  "url": "https://news.google.com/rss/search?q=%22Fabrizio%20Romano%22%20fichaje%20OR%20transfer&hl=es-419&gl=AR&ceid=AR:es-419", "color": "#1a1a2e", "es_rss": True},
    # ── Nuevas internacionales: medios + instituciones — antes faltaban ──
    {"id": "kicker",     "nombre": "Kicker (DE)",      "url": "https://news.google.com/rss/search?q=site:kicker.de&hl=de&gl=DE&ceid=DE:de",                                    "color": "#c00d0d", "es_rss": True},
    {"id": "athletic",   "nombre": "The Athletic",     "url": "https://news.google.com/rss/search?q=site:nytimes.com/athletic%20football&hl=en-US&gl=US&ceid=US:en",           "color": "#00292f", "es_rss": True},
    {"id": "ovacion",    "nombre": "Ovación (UY)",     "url": "https://news.google.com/rss/search?q=site:elpais.com.uy%20futbol&hl=es-419&gl=AR&ceid=AR:es-419",               "color": "#75aadb", "es_rss": True},
    {"id": "conmebol",   "nombre": "CONMEBOL",         "url": "https://news.google.com/rss/search?q=site:conmebol.com&hl=es-419&gl=AR&ceid=AR:es-419",                         "color": "#002b5c", "es_rss": True},
    {"id": "uefa",       "nombre": "UEFA / Champions", "url": "https://news.google.com/rss/search?q=(UEFA%20OR%20%22Champions%20League%22%20OR%20Europa%20League)&hl=es-419&gl=AR&ceid=AR:es-419", "color": "#00004b", "es_rss": True},
    # ── Nuevas internacionales vía Google News, con edición de idioma — antes faltaban ──
    {"id": "geglobo",   "nombre": "GE Globo (BR)",   "url": "https://news.google.com/rss/search?q=site:ge.globo.com&hl=pt-BR&gl=BR&ceid=BR:pt-419",        "color": "#c4170c", "es_rss": True},
    {"id": "latercera", "nombre": "La Tercera (CL)", "url": "https://news.google.com/rss/search?q=site:latercera.com%20futbol&hl=es-419&gl=CL&ceid=CL:es-419", "color": "#e2231a", "es_rss": True},
    {"id": "abola",     "nombre": "A Bola (PT)",     "url": "https://news.google.com/rss/search?q=site:abola.pt&hl=pt-PT&gl=PT&ceid=PT:pt-150",             "color": "#e30613", "es_rss": True},
    {"id": "bild",      "nombre": "Bild Sport (DE)", "url": "https://news.google.com/rss/search?q=site:bild.de%20fussball&hl=de&gl=DE&ceid=DE:de",           "color": "#d00000", "es_rss": True},
    {"id": "skyit",     "nombre": "Sky Sport (IT)",  "url": "https://news.google.com/rss/search?q=site:sport.sky.it&hl=it&gl=IT&ceid=IT:it",                "color": "#0a1a3f", "es_rss": True},
]

# ─── GRUPO 3: PRIMICIAS E INSTITUCIONES (grupo entero nuevo — no existía en
# el cuaderno viejo) ───────────────────────────────────────────────────────
# No son diarios genéricos: traen primicias de mercado, comunicados
# oficiales, designaciones, agregadores temáticos — todo vía Google News.
G_AR = "&hl=es-419&gl=AR&ceid=AR:es-419"
FUENTES_ESP = [
    {"id": "merlo",     "nombre": "César Merlo",      "url": f"https://news.google.com/rss/search?q=%22C%C3%A9sar%20Luis%20Merlo%22%20OR%20%22Cesar%20Merlo%22{G_AR}", "color": "#0b7a3b", "es_rss": True, "sin_fallback": True},
    {"id": "grova",     "nombre": "García Grova",     "url": f"https://news.google.com/rss/search?q=%22Germ%C3%A1n%20Garc%C3%ADa%20Grova%22%20OR%20%22Garcia%20Grova%22{G_AR}", "color": "#0b7a3b", "es_rss": True, "sin_fallback": True},
    {"id": "ligapro",   "nombre": "Liga Profesional", "url": f"https://news.google.com/rss/search?q=%22Liga%20Profesional%22%20(fixture%20OR%20fecha%20OR%20programacion%20OR%20oficial){G_AR}", "color": "#1a3c8f", "es_rss": True},
    {"id": "arbitros",  "nombre": "Designaciones/Arbitraje", "url": f"https://news.google.com/rss/search?q=(designaciones%20arbitrales%20OR%20%22arbitros%20para%20la%20fecha%22%20OR%20%22dirigir%C3%A1%22){G_AR}", "color": "#111111", "es_rss": True},
    {"id": "gn_river",  "nombre": "GNews · River",    "url": f"https://news.google.com/rss/search?q=River%20Plate%20futbol{G_AR}", "color": "#c8102e", "es_rss": True, "sin_fallback": True},
    {"id": "gn_boca",   "nombre": "GNews · Boca",     "url": f"https://news.google.com/rss/search?q=Boca%20Juniors%20futbol{G_AR}", "color": "#005baa", "es_rss": True, "sin_fallback": True},
    {"id": "gn_selec",  "nombre": "GNews · Selección","url": f"https://news.google.com/rss/search?q=%22selecci%C3%B3n%20argentina%22{G_AR}", "color": "#6cace4", "es_rss": True, "sin_fallback": True},
    {"id": "gn_pases",  "nombre": "GNews · Mercado AR","url": f"https://news.google.com/rss/search?q=(fichaje%20OR%20refuerzo%20OR%20%22mercado%20de%20pases%22)%20futbol%20argentino{G_AR}", "color": "#d68910", "es_rss": True, "sin_fallback": True},
    {"id": "juveniles", "nombre": "Juveniles/Sub",    "url": f"https://news.google.com/rss/search?q=(sub%2020%20OR%20sub%2017%20OR%20juveniles)%20seleccion%20argentina{G_AR}", "color": "#0891b2", "es_rss": True},
    {"id": "gn_racing", "nombre": "GNews · Racing",   "url": f"https://news.google.com/rss/search?q=Racing%20Club%20futbol{G_AR}", "color": "#6cb4e4", "es_rss": True, "sin_fallback": True},
    {"id": "gn_inde",   "nombre": "GNews · Independiente", "url": f"https://news.google.com/rss/search?q=%22Independiente%22%20futbol%20argentina{G_AR}", "color": "#e30613", "es_rss": True, "sin_fallback": True},
    {"id": "gn_sanlo",  "nombre": "GNews · San Lorenzo", "url": f"https://news.google.com/rss/search?q=%22San%20Lorenzo%22%20futbol%20argentina{G_AR}", "color": "#1a2a6c", "es_rss": True, "sin_fallback": True},
    {"id": "gn_messi",  "nombre": "GNews · Messi",    "url": f"https://news.google.com/rss/search?q=Messi{G_AR}", "color": "#6cace4", "es_rss": True, "sin_fallback": True},
    {"id": "gn_colap",  "nombre": "GNews · Colapinto", "url": f"https://news.google.com/rss/search?q=Colapinto{G_AR}", "color": "#0090d0", "es_rss": True, "sin_fallback": True},
]

TODAS_FUENTES = FUENTES_NAC + FUENTES_INT + FUENTES_ESP
FUENTES_NAC_IDS = {f["id"] for f in FUENTES_NAC}
FUENTES_INT_IDS = {f["id"] for f in FUENTES_INT}
FUENTES_ESP_IDS = {f["id"] for f in FUENTES_ESP}

# ─── STOPWORDS ────────────────────────────────────────────────────────────────
STOPWORDS = set([
    "de","la","el","en","y","a","los","del","se","las","por","un","para","con","una","su","al","lo",
    "como","más","pero","sus","le","ya","o","fue","este","ha","si","porque","esta","son","entre",
    "cuando","muy","sin","sobre","también","me","hasta","hay","donde","quien","desde","todo","nos",
    "durante","e","esto","mi","antes","yo","otro","otras","otra","él","bien","así","cada","ser",
    "tiene","había","era","no","es","que","the","a","an","and","or","but","in","on","at","to","for",
    "of","with","by","from","is","was","are","were","be","been","have","has","had","will","would",
    "could","should","may","might","can","da","do","em","para","com","por","que","um","uma",
    "os","as","ao","na","no","nas","nos","se","seu","sua","seus","suas","não","após","tras",
    "vs","vs.","after","over","into","than","then","their","they","this","that",
])

# ─── SIMILITUD SEMÁNTICA ──────────────────────────────────────────────────────
@lru_cache(maxsize=8192)
def normalizar_titulo(titulo: str) -> set:
    t = titulo.lower()
    t = unicodedata.normalize("NFD", t)
    t = "".join(c for c in t if unicodedata.category(c) != "Mn")
    t = re.sub(r"[^a-z0-9\s]", " ", t)
    return {w for w in t.split() if len(w) >= 3 and w not in STOPWORDS}

def similitud_jaccard(set_a: set, set_b: set) -> float:
    if not set_a or not set_b:
        return 0.0
    interseccion = len(set_a & set_b)
    union = len(set_a | set_b)
    return interseccion / union if union > 0 else 0.0

def similitud_ponderada(set_a: set, set_b: set, pesos: dict) -> float:
    """Como Jaccard pero cada palabra vale según su rareza (idf): los nombres
    propios (raros) pesan más que 'partido' o 'gol'. TF-IDF simplificado."""
    if not set_a or not set_b:
        return 0.0
    inter = set_a & set_b
    union = set_a | set_b
    peso_inter = sum(pesos.get(w, 1.0) for w in inter)
    peso_union = sum(pesos.get(w, 1.0) for w in union)
    return peso_inter / peso_union if peso_union > 0 else 0.0

def _calcular_idf(listas_keys: list) -> dict:
    N = len(listas_keys) or 1
    df = Counter()
    for keys in listas_keys:
        for w in keys:
            df[w] += 1
    return {w: math.log((N + 1) / (c + 1)) + 1.0 for w, c in df.items()}

def es_exclusivo(titulo: str, propio_id: str, resultados: dict) -> bool:
    keys = normalizar_titulo(titulo)
    if len(keys) < 2:
        return False
    for f in TODAS_FUENTES:
        if f["id"] == propio_id:
            continue
        for n in resultados.get(f["id"], []):
            if similitud_jaccard(keys, normalizar_titulo(n["titulo"])) >= SIMILITUD_UMBRAL:
                return False
    return True

def analizar_ole_vs_competencia(resultados: dict) -> dict:
    keysets = {}
    for f in TODAS_FUENTES:
        keysets[f["id"]] = [
            {"noticia": n, "keys": normalizar_titulo(n["titulo"])}
            for n in resultados.get(f["id"], [])
        ]
    ole_items = keysets.get("ole", [])
    competencia = [f for f in TODAS_FUENTES if not f.get("es_ole")]

    exclusivos_ole = []
    for item in ole_items:
        encontrado = any(
            similitud_jaccard(item["keys"], ci["keys"]) >= SIMILITUD_UMBRAL
            for fid, citems in keysets.items() if fid != "ole" for ci in citems
        )
        if not encontrado:
            exclusivos_ole.append(item["noticia"])

    faltantes_en_ole = []
    ya_agregados_keys = []
    for fuente in competencia:
        for item in keysets.get(fuente["id"], []):
            tiene_ole = any(
                similitud_jaccard(item["keys"], oi["keys"]) >= SIMILITUD_UMBRAL for oi in ole_items
            )
            if not tiene_ole:
                es_dup = any(
                    similitud_jaccard(item["keys"], k) >= SIMILITUD_UMBRAL for k in ya_agregados_keys
                )
                if not es_dup:
                    ya_agregados_keys.append(item["keys"])
                    faltantes_en_ole.append({
                        "titulo": item["noticia"]["titulo"],
                        "url": item["noticia"].get("url"),
                        "fuente_id": fuente["id"],
                        "fuente_nombre": fuente["nombre"],
                        "fuente_color": fuente["color"],
                    })

    cubiertos_por_ambos = []
    for item in ole_items:
        competidores = []
        for fid, citems in keysets.items():
            if fid == "ole":
                continue
            for ci in citems:
                sim = similitud_jaccard(item["keys"], ci["keys"])
                if sim >= SIMILITUD_UMBRAL:
                    competidores.append({"fuente_id": fid, "noticia": ci["noticia"], "sim": sim})
                    break
        if competidores:
            cubiertos_por_ambos.append({
                "noticia_ole": item["noticia"],
                "competencia": competidores[:4],
            })

    return {
        "exclusivos_ole": exclusivos_ole,
        "faltantes_en_ole": faltantes_en_ole,
        "cubiertos_por_ambos": cubiertos_por_ambos,
    }

def analizar_ole_vs_compecencia_safe(resultados: dict) -> dict:
    try:
        return analizar_ole_vs_competencia(resultados)
    except Exception:
        return {"exclusivos_ole": [], "faltantes_en_ole": [], "cubiertos_por_ambos": []}

def calcular_tendencias(resultados: dict) -> list:
    todas = []
    for f in TODAS_FUENTES:
        for n in resultados.get(f["id"], []):
            todas.append({"noticia": n, "fuente": f, "keys": normalizar_titulo(n["titulo"])})

    pesos = _calcular_idf([t["keys"] for t in todas])
    UMBRAL_CLUSTER = 0.22
    clusters = []
    asignado = [False] * len(todas)

    for i in range(len(todas)):
        if asignado[i]:
            continue
        cluster = {
            "titulo": todas[i]["noticia"]["titulo"],
            "url": todas[i]["noticia"].get("url"),
            "fuente_ids": {todas[i]["fuente"]["id"]},
            "noticias": [{"noticia": todas[i]["noticia"], "fuente": todas[i]["fuente"]}],
            "keys": todas[i]["keys"],
        }
        asignado[i] = True
        for j in range(i + 1, len(todas)):
            if asignado[j]:
                continue
            if similitud_ponderada(cluster["keys"], todas[j]["keys"], pesos) >= UMBRAL_CLUSTER:
                cluster["fuente_ids"].add(todas[j]["fuente"]["id"])
                cluster["noticias"].append({"noticia": todas[j]["noticia"], "fuente": todas[j]["fuente"]})
                asignado[j] = True
        if len(cluster["fuente_ids"]) >= 2:
            clusters.append(cluster)

    clusters.sort(key=lambda c: (-len(c["fuente_ids"]), -len(c["noticias"])))
    return [
        {
            "titulo": c["titulo"],
            "url": c["url"],
            "cant_medios": len(c["fuente_ids"]),
            "fuente_ids": list(c["fuente_ids"]),
            "noticias": [
                {"titulo": nn["noticia"]["titulo"], "url": nn["noticia"].get("url"),
                 "imagen": nn["noticia"].get("imagen", ""),
                 "fuente": {"id": nn["fuente"]["id"], "nombre": nn["fuente"]["nombre"], "color": nn["fuente"]["color"]}}
                for nn in c["noticias"]
            ],
            "tiene_ole": "ole" in c["fuente_ids"],
            "nac": sum(1 for n in c["noticias"] if n["fuente"]["id"] in FUENTES_NAC_IDS),
            "intl": sum(1 for n in c["noticias"] if n["fuente"]["id"] not in FUENTES_NAC_IDS),
        }
        for c in clusters
    ]

def nube_palabras(resultados: dict, fuente_ids: list, color_hex: str) -> list:
    """Nube de palabras (posicionamiento en espiral) para un grupo de fuentes."""
    EXTRA_STOP = {
        "partido","partidos","juego","juegos","dice","dijo","señalo","aseguro","confirmo",
        "revelo","anuncio","hablo","tiene","hoy","ayer","manana","semana","anno","mes","vez",
        "nuevo","nueva","gran","primer","primera","sera","puede","equipo","sobre","habla",
        "luego","hace","dado","segun","after","over","into","than","their","they","this",
        "that","with","will","from",
    }
    freq = {}
    for fid in fuente_ids:
        for n in resultados.get(fid, []):
            for w in normalizar_titulo(n["titulo"]) - EXTRA_STOP:
                if len(w) > 3:
                    freq[w] = freq.get(w, 0) + 1
    words = sorted(freq.items(), key=lambda x: -x[1])[:60]
    if not words:
        return []
    max_c, min_c = words[0][1], words[-1][1]
    rng = max_c - min_c or 1
    h = color_hex.lstrip("#")
    cr, cg, cb = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    placed, out = [], []
    random.seed(42)
    for word, count in words:
        t = (count - min_c) / rng
        fs = 11 + t * 26
        r = int(cr + (220 - cr) * (1 - t)); g = int(cg + (225 - cg) * (1 - t)); b = int(cb + (230 - cb) * (1 - t))
        hw = len(word) * fs * 0.30 / 4.8; hh = fs * 0.65 / 2.6
        for step in range(400):
            ang = step * 0.28; rad = step * 0.15
            cx = 50 + rad * math.cos(ang); cy = 50 + rad * math.sin(ang) * 0.55
            if cx - hw < 1 or cx + hw > 99 or cy - hh < 2 or cy + hh > 98:
                continue
            if not any(abs(cx - px) < hw + phw + 1.2 and abs(cy - py) < hh + phh + 1.2 for px, py, phw, phh in placed):
                placed.append((cx, cy, hw, hh))
                out.append({"word": word, "count": count, "x": round(cx, 1), "y": round(cy, 1),
                            "size": round(fs, 1), "color": f"rgb({r},{g},{b})",
                            "weight": "700" if t > 0.45 else "400", "opacity": round(0.5 + t * 0.5, 2)})
                break
    return out

# ─── FRAMEWORK EDITORIAL: LOS 10 ÁNGULOS ─────────────────────────────────────
FRAMEWORK_ANGULOS = """Antes de proponer nada, identificá: qué pasó, qué cambió, a quién afecta, qué emoción genera, qué patrón revela y qué consecuencia deja. No digas qué pasó: decí por qué importa para el hincha. Competí por el significado antes que por la información.

Los 10 ángulos que más rinden (elegí los 2-3 que mejor apliquen a cada tema):
1. CAMBIO DE ESTATUS — ¿alguien dejó de ser lo que era? ("ya no es revelación: es campeón")
2. PATRÓN — ¿esto ya pasó antes? ("la historia que Boca vuelve a repetir")
3. CONSECUENCIA — ¿qué cambia desde mañana? ("lo que cambia para River después de la final")
4. HÉROE INESPERADO — ¿quién apareció donde nadie lo esperaba?
5. CONFLICTO — ¿quién piensa distinto? ("la grieta que dejó la final")
6. PARADOJA — ¿qué contradicción hay? ("jugó mejor y perdió")
7. IDENTIDAD — ¿qué dice esto sobre el club y su gente?
8. TENDENCIA — ¿qué se está viendo venir?
9. QUÉ SIGNIFICA — ¿qué representa realmente? ("mucho más que un campeonato")
10. EL DÍA DESPUÉS — ¿qué queda cuando termina el ruido? ("la pregunta que River debe responder ahora")"""

CRITERIOS_EDITOR = ""  # criterios propios del editor; se puede completar a mano acá

def bloque_criterios() -> str:
    if CRITERIOS_EDITOR.strip():
        return f"\n\nCRITERIOS DEL EDITOR (respetalos siempre):\n{CRITERIOS_EDITOR.strip()}"
    return ""

PASES_KEYWORDS = [
    "fichaje", "fichajes", "ficha a", "el pase de", "pase a", "refuerzo", "refuerzos",
    "transfer", "mercado de pases", "libro de pases", "oferta por", "ofertas por",
    "prestamo", "préstamo", "a prestamo", "cedido", "cesion", "cesión",
    "clausula", "cláusula", "acuerdo por el pase", "cerro la llegada", "cerró la llegada",
    "incorpora a", "incorporacion de", "incorporación de", "sumo a", "sumó a",
    "negocia por", "negociacion por", "negociación por", "here we go",
    "se va a", "deja el club", "rescision", "rescisión", "renovacion de contrato",
    "renovación de contrato", "renueva con", "firma con", "firmó con", "firmo con",
    "nuevo refuerzo", "vendido a", "venta de", "traspaso", "quiere contratar a",
    "oferta millonaria", "pretendido por", "seria nuevo", "sería nuevo",
    "es nuevo jugador", "llega a", "desembarca en",
]

def es_tema_de_pases(titulo: str) -> bool:
    t = titulo.lower()
    return any(k in t for k in PASES_KEYWORDS)

def solapamiento(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))

GENERICOS_FUTBOL = {
    "acuerdo", "acordo", "pase", "pases", "fichaje", "fichajes", "refuerzo",
    "refuerzos", "vuelve", "vuelta", "regreso", "regresa", "llegada", "llega",
    "club", "equipo", "partido", "partidos", "gol", "goles", "final", "torneo",
    "futbol", "mercado", "oficial", "confirmado", "confirmada", "negociacion",
    "negociaciones", "jugador", "jugadores", "tecnico", "entrenador", "bombazo",
    "ultimo", "ultima", "primera", "primer", "hora", "horas", "video", "fotos",
}

def coincide_cobertura(a: set, b: set) -> bool:
    if similitud_jaccard(a, b) >= 0.35:
        return True
    sa, sb = a - GENERICOS_FUTBOL, b - GENERICOS_FUTBOL
    return len(sa & sb) >= 2 and solapamiento(sa, sb) >= 0.5

# ─── ENTIDADES (detección sin IA) ────────────────────────────────────────────
ENTIDADES_BASE = {
    "River": ["river", "millonario", "nunez"], "Boca": ["boca", "xeneize", "bombonera"],
    "Racing": ["racing", "academia"], "Independiente": ["independiente", "rojo"],
    "San Lorenzo": ["san lorenzo", "ciclon", "cuervo"], "Huracan": ["huracan", "globo"],
    "Velez": ["velez", "fortin"], "Estudiantes": ["estudiantes", "pincha"],
    "Gimnasia": ["gimnasia", "lobo"], "Newells": ["newells", "newell", "leproso"],
    "Rosario Central": ["rosario central", "central", "canalla"],
    "Lanus": ["lanus", "granate"], "Banfield": ["banfield", "taladro"],
    "Talleres": ["talleres cordoba", "talleres", "matador"], "Belgrano": ["belgrano", "pirata"],
    "Defensa": ["defensa y justicia", "halcon"], "Argentinos": ["argentinos juniors", "argentinos", "bicho"],
    "Tigre": ["tigre matador", "tigre"], "Platense": ["platense", "calamar"],
    "Instituto": ["instituto cordoba", "instituto"], "Barracas": ["barracas central", "barracas"],
    "Sarmiento": ["sarmiento junin", "sarmiento"], "Union": ["union santa fe", "union"],
    "Colon": ["colon santa fe", "colon"], "Godoy Cruz": ["godoy cruz", "tomba"],
    "Central Cordoba": ["central cordoba"], "Riestra": ["deportivo riestra", "riestra"],
    "Seleccion": ["seleccion argentina", "seleccion", "albiceleste", "scaloneta"],
    "Sub-20": ["sub 20", "sub-20", "seleccion sub"], "Sub-23": ["sub 23", "sub-23"],
    "Messi": ["messi", "leo messi"], "Scaloni": ["scaloni"], "Di Maria": ["di maria", "fideo"],
    "Julian Alvarez": ["julian alvarez", "julian"], "Dibu Martinez": ["dibu", "emiliano martinez"],
    "Gallardo": ["gallardo"], "Costas": ["costas"], "Enzo Fernandez": ["enzo fernandez"],
    "Mastantuono": ["mastantuono"], "Colapinto": ["colapinto"],
    "Libertadores": ["libertadores"], "Sudamericana": ["sudamericana"],
    "Mundial": ["mundial", "copa del mundo", "world cup"],
    "Champions": ["champions", "champions league"],
    "Liga Profesional": ["liga profesional", "torneo local", "copa de la liga"],
    "Eliminatorias": ["eliminatorias"],
    "Real Madrid": ["real madrid"], "Barcelona": ["barcelona", "barca", "culé"],
    "PSG": ["psg", "paris saint"], "City": ["manchester city"], "United": ["manchester united"],
    "Inter": ["inter de milan", "inter milan"], "Milan": ["ac milan"], "Juventus": ["juventus", "juve"],
}

def _norm_texto(t: str) -> str:
    t = t.lower()
    t = unicodedata.normalize("NFD", t)
    return "".join(c for c in t if unicodedata.category(c) != "Mn")

def detectar_entidades(titulo: str, dic: dict = None) -> list:
    dic = dic or ENTIDADES_BASE
    t = " " + _norm_texto(titulo) + " "
    encontradas = []
    for canonico, alias in dic.items():
        for a in alias:
            if f" {a} " in t or t.startswith(f"{a} ") or t.endswith(f" {a}"):
                encontradas.append(canonico)
                break
    return encontradas

def ranking_entidades(resultados: dict, dic: dict = None) -> list:
    conteo = defaultdict(lambda: {"menciones": 0, "medios": set(), "ole": False})
    for f in TODAS_FUENTES:
        for n in resultados.get(f["id"], []):
            for ent in detectar_entidades(n.get("titulo", ""), dic):
                c = conteo[ent]
                c["menciones"] += 1
                c["medios"].add(f["id"])
                if f["id"] == "ole":
                    c["ole"] = True
    out = [{"entidad": e, "menciones": v["menciones"], "medios": len(v["medios"]),
            "tiene_ole": v["ole"]} for e, v in conteo.items()]
    out.sort(key=lambda x: (-x["menciones"], -x["medios"]))
    return out

# ─── RELEVANCIA ARGENTINA (para notas del exterior) ──────────────────────────
RELEVANCIA_AR_KEYWORDS = [
    "argentin", "albiceleste", "seleccion argentina", "scaloneta",
    "afa", "eliminatorias sudamericana",
    "messi", "di maria", "julian alvarez", "lautaro", "mac allister",
    "enzo fernandez", "cuti romero", "dibu", "emiliano martinez", "garnacho",
    "mastantuono", "nico paz", "nico gonzalez", "otamendi", "paredes",
    "de paul", "lo celso", "tagliafico", "lisandro martinez", "licha martinez",
    "foyth", "molina", "montiel", "acuna", "palacios", "almada",
    "gonzalo montiel", "thiago almada", "valentin carboni", "carboni",
    "soule", "matias soule", "buonanotte", "simeone hijo", "giuliano simeone",
    "colapinto", "river", "boca", "gallardo", "scaloni", "simeone",
    "cholo", "pochettino", "martino", "bielsa", "batistuta",
    "mascherano", "sebastian beccacece", "gustavo alfaro",
    "borre", "santos borre", "driussi", "beltran", "lucas beltran",
    "libertadores", "copa sudamericana", "mundial de clubes",
    "rival de argentina", "grupo de argentina",
    "here we go", "fabrizio romano",
]

def relevancia_argentina(titulo: str) -> bool:
    t = _norm_texto(titulo)
    return any(k in t for k in RELEVANCIA_AR_KEYWORDS)

def notas_exterior_relevantes(resultados: dict, max_items: int = 40) -> list:
    out, vistos = [], set()
    for f in TODAS_FUENTES:
        if f["id"] in FUENTES_NAC_IDS:
            continue
        for n in resultados.get(f["id"], []):
            t = n.get("titulo", "")
            if not relevancia_argentina(t):
                continue
            k = frozenset(normalizar_titulo(t))
            if not k or k in vistos:
                continue
            vistos.add(k)
            out.append({"fuente": {"id": f["id"], "nombre": f["nombre"], "color": f["color"]},
                        "titulo": t, "url": n.get("url"), "entidades": detectar_entidades(t)})
    out.sort(key=lambda x: -len(x["entidades"]))
    return out[:max_items]

# ─── FILTROS TEMÁTICOS (rebanadas del panorama, sin IA) ──────────────────────
FILTROS_TEMATICOS = {
    "mercado": {
        "titulo": "💸 Mercado de pases",
        "desc": "Fichajes, ofertas, negociaciones y movimientos del libro de pases.",
        "keywords": PASES_KEYWORDS,
    },
    "polemica": {
        "titulo": "🔥 Polémicas y conflictos",
        "desc": "Escándalos, cruces, denuncias, sanciones y líos que generan debate.",
        "keywords": [
            "polemica", "escandalo", "denuncia", "sancion", "sancionado", "multa",
            "expulsado", "expulsion", "roja", "insulto", "agresion", "pelea",
            "cruce", "picante", "fuerte contra", "apunto contra", "estallo",
            "renuncia", "renuncio", "echado", "despido", "crisis", "conflicto",
            "arbitro", "arbitraje", "var polemico", "penal inexistente",
            "amenaza", "investigacion", "acusacion", "acuso", "repudio", "furia",
        ],
    },
    "viral": {
        "titulo": "😮 Virales y color",
        "desc": "Lo insólito, emotivo, curioso y con potencial de tráfico.",
        "keywords": [
            "insolito", "insólito", "insolita", "viral", "se hizo viral", "furor",
            "increible", "increíble", "emotivo", "emocionante", "conmovedor",
            "el gesto de", "insolita imagen", "nunca visto", "las redes",
            "estallaron las redes", "el video que", "el video de", "video viral",
            "las fotos de", "memes", "los memes", "se emociono", "se emocionó",
            "hasta las lagrimas", "hasta las lágrimas", "revoluciono", "revolucionó",
            "curioso", "curiosa", "bizarro", "papelon", "papelón", "blooper",
            "la reaccion de", "la reacción de", "lo que hizo", "no vas a creer",
            "insolita situacion", "camara capto", "cámara captó",
        ],
    },
}

def filtrar_custom(resultados: dict, keywords: list, solo_ar: bool = False, max_items: int = 60) -> list:
    if not keywords:
        return []
    kws = [_norm_texto(k) for k in keywords if k.strip()]
    out, vistos = [], set()
    for f in TODAS_FUENTES:
        for n in resultados.get(f["id"], []):
            t = n.get("titulo", "")
            tn = _norm_texto(t)
            if not any(k in tn for k in kws):
                continue
            if solo_ar and not relevancia_argentina(t):
                continue
            k = frozenset(normalizar_titulo(t))
            if not k or k in vistos:
                continue
            vistos.add(k)
            out.append({"fuente": {"id": f["id"], "nombre": f["nombre"], "color": f["color"]},
                        "titulo": t, "url": n.get("url"), "entidades": detectar_entidades(t)})
    out.sort(key=lambda x: -len(x["entidades"]))
    return out[:max_items]

def filtrar_por_tema(resultados: dict, filtro_id: str, solo_ar: bool = False, max_items: int = 50) -> list:
    conf = FILTROS_TEMATICOS.get(filtro_id)
    if not conf:
        return []
    return filtrar_custom(resultados, conf["keywords"], solo_ar=solo_ar, max_items=max_items)

# ─── AGENDA ACCIONABLE + MOMENTUM ─────────────────────────────────────────────
def calcular_momentum(tendencias: list, prev_tendencias: list) -> dict:
    prev = prev_tendencias or []
    prev_keys = [normalizar_titulo(c["titulo"]) for c in prev]
    out = {}
    for i, c in enumerate(tendencias):
        k = normalizar_titulo(c["titulo"])
        best_j, best_sim = -1, 0.0
        for j, pk in enumerate(prev_keys):
            s = similitud_jaccard(k, pk)
            if s > best_sim:
                best_sim, best_j = s, j
        if best_j >= 0 and best_sim >= 0.30:
            out[i] = {"delta": c["cant_medios"] - prev[best_j]["cant_medios"], "nuevo": False}
        else:
            out[i] = {"delta": c["cant_medios"], "nuevo": True}
    return out

def construir_agenda(tendencias: list, ole_analisis: dict, prev_tendencias: list,
                     max_items: int = 14, cubiertos: list = None) -> list:
    momentum = calcular_momentum(tendencias, prev_tendencias)
    items = []
    for i, c in enumerate(tendencias):
        mom = momentum.get(i, {"delta": 0, "nuevo": False})
        delta, nuevo = mom["delta"], mom["nuevo"]
        base = c["cant_medios"]
        tiene_ole = c.get("tiene_ole")
        score = base + max(delta, 0) * 2.5 + (3 if nuevo else 0) + (4 if not tiene_ole else 0)

        ya_dado = None
        if not tiene_ole and cubiertos:
            k_actual = normalizar_titulo(c["titulo"])
            for cub in cubiertos:
                if coincide_cobertura(k_actual, normalizar_titulo(cub["titulo"])):
                    ya_dado = cub
                    break
        if ya_dado is not None:
            cuando = ya_dado.get("fecha") or "estos días"
            accion = "RETOMAR"
            motivo = f"lo diste el {cuando} y hoy {base} medios lo mueven — ¿actualización o segunda vuelta?"
            score += 1
        elif not tiene_ole and base >= 3:
            accion, motivo = "SUBIR YA", f"{base} medios lo tienen y Olé no"
        elif not tiene_ole:
            accion, motivo = "REDACTAR", f"{base} medio(s) lo cubren y Olé no"
        elif nuevo or delta >= 2:
            accion = "SEGUIR"
            motivo = ("tema nuevo creciendo" if nuevo else f"creciendo (+{delta} medios)") + " — reforzá tu ángulo"
            score += 1
        else:
            continue

        items.append({
            "accion": accion, "motivo": motivo, "titulo": c["titulo"], "url": c.get("url"),
            "cant_medios": base, "delta": delta, "nuevo": nuevo,
            "nac": c.get("nac", 0), "intl": c.get("intl", 0),
            "noticias": c.get("noticias", []), "score": score,
        })

    for n in (ole_analisis or {}).get("exclusivos_ole", [])[:5]:
        items.append({
            "accion": "EMPUJAR", "motivo": "exclusivo de Olé — promocionalo o hacé segunda vuelta",
            "titulo": n["titulo"], "url": n.get("url"),
            "cant_medios": 1, "delta": 0, "nuevo": False, "nac": 1, "intl": 0,
            "noticias": [], "score": 2.0,
        })

    items.sort(key=lambda x: -x["score"])
    return items[:max_items]

AGENDA_COLORES = {
    "SUBIR YA": "#c0392b", "REDACTAR": "#d68910", "RETOMAR": "#7d3c98", "EXPLOTA": "#e67e22",
    "SEGUIR": "#2471a3", "EMPUJAR": "#1e8449",
}

def prompt_parte_editorial(agenda: list) -> str:
    lineas = "\n".join(
        f"  {i+1}. [{it['accion']}] {it['titulo']} ({it['cant_medios']} medios; {it['motivo']})"
        for i, it in enumerate(agenda[:10])
    )
    return f"""Sos editor jefe de Olé. Esta es la agenda priorizada de forma automática.
Por cada ítem, en UNA sola línea, decime por qué le importa a un lector argentino y un ángulo concreto para la nota. Telegráfico, español rioplatense, sin relleno.

{lineas}"""

def prompt_brief_item(item: dict) -> str:
    fuentes_ctx = ""
    if item.get("noticias"):
        fuentes_ctx = "\nCómo lo titularon otros medios:\n" + "\n".join(
            f'  • [{n["fuente"]["nombre"]}] {n["titulo"]}' for n in item["noticias"][:6]
        )
    return f"""Sos editor jefe de Olé.

{FRAMEWORK_ANGULOS}{bloque_criterios()}

Para este tema, dame un mini-brief telegráfico en español rioplatense:
VALOR: por qué importa para el hincha (qué está en juego, no cuántos medios lo tienen).
ÁNGULOS: los 2 mejores ángulos del framework aplicados a ESTE tema — que ningún otro medio haya usado (mirá cómo titularon ellos).
TÍTULO: un título filoso para el mejor ángulo.

TEMA: {item["titulo"]}{fuentes_ctx}"""

# ─── EXTRACCIÓN HTML ──────────────────────────────────────────────────────────
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "es-AR,es;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Cache-Control": "no-cache",
    "Referer": "https://www.google.com/",
}

_GENERIC_IMAGE_PATTERNS_EARLY = [
    "logo", "brand", "favicon", "default", "placeholder",
    "og-default", "og_default", "share-default",
    "ole-logo", "ole_logo", "icon",
]

def _es_imagen_generica(img_url: str) -> bool:
    if not img_url:
        return True
    return any(pat in img_url.lower() for pat in _GENERIC_IMAGE_PATTERNS_EARLY)

def _extraer_imagen_rss_item(item_raw: str) -> str:
    m = re.search(r'<media:content[^>]+url=["\']([^"\']+)["\']', item_raw)
    if m:
        src = m.group(1)
        if src.startswith("http") and not src.endswith(".gif") and not _es_imagen_generica(src):
            return src
    m = re.search(r'<media:thumbnail[^>]+url=["\']([^"\']+)["\']', item_raw)
    if m:
        src = m.group(1)
        if src.startswith("http") and not src.endswith(".gif") and not _es_imagen_generica(src):
            return src
    m = re.search(r'<enclosure[^>]+type=["\']image/[^"\']*["\'][^>]+url=["\']([^"\']+)["\']', item_raw)
    if not m:
        m = re.search(r'<enclosure[^>]+url=["\']([^"\']+)["\'][^>]+type=["\']image/[^"\']*["\']', item_raw)
    if m:
        src = m.group(1)
        if src.startswith("http") and not _es_imagen_generica(src):
            return src
    for tag in ["content:encoded", "description"]:
        m = re.search(rf'<{tag}[^>]*>(.*?)</{tag}>', item_raw, re.DOTALL)
        if m:
            content = m.group(1)
            cdata = re.search(r'<!\[CDATA\[(.*?)\]\]>', content, re.DOTALL)
            if cdata:
                content = cdata.group(1)
            img_m = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', content)
            if img_m:
                src = img_m.group(1)
                if src.startswith("http") and not src.endswith(".gif") and not _es_imagen_generica(src):
                    return src
            wp_m = re.search(r'https?://[^\s"\'<>]+(?:jpg|jpeg|png|webp)', content, re.IGNORECASE)
            if wp_m:
                src = wp_m.group(0)
                if not _es_imagen_generica(src):
                    return src
    return ""

MAX_ANTIGUEDAD_HORAS = 48  # notas de RSS/Google News más viejas que esto se descartan

def _fecha_item_rss(item):
    for tag in ("pubDate", "published", "updated", "dc:date"):
        t = item.find(tag)
        if t and t.get_text(strip=True):
            texto = t.get_text(strip=True)
            try:
                from email.utils import parsedate_to_datetime
                return parsedate_to_datetime(texto)
            except Exception:
                pass
            try:
                return datetime.fromisoformat(texto.replace("Z", "+00:00"))
            except Exception:
                pass
    return None

def extraer_rss(xml_text: str) -> list:
    noticias, vistos = [], set()
    try:
        soup = BeautifulSoup(xml_text, "xml")
        items_raw = re.findall(r'<item[^>]*>(.*?)</item>', xml_text, re.DOTALL | re.IGNORECASE)
        if not items_raw:
            items_raw = re.findall(r'<entry[^>]*>(.*?)</entry>', xml_text, re.DOTALL | re.IGNORECASE)

        for i, item in enumerate(soup.find_all(["item", "entry"])[:MAX_ITEMS]):
            titulo_tag = item.find("title")
            if not titulo_tag:
                continue
            fecha_pub = _fecha_item_rss(item)
            if fecha_pub is not None:
                try:
                    ahora = datetime.now(timezone.utc)
                    fp = fecha_pub if fecha_pub.tzinfo else fecha_pub.replace(tzinfo=timezone.utc)
                    if (ahora - fp).total_seconds() > MAX_ANTIGUEDAD_HORAS * 3600:
                        continue
                except Exception:
                    pass
            titulo = titulo_tag.get_text(strip=True)
            titulo = re.sub(r"<[^>]+>", "", titulo)
            titulo = titulo.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"').replace("&#39;", "'")
            if not titulo or len(titulo) < 15 or len(titulo) > 300 or titulo in vistos:
                continue
            vistos.add(titulo)
            url = None
            link_tag = item.find("link")
            if link_tag:
                url = link_tag.get_text(strip=True) or link_tag.get("href")
            if not url or not url.startswith("http"):
                guid = item.find("guid", isPermaLink="true")
                url = guid.get_text(strip=True) if guid else None
            imagen = ""
            if i < len(items_raw):
                imagen = _extraer_imagen_rss_item(items_raw[i])
            noticias.append({"titulo": titulo, "url": url, "imagen": imagen})
    except Exception:
        pass
    return noticias[:MAX_ITEMS]

def _extraer_ole(html: str, fuente: dict) -> list:
    soup = BeautifulSoup(html, "html.parser")
    BASE = "https://www.ole.com.ar"
    noticias, vistos = [], set()

    _OLE_URL_SKIP = [
        "/autor/", "/autores/", "/firma/", "/columnistas/", "/tag/", "/tags/",
        "/categoria/", "/seccion/", "/author/", "tag=", "/tema/",
        "mailto:", "javascript:", "#",
    ]
    _FIRMA_CLASES = [
        "author", "autor", "firma", "byline", "avatar", "perfil", "profile",
        "journalist", "periodista", "columnist", "writer", "reporter",
        "signature", "bio", "headshot",
    ]

    def resolve_ole(href):
        if not href:
            return None
        if any(s in href for s in _OLE_URL_SKIP):
            return None
        if href.startswith("//"):
            return "https:" + href
        if href.startswith("/"):
            return BASE + href
        if href.startswith("http"):
            return href
        return None

    def _es_img_firma(tag):
        for parent in tag.parents:
            cls = " ".join(parent.get("class", [])).lower()
            pid = (parent.get("id") or "").lower()
            if any(p in cls or p in pid for p in _FIRMA_CLASES):
                return True
            if parent.name in ("article", "section", "main"):
                break
        return False

    def get_best_link(titulo_el, card):
        candidatos = []
        p = titulo_el.find_parent("a")
        if p:
            u = resolve_ole(p.get("href", ""))
            if u:
                candidatos.append(u)
        ic = titulo_el.find("a")
        if ic:
            u = resolve_ole(ic.get("href", ""))
            if u and u not in candidatos:
                candidatos.append(u)
        for a in card.find_all("a", href=True):
            u = resolve_ole(a.get("href", ""))
            if u and u not in candidatos:
                candidatos.append(u)
        parent = card.parent
        for _ in range(4):
            if not parent or parent.name in ("body", "html", "[document]"):
                break
            if parent.name == "a":
                u = resolve_ole(parent.get("href", ""))
                if u and u not in candidatos:
                    candidatos.append(u)
            for a in (parent.find_all("a", href=True, recursive=False) or []):
                u = resolve_ole(a.get("href", ""))
                if u and u not in candidatos:
                    candidatos.append(u)
            parent = parent.parent
        if not candidatos:
            return None
        html_links = [u for u in candidatos if u.endswith(".html")]
        return html_links[0] if html_links else candidatos[0]

    def get_mejor_imagen(card):
        IMG_ATTRS = ["src", "data-src", "data-lazy-src", "data-original", "data-url"]
        candidatos = []
        for tag in card.find_all("img"):
            if _es_img_firma(tag):
                continue
            best_src = ""
            srcset = tag.get("srcset", "") or tag.get("data-srcset", "")
            if srcset:
                parts = [s.strip().split(" ") for s in srcset.split(",") if s.strip()]
                sized = []
                for p in parts:
                    url_s = p[0]
                    try:
                        w = int(p[1].rstrip("w")) if len(p) > 1 and p[1].endswith("w") else 0
                    except ValueError:
                        w = 0
                    sized.append((w, url_s))
                sized.sort(key=lambda x: x[0], reverse=True)
                for _, url_s in sized:
                    if url_s.startswith("http") and not _es_imagen_generica(url_s) and "1x1" not in url_s:
                        best_src = url_s
                        break
            if not best_src:
                for attr in IMG_ATTRS:
                    src = tag.get(attr, "")
                    if (src and src.startswith("http") and not src.endswith(".gif")
                            and not _es_imagen_generica(src) and "1x1" not in src and "pixel" not in src.lower()):
                        best_src = src
                        break
            if best_src:
                score = 0
                cls = " ".join(tag.get("class", [])).lower()
                for good in ["featured", "hero", "portada", "principal", "cover", "thumb", "thumbnail", "wp-post-image", "article-image"]:
                    if good in cls:
                        score += 300
                m = re.search(r'[-/](\d{3,4})x(\d{3,4})[-/.]', best_src)
                if m:
                    score += int(m.group(1)) + int(m.group(2))
                if tag.get("srcset") or tag.get("data-srcset"):
                    score += 100
                candidatos.append((score, best_src))
        if not candidatos:
            return ""
        candidatos.sort(key=lambda x: x[0], reverse=True)
        return candidatos[0][1]

    CARD_SELS_OLE = ["article", "[class*=card]", "[class*=nota]", "[class*=story]", "[class*=article]", "[class*=item]"]
    TITLE_SELS_OLE = ["h1", "h2", "h3", "h4", "[class*=title]", "[class*=titular]", "[class*=headline]"]

    for sel in CARD_SELS_OLE:
        for card in soup.select(sel)[:MAX_ITEMS * 2]:
            if len(noticias) >= MAX_ITEMS:
                break
            titulo_el = None
            for tsel in TITLE_SELS_OLE:
                titulo_el = card.select_one(tsel)
                if titulo_el:
                    break
            if not titulo_el:
                continue
            titulo = titulo_el.get_text(strip=True)
            if len(titulo) < 20 or len(titulo) > 300 or titulo in vistos:
                continue
            vistos.add(titulo)
            url = get_best_link(titulo_el, card)
            img = get_mejor_imagen(card)
            noticias.append({"titulo": titulo, "url": url, "imagen": img})

    if len(noticias) < 8:
        for el in soup.select("h2 a[href], h3 a[href]"):
            if len(noticias) >= MAX_ITEMS:
                break
            titulo = el.get_text(strip=True)
            if len(titulo) < 20 or len(titulo) > 300 or titulo in vistos:
                continue
            url = resolve_ole(el.get("href", ""))
            if url:
                vistos.add(titulo)
                noticias.append({"titulo": titulo, "url": url, "imagen": ""})

    return noticias[:MAX_ITEMS]

def _extraer_as(html: str, fuente: dict) -> list:
    soup = BeautifulSoup(html, "html.parser")
    BASE = "https://as.com"
    noticias, vistos = [], set()
    _AS_URL_SKIP = ["/autor/", "/autores/", "/tag/", "/tags/", "/tema/", "/categoria/", "mailto:", "javascript", "/redaccion/"]

    def resolve_as(href):
        if not href:
            return None
        if any(s in href for s in _AS_URL_SKIP):
            return None
        if href.startswith("javascript") or href == "#":
            return None
        if href.startswith("//"):
            return "https:" + href
        if href.startswith("/"):
            return BASE + href
        if href.startswith("http"):
            return href
        return None

    def get_nota_url_as(titulo_el, card):
        parent_a = titulo_el.find_parent("a")
        if parent_a:
            u = resolve_as(parent_a.get("href", ""))
            if u:
                return u
        inner_a = titulo_el.find("a")
        if inner_a:
            u = resolve_as(inner_a.get("href", ""))
            if u:
                return u
        for a in card.find_all("a", href=True):
            u = resolve_as(a.get("href", ""))
            if u:
                return u
        return None

    CARD_SELS_AS = ["article", "[class*=card]", "[class*=article]", "[class*=noticia]", "[class*=story]", "[class*=item]", "li[class*=list]"]
    TITLE_SELS_AS = ["h1", "h2", "h3", "[class*=title]", "[class*=headline]", "[class*=titular]"]

    for sel in CARD_SELS_AS:
        for card in soup.select(sel)[:MAX_ITEMS * 2]:
            if len(noticias) >= MAX_ITEMS:
                break
            titulo_el = None
            for tsel in TITLE_SELS_AS:
                titulo_el = card.select_one(tsel)
                if titulo_el:
                    break
            if not titulo_el:
                continue
            titulo = titulo_el.get_text(strip=True)
            if len(titulo) < 20 or len(titulo) > 300 or titulo in vistos:
                continue
            vistos.add(titulo)
            url = get_nota_url_as(titulo_el, card)
            img = ""
            for tag in card.find_all("img"):
                src = tag.get("src") or tag.get("data-src") or tag.get("data-lazy-src") or tag.get("data-original") or ""
                if src and src.startswith("http") and not _es_imagen_generica(src):
                    img = src
                    break
            noticias.append({"titulo": titulo, "url": url, "imagen": img})

    if len(noticias) < 8:
        for el in soup.select("h2 a[href], h3 a[href]"):
            if len(noticias) >= MAX_ITEMS:
                break
            titulo = el.get_text(strip=True)
            if len(titulo) < 20 or len(titulo) > 300 or titulo in vistos:
                continue
            vistos.add(titulo)
            url = resolve_as(el.get("href", ""))
            if url:
                noticias.append({"titulo": titulo, "url": url, "imagen": ""})

    return noticias[:MAX_ITEMS]

def _extraer_espn(html: str, fuente: dict) -> list:
    noticias, seen = [], set()
    soup = BeautifulSoup(html, "html.parser")
    BASE = "https://www.espn.com.ar"
    ESPN_SKIP = ["/autor/", "/author/", "/tag/", "/tags/", "/equipo/", "/liga/", "/atletismo/", "javascript:", "mailto:", "#", "/video/"]

    def resolve_espn(href):
        if not href:
            return None
        if any(s in href for s in ESPN_SKIP):
            return None
        if href.startswith("//"):
            return "https:" + href
        if href.startswith("/"):
            return BASE + href
        if href.startswith("http"):
            return href
        return None

    def es_url_nota(url):
        if not url:
            return False
        return "/_/id/" in url or "/nota/" in url or "/historia/" in url or "/story/" in url

    urls_json = []
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")

            def _walk(obj):
                if isinstance(obj, dict):
                    if obj.get("@type") in ("NewsArticle", "Article", "WebPage"):
                        u = obj.get("url") or obj.get("mainEntityOfPage", {}).get("@id", "")
                        if u and es_url_nota(u) and u not in urls_json:
                            urls_json.append(u)
                    if obj.get("@type") == "ItemList":
                        for item in obj.get("itemListElement", []):
                            u = item.get("url") or item.get("item", {}).get("url", "")
                            if u and es_url_nota(u) and u not in urls_json:
                                urls_json.append(u)
                    for v in obj.values():
                        _walk(v)
                elif isinstance(obj, list):
                    for v in obj:
                        _walk(v)
            _walk(data)
        except Exception:
            pass

    urls_html = []
    for a in soup.find_all("a", href=True):
        url = resolve_espn(a.get("href", ""))
        if url and es_url_nota(url) and url not in urls_html:
            urls_html.append(url)

    todas_urls = list(dict.fromkeys(urls_json + urls_html))

    url_to_titulo = {}
    TITLE_SELS_ESPN = ["h1", "h2", "h3", "h4", "[class*=title]", "[class*=Title]", "[class*=headline]", "[class*=Headline]", "[class*=contentItem__title]"]
    for a in soup.find_all("a", href=True):
        url = resolve_espn(a.get("href", ""))
        if not url or not es_url_nota(url):
            continue
        titulo = None
        for sel in TITLE_SELS_ESPN:
            t_el = a.select_one(sel)
            if t_el:
                titulo = t_el.get_text(strip=True)
                break
        if not titulo:
            titulo = a.get_text(strip=True)
        titulo = " ".join(titulo.split())
        if 20 <= len(titulo) <= 300 and url not in url_to_titulo:
            url_to_titulo[url] = titulo

    for url in todas_urls:
        if len(noticias) >= MAX_ITEMS:
            break
        titulo = url_to_titulo.get(url)
        if not titulo:
            slug = url.rstrip("/").split("/")[-1]
            slug = re.sub(r"^\d+-", "", slug)
            titulo = slug.replace("-", " ").title()
            if len(titulo) < 15:
                continue
        if titulo in seen:
            continue
        seen.add(titulo)
        noticias.append({"titulo": titulo, "url": url, "imagen": ""})

    return noticias[:MAX_ITEMS]

def extraer_generico(html: str, fuente: dict) -> list:
    if fuente.get("es_ole"):
        return _extraer_ole(html, fuente)
    if fuente.get("es_as"):
        return _extraer_as(html, fuente)
    if fuente.get("es_espn"):
        return _extraer_espn(html, fuente)
    if fuente.get("es_rss"):
        return extraer_rss(html)

    if fuente.get("es_wp"):
        feed_url = fuente["url"].rstrip("/") + "/feed/"
        try:
            resp = requests.get(feed_url, headers=_FETCH_HEADERS, timeout=(3, 8))
            if resp.status_code == 200 and "<rss" in resp.text[:500]:
                return extraer_rss(resp.text)
        except Exception:
            pass

    soup = BeautifulSoup(html, "html.parser")
    base_url = re.match(r"https?://[^/]+", fuente["url"])
    base = base_url.group(0) if base_url else ""
    noticias, vistos = [], set()

    CARD_SELS = ["article", "[class*=card]", "[class*=story]", "[class*=nota]", "[class*=item]", "[class*=news]"]
    TITLE_SELS = ["h1", "h2", "h3", "h4", "[class*=title]", "[class*=headline]", "[class*=titular]"]

    def resolve_url(href):
        if not href or href.startswith("javascript") or href == "#":
            return None
        if href.startswith("//"):
            return "https:" + href
        if href.startswith("/"):
            return base + href
        if href.startswith("http"):
            return href
        return None

    def get_url(el, titulo_el):
        link = titulo_el.find_parent("a") or titulo_el.find("a") or el.find("a")
        if link:
            return resolve_url(link.get("href", ""))
        return None

    AUTOR_PATTERNS = [
        "author", "autor", "firma", "byline", "avatar", "perfil", "profile",
        "journalist", "periodista", "columnist", "writer", "reporter",
        "signature", "bio", "headshot",
    ]

    def _es_img_autor(tag):
        for parent in tag.parents:
            cls = " ".join(parent.get("class", [])).lower()
            pid = (parent.get("id") or "").lower()
            combined = cls + " " + pid
            if any(p in combined for p in AUTOR_PATTERNS):
                return True
            if parent == tag.parent.parent.parent:
                break
        return False

    def _img_score(tag, src):
        score = 0
        try:
            w = int(tag.get("width") or tag.get("data-width") or 0)
            h = int(tag.get("height") or tag.get("data-height") or 0)
            score += w + h
        except (ValueError, TypeError):
            pass
        cls = " ".join(tag.get("class", [])).lower()
        for good in ["featured", "hero", "portada", "principal", "cover", "thumb", "thumbnail",
                     "featured-image", "post-image", "article-image", "nota-img", "card-img",
                     "wp-post-image", "attachment-", "size-large", "size-full", "size-medium_large",
                     "wp-block-image", "entry-thumb"]:
            if good in cls:
                score += 500
        for bad in AUTOR_PATTERNS:
            if bad in cls:
                score -= 9999
        if _es_img_autor(tag):
            score -= 9999
        if tag.get("srcset") or tag.get("data-srcset"):
            score += 200
        m = re.search(r'[-/](\d{3,4})x(\d{3,4})[-/.]', src)
        if m:
            score += int(m.group(1)) + int(m.group(2))
        alt = (tag.get("alt") or "").lower()
        if alt and len(alt) > 5 and "logo" not in alt:
            score += 50
        return score

    def get_imagen(el):
        IMG_ATTRS = ["src", "data-src", "data-lazy-src", "data-original", "data-url", "data-image"]
        candidatos = []
        for tag in el.find_all("img"):
            best_src = ""
            srcset = tag.get("srcset", "") or tag.get("data-srcset", "")
            if srcset:
                parts = [s.strip().split(" ") for s in srcset.split(",") if s.strip()]
                sized = []
                for p in parts:
                    url = p[0]
                    try:
                        w = int(p[1].rstrip("w")) if len(p) > 1 and p[1].endswith("w") else 0
                    except ValueError:
                        w = 0
                    sized.append((w, url))
                sized.sort(key=lambda x: x[0], reverse=True)
                for _, url in sized:
                    if url.startswith("http") and not _es_imagen_generica(url) and "1x1" not in url:
                        best_src = url
                        break
            if not best_src:
                for attr in IMG_ATTRS:
                    src = tag.get(attr, "")
                    if (src and src.startswith("http") and not src.endswith(".gif")
                            and not _es_imagen_generica(src) and "1x1" not in src and "pixel" not in src.lower()):
                        best_src = src
                        break
            if best_src:
                score = _img_score(tag, best_src)
                candidatos.append((score, best_src))
        for tag in el.find_all(style=True):
            m = re.search(r'background(?:-image)?:\s*url\(["\']?(https?://[^"\')\s]+)["\']?\)', tag["style"])
            if m:
                src = m.group(1)
                if not _es_imagen_generica(src) and "1x1" not in src:
                    cls = " ".join(tag.get("class", [])).lower()
                    score = 100
                    for bad in AUTOR_PATTERNS:
                        if bad in cls:
                            score = -9999
                    candidatos.append((score, src))
        if not candidatos:
            return ""
        candidatos.sort(key=lambda x: x[0], reverse=True)
        best_score, best_src = candidatos[0]
        return best_src if best_score > -100 else ""

    for sel in CARD_SELS:
        for card in soup.select(sel)[:MAX_ITEMS * 2]:
            if len(noticias) >= MAX_ITEMS:
                break
            titulo_el = None
            for tsel in TITLE_SELS:
                titulo_el = card.select_one(tsel)
                if titulo_el:
                    break
            if not titulo_el:
                continue
            titulo = titulo_el.get_text(strip=True)
            if len(titulo) < 20 or len(titulo) > 300 or titulo in vistos:
                continue
            vistos.add(titulo)
            url = get_url(card, titulo_el)
            imagen = get_imagen(card)
            noticias.append({"titulo": titulo, "url": url, "imagen": imagen})

    if len(noticias) < 8:
        for sel in ["h2", "h3"]:
            for el in soup.select(sel)[:MAX_ITEMS * 2]:
                if len(noticias) >= MAX_ITEMS:
                    break
                titulo = el.get_text(strip=True)
                if len(titulo) < 20 or len(titulo) > 300 or titulo in vistos:
                    continue
                vistos.add(titulo)
                link = el.find_parent("a") or el.find("a")
                url = resolve_url(link.get("href", "")) if link else None
                noticias.append({"titulo": titulo, "url": url, "imagen": ""})

    return noticias[:MAX_ITEMS]

# ─── FALLBACK UNIVERSAL: GOOGLE NEWS RSS ─────────────────────────────────────
GNEWS_LOC = {
    "dimarzio": ("it", "IT", "IT:it"), "calciomer": ("it", "IT", "IT:it"),
    "gazzetta": ("it", "IT", "IT:it"), "corriere": ("it", "IT", "IT:it"),
    "guardian": ("en-US", "US", "US:en"), "skysports": ("en-US", "US", "US:en"),
    "bbc": ("en-US", "US", "US:en"), "cbssport": ("en-US", "US", "US:en"),
    "goal": ("en-US", "US", "US:en"), "espnint": ("en-US", "US", "US:en"),
    "sportnews": ("en-US", "US", "US:en"), "fifa": ("en-US", "US", "US:en"),
    "lequipe": ("fr", "FR", "FR:fr"), "footmercato": ("fr", "FR", "FR:fr"),
    "globo": ("pt-BR", "BR", "BR:pt-419"),
    "record": ("pt-PT", "PT", "PT:pt-150"),
    "marca": ("es", "ES", "ES:es"), "as": ("es", "ES", "ES:es"),
    "sport": ("es", "ES", "ES:es"), "mundodep": ("es", "ES", "ES:es"),
    "geglobo": ("pt-BR", "BR", "BR:pt-419"),
    "latercera": ("es-419", "CL", "CL:es-419"), "abola": ("pt-PT", "PT", "PT:pt-150"),
    "bild": ("de", "DE", "DE:de"), "skyit": ("it", "IT", "IT:it"),
}

def _gnews_url(dominio: str, fuente_id: str = "") -> str:
    hl, gl, ceid = GNEWS_LOC.get(fuente_id, ("es-419", "AR", "AR:es-419"))
    return f"https://news.google.com/rss/search?q=site:{dominio}&hl={hl}&gl={gl}&ceid={ceid}"

def _limpiar_titulo_gnews(titulo: str) -> str:
    if " - " in titulo:
        base = titulo.rsplit(" - ", 1)[0].strip()
        if len(base) >= 15:
            return base
    return titulo

def _dominio_de(fuente: dict) -> str:
    if fuente.get("gnews"):
        return fuente["gnews"]
    url = fuente.get("url", "")
    m = re.search(r"https?://(?:www\.)?([^/]+)", url)
    return m.group(1) if m else ""

def _fallback_gnews(fuente: dict, motivo_original: str) -> dict:
    if fuente.get("sin_fallback"):
        return {"id": fuente["id"], "noticias": [], "error": motivo_original}
    dominio = _dominio_de(fuente)
    if not dominio or "news.google.com" in fuente.get("url", ""):
        return {"id": fuente["id"], "noticias": [], "error": motivo_original}
    try:
        resp = requests.get(_gnews_url(dominio, fuente.get("id", "")), headers=HEADERS, timeout=(3, 8))
        resp.raise_for_status()
        noticias = extraer_rss(resp.text)
        for n in noticias:
            n["titulo"] = _limpiar_titulo_gnews(n["titulo"])
        if noticias:
            return {"id": fuente["id"], "noticias": noticias[:MAX_ITEMS], "error": None, "via": "gnews"}
    except Exception:
        pass
    return {"id": fuente["id"], "noticias": [], "error": motivo_original}

def fetch_ultimas_ole() -> list:
    """Scrapea ole.com.ar/ultimas-noticias: el listado completo publicado, más allá de portada."""
    try:
        resp = requests.get("https://www.ole.com.ar/ultimas-noticias", headers=HEADERS, timeout=(3, 8))
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        contenedores = soup.select("div[data-noteid]")
        if not contenedores:
            contenedores = soup.select("li[class*='listado']")
        out, vistos = [], set()
        for cont in contenedores:
            a = cont.find("a", href=True)
            if not a:
                continue
            href = a["href"]
            if href.startswith("/"):
                href = "https://www.ole.com.ar" + href
            if not href.startswith("http") or href in vistos:
                continue
            t_el = cont.find(["h1", "h2", "h3", "h4"])
            titulo = " ".join((t_el.get_text(strip=True) if t_el else a.get_text(strip=True)).split())
            if len(titulo) < 16:
                slug = href.rstrip("/").split("/")[-1].replace(".html", "")
                titulo = slug.replace("-", " ").capitalize()
                if len(titulo) < 16:
                    continue
            vistos.add(href)
            out.append({"titulo": titulo[:250], "url": href, "imagen": ""})
        return out[:MAX_ITEMS]
    except Exception:
        return []

def fetch_cobertura_ole_gnews() -> list:
    try:
        resp = requests.get(_gnews_url("ole.com.ar", "ole"), headers=HEADERS, timeout=(3, 8))
        resp.raise_for_status()
        return [{"titulo": _limpiar_titulo_gnews(n["titulo"]), "url": n.get("url") or ""} for n in extraer_rss(resp.text)]
    except Exception:
        return []

def fetch_fuente(fuente: dict) -> dict:
    try:
        resp = requests.get(fuente["url"], headers=HEADERS, timeout=(3, 8))
        resp.raise_for_status()

        if fuente.get("es_rss"):
            resp.encoding = resp.encoding or "utf-8"
            noticias = extraer_rss(resp.text)
            for n in noticias:
                n["titulo"] = _limpiar_titulo_gnews(n["titulo"])
            noticias = noticias[:MAX_ITEMS]
            if noticias:
                return {"id": fuente["id"], "noticias": noticias, "error": None}
            if not fuente.get("sin_fallback"):
                return _fallback_gnews(fuente, "feed rss vacío")
            return {"id": fuente["id"], "noticias": [], "error": "feed rss vacío"}

        content_type = resp.headers.get("content-type", "").lower()
        if "charset=" in content_type:
            encoding = content_type.split("charset=")[-1].split(";")[0].strip()
        else:
            raw = resp.content
            sniff = raw[:4096].decode("ascii", errors="ignore").lower()
            if 'charset="utf-8"' in sniff or "charset=utf-8" in sniff:
                encoding = "utf-8"
            elif 'charset="iso-8859-1"' in sniff or 'charset=iso-8859-1' in sniff:
                encoding = "iso-8859-1"
            elif 'charset="windows-1252"' in sniff or 'charset=windows-1252' in sniff:
                encoding = "windows-1252"
            else:
                detected = (resp.apparent_encoding or "utf-8").lower()
                encoding = "utf-8" if detected in ("ascii", "") else detected
        resp.encoding = encoding
        noticias = extraer_generico(resp.text, fuente)
        if noticias:
            if fuente.get("gnews_extra") and len(noticias) < 25:
                extra = _fallback_gnews(fuente, "")
                vistos = {frozenset(normalizar_titulo(n["titulo"])) for n in noticias}
                for n in extra.get("noticias", []):
                    k = frozenset(normalizar_titulo(n["titulo"]))
                    if k and k not in vistos:
                        vistos.add(k)
                        noticias.append(n)
                noticias = noticias[:MAX_ITEMS]
            return {"id": fuente["id"], "noticias": noticias, "error": None}
        return _fallback_gnews(fuente, "scraping directo: 0 notas")
    except Exception as e:
        return _fallback_gnews(fuente, str(e))

# ─── IA — CLAUDE ──────────────────────────────────────────────────────────────
MODELO_ANALISIS = "claude-sonnet-5"              # para notas y análisis profundos
MODELO_ECONOMICO = "claude-haiku-4-5-20251001"   # para partes/resúmenes: mucho más barato

def call_claude(prompt: str, api_key: str, max_tokens: int = 2000, modelo: str = None) -> str:
    if not api_key:
        raise RuntimeError("Falta la API key de Anthropic.")
    try:
        client = anthropic.Anthropic(api_key=api_key)
        msg = client.messages.create(
            model=modelo or MODELO_ANALISIS,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as e:
        raise RuntimeError(f"Error al llamar a Claude: {e}") from e
    partes = [b.text for b in msg.content if getattr(b, "type", None) == "text"]
    return "\n".join(partes).strip()

PERLITA_KEYWORDS = [
    "insolito", "insólito", "viral", "furor", "locura", "increible", "increíble",
    "inedito", "inédito", "record", "récord", "historico", "histórico", "blooper",
    "papelon", "papelón", "escandalo", "escándalo", "polemica", "polémica",
    "sorpresa", "sorprend", "curios", "emotivo", "conmovedor", "gesto de",
    "no lo vio nadie", "nunca visto", "por primera vez", "el más", "la más",
    "wtf", "video:", "el video", "la foto", "se volvio", "se volvió",
    "estallo", "estalló", "explotaron", "memes", "reaccion", "reacción",
]

def candidatas_perlitas(resultados: dict, max_items: int = 30) -> list:
    out, vistos = [], set()
    for f in TODAS_FUENTES:
        for n in resultados.get(f["id"], []):
            t = n.get("titulo", "")
            tl = t.lower()
            if not any(k in tl for k in PERLITA_KEYWORDS):
                continue
            k = frozenset(normalizar_titulo(t))
            if not k or k in vistos:
                continue
            vistos.add(k)
            out.append((f["nombre"], t[:150]))
            if len(out) >= max_items:
                return out
    return out

def prompt_analisis_general(resultados: dict) -> str:
    tendencias = calcular_tendencias(resultados)[:30]
    perlitas = candidatas_perlitas(resultados)
    bloque_perlitas = "\n".join(f"  • [{f}] {t}" for f, t in perlitas) or "  (ninguna detectada en esta pasada)"
    lineas = "\n".join(
        f"{i+1}. {c['titulo'][:130]} — {c['cant_medios']} medios "
        f"({c.get('nac', 0)} nac / {c.get('intl', 0)} int) · Olé: {'sí' if c.get('tiene_ole') else 'NO'}"
        for i, c in enumerate(tendencias)
    )
    return f"""Sos editor jefe de un portal deportivo argentino. Abajo están los 30 temas
que más medios están cubriendo AHORA (de {len(TODAS_FUENTES)} medios monitoreados),
ya agrupados y ordenados por volumen.{bloque_criterios()}

Escribí un RESUMEN EJECUTIVO en español rioplatense, directo:

LECTURA GENERAL — 3 líneas: qué domina la conversación y qué tono tiene el día.

LOS 30 TEMAS, agrupados por eje (Selección / mercado de pases / torneo local /
fútbol internacional / otros). Por cada tema, UNA sola línea: qué pasó y por qué
importa para el hincha argentino. No repitas el título textual: interpretalo.
Marcá con ⚠️ los temas donde Olé no tiene cobertura.

DATO SALIENTE — 1 línea: la asimetría o el patrón más llamativo del panorama.

PERLITAS — 3 a 5 joyitas con potencial de tráfico: lo viral, lo insólito, la
sorpresa, el gesto, el récord raro. Elegilas de la canasta de candidatas (y del
top 30 si alguna califica). Por cada una: por qué puede rendir en una línea +
un título con gancho. Si una candidata es puro clickbait sin sustancia, salteala.

TEMAS:
{lineas}

CANDIDATAS A PERLITA (detectadas por señales de viralidad/rareza):
{bloque_perlitas}"""

def _temas_por_origen(resultados: dict, origen: str, top: int = 25) -> list:
    tendencias = calcular_tendencias(resultados)
    out = []
    for c in tendencias:
        if origen == "nac" and c.get("nac", 0) >= 1:
            out.append(c)
        elif origen == "int" and c.get("intl", 0) >= 1:
            out.append(c)
    return out[:top]

def prompt_parte_nacional(resultados: dict) -> str:
    """Análisis general enfocado en el fútbol argentino (mismo nivel que el
    Análisis General, pero solo temas que cubren los medios nacionales)."""
    tendencias = _temas_por_origen(resultados, "nac", 30)
    perlitas = candidatas_perlitas(resultados)
    bloque_perlitas = "\n".join(f"  • [{f}] {t}" for f, t in perlitas) or "  (ninguna detectada en esta pasada)"
    lineas = "\n".join(
        f"{i+1}. {c['titulo'][:130]} — {c['cant_medios']} medios "
        f"({c.get('nac', 0)} nac / {c.get('intl', 0)} int) · Olé: {'sí' if c.get('tiene_ole') else 'NO'}"
        for i, c in enumerate(tendencias)
    )
    return f"""Sos editor jefe de un diario deportivo argentino. Abajo están los temas
del FÚTBOL ARGENTINO que más medios están cubriendo AHORA, ya agrupados y
ordenados por volumen.{bloque_criterios()}

Escribí un RESUMEN EJECUTIVO en español rioplatense, directo:

LECTURA GENERAL — 3 líneas: qué domina la conversación del fútbol argentino y qué tono tiene el día.

LOS TEMAS, agrupados por eje (River / Boca / otros clubes / Selección / mercado local / otros).
Por cada tema, UNA sola línea: qué pasó y por qué importa para el hincha argentino.
No repitas el título textual: interpretalo. Marcá con ⚠️ los temas donde Olé no tiene cobertura.

DATO SALIENTE — 1 línea: la asimetría o el patrón más llamativo del panorama local.

PERLITAS — 3 a 5 joyitas con potencial de tráfico: lo viral, lo insólito, la
sorpresa, el gesto, el récord raro. Elegilas de la canasta de candidatas (y del
top si alguna califica). Por cada una: por qué puede rendir en una línea +
un título con gancho. Si una candidata es puro clickbait sin sustancia, salteala.

TEMAS:
{lineas}

CANDIDATAS A PERLITA (detectadas por señales de viralidad/rareza):
{bloque_perlitas}"""

def prompt_parte_internacional(resultados: dict) -> str:
    """Análisis general del fútbol internacional (mismo nivel que el Análisis
    General) más una sección enfocada en impacto argentino."""
    tendencias = _temas_por_origen(resultados, "int", 30)
    relevantes = notas_exterior_relevantes(resultados, 15)
    lineas = "\n".join(
        f"{i+1}. {c['titulo'][:130]} — {c['cant_medios']} medios"
        for i, c in enumerate(tendencias)
    )
    bloque_ar = "\n".join(f"  • [{r['fuente']['nombre']}] {r['titulo'][:120]}"
                           + (f" ({' · '.join(r['entidades'][:3])})" if r["entidades"] else "")
                           for r in relevantes) or "  (nada con gancho argentino ahora)"
    return f"""Sos editor de la sección internacional de un diario deportivo argentino.
Abajo están los temas del FÚTBOL MUNDIAL que más medios están cubriendo AHORA,
ya agrupados y ordenados por volumen.{bloque_criterios()}

Escribí un RESUMEN EJECUTIVO en español rioplatense, directo:

PANORAMA INTERNACIONAL — 3 líneas: qué domina el fútbol mundial hoy (ligas,
Champions, mercado europeo, figuras) y qué tono tiene.

LOS TEMAS DEL MUNDO, agrupados por eje (España / Italia / Inglaterra / mercado
europeo / Champions y copas / Brasil y Sudamérica / otros). Por cada tema, UNA
sola línea: qué pasó y por qué importa. No repitas el título: interpretalo.

DATO SALIENTE — 1 línea: el patrón o la historia más llamativa del fútbol mundial hoy.

🧉 IMPACTO ARGENTINO — la sección clave: de todo lo internacional, qué le toca
directamente a un hincha argentino (jugadores argentinos en el exterior, rivales
de la Selección, nombres que suenan para el fútbol local). Por cada uno, una
línea con el ángulo para trabajarlo desde acá.

TEMAS INTERNACIONALES:
{lineas}

NOTAS DEL EXTERIOR CON GANCHO ARGENTINO:
{bloque_ar}"""

def prompt_informe_ole(resultados: dict, analisis: dict, temas_editor: str = "") -> str:
    tendencias = calcular_tendencias(resultados)
    top = tendencias[:10]
    faltantes = analisis.get("faltantes_en_ole", [])[:15]
    bloque_top = "\n\n".join(
        f"TEMA {i+1}: {c['titulo'][:130]} ({c['cant_medios']} medios · Olé: {'sí' if c.get('tiene_ole') else 'NO'})\n"
        + "\n".join(f"   · [{n['fuente']['nombre']}] {n['titulo'][:110]}" for n in c.get("noticias", [])[:5])
        for i, c in enumerate(top)
    )
    bloque_falt = "\n".join(f"  • [{f['fuente_nombre']}] {f['titulo'][:120]}" for f in faltantes) or "  (ninguno)"

    if temas_editor.strip():
        pedidos = [t.strip() for t in re.split(r"[\n,;]+", temas_editor) if t.strip()]
        ctx = []
        for pedido in pedidos[:6]:
            kp = normalizar_titulo(pedido)
            relacionadas = []
            for c in tendencias:
                if solapamiento(kp, normalizar_titulo(c["titulo"])) >= 0.3 or \
                   any(w in c["titulo"].lower() for w in pedido.lower().split() if len(w) > 3):
                    for n in c.get("noticias", [])[:4]:
                        relacionadas.append(f"     · [{n['fuente']['nombre']}] {n['titulo'][:110]}")
                    if len(relacionadas) >= 6:
                        break
            ctx.append(f"TEMA PEDIDO: {pedido}\n" + ("\n".join(relacionadas[:6]) if relacionadas else "     (sin cobertura detectada en el panorama actual)"))
        bloque_ctx = "\n\n".join(ctx)
        return f"""Sos editor de Olé. No generes noticias descriptivas: competí por el
significado antes que por la información.

{FRAMEWORK_ANGULOS}{bloque_criterios()}

El editor quiere trabajar ESTOS temas hoy. Para cada uno, dale munición:

POR CADA TEMA PEDIDO:
  • Los niveles de lectura que manda (qué cambió / a quién afecta / qué emoción /
    qué patrón / qué consecuencia).
  • CINCO ÁNGULOS del framework, cada uno con su título sugerido y el tipo de
    ángulo nombrado. Si la competencia ya cubrió el tema (tenés sus títulos),
    priorizá los ángulos que NADIE usó.
  • Tu recomendación: cuál es EL ángulo ganador y por qué.
  • Un dato o pregunta que le falta a la nota para ser imbatible.

Español rioplatense, directo, sin relleno.

TEMAS PEDIDOS (con cómo los tituló la competencia):
{bloque_ctx}"""

    return f"""Sos editor de Olé. No generes noticias descriptivas: para cada hecho
detectá patrones, conflictos, consecuencias, cambios de estatus, héroes
inesperados, paradojas e impacto emocional en el hincha. Competí por el
significado antes que por la información.

{FRAMEWORK_ANGULOS}{bloque_criterios()}

Abajo tenés los 10 temas más calientes (con cómo tituló cada medio) y los
huecos donde Olé no entró. Escribí en español rioplatense:

FOCOS SUGERIDOS — Elegí los 6 temas con más potencial. Por cada uno:
  • El tema en una línea y qué nivel de lectura manda (qué cambió / a quién
    afecta / qué emoción genera / qué patrón revela / qué consecuencia deja).
  • AL MENOS CINCO ÁNGULOS DISTINTOS del framework, cada uno con su título
    sugerido. Nombrá el tipo de ángulo. Evitá los que ya usó la competencia
    (sus títulos están a la vista).
  • Tu recomendación: cuál de los cinco es EL ángulo, y por qué.

HUECOS RÁPIDOS — De la lista de faltantes, marcá los 3 que valen la pena y el
ángulo de entrada en una línea; ignorá el resto.

TOP 10 TEMAS:
{bloque_top}

FALTANTES EN OLÉ:
{bloque_falt}"""

_FETCH_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "es-AR,es;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Cache-Control": "no-cache",
    "Referer": "https://www.google.com/",
}

def _extraer_cuerpo_nota(url: str, max_chars: int = 6000) -> str:
    """Extrae el texto completo de una nota. En vez de quedarse con el primer
    selector que matchee (y cortar a los primeros N párrafos, como antes),
    evalúa TODOS los contenedores candidatos y se queda con el que tenga más
    texto real en párrafos — heurística tipo "readability", mucho más robusta
    across los ~65 sitios distintos que monitoreamos. Devuelve el artículo
    completo (hasta max_chars), no un resumen de 4-5 párrafos."""
    if not url or not url.startswith("http"):
        return ""
    try:
        resp = requests.get(url, headers=_FETCH_HEADERS, timeout=15, allow_redirects=True)
        if resp.status_code != 200:
            return ""
        soup = BeautifulSoup(resp.text, "html.parser")
        for tag in soup(["script", "style", "nav", "header", "footer", "aside", "form", "figure", "noscript", "iframe", "svg"]):
            tag.decompose()
        BODY_SELS = [
            "article .article-body", "article .nota-cuerpo", "article .entry-content",
            "article .article-content", "article .post-content", "article .content-body",
            ".article__body", ".nota__cuerpo", ".article-text", ".news-body",
            "[class*=article-body]", "[class*=nota-cuerpo]", "[class*=entry-content]",
            "[class*=article-content]", "[class*=post-body]", "[class*=cuerpo-nota]",
            "[itemprop=articleBody]", "[class*=story-body]", "[class*=text-content]",
            "[class*=story-content]", "[class*=post-text]",
            "article", "main", "[role=main]",
        ]

        mejor_texto, mejor_len = "", 0
        for sel in BODY_SELS:
            for el in soup.select(sel):
                parrafos = [p.get_text(" ", strip=True) for p in el.find_all("p") if len(p.get_text(strip=True)) > 30]
                texto = "\n\n".join(parrafos)
                if len(texto) > mejor_len:
                    mejor_texto, mejor_len = texto, len(texto)

        # Fallback final: todos los <p> del documento, por si ningún
        # contenedor de la lista de arriba rindió resultado suficiente.
        if mejor_len < 250:
            parrafos = [p.get_text(" ", strip=True) for p in soup.find_all("p") if len(p.get_text(strip=True)) > 40]
            texto_todos = "\n\n".join(parrafos)
            if len(texto_todos) > mejor_len:
                mejor_texto = texto_todos

        return mejor_texto[:max_chars].strip()
    except Exception:
        return ""

def scrape_cuerpos_notas(titulares: list, max_notas: int = 6, max_chars: int = 4000) -> list:
    enriquecidos = []
    con_url = [item for item in titulares if item["noticia"].get("url")][:max_notas]
    sin_url = [item for item in titulares if not item["noticia"].get("url")]

    if con_url:
        with ThreadPoolExecutor(max_workers=5) as ex:
            futures = {ex.submit(_extraer_cuerpo_nota, item["noticia"]["url"], max_chars): item for item in con_url}
            for future in as_completed(futures):
                item = futures[future]
                try:
                    cuerpo = future.result()
                except Exception:
                    cuerpo = ""
                enriquecidos.append({**item, "cuerpo": cuerpo, "ok": bool(cuerpo)})

    for item in sin_url:
        enriquecidos.append({**item, "cuerpo": "", "ok": False})

    ids_procesados = {id(item) for item in con_url + sin_url}
    for item in titulares:
        if id(item) not in ids_procesados:
            enriquecidos.append({**item, "cuerpo": "", "ok": False})

    return enriquecidos

def prompt_nota_rapida(tema: str, titulares_enriquecidos: list, estilo: str, tipo_nota: str, contexto_extra: str = "") -> str:
    con_cuerpo = [t for t in titulares_enriquecidos if t.get("ok")]
    solo_titulo = [t for t in titulares_enriquecidos if not t.get("ok")]
    tiene_info_real = len(con_cuerpo) > 0

    bloque_completo = ""
    if con_cuerpo:
        partes = []
        for t in con_cuerpo:
            f, n = t["fuente"], t["noticia"]
            partes.append(f"── [{f['nombre']}] {n['titulo']}\nURL: {n.get('url','')}\nTEXTO:\n{t['cuerpo']}")
        bloque_completo = "\n\n".join(partes)

    bloque_titulares = ""
    if solo_titulo:
        bloque_titulares = "\n".join(f"  • [{t['fuente']['nombre']}] {t['noticia']['titulo']}" for t in solo_titulo)

    estilos = {
        "Informativa": (
            "Estilo agencia de noticias argentina (Télam/NA). "
            "Tono directo, neutro, sin opinión ni adjetivos innecesarios. "
            "Verbos en pasado o presente simple. Oraciones cortas. "
            "Los datos concretos van primero, el contexto después."
        ),
        "Analítica": (
            "Estilo agencia argentina con profundidad. "
            "Tono directo y neutro pero con contexto, antecedentes y proyección. "
            "Cada afirmación tiene respaldo en las fuentes. "
            "Párrafos más largos, estructura de causa-efecto."
        ),
        "Urgente/Flash": (
            "Estilo despacho urgente de agencia argentina. "
            "Máximo 3 párrafos muy cortos. Verbo en presente. "
            "Solo el dato central, sin contexto. "
            "Primera oración = toda la noticia en una línea."
        ),
    }
    tipos = {
        "Nota completa": (
            "Nota con subtítulos (SIN lead/cierre clásico de manual). Estructura:\n"
            "- Primer párrafo suelto: el hecho central en 2-3 oraciones directas, sin subtítulo.\n"
            "- Luego 3 o 4 secciones, cada una con subtítulo informativo en negrita (## Subtítulo), "
            "seguido de 2-3 párrafos de 60-80 palabras.\n"
            "- La nota entera: entre 400 y 550 palabras.\n"
            "- Los subtítulos deben ser concretos y periodísticos, no genéricos "
            "(ej: '## La lesión y los plazos de recuperación' en vez de '## Contexto')."
        ),
        "Solo titulares alternativos": (
            "Generá 8 titulares alternativos: 2 impactantes, 2 SEO, "
            "2 para redes sociales (con gancho), 2 estilo agencia neutro. "
            "Para cada uno agregá una línea corta explicando el enfoque."
        ),
        "Esqueleto + ángulos": (
            "Esqueleto con subtítulos numerados (## 1. ..., ## 2. ...) "
            "y una línea describiendo qué información va en cada sección. "
            "Al final, 3 ángulos posibles con título sugerido para cada uno."
        ),
    }

    instruccion_angulo = f"""ANTES DE ESCRIBIR — el método (obligatorio, no lo saltees):
Identificá en silencio los 6 niveles de lectura del hecho: qué pasó, qué cambió,
a quién afecta, qué emoción genera, qué tendencia o patrón revela y qué
consecuencia deja. Después elegí UN ángulo del framework de Olé (cambio de
estatus, patrón, consecuencia, héroe inesperado, conflicto, paradoja, identidad,
tendencia, qué significa, el día después) y construí TODA la nota alrededor de
ese ángulo: el título compite por el significado, no por la información; el
primer párrafo instala el ángulo, no la crónica.{bloque_criterios()}

"""

    if tiene_info_real:
        instruccion_alucinacion = instruccion_angulo + """⚠️ REGLAS ANTI-ALUCINACIÓN (CRÍTICAS — leelas antes de escribir una sola palabra):
- Usá ÚNICAMENTE datos, cifras, citas y hechos que aparezcan textualmente en las FUENTES de abajo.
- Prohibido agregar contexto histórico, estadísticas o antecedentes que no estén en los textos.
- Las citas entre comillas SOLO pueden ser frases que aparezcan literalmente en los textos fuente.
- Si un dato no está en los textos, escribí [DATO A CONFIRMAR] en su lugar. Sin excepciones.
- Si dos fuentes se contradicen, mencioná la contradicción explícitamente."""

        instruccion_formato = """
FORMATO DE RESPUESTA OBLIGATORIO — respetá este orden exacto:

════════════════════════════════════
NOTA
════════════════════════════════════
[Aquí va la nota redactada según el estilo y entregable solicitado]


════════════════════════════════════
TABLA DE VERIFICACIÓN
════════════════════════════════════
Lista TODOS los datos concretos que usaste en la nota (cifras, nombres, citas, hechos).
Para cada uno indicá:
• DATO: el dato exacto como aparece en la nota
• FUENTE: nombre del medio de donde lo tomaste
• VERIFICADO: ✅ si está textualmente en el cuerpo scrapeado | ⚠️ si solo aparece en el titular | ❌ si no encontrás respaldo

Ejemplo de fila:
• DATO: "sufrió un desgarro en el isquiotibial derecho" | FUENTE: TyC Sports | VERIFICADO: ✅

════════════════════════════════════
ÁNGULOS ALTERNATIVOS
════════════════════════════════════
3 enfoques distintos del framework (nombrá el tipo de ángulo), con título sugerido para cada uno.
"""
        bloque_fuentes = f"""=== FUENTES CON TEXTO COMPLETO ({len(con_cuerpo)}) — de estas podés extraer datos ===
{bloque_completo}"""
        if bloque_titulares:
            bloque_fuentes += f"""

=== FUENTES SOLO CON TITULAR ({len(solo_titulo)}) — NO inferir datos, solo confirmar que el tema existe ===
{bloque_titulares}"""
    else:
        instruccion_alucinacion = instruccion_angulo + """⚠️ MODO ESQUELETO SEGURO — no se pudo leer el cuerpo de ninguna nota.
No redactes la nota. En cambio, seguí el formato de respuesta obligatorio de abajo."""

        instruccion_formato = """
FORMATO DE RESPUESTA OBLIGATORIO:

════════════════════════════════════
ESQUELETO DE NOTA
════════════════════════════════════
Estructura con secciones numeradas y vacías, listas para que el redactor complete.
Indicá qué tipo de información va en cada sección.

════════════════════════════════════
DATOS CONFIRMADOS (solo desde titulares)
════════════════════════════════════
Lista con bullet points. Solo lo que los titulares permiten afirmar con certeza.
Formato: • [dato] — confirmado por: [medio]

════════════════════════════════════
DATOS A CONFIRMAR ANTES DE PUBLICAR
════════════════════════════════════
Lista de preguntas concretas que el redactor debe responder antes de publicar.

════════════════════════════════════
ÁNGULOS ALTERNATIVOS
════════════════════════════════════
3 enfoques distintos según qué datos aparezcan, con título sugerido para cada uno.
"""
        bloque_fuentes = f"""=== SOLO TITULARES DISPONIBLES ({len(solo_titulo)}) ===
{bloque_titulares}"""

    return f"""Sos un redactor deportivo de un portal argentino. Tu tarea es trabajar sobre este tema:

TEMA: {tema}
ESTILO: {estilos.get(estilo, estilos["Informativa"])}
ENTREGABLE: {tipos.get(tipo_nota, tipos["Nota completa"])}

{instruccion_alucinacion}
{instruccion_formato}

{bloque_fuentes}

Escribí en español rioplatense con voseo. Tono de agencia de noticias argentina (estilo Télam, NA, DyN).
Reglas de estilo periodístico argentino:
- Los clubes se nombran como los nombra la prensa argentina: "River" (no "River Plate"), "Boca" (no "Boca Juniors"), "Racing" (no "Racing Club"), "San Lorenzo" (no "San Lorenzo de Almagro"), "Independiente", "Huracán", "Vélez", "Lanús", "Defensa", etc.
- Los seleccionados: "la Selección" o "el equipo nacional" (no "la Albiceleste" salvo que sea en un contexto festivo), "la Sub-20", "la Sub-23".
- Los jugadores se mencionan por apellido a partir de la segunda referencia: "Messi" (no "La Pulga"), "Di María" (no "el Fideo"). Sin apodos en texto de agencia.
- Cargos y funciones en minúscula: "el entrenador Scaloni", "el presidente Laporta", "el director técnico".
- Evitá frases como "en este contexto", "cabe destacar", "vale la pena mencionar", "a su vez", "en tanto".
- No uses adjetivos valorativos ("increíble", "impresionante", "histórico", "brillante") salvo que estén textualmente en la fuente.
- Nunca uses "lead", "bajada" ni ningún término de manual de redacción en el cuerpo de la nota.
{("\n=== CONTEXTO ADICIONAL DEL REDACTOR ===\n" + contexto_extra + "\n(Podés usar este contexto libremente en la nota — es información aportada por el redactor, no requiere verificación de fuente.)") if contexto_extra else ""}
"""

def prompt_tono_editorial(query: str, titulares_filtrados: list) -> str:
    bloque = "\n".join(f'[{item["fuente"]["nombre"]}] {item["noticia"]["titulo"]}' for item in titulares_filtrados)
    return f"""Analizá el tono editorial de estos titulares sobre "{query}".

TITULARES ({len(titulares_filtrados)} en total):
{bloque}

Respondé ÚNICAMENTE con un objeto JSON válido, sin texto antes ni después, sin backticks.
El JSON debe tener exactamente esta estructura:

{{
  "resumen": "una oración que describe el tono general de la cobertura",
  "distribucion": {{
    "positivo": 0,
    "negativo": 0,
    "neutro": 0,
    "alarmista": 0,
    "expectante": 0
  }},
  "por_medio": [
    {{
      "medio": "nombre del medio",
      "tono": "positivo|negativo|neutro|alarmista|expectante",
      "titular": "el titular analizado",
      "razon": "una línea explicando por qué ese tono"
    }}
  ],
  "patrones": [
    "patrón editorial detectado 1",
    "patrón editorial detectado 2"
  ]
}}

Tonos posibles:
- positivo: elogio, logro, buena noticia
- negativo: crítica, fracaso, escándalo, mala noticia
- neutro: informativo puro, sin carga valorativa
- alarmista: urgencia, crisis, peligro, dramatismo
- expectante: incertidumbre, espera, "podría", "se espera"
"""

# ─── IMÁGENES OG (para noticias que no trajeron imagen en el scraping) ───────
_IMAGE_CACHE = {}

def fetch_og_image(url: str) -> str:
    if not url or not url.startswith("http") or "google.com/search" in url:
        return ""
    if url in _IMAGE_CACHE:
        return _IMAGE_CACHE[url]
    try:
        resp = requests.get(url, headers=_FETCH_HEADERS, timeout=10, allow_redirects=True)
        soup = BeautifulSoup(resp.text, "html.parser")
        for meta in [
            soup.find("meta", property="og:image"), soup.find("meta", property="og:image:url"),
            soup.find("meta", attrs={"name": "twitter:image"}), soup.find("meta", attrs={"name": "twitter:image:src"}),
        ]:
            if not meta:
                continue
            candidate = meta.get("content", "") or meta.get("value", "") or ""
            if candidate and not _es_imagen_generica(candidate):
                _IMAGE_CACHE[url] = candidate
                return candidate
        img_selectors = [
            "article figure img", "article .image img", "article img[src]",
            ".nota-cuerpo img", ".article-body img", ".entry-content img", "figure img",
            "[class*=hero] img", "[class*=featured] img", "[class*=portada] img", "[class*=cover] img",
        ]
        for sel in img_selectors:
            for tag in soup.select(sel):
                src = tag.get("src") or tag.get("data-src") or tag.get("data-lazy-src") or tag.get("data-original") or ""
                if (src and src.startswith("http") and not src.endswith(".gif")
                        and not _es_imagen_generica(src) and "1x1" not in src and "pixel" not in src.lower()):
                    _IMAGE_CACHE[url] = src
                    return src
        _IMAGE_CACHE[url] = ""
        return ""
    except Exception:
        _IMAGE_CACHE[url] = ""
        return ""

def fetch_og_images_batch(noticias: list) -> None:
    urls_sin_cache = [
        n["url"] for n in noticias
        if n.get("url") and n["url"] not in _IMAGE_CACHE and "news.google.com" not in n["url"]
    ]
    if not urls_sin_cache:
        return
    with ThreadPoolExecutor(max_workers=8) as ex:
        futures = [ex.submit(fetch_og_image, u) for u in urls_sin_cache]
        for f in as_completed(futures):
            try:
                f.result()
            except Exception:
                pass

# ─── ESTADO EN MEMORIA (equivalente a st.session_state — un solo usuario) ────
ESTADO = {
    "resultados": {},        # fid -> [noticias]
    "tendencias": [],
    "prev_tendencias": [],
    "ole_analisis": {},
    "agenda": [],
    "ultima_act": None,
    "canasta": [],           # [{fuente, noticia:{titulo,url}, cuerpo}]
}

# ─── MODELOS ──────────────────────────────────────────────────────────────────
class ActualizarRequest(BaseModel):
    solo_nac: bool = False
    solo_int: bool = False

class FiltrarCustomRequest(BaseModel):
    keywords: list[str]
    solo_ar: bool = False

class ApiKeyRequest(BaseModel):
    api_key: str

class InformeOleRequest(BaseModel):
    api_key: str
    temas_editor: str = ""

class BriefRequest(BaseModel):
    api_key: str
    titulo: str

class FuenteRef(BaseModel):
    id: str
    nombre: str
    color: str = "#666666"

class NoticiaRef(BaseModel):
    titulo: str
    url: Optional[str] = None

class ItemSeleccionado(BaseModel):
    fuente: FuenteRef
    noticia: NoticiaRef

class NotaRapidaRequest(BaseModel):
    api_key: str
    tema: str
    titulares: list[ItemSeleccionado] = []
    estilo: str = "Informativa"
    tipo: str = "Nota completa"
    contexto_extra: str = ""

class TonoEditorialRequest(BaseModel):
    api_key: str
    query: str
    titulares: list[ItemSeleccionado]

class CuerpoRequest(BaseModel):
    url: str

class CanastaAgregarRequest(BaseModel):
    titulo: str
    url: Optional[str] = None
    fuente: FuenteRef
    scrape_cuerpo: bool = True

class CanastaQuitarRequest(BaseModel):
    index: int

class CanastaEditarCuerpoRequest(BaseModel):
    index: int
    cuerpo: str

class CanastaGenerarRequest(BaseModel):
    api_key: str
    tema: str = ""
    estilo: str = "Informativa"
    tipo: str = "Nota completa"
    contexto_extra: str = ""

class ImagenesRequest(BaseModel):
    urls: list[str]

# ─── HELPERS DE SERIALIZACIÓN ────────────────────────────────────────────────
def _fuente_pub(f: dict) -> dict:
    return {"id": f["id"], "nombre": f["nombre"], "color": f["color"]}

def _resultados_a_fuentes(resultados: dict, grupo: list) -> list:
    return [
        {**_fuente_pub(f), "items": resultados.get(f["id"], []), "status": "ok" if resultados.get(f["id"]) else "empty"}
        for f in grupo
    ]

# ─── ENDPOINTS: FUENTES Y ACTUALIZACIÓN ──────────────────────────────────────
@app.get("/api/fuentes")
def get_fuentes():
    return {
        "nacionales": [_fuente_pub(f) for f in FUENTES_NAC],
        "internacionales": [_fuente_pub(f) for f in FUENTES_INT],
        "primicias": [_fuente_pub(f) for f in FUENTES_ESP],
        "core_version": CORE_VERSION,
        "total": len(TODAS_FUENTES),
    }

_ACTUALIZAR_LOCK = threading.Lock()

def _respuesta_estado(total_medios: int, actualizacion_parcial: bool = False, errores: list = None) -> dict:
    """Arma la misma forma de respuesta tanto para una actualización recién
    hecha como para el estado ya cacheado en ESTADO (sin disparar scraping)."""
    resultados = ESTADO["resultados"]
    total_noticias = sum(len(v) for v in resultados.values())
    return {
        "cargado": True,
        "fuentes": {
            "nacionales": _resultados_a_fuentes(resultados, FUENTES_NAC),
            "internacionales": _resultados_a_fuentes(resultados, FUENTES_INT),
            "primicias": _resultados_a_fuentes(resultados, FUENTES_ESP),
        },
        "total_noticias": total_noticias,
        "total_medios": total_medios,
        "actualizacion_parcial": actualizacion_parcial,
        "errores": errores or [],
        "ultima_act": ESTADO["ultima_act"],
        "tendencias": ESTADO["tendencias"],
        "ole_analisis": ESTADO["ole_analisis"],
        "agenda": ESTADO["agenda"],
        "hay_momentum_previo": bool(ESTADO["prev_tendencias"]),
        "stats": {
            "tendencias": len(ESTADO["tendencias"]),
            "sin_ole": len([t for t in ESTADO["tendencias"] if not t["tiene_ole"]]),
            "con_ole": len([t for t in ESTADO["tendencias"] if t["tiene_ole"]]),
            "hot": len([t for t in ESTADO["tendencias"] if t["cant_medios"] / max(len(TODAS_FUENTES), 1) >= 0.20]),
        },
    }

def _ejecutar_actualizacion(solo_nac: bool = False, solo_int: bool = False) -> dict:
    """El scraping en sí — la usan tanto el endpoint /api/actualizar (cuando
    el usuario aprieta el botón) como la pre-carga automática al arrancar el
    servidor. El lock evita que las dos cosas escaneen al mismo tiempo y se
    pisen los resultados."""
    with _ACTUALIZAR_LOCK:
        actualizacion_parcial = solo_nac or solo_int
        _ole = next(f for f in FUENTES_NAC if f["id"] == "ole")
        if solo_nac:
            fuentes_a_cargar = FUENTES_NAC + FUENTES_ESP
        elif solo_int:
            fuentes_a_cargar = FUENTES_INT + [_ole] + FUENTES_ESP
        else:
            fuentes_a_cargar = TODAS_FUENTES

        resultados_nuevos, errores = {}, []
        with ThreadPoolExecutor(max_workers=min(30, len(fuentes_a_cargar))) as executor:
            futures = {executor.submit(fetch_fuente, f): f for f in fuentes_a_cargar}
            for future in as_completed(futures):
                res = future.result()
                resultados_nuevos[res["id"]] = res["noticias"]
                if res["error"]:
                    errores.append(f"{res['id']}: {res['error']}")

        if actualizacion_parcial:
            fusion = dict(ESTADO["resultados"])
            fusion.update(resultados_nuevos)
            resultados_nuevos = fusion

        ESTADO["resultados"] = resultados_nuevos
        ESTADO["ultima_act"] = datetime.now().isoformat()
        ESTADO["ole_analisis"] = analizar_ole_vs_compecencia_safe(resultados_nuevos)
        ESTADO["prev_tendencias"] = ESTADO.get("tendencias") or []
        ESTADO["tendencias"] = calcular_tendencias(resultados_nuevos)
        ESTADO["agenda"] = construir_agenda(ESTADO["tendencias"], ESTADO["ole_analisis"], ESTADO["prev_tendencias"])

        return _respuesta_estado(len(fuentes_a_cargar), actualizacion_parcial, errores)

@app.post("/api/actualizar")
def actualizar(req: ActualizarRequest):
    return _ejecutar_actualizacion(req.solo_nac, req.solo_int)

@app.get("/api/estado-inicial")
def estado_inicial():
    """El frontend llama esto apenas abre la página — si el servidor ya
    tiene noticias cargadas (de la pre-carga automática al arrancar, o de
    una visita anterior reciente), las devuelve al toque, sin escanear de
    nuevo. Si todavía no hay nada, dice cargado:false y el frontend se
    queda esperando un toque más o muestra el botón manual."""
    if not ESTADO.get("ultima_act"):
        return {"cargado": False}
    return _respuesta_estado(len(ESTADO["resultados"]))

@app.on_event("startup")
def _precarga_automatica():
    """Apenas arranca el proceso (deploy nuevo, o Render despertando del
    sueño), dispara un escaneo completo en segundo plano — así, si alguien
    entra mientras el proceso ya está despierto, encuentra todo cargado sin
    tocar el botón. No bloquea el arranque del servidor: corre en un thread
    aparte."""
    def _tarea():
        try:
            _ejecutar_actualizacion(False, False)
            print("✅ Pre-carga automática al arrancar: completada.")
        except Exception as e:
            print(f"⚠️ Pre-carga automática al arrancar falló: {e}")
    threading.Thread(target=_tarea, daemon=True).start()

@app.get("/api/imagenes")
def api_imagenes_noop():
    return {"ok": True}

@app.post("/api/imagenes")
def fetch_imagenes(req: ImagenesRequest):
    """Trae og:images para URLs que todavía no tienen imagen — se llama al
    abrir un tab de medio, igual que hacía render_news_cards() en Streamlit."""
    noticias = [{"url": u} for u in req.urls if u]
    fetch_og_images_batch(noticias)
    return {"images": {u: _IMAGE_CACHE.get(u, "") for u in req.urls if u}}

# ─── ENDPOINTS: BÚSQUEDA Y FILTROS ───────────────────────────────────────────
@app.get("/api/buscar")
def buscar(q: str, ambito: str = "todas"):
    q = q.strip().lower()
    if len(q) < 3:
        return {"resultados": [], "total": 0}
    fuentes_b = (FUENTES_NAC if ambito == "nacionales" else FUENTES_INT if ambito == "internacionales" else TODAS_FUENTES)
    out = []
    for f in fuentes_b:
        hits = [n for n in ESTADO["resultados"].get(f["id"], []) if q in n["titulo"].lower()]
        if hits:
            out.append({"fuente": _fuente_pub(f), "items": hits})
    total = sum(len(g["items"]) for g in out)
    return {"resultados": out, "total": total}

@app.get("/api/filtros-tematicos")
def get_filtros_tematicos():
    return {fid: {"titulo": c["titulo"], "desc": c["desc"]} for fid, c in FILTROS_TEMATICOS.items()}

@app.get("/api/filtrar")
def filtrar(filtro_id: str, solo_ar: bool = False):
    notas = filtrar_por_tema(ESTADO["resultados"], filtro_id, solo_ar=solo_ar)
    return {"notas": notas}

@app.post("/api/filtrar-custom")
def filtrar_custom_ep(req: FiltrarCustomRequest):
    notas = filtrar_custom(ESTADO["resultados"], req.keywords, solo_ar=req.solo_ar)
    return {"notas": notas}

# ─── ENDPOINTS: TENDENCIAS, OLÉ, RANKING, EXTERIOR ───────────────────────────
@app.get("/api/tendencias")
def get_tendencias():
    nac_ids = [f["id"] for f in FUENTES_NAC]
    int_ids = [f["id"] for f in FUENTES_INT]
    return {
        "tendencias": ESTADO["tendencias"],
        "nube_nac": nube_palabras(ESTADO["resultados"], nac_ids, "#00a846"),
        "nube_int": nube_palabras(ESTADO["resultados"], int_ids, "#1a7fc1"),
        "total_medios": len(TODAS_FUENTES),
    }

@app.get("/api/ole")
def get_ole():
    return ESTADO["ole_analisis"]

@app.get("/api/exclusivos-todos")
def get_exclusivos_todos():
    out = []
    for f in TODAS_FUENTES:
        for n in ESTADO["resultados"].get(f["id"], []):
            if es_exclusivo(n["titulo"], f["id"], ESTADO["resultados"]):
                out.append({"fuente": _fuente_pub(f), "noticia": n})
    return {"exclusivos": out[:100], "total": len(out)}

@app.get("/api/ranking-entidades")
def get_ranking_entidades():
    return {"ranking": ranking_entidades(ESTADO["resultados"])}

@app.get("/api/notas-exterior")
def get_notas_exterior():
    return {"notas": notas_exterior_relevantes(ESTADO["resultados"])}

@app.get("/api/ole-ultimas")
def get_ole_ultimas():
    """Bonus: el listado completo de ole.com.ar/ultimas-noticias (más allá de
    la portada), útil para ver qué publicó Olé que no llegó a home."""
    return {"noticias": fetch_ultimas_ole()}

# ─── ENDPOINTS: AGENDA ────────────────────────────────────────────────────────
@app.get("/api/agenda")
def get_agenda():
    return {"agenda": ESTADO["agenda"], "hay_momentum_previo": bool(ESTADO["prev_tendencias"])}

@app.post("/api/parte-editorial")
def parte_editorial(req: ApiKeyRequest):
    if not ESTADO["agenda"]:
        raise HTTPException(400, "No hay agenda calculada todavía — actualizá las fuentes primero.")
    try:
        texto = call_claude(prompt_parte_editorial(ESTADO["agenda"]), req.api_key, 1200)
        return {"resultado": texto}
    except Exception as e:
        raise HTTPException(400, str(e))

@app.post("/api/brief")
def brief(req: BriefRequest):
    item = next((it for it in ESTADO["agenda"] if it["titulo"] == req.titulo), None)
    if item is None:
        item = {"titulo": req.titulo, "noticias": []}
    try:
        texto = call_claude(prompt_brief_item(item), req.api_key, 400)
        return {"resultado": texto}
    except Exception as e:
        raise HTTPException(400, str(e))

# ─── ENDPOINTS: IA — ANÁLISIS GENERAL / INFORME OLÉ ──────────────────────────
@app.post("/api/analisis-general")
def analisis_general(req: ApiKeyRequest):
    if not ESTADO["resultados"]:
        raise HTTPException(400, "Actualizá las fuentes primero.")
    try:
        texto = call_claude(prompt_analisis_general(ESTADO["resultados"]), req.api_key, 5000)
        return {"resultado": texto}
    except Exception as e:
        raise HTTPException(400, str(e))

@app.post("/api/parte-nacional")
def parte_nacional(req: ApiKeyRequest):
    """Análisis del día enfocado solo en fútbol argentino — misma calidad que
    el Análisis General, con el modelo económico (Haiku) por ser un reporte
    más frecuente y acotado."""
    if not ESTADO["resultados"]:
        raise HTTPException(400, "Actualizá las fuentes primero.")
    try:
        texto = call_claude(prompt_parte_nacional(ESTADO["resultados"]), req.api_key, 5000, modelo=MODELO_ECONOMICO)
        return {"resultado": texto}
    except Exception as e:
        raise HTTPException(400, str(e))

@app.post("/api/parte-internacional")
def parte_internacional(req: ApiKeyRequest):
    """Análisis del día del fútbol mundial + sección de impacto argentino."""
    if not ESTADO["resultados"]:
        raise HTTPException(400, "Actualizá las fuentes primero.")
    try:
        texto = call_claude(prompt_parte_internacional(ESTADO["resultados"]), req.api_key, 5000, modelo=MODELO_ECONOMICO)
        return {"resultado": texto}
    except Exception as e:
        raise HTTPException(400, str(e))

@app.post("/api/informe-ole")
def informe_ole(req: InformeOleRequest):
    if not ESTADO["resultados"]:
        raise HTTPException(400, "Actualizá las fuentes primero.")
    analisis = ESTADO["ole_analisis"] or analizar_ole_vs_compecencia_safe(ESTADO["resultados"])
    try:
        texto = call_claude(prompt_informe_ole(ESTADO["resultados"], analisis, req.temas_editor), req.api_key, 5000)
        return {"resultado": texto}
    except Exception as e:
        raise HTTPException(400, str(e))

# ─── ENDPOINTS: NOTA RÁPIDA Y TONO EDITORIAL ─────────────────────────────────
@app.post("/api/scrape-cuerpo")
def scrape_cuerpo_ep(req: CuerpoRequest):
    return {"cuerpo": _extraer_cuerpo_nota(req.url, max_chars=8000)}

@app.post("/api/nota-rapida")
def nota_rapida(req: NotaRapidaRequest):
    if not req.api_key:
        raise HTTPException(400, "Falta la API key de Anthropic.")
    tema = req.tema.strip()
    titulares = [t.dict() for t in req.titulares]
    if not tema and titulares:
        tema = titulares[0]["noticia"]["titulo"]
    if not tema:
        raise HTTPException(400, "Seleccioná al menos una nota o escribí un tema.")

    con_url = [t for t in titulares if t["noticia"].get("url")]
    max_scrape = min(6, len(con_url))
    if con_url:
        titulares_enr = scrape_cuerpos_notas(titulares, max_notas=max_scrape)
    else:
        titulares_enr = [{**t, "cuerpo": "", "ok": False} for t in titulares]

    try:
        prompt = prompt_nota_rapida(tema, titulares_enr, req.estilo, req.tipo, req.contexto_extra.strip())
        texto = call_claude(prompt, req.api_key, 3500)
    except Exception as e:
        raise HTTPException(400, str(e))

    ok_count = sum(1 for t in titulares_enr if t.get("ok"))
    modo = "con cuerpo completo" if ok_count > 0 else "esqueleto seguro (sin cuerpo)"
    return {"resultado": texto, "modo": modo, "ok_count": ok_count, "total": len(titulares_enr)}

@app.post("/api/tono-editorial")
def tono_editorial(req: TonoEditorialRequest):
    if not req.api_key:
        raise HTTPException(400, "Falta la API key de Anthropic.")
    if not req.titulares:
        raise HTTPException(400, "No hay titulares para analizar.")
    titulares = [t.dict() for t in req.titulares][:40]
    try:
        raw_json = call_claude(prompt_tono_editorial(req.query, titulares), req.api_key, 1200)
        clean = raw_json.strip()
        if clean.startswith("```"):
            clean = clean.strip("`")
            if clean.lower().startswith("json"):
                clean = clean[4:]
        resultado = json.loads(clean.strip())
    except json.JSONDecodeError:
        raise HTTPException(400, "Error al parsear la respuesta de Claude. Probá de nuevo.")
    except Exception as e:
        raise HTTPException(400, str(e))
    return {"resultado": resultado}

# ─── ENDPOINTS: CANASTA ───────────────────────────────────────────────────────
@app.get("/api/canasta")
def get_canasta():
    return {"canasta": ESTADO["canasta"]}

@app.post("/api/canasta/agregar")
def canasta_agregar(req: CanastaAgregarRequest):
    ya = any(item["noticia"]["titulo"] == req.titulo for item in ESTADO["canasta"])
    if ya:
        return {"canasta": ESTADO["canasta"], "agregado": False}
    cuerpo = ""
    if req.scrape_cuerpo and req.url:
        cuerpo = _extraer_cuerpo_nota(req.url, max_chars=8000)
    ESTADO["canasta"].append({
        "fuente": req.fuente.dict(),
        "noticia": {"titulo": req.titulo, "url": req.url},
        "cuerpo": cuerpo,
    })
    return {"canasta": ESTADO["canasta"], "agregado": True}

@app.post("/api/canasta/quitar")
def canasta_quitar(req: CanastaQuitarRequest):
    if 0 <= req.index < len(ESTADO["canasta"]):
        ESTADO["canasta"].pop(req.index)
    return {"canasta": ESTADO["canasta"]}

@app.post("/api/canasta/vaciar")
def canasta_vaciar():
    ESTADO["canasta"] = []
    return {"canasta": ESTADO["canasta"]}

@app.post("/api/canasta/rescrapear")
def canasta_rescrapear(req: CanastaQuitarRequest):
    if 0 <= req.index < len(ESTADO["canasta"]):
        url = ESTADO["canasta"][req.index]["noticia"].get("url")
        ESTADO["canasta"][req.index]["cuerpo"] = _extraer_cuerpo_nota(url, max_chars=8000) if url else ""
    return {"canasta": ESTADO["canasta"]}

@app.post("/api/canasta/editar-cuerpo")
def canasta_editar_cuerpo(req: CanastaEditarCuerpoRequest):
    """Permite pegar o corregir a mano el texto de una nota de la canasta —
    por si el scraping automático no trajo todo el contenido (paywalls,
    sitios con JS, anti-bot, etc.)."""
    if 0 <= req.index < len(ESTADO["canasta"]):
        ESTADO["canasta"][req.index]["cuerpo"] = req.cuerpo.strip()
    return {"canasta": ESTADO["canasta"]}

def _texto_item_canasta(item: dict) -> str:
    fuente_n = item["fuente"]["nombre"]
    titulo_n = item["noticia"]["titulo"]
    url_n = item["noticia"].get("url") or "(sin URL)"
    cuerpo_n = (item.get("cuerpo") or "").strip()
    partes = [f"[{fuente_n}] {titulo_n}", f"URL: {url_n}"]
    if cuerpo_n:
        partes.append(f"TEXTO:\n{cuerpo_n}")
    return "\n".join(partes)

@app.get("/api/canasta/exportar")
def canasta_exportar():
    texto = "\n\n──────────────────────\n\n".join(_texto_item_canasta(item) for item in ESTADO["canasta"])
    return {"texto": texto}

@app.post("/api/canasta/generar")
def canasta_generar(req: CanastaGenerarRequest):
    if not req.api_key:
        raise HTTPException(400, "Falta la API key de Anthropic.")
    canasta = ESTADO["canasta"]
    if not canasta:
        raise HTTPException(400, "La canasta está vacía.")
    tema_final = req.tema.strip() or canasta[0]["noticia"]["titulo"]

    titulares_enr, sin_cuerpo = [], []
    for item in canasta:
        if item.get("cuerpo"):
            titulares_enr.append({"fuente": item["fuente"], "noticia": item["noticia"], "cuerpo": item["cuerpo"], "ok": True})
        elif item["noticia"].get("url"):
            sin_cuerpo.append(item)
        else:
            titulares_enr.append({"fuente": item["fuente"], "noticia": item["noticia"], "cuerpo": "", "ok": False})

    if sin_cuerpo:
        max_extra = min(6, len(sin_cuerpo))
        enriquecidos_extra = scrape_cuerpos_notas(sin_cuerpo, max_notas=max_extra)
        for enr in enriquecidos_extra:
            titulo_enr = enr["noticia"]["titulo"]
            for i, ci in enumerate(ESTADO["canasta"]):
                if ci["noticia"]["titulo"] == titulo_enr and enr.get("cuerpo"):
                    ESTADO["canasta"][i]["cuerpo"] = enr["cuerpo"]
                    break
            titulares_enr.append(enr)

    try:
        prompt = prompt_nota_rapida(tema_final, titulares_enr, req.estilo, req.tipo, req.contexto_extra.strip())
        texto = call_claude(prompt, req.api_key, 3000)
    except Exception as e:
        raise HTTPException(400, str(e))

    ok_count = sum(1 for t in titulares_enr if t.get("ok"))
    return {"resultado": texto, "ok_count": ok_count, "total": len(titulares_enr)}

# ─── FRONTEND ESTÁTICO ────────────────────────────────────────────────────────
_FRONTEND_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "frontend")
app.mount("/", StaticFiles(directory=_FRONTEND_DIR, html=True), name="frontend")
print(f"✅ Backend listo — {CORE_VERSION} · {len(TODAS_FUENTES)} fuentes ({len(FUENTES_NAC)} nac + {len(FUENTES_INT)} int + {len(FUENTES_ESP)} primicias)")
