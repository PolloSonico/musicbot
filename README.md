# 🎵 MusicBot para Discord

Bot de Discord que se une a tu canal de voz y reproduce música/videos de YouTube
(por link o buscando por nombre). Hecho en Python con `discord.py` + `yt-dlp` + FFmpeg,
y pensado para **arrancar solo al encender el PC con Windows** (Programador de tareas).

---

## 1. Crear el bot en Discord (una sola vez)

1. Entra a <https://discord.com/developers/applications> → **New Application** → ponle nombre.
2. Menú **Bot**:
   - Pulsa **Reset Token** y copia el token (lo usarás en el paso 3). **No lo compartas con nadie.**
   - Activa **MESSAGE CONTENT INTENT** (sin esto el bot no lee los comandos).
3. Menú **OAuth2 → URL Generator**:
   - Scopes: `bot`
   - Bot Permissions: `View Channels`, `Send Messages`, `Embed Links`, `Add Reactions`,
     `Read Message History`, `Connect`, `Speak`
   - Abre la URL generada e invita al bot a tu servidor.

## 2. Instalar en Windows

Necesitas **Python 3.11 o superior** → <https://www.python.org/downloads/>
(durante la instalación marca **"Add python.exe to PATH"**).

1. Descarga/clona este repositorio en una carpeta fija, por ejemplo `C:\MusicBot`
   (no lo muevas después de registrar la tarea).
2. Doble clic en **`windows\instalar.bat`**. Esto:
   - crea un entorno virtual `.venv` e instala las dependencias,
   - instala **FFmpeg** con `winget` si no lo tienes,
   - crea el archivo `.env`.

   > Si winget acaba de instalar FFmpeg, cierra la ventana y ejecuta `instalar.bat` otra vez
   > para que guarde la ruta de FFmpeg en `.env`.

## 3. Configurar

Abre el archivo **`.env`** con el Bloc de notas:

| Variable | Qué es |
|---|---|
| `DISCORD_TOKEN` | El token del paso 1 |
| `PREFIX` | Prefijo de los comandos (por defecto `!`) |
| `FFMPEG_PATH` | `ffmpeg` o la ruta completa a `ffmpeg.exe` |
| `IDLE_MINUTES` | Minutos sin música antes de salir del canal (por defecto 5) |
| `DEFAULT_VOLUME` | Volumen inicial 0‑100 (por defecto 50) |

## 4. Probar

Doble clic en **`windows\probar_bot.bat`**. Cuando veas `Conectado como ...`, entra a un canal
de voz y escribe en el chat `!play never gonna give you up`. Cierra la ventana para detenerlo.

## 5. Que arranque solo con el PC

Doble clic en **`windows\registrar_tarea.bat`** (pedirá permisos de administrador).

Crea la tarea **"MusicBot Discord"** en el Programador de tareas, que:

- arranca **al encender el PC**, 30 s después (para dar tiempo a que haya internet), **sin
  necesidad de iniciar sesión** y sin ninguna ventana abierta;
- **actualiza yt-dlp** en cada arranque (YouTube cambia a menudo y esto evita que se rompa);
- **reinicia el bot** automáticamente si se cae.

| Script (`windows\`) | Para qué |
|---|---|
| `registrar_tarea.bat` | Crea/actualiza la tarea y arranca el bot |
| `reiniciar_bot.bat` | Reinicia el bot (p. ej. después de cambiar `.env` o el código) |
| `detener_bot.bat` | Lo detiene hasta el próximo encendido |
| `quitar_tarea.bat` | Borra la tarea: deja de arrancar con el PC |

Logs: `logs\bot.log` (lo que hace el bot) y `logs\launcher.log` (arranques, actualizaciones y errores).

---

## Comandos

(Con el prefijo `!` por defecto. También funcionan mencionando al bot: `@MusicBot play ...`)

| Comando | Alias | Qué hace |
|---|---|---|
| `!play <link o búsqueda>` | `!p` | Entra a tu canal y reproduce, o añade a la cola. Acepta playlists (hasta 100 canciones) |
| `!skip` | `!s`, `!next` | Salta la canción actual |
| `!pause` / `!resume` | `!r` | Pausa / reanuda |
| `!stop` | | Para la música y vacía la cola |
| `!queue [página]` | `!q`, `!cola` | Muestra la cola |
| `!nowplaying` | `!np` | Canción actual con barra de progreso |
| `!loop` | `!repeat` | Repite la canción actual (on/off) |
| `!shuffle` | | Mezcla la cola |
| `!remove <n>` | `!rm` | Quita la canción número *n* de la cola |
| `!clear` | `!cq` | Vacía la cola sin parar la canción actual |
| `!volume [0-100]` | `!vol`, `!v` | Muestra o cambia el volumen |
| `!join` / `!leave` | `!j` / `!dc` | Entra / sale del canal de voz |
| `!help` | | Lista de comandos |

El bot también sale solo del canal si se queda sin nadie durante 1 minuto o sin música
durante `IDLE_MINUTES`.

## Problemas comunes

- **No responde a los comandos** → activa *MESSAGE CONTENT INTENT* (paso 1) y revisa el prefijo en `.env`.
- **Entra al canal pero no suena** → FFmpeg no se encuentra: pon la ruta completa de
  `ffmpeg.exe` en `FFMPEG_PATH` y ejecuta `reiniciar_bot.bat`.
- **"No pude reproducir..." en todas las canciones** → YouTube cambió algo; ejecuta
  `reiniciar_bot.bat` (actualiza yt-dlp) o `instalar.bat` de nuevo.
- **Cualquier otra cosa** → mira `logs\launcher.log` y `logs\bot.log`.

## Futuro: Raspberry Pi

El código es el mismo; solo cambia el arranque automático (un servicio de `systemd` en lugar
del Programador de tareas). Queda pendiente para cuando llegue la Raspberry.

> Nota: reproducir YouTube con bots va contra los términos de servicio de YouTube. Úsalo en tu
> servidor privado.
