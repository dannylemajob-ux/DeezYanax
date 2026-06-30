# 🎵 DeezYanax — Music Downloader

Clone web completo de @deezload2bot. Busca y descarga música de Deezer y Spotify directamente desde el navegador.

---

## 🏗️ Arquitectura

```
[Navegador / Frontend]
        ↕ HTTP REST API
[Backend FastAPI (Python)]
        ↕ Telethon (MTProto)
[Tu cuenta de Telegram]
        ↕ Telegram
[@deezload2bot]
```

---

## 📋 Requisitos

- **Python 3.10+**
- **Cuenta de Telegram** con número de teléfono verificado
- **API credentials de Telegram** (gratis, tarda 2 minutos obtenerlos)

---

## 🚀 Instalación paso a paso

### Paso 1 — Obtener credenciales de Telegram

1. Ve a **https://my.telegram.org/apps**
2. Inicia sesión con tu número de teléfono
3. Crea una nueva aplicación (nombre y plataforma = cualquiera)
4. Copia el **`api_id`** (número) y el **`api_hash`** (string largo)

### Paso 2 — Configurar el backend

```bash
cd backend
pip install -r requirements.txt
```

Copia el archivo de configuración:
```bash
cp .env.example .env
```

Edita `.env` con tus datos:
```env
TELEGRAM_API_ID=12345678
TELEGRAM_API_HASH=abcdef1234567890abcdef1234567890
TELEGRAM_PHONE=+591XXXXXXXXX
TELEGRAM_SESSION=deezyanax_session
DEEZYANAX_AUTH_USER=yanax
DEEZYANAX_AUTH_PASSWORD=tu_password
DEEZYANAX_AUTH_SECRET=un_secreto_largo_aleatorio
```

### Paso 3 — Autenticar tu sesión (solo la primera vez)

```bash
cd backend
python setup_session.py
```

Esto pedirá el código de verificación que llega a tu Telegram.
Después de ingresar el código, se crea el archivo `deezyanax_session.session`.

> ⚠️ **Guarda ese archivo .session** — sin él no funciona el servidor.

### Paso 4 — Iniciar el servidor

```bash
cd backend
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

Verás:
```
🚀 Iniciando cliente Telegram...
✅ Conectado como: tu_usuario
INFO:     Uvicorn running on http://0.0.0.0:8000
```

### Paso 5 — Abrir el frontend

Abre el archivo `frontend/index.html` en tu navegador.

O si prefieres servirlo con Python:
```bash
cd frontend
python -m http.server 3000
# Luego ve a http://localhost:3000
```

---

## ☁️ Deploy en GitHub + Render

En Render la app corre en un servidor remoto. Por eso el backend no puede abrir tu gestor de archivos ni escribir en tu carpeta `Descargas`.

Flujo esperado en Render:

1. El backend descarga canciones temporalmente.
2. Si el navegador soporta selector de carpeta, el botón **DESCARGA TU CARPETA** pide elegir una carpeta local.
3. Si el navegador no soporta selector de carpeta, la app descarga un ZIP.

### Variables de entorno en Render

Configura estas variables como secret/env vars:

```env
TELEGRAM_API_ID=12345678
TELEGRAM_API_HASH=abcdef1234567890abcdef1234567890
TELEGRAM_PHONE=+591XXXXXXXXX
TELEGRAM_SESSION_STRING=pega_aqui_el_string_generado
DEEZYANAX_AUTH_USER=yanax
DEEZYANAX_AUTH_PASSWORD=tu_password
DEEZYANAX_AUTH_SECRET=un_secreto_largo_aleatorio
```

Para generar `TELEGRAM_SESSION_STRING` localmente:

```bash
cd backend
PRINT_SESSION_STRING=1 python setup_session.py
```

Copia el valor impreso en Render. No subas archivos `.session` a GitHub.

### Blueprint

El proyecto incluye `render.yaml`. En Render, crea un Blueprint desde el repo de GitHub y completa las variables secretas.

---

## 🎛️ Cómo usar la app

### Búsqueda por texto
1. Escribe el nombre de la canción, artista o álbum en la barra de búsqueda
2. Selecciona el tipo: **Canción / Álbum / Artista / Playlist / Global**
3. Presiona Enter o el botón 🔍
4. De los resultados, haz clic en el que quieras
5. El audio se descargará y aparecerá listo para escuchar o descargar

### Búsqueda por link directo
1. Pega un link de Deezer o Spotify en el campo de link
   - `https://www.deezer.com/track/XXXXXXX`
   - `https://open.spotify.com/track/XXXXXXX`
   - También funciona con álbumes y playlists
