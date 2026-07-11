# Monitor Deportivo Pro — despliegue en Render

Versión standalone (sin Colab, sin ngrok) del Monitor Deportivo Pro, lista para
desplegar en [Render](https://render.com) con un usuario y contraseña que
protegen toda la app.

## Qué incluye esta carpeta

```
monitor-deportivo-pro/
├── main.py              ← backend FastAPI (toda la lógica de scraping/IA)
├── frontend/
│   └── index.html        ← frontend (HTML/CSS/JS en un solo archivo)
├── requirements.txt       ← dependencias de Python
├── render.yaml            ← blueprint de Render (deploy con un clic)
└── README.md               ← este archivo
```

---

## 1. Subir esto a GitHub

Render despliega desde un repositorio de git. Si no tenés uno todavía:

1. Andá a [github.com/new](https://github.com/new) y creá un repositorio
   (puede ser privado — Render igual puede leerlo).
2. Subí el contenido de esta carpeta. Desde tu computadora, dentro de la
   carpeta `monitor-deportivo-pro`:
   ```bash
   git init
   git add .
   git commit -m "Monitor Deportivo Pro"
   git branch -M main
   git remote add origin https://github.com/TU-USUARIO/TU-REPO.git
   git push -u origin main
   ```
   (También podés arrastrar los archivos directo en la web de GitHub si no
   usás git desde la terminal.)

---

## 2. Crear el servicio en Render

### Opción A — con el blueprint (`render.yaml`), más rápido

1. Entrá a [render.com](https://render.com) y creá una cuenta (no pide
   tarjeta para el plan free).
2. **New +** → **Blueprint** → conectá tu repositorio de GitHub.
3. Render va a leer `render.yaml` solo y te va a pedir que completes
   `BASIC_AUTH_USER` y `BASIC_AUTH_PASS` (quedan guardadas como variables de
   entorno en Render, nunca en el código ni en GitHub).
4. **Apply** y esperá el primer build (2-3 minutos).

### Opción B — manual, si preferís no usar el blueprint

1. **New +** → **Web Service** → conectá el repositorio.
2. Configurá:
   - **Runtime**: Python 3
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `uvicorn main:app --host 0.0.0.0 --port $PORT`
   - **Plan**: Free
3. En **Environment** agregá dos variables:
   - `BASIC_AUTH_USER` → el usuario que quieras
   - `BASIC_AUTH_PASS` → una contraseña (no la que usás para otras cosas)
4. **Create Web Service**.

---

## 3. Listo

Render te da una URL tipo `https://monitor-deportivo-pro.onrender.com`.
Al abrirla, el navegador va a mostrar un cartel pidiendo usuario y
contraseña (autenticación HTTP Basic) — son las que pusiste en el paso
anterior. Una vez adentro, pegá tu API key de Anthropic en la barra lateral
como siempre; esa key se guarda solo en tu navegador, no pasa por Render.

## Cosas para tener en cuenta con el plan free

- **Se duerme a los 15 minutos sin uso.** El primer click después de eso
  tarda 30-60 segundos en despertar el servidor — normal, no es un error.
- **La canasta y el "momentum" de la Agenda se resetean cuando el servicio
  se duerme y vuelve a arrancar**, porque viven en memoria del proceso (no
  hay base de datos). Es el mismo comportamiento que ya tenías en Colab
  cuando reiniciabas el entorno.
- Si en algún momento el cold-start te resulta molesto, el plan **Starter**
  ($7/mes) lo mantiene siempre despierto — se cambia desde el dashboard de
  Render sin tocar el código.

## Actualizar la app más adelante

Cualquier cambio que quieras (nuevas fuentes, features, etc.) se sube igual
que la primera vez:
```bash
git add .
git commit -m "cambios"
git push
```
Render redespliega solo apenas detecta el push.

## Probarlo en tu computadora antes de desplegar (opcional)

```bash
pip install -r requirements.txt
export BASIC_AUTH_USER=facu
export BASIC_AUTH_PASS=loquesea
uvicorn main:app --reload --port 8000
```
Abrís `http://localhost:8000` y te va a pedir el usuario/contraseña igual
que en producción.