2. Presiona **Descargar**

### Cambiar calidad de audio
- Usa los botones **FLAC / MP3 320 / MP3 128** en la parte superior
- El cambio se aplica a todas las descargas siguientes

---

## 📡 API Reference

El backend expone una REST API en `http://localhost:8000`:

### `GET /health`
Verifica el estado del servidor y la conexión con Telegram.

### `POST /api/search`
Busca música por texto.
```json
{
  "query": "Bohemian Rhapsody",
  "search_type": "track"
}
```
`search_type`: `track` | `album` | `artist` | `playlist` | `global`

### `POST /api/link`
Procesa un link directo de Deezer o Spotify.
```json
{
  "url": "https://www.deezer.com/track/3135556"
}
```

### `POST /api/select`
Hace clic en un botón inline de los resultados del bot.
```json
{
  "message_id": 12345,
  "row_index": 0,
  "button_index": 0
}
```

### `POST /api/quality`
Cambia la calidad de audio.
```json
{
  "quality": "FLAC"
}
```
Opciones: `FLAC` | `MP3_320` | `MP3_128`

### `GET /downloads/{file_name}`
Descarga un archivo de audio por nombre.

---

## ⚠️ Notas importantes

### Limitaciones técnicas
- **Una petición a la vez:** El bot responde secuencialmente. Si haces búsquedas simultáneas pueden mezclarse las respuestas. El backend tiene una cola de requests.
- **Timeout:** El bot tiene timeouts de 20-60 segundos dependiendo de la operación. Los álbumes grandes pueden tardar más.
- **Rate limiting:** No hagas demasiadas peticiones seguidas. El bot puede ignorar mensajes si se satura.

### Archivos descargados
- Los archivos de audio se guardan temporalmente en `backend/downloads/`
- Se limpian automáticamente después de 1 hora
- Para descarga permanente, usa el botón "⬇ Descargar" en la interfaz

### Legalidad
> ⚠️ Este proyecto es de uso **educativo y personal**. La descarga de música protegida por derechos de autor puede infringir los términos de servicio de Deezer/Spotify. Úsalo bajo tu propia responsabilidad. El autor no se hace responsable del uso indebido.

---

## 🔧 Estructura del proyecto

```
deezyanax/
├── backend/
│   ├── main.py              # Servidor FastAPI principal
│   ├── setup_session.py     # Script de autenticación (ejecutar 1 vez)
│   ├── requirements.txt     # Dependencias Python
│   ├── .env.example         # Plantilla de configuración
│   └── downloads/           # Audios descargados (se crea automático)
│
└── frontend/
    └── index.html           # App web completa (SPA)
```

---

## 🐛 Solución de problemas

### "Backend offline" en la app
→ Verifica que el servidor esté corriendo: `uvicorn main:app --port 8000`

### "Telegram desconectado"
→ El archivo `.session` puede estar corrupto. Bórralo y ejecuta `python setup_session.py` de nuevo.

### "El bot no respondió a tiempo"
→ El bot puede estar ocupado. Espera unos segundos y vuelve a intentar.

### "Error de CORS" en el navegador
→ Abre `index.html` directamente (doble clic) o sírvelo con `python -m http.server`. No uses Live Server de VSCode con http en modo mixed.

### El audio no reproduce en el player
→ Algunos formatos FLAC pueden no reproducirse en todos los navegadores. Usa el botón "⬇ Descargar" y abre el archivo localmente.

---

## 📬 Créditos

- **Bot original:** [@deezload2bot](https://t.me/deezload2bot) por @DEDSECemo
- **Librería Telegram:** [Telethon](https://github.com/LonamiWebs/Telethon)
- **Framework backend:** [FastAPI](https://fastapi.tiangolo.com/)
