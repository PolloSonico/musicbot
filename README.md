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
   - Activa **SERVER MEMBERS INTENT** (para que Lillia salude a los miembros nuevos; si no lo activas,
     el bot funciona igual, sin la bienvenida, y te avisa por DM).
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
| `detener_bot.bat` | Lo apaga del todo (tampoco arranca al encender el PC) hasta que uses `reiniciar_bot.bat` |
| `quitar_tarea.bat` | Borra la tarea: deja de arrancar con el PC |

Logs: `logs\bot.log` (lo que hace el bot) y `logs\launcher.log` (arranques, actualizaciones y errores).

---

## Comandos

(Con el prefijo `!` por defecto. También funcionan mencionando al bot: `@MusicBot play ...`)

| Comando | Alias | Qué hace |
|---|---|---|
| `!play <link o búsqueda>` | `!p`, `!poner`, `!pon` | Entra a tu canal y reproduce, o añade a la cola. Acepta playlists (hasta 100 canciones) |
| `!buscar <búsqueda>` | `!b` | Muestra los 5 primeros resultados de YouTube en un menú para que elijas (evita covers o videos equivocados) |
| `!radio [on/off]` | | Modo radio: cuando se vacía la cola, elige canciones parecidas a lo que suelen pedir los que están en el canal |
| `!skip` | `!s`, `!saltar`, `!siguiente` | Salta la canción actual |
| `!pause` / `!resume` | `!pausa` / `!seguir`, `!r` | Pausa / reanuda |
| `!stop` | `!parar`, `!detener` | Para la música y vacía la cola |
| `!queue [página]` | `!q`, `!cola`, `!lista` | Muestra la cola |
| `!nowplaying` | `!np`, `!sonando` | Canción actual con barra de progreso |
| `!loop` | `!repetir` | Repite la canción actual (on/off) |
| `!shuffle` | `!mezclar` | Mezcla la cola |
| `!remove <n>` | `!rm`, `!quitar` | Quita la canción número *n* de la cola |
| `!clear` | `!cq`, `!limpiar`, `!vaciar` | Vacía la cola sin parar la canción actual |
| `!volume [0-100]` | `!vol`, `!volumen` | Muestra o cambia el volumen |
| `!join` / `!leave` | `!entrar` / `!salir`, `!chau` | Entra / sale del canal de voz |
| `!wrapped [@persona \| server] [mes]` | `!resumen` | 🌸 Lillia Wrapped: resumen musical del mes (`pasado`, `septiembre`, `2026-09`) |
| `!quesabes` | `!memoria` | Lo que Lillia recuerda de ti |
| `!olvidame [palabra]` | | Borra lo que recuerda de ti (todo, o solo lo que contenga esa palabra) |
| `!eventos` | `!calendario` | Fechas especiales de los próximos días (el Mundial, etc.) |
| `!vincular Nombre#TAG [servidor]` | | Vincula tu cuenta de League (servidor por defecto LAS). El dueño del bot y los admins del servidor pueden vincular la de otro: `!vincular @amigo Nombre#TAG` |
| `!desvincular [@persona]` | | La desvincula (la de otro: solo el dueño y los admins) |
| `!perfil [@persona \| Nombre#TAG]` | `!rango`, `!lol` | Nivel, rango Solo/Dúo y Flex y campeones más jugados |
| `!partida [@persona \| Nombre#TAG]` | `!envivo` | La partida en vivo: campeones, jugadores y rangos de los dos equipos |
| `!historial [@persona \| Nombre#TAG]` | `!partidas` | Las últimas 5 partidas (incluye ARAM: Caos leídas del cliente) |
| `!estado` | `!status` | (Solo el dueño) IA, música, League, cliente local, respaldos y errores de hoy |
| `!cupo` | | Cuánto cupo de IA se usó hoy (Gemini y la IA de respaldo) |
| `!dado [tirada]` | `!dados`, `!tirar`, `!roll` | Tira dados estilo D&D: `!dado` (1d20), `!dado 2d6+3`, `!dado d100`, `!dado ventaja`, `!dado desventaja+2` |
| `!recordatorio [@personas] <cuándo> <qué>` | `!recordame`, `!avisame` | Avisa a esa hora: `!recordatorio mañana 21:00 sesión de D&D`, `!recordatorio @Lucía en 30 minutos sacar la pizza`, `!recordatorio todos los viernes 21hs D&D` |
| `!recordatorios` | | Tus recordatorios pendientes (los que creaste y los que son para ti) |
| `!borrarrecordatorio <n>` | | Cancela uno (o te saca a ti, si era para varios) |
| `!panel [mes]` | `!estadisticas`, `!stats` | Página con gráficos de música y League (sin mes: todo el historial) |
| `!help` | | Lista de comandos |

El bot también sale solo del canal si se queda sin nadie durante 1 minuto o sin música
durante `IDLE_MINUTES`.

**Botones:** debajo de cada "🎶 Reproduciendo" hay botones para pausar/seguir ⏯️, saltar ⏭️, parar ⏹️,
repetir 🔁, mezclar 🔀, bajar/subir el volumen 🔉🔊, ver la cola 📜 y prender/apagar la radio 📻. Los puede
usar cualquiera que esté en el mismo canal de voz que el bot, y siguen funcionando después de un reinicio.

**Spotify:** `!play` acepta links de Spotify de canciones, álbumes y playlists (hasta 100 canciones). Como
Spotify no deja reproducir su audio, cada canción se busca en YouTube justo cuando le toca sonar. Funciona
sin configurar nada; si algún día Spotify cambia su página y deja de leer los links, crea una app gratis en
<https://developer.spotify.com/dashboard> y pon `SPOTIFY_CLIENT_ID` y `SPOTIFY_CLIENT_SECRET` en `.env`.

**Volumen parejo:** todas las canciones suenan con la misma intensidad (filtro *loudnorm* de FFmpeg), así
no hay una bajita y la siguiente a todo volumen. Se apaga con `NORMALIZE_VOLUME=false`.

## 🦌 Personaje con IA (opcional)

El bot puede hablar como un personaje, por defecto **Lillia** de League of Legends, usando
**Google Gemini**, que es gratis con un límite diario:

- **Charla:** responde si lo mencionas (`@Bot hola`), si respondes a uno de sus mensajes, si usas el
  prefijo con algo que no es un comando (`!hola Lillia, ¿cómo estás?`), por mensaje privado, o a
  todo en los canales que pongas en `PERSONA_CHANNELS`. Recuerda la conversación de cada canal
  (`!reset` la borra).
- **Música con personalidad:** los avisos (canción en cola, qué está sonando, skip, errores,
  desconexión...) los dice el personaje. Debajo sigue apareciendo el dato en pequeño.
- **Humor según la hora:** de madrugada está medio dormida y sugiere música tranquila; de día está más activa.
- **Builds y picks de League:** si le preguntas por builds, runas, counters, enfrentamientos o qué pickear,
  trae los datos del parche actual de **OP.GG** (por su servidor oficial para IAs, gratis y sin clave) y responde
  con su personalidad (`PERSONA_LOL_SEARCH`; `OPGG=false` lo apaga). Además usa **Data Dragon** (los datos oficiales
  de Riot, sin clave): sabe el número del parche actual (para buscar datos de ESE parche), la lista de
  objetos que existen hoy en la Grieta (para no recomendar objetos que ya no están) y el kit real de los
  campeones que nombres. Se actualiza solo con cada parche (`data/datadragon/`).
- **Esports:** si preguntas por el Mundial, MSI, ligas, equipos o jugadores, busca resultados actuales.
- **Memoria por persona:** si le cuentas algo tuyo que vale la pena recordar (tu main, tu rol, tu rango,
  cómo quieres que te llame...), lo guarda y lo tiene en cuenta en las próximas charlas, aunque se borre
  la conversación del canal (`data/memoria_personas.json`). `!quesabes` muestra lo que recuerda de ti y
  `!olvidame` lo borra. No guarda datos privados (teléfonos, contraseñas, etc.).
- **Letras:** busca la letra de la canción que suena en [LRCLIB](https://lrclib.net) (gratis, sin clave) y
  la usa solo como contexto: para presentar la canción, en las curiosidades y cuando le preguntas por lo
  que suena ("¿de qué trata esta canción?"). No publica la letra en el chat. La de la siguiente canción
  se busca por adelantado, junto con el audio.
- **Imágenes:** mira las imágenes que le mandas (o las del mensaje al que respondes cuando la mencionas):
  capturas de fin de partida, builds, memes... (PNG, JPG o WEBP, hasta 3 por mensaje).
- **Emojis del servidor:** conoce los emojis propios del servidor y los usa en sus mensajes; también
  puede reaccionar a tus mensajes. Si alguien la nombra sin hablarle ("Lillia es la mejor"), a veces
  reacciona con un emoji, sin gastar IA (`PERSONA_NAME_REACT`).
- **Pedirle música hablando normal:** mencionándola o respondiéndole, por ejemplo
  `@Lillia poneme Tik Tok de Kesha`, `@Lillia pon una canción que te guste a ti`,
  `@Lillia salta esta`, `@Lillia bajale el volumen`. Ella responde y ejecuta la orden
  (se desactiva con `PERSONA_MUSIC_CONTROL=false`). Si la IA está sin cupo, usa los comandos `!`.
- **Recuerda qué pide cada uno:** guarda las últimas 50 canciones pedidas por persona (por su cuenta
  de Discord, aunque cambie el apodo del servidor) en `data/canciones_por_usuario.json`. Así puedes
  pedirle `@Lillia poneme algo que me pueda gustar` o preguntarle `¿qué música me gusta?`.
- **Conoce la música de League:** K/DA, Pentakill, True Damage, HEARTSTEEL, los temas de los Mundiales
  y Arcane; cuando suenan, habla de los campeones que las cantan.
- **Curiosidades:** de vez en cuando (15% de las canciones, máximo una vez al día) comenta por su
  cuenta algún dato de la canción que suena: la banda, la letra, la melodía... Se ajusta con
  `PERSONA_TRIVIA_CHANCE` y `PERSONA_TRIVIA_PER_DAY`.
- **Nombre y avatar:** el bot se pone el apodo (y el avatar, si hay imagen) del personaje.

### Activarlo
1. Entra a <https://aistudio.google.com/apikey> con tu cuenta de Google, pulsa **Create API key** y cópiala.
2. En tu `.env` añade `GEMINI_API_KEY=la_clave` (mira `.env.example` para todas las opciones).
3. Ejecuta `windows\diagnostico_ia.bat` para comprobar que funciona, y después `reiniciar_bot.bat`.

### Cambiar o editar el personaje
- La personalidad de Lillia está en **`personajes/lillia.txt`**: edítalo con el Bloc de notas
  (cómo habla, su historia, sus manías...) y reinicia el bot.
- **Frases reales:** en **`personajes/lillia_frases.txt`** pega las frases del juego (de la wiki,
  página *Lillia/LoL/Audio*), una por línea. La IA las usa como ejemplo de cómo habla. Unas 100-300
  frases está bien: más que eso hace las respuestas más lentas y gasta más cupo.
- También puedes usar una **character card** de [chub.ai](https://chub.ai): descarga el personaje
  como PNG, ponlo en la carpeta `personajes` y en `.env` pon `PERSONA_FILE=personajes/archivo.png`.

### Modelos lentos o saturados
El bot aprende qué modelos de Gemini responden rápido: cada vez que uno está saturado o tarda
demasiado, baja en la lista, y los que responden bien suben (el castigo se va olvidando con las
horas, así que un modelo puede volver a subir). `!personaje` muestra el orden actual y el tiempo
medio de cada modelo.

### Si se acaba el cupo gratis
El bot usa el mejor modelo gratuito y, si se le acaba el cupo, pasa al siguiente. Si se acaban todos:
- **la música sigue funcionando normal**, con los textos fijos y sin esperas;
- si alguien le habla, avisa una vez que "se quedó dormida" y a qué hora vuelve (después solo reacciona con 😴);
- a medianoche (hora de EE. UU.) se renueva el cupo y vuelve a hablar sola.

Para gastar menos cupo: `PERSONA_MUSIC_COMMENTS=false` (no comenta la música) y no uses `PERSONA_CHANNELS`.

**IA de respaldo (recomendada):** si pones `BACKUP_AI_KEY` en `.env`, cuando Gemini se queda sin cupo
responde otra IA y Lillia no se duerme. Por defecto usa **Groq** (gratis, sin tarjeta, unas 1000 respuestas
por día con Llama 3.3 70B): entra a <https://console.groq.com>, crea una API key y pégala. El respaldo no ve
imágenes. Sirve cualquier proveedor compatible con OpenAI (`BACKUP_AI_URL`, `BACKUP_AI_MODELS`).

**¿Cuánto cupo queda?** `!cupo` muestra los pedidos y tokens de hoy por modelo. Google no informa cuánto
queda, así que el bot **aprende** el límite de cada modelo el día que se agota (cuántos pedidos llevaba) y
desde ahí muestra el porcentaje. Groq sí lo informa, así que su porcentaje es exacto. El detalle oficial
de Gemini está en <https://aistudio.google.com> (página de límites de tu proyecto).

Comandos: `!personaje` (qué personaje e IA usa y si le queda cupo) y `!reset` / `!olvidar`
(borra su memoria en el canal, también la larga).

### Memoria larga
Además de los últimos 24 mensajes de cada canal, cuando la charla se alarga los mensajes que se olvidan
se **resumen** (cada 8) en unas viñetas con lo importante: quién dijo qué, gustos, planes, bromas internas.
Ese resumen se le pasa siempre, así se acuerda de charlas de otros días (`data/resumenes.json`).

## 🎮 League of Legends (Riot API)

1. Pon tu clave en `.env`: `RIOT_API_KEY=RGAPI-...` (la de tu app en <https://developer.riotgames.com>).
2. Cada uno vincula su cuenta una vez: `!vincular Nombre#TAG` (si no juega en LAS: `!vincular Nombre#TAG lan`).
   El dueño del bot o quien administra el servidor también puede hacerlo por otro: `!vincular @amigo Nombre#TAG`.

Con eso:
- `!perfil`, `!partida` y `!historial` (de uno mismo, de un @amigo vinculado o de cualquier `Nombre#TAG`),
  siempre con un comentario de Lillia.
- **Resumen post-partida:** cuando alguien vinculado termina una partida, Lillia escribe en el canal de eventos
  un comentario que lo celebra o lo consuela (quién jugó, el modo y el campeón, sin números). Con
  `RIOT_POST_GAME_DETAILS=true` también muestra el cuadro con el resultado (KDA, CS, daño, visión). Si jugaron
  varios amigos juntos, sale un solo mensaje. No se anuncian remakes, personalizadas ni partidas de hace más de 3 horas.
- **Lillia conoce tu cuenta:** en la charla sabe tu rango, tus campeones más jugados y cómo te fue en la
  última partida (por ejemplo, para recomendarte builds o picks a tu medida).
- **Lillia mira tus partidas cuando le preguntas** ("¿cómo me fue en la última?", "¿con qué campeón jugué?"):
  trae tus últimas 5 partidas reales de Riot y responde con esos datos.
- **ARAM: Caos (ARAM Mayhem):** Riot no comparte esas partidas por su API. Para no perderlas, el bot las lee
  del **cliente de League abierto en la PC del bot** (su API local): cada pocos minutos busca partidas
  nuevas de ARAM: Caos, mira los 10 jugadores y, si alguno tiene la cuenta vinculada, la suma a sus datos
  (resumen post-partida, `!historial`, la charla y el Wrapped). Solo se ven las partidas de la cuenta con
  sesión en ese cliente (y las de los amigos que jugaron en esas partidas). Se guardan en
  `data/partidas_mayhem.json`. Se apaga con `LCU_MAYHEM=false`.
- **Escena competitiva:** si preguntas por un jugador o equipo profesional ("¿cuándo vuelve a jugar Josedeodo
  en la LCS?"), Lillia busca en OP.GG en qué equipo está hoy el jugador y el calendario y resultados de la liga
  o del equipo, y suma una búsqueda en internet (cuadro de playoffs, fechas). Da las horas en hora local.
- **¿Quién está jugando?:** si le preguntas a Lillia "¿alguien está en partida?" o "¿@Lucía está jugando?", consulta
  a Riot en el momento (cola, campeón, minutos de juego y qué amigos van juntos). Solo ve las cuentas vinculadas;
  ARAM: Caos solo si se juega desde la PC del bot. Para ver los dos equipos completos: `!partida @persona`.
- **Consejos en la selección de campeones:** con el cliente abierto en la PC del bot, cuando ya tienes
  campeón Lillia te manda por privado consejos para esa partida (a quien tenga vinculada la cuenta del
  cliente; si nadie, al dueño). Se adapta al modo: en Grieta (normal, clasificatoria, Clash) espera a que
  confirmes el campeón y da runas, hechizos, build y el enfrentamiento si ya se ve el rival; en ARAM
  (campeón al azar) te dice si conviene cambiar por alguno del banco; en **ARAM: Caos** y **Arena** no
  hay runas, así que habla de aumentos y objetos. Si cambias de campeón en ARAM, manda uno nuevo (máximo 3).
  Se apaga con `LCU_CHAMP_SELECT=false`.
- **Clash:** consulta los torneos de Clash de tu servidor; Lillia avisa una semana antes y 2 días antes (para armar equipo) y
  el mismo día, y aparecen en `!eventos`.
- **League en el Wrapped:** el `!wrapped` suma tus partidas del mes (partidas, % de victorias, KDA, campeones
  más jugados, mejor partida, pentakills) y el del servidor muestra quién jugó más, el mejor KDA y el
  campeón del mes. Cuenta las partidas desde que vinculaste la cuenta.
- En Arena se muestra el puesto en vez de los súbditos.
- La clave personal permite 100 pedidos cada 2 minutos: el bot nunca se pasa (espera solo si hace falta).
  Si la clave deja de funcionar, te avisa por DM.

## 🔊 Música en varios canales a la vez (bots ayudantes)

Discord no deja que un bot esté en dos canales de voz del mismo servidor. Para eso están los
**ayudantes**: otras cuentas de bot que solo ponen audio. Lillia sigue siendo la que habla, recibe los
comandos y manda los mensajes y botones; si alguien pide música desde otro canal de voz mientras ella
está ocupada, la pone un ayudante libre (el "🎶 Reproduciendo" dice en qué canal suena). Los botones y
los comandos (`!skip`, `!cola`...) actúan sobre la música del canal de voz donde estás.

Para crear cada ayudante (una vez por ayudante):
1. <https://discord.com/developers/applications> → **New Application** → ponle un nombre (ej: "Lillia 2").
2. Menú **Bot** → **Reset Token** → **Copy**. Ese texto es la clave: no la compartas. No hace falta
   activar ningún *Privileged Gateway Intent*.
3. Menú **OAuth2** → **URL Generator** → marca `bot` y los permisos **View Channels**, **Connect** y
   **Speak** → abre el link de abajo e invítalo a tu servidor.
4. En `.env`: `HELPER_TOKENS=token_del_1,token_del_2` y reinicia con `reiniciar_bot.bat`.

`!estado` muestra cuántos ayudantes están conectados.

## 🌐 Búsqueda en internet y clima

- **Búsqueda (Tavily):** crea una cuenta gratis en <https://tavily.com> (1000 búsquedas por mes), copia la clave y
  ponla en `.env` como `TAVILY_API_KEY=tvly-...`. Con eso Lillia busca datos actuales cuando se los preguntan:
  noticias, precios, fechas, resultados, notas de parche (las builds salen de OP.GG, sin gastar búsquedas). Si le hablan directamente y no sabe un
  dato actual, ella misma pide la búsqueda. La misma búsqueda no se repite por 10 minutos y nunca pasa de
  `TAVILY_MONTHLY_LIMIT` por mes. `!cupo` y `!estado` muestran cuántas lleva. Sin Tavily, Lillia
  avisa que no puede buscar en vez de inventar.
- **Clima (Open-Meteo):** gratis y sin clave. "¿Llueve el finde en Berazategui?" trae el tiempo actual y el
  pronóstico de 10 días de ese lugar. `WEATHER_DEFAULT_PLACE` es el lugar que se usa si no dicen dónde.
- **Builds y calendario de esports (OP.GG):** gratis y sin clave. Las preguntas de esports suman además una
  búsqueda de Tavily.

## ⏰ Recordatorios

Se piden hablando ("@Lillia recordame el viernes a las 21 la sesión de D&D", "@Lillia recordale a @Lucía
mañana a las 18 que traiga los dados") o con `!recordatorio`. Debajo de su respuesta sale la fecha que
guardó, para que se pueda revisar. A la hora, Lillia escribe en el mismo canal y **pinguea solo a las
personas del recordatorio** (nunca @everyone, @here ni roles, aunque el texto los tenga). Si el bot estaba
apagado a esa hora, avisa al volver. Se pueden repetir ("todos los viernes", "todos los días"). Cada uno
puede tener hasta 15 pendientes y mandar a como mucho 5 personas por recordatorio; quien lo recibe se
puede sacar con `!borrarrecordatorio`.

## 🎲 Dados (D&D)

- **Tiradas de otros bots:** cuando el bot de dados del servidor publica una tirada, Lillia reacciona con un
  emoji (✨ alta, 🥺 baja, 🎲 normal) y en los **20 y 1 naturales** la comenta (🎉 / 💀). A veces comenta
  también alguna normal (`DICE_COMMENT_CHANCE`). Los bots no pueden usar los comandos `/` de otros bots,
  así que no puede tirar con ese bot: solo lee lo que publica.
- **Sus propios dados:** `!dado 1d20+3`, o pídeselo hablando: "@Lillia tira iniciativa con +2".
- `DICE_REACTIONS=false` lo apaga; `DICE_BOT_IDS` sirve si no reconoce al bot por el nombre.

## 📅 Eventos y cosas que hace sola

Nadie del servidor tiene que configurar nada:

- **Calendario** (`personajes/eventos.json`, se edita con el Bloc de notas y se recarga solo): mientras
  dura un evento lo tiene presente en la charla, y el primer día de los que tienen `"anunciar": true` lo
  anuncia (desde las `PERSONA_EVENTS_HOUR`). Ya trae **todo el Mundial 2026** (Play-In, Fase Suiza,
  cuartos, semis y la final del 14/11 en Nueva York; en los días de partidos busca qué pasa ese día),
  su aniversario (22/07), Halloween, Navidad, Año Nuevo y el Día del Amigo, y las finales de la LCS (4/10)
  y del CBLOL (10/10).
- **MSI y finales de ligas:** cada 2 semanas busca sola en internet (Tavily) las fechas confirmadas del MSI y de las
  finales de LCK, LPL, LEC, LCS, CBLOL y LCP que todavía no estén en el calendario, las guarda en
  `data/eventos_esports.json` y las anuncia el día que empiezan (`PERSONA_ESPORTS_AUTO`). Si quieres
  corregir una fecha, ponla en `personajes/eventos.json`: la tuya tiene prioridad.
- **Parche nuevo:** cuando sale un parche de League lo anuncia con un resumen de los cambios principales.
- **Wrapped mensual:** el día 1 publica el Lillia Wrapped del servidor del mes que terminó.
- **Buenas noches:** si alguien escribe entre la 1 y las 6 de la mañana, una vez por noche le dice que
  se vaya a dormir (`PERSONA_NIGHT_GREETING`, `PERSONA_NIGHT_HOURS`).
- Los anuncios van a `PERSONA_EVENTS_CHANNEL` o, si está vacío, al último canal donde se usó el bot.

## 🔔 Avisos al dueño (por mensaje privado)

El bot te escribe por DM (a `OWNER_ID`, o al dueño de la app en el Developer Portal) cuando:
- **la IA se queda sin cupo:** te dice qué día y a qué hora vuelve (un aviso por vez que se agota);
- **la API key de Gemini deja de funcionar;**
- **yt-dlp falla 3 veces seguidas:** con los últimos errores (casi siempre se arregla con `reiniciar_bot.bat`,
  que actualiza yt-dlp). Como mucho un aviso cada 6 horas.

Para que te lleguen, tienes que tener abiertos los mensajes directos de miembros del servidor.
Se apagan con `OWNER_ALERTS=false`.

## 📊 Panel de estadísticas
`!panel` genera una página con gráficos (canciones y horas por mes, a qué hora y qué días se pide música,
artistas, canciones, DJs y oyentes, y de League: victorias y derrotas, KDA, campeones y modos) y la manda
como archivo para abrir en el navegador. `!panel septiembre` hace la de un mes. También queda guardada en
la carpeta `panel\`. Se ve bien en modo claro y oscuro, y cada gráfico tiene "Ver tabla".

## 🧪 Pruebas automáticas
`windows\probar_codigo.bat` corre las pruebas (carpeta `tests\`): revisan en segundos que el guardado de
datos, el calendario, el Wrapped, Spotify, la Riot API, el cliente local, la IA de respaldo, la memoria
larga y la carga de todos los comandos sigan funcionando. No tocan tus datos ni se conectan a internet.
Úsalo después de cambiar algo del código.

## Otros detalles
- **Bienvenida:** cuando entra alguien nuevo al servidor, Lillia lo saluda (en el canal de bienvenida del
  servidor o en el de eventos) y le cuenta cómo pedirle música (`PERSONA_WELCOME`).
- **Copias de seguridad:** una vez por día se comprime `data/` (memoria de cada uno, historial del Wrapped,
  etc.) en `backups\data_AAAA-MM-DD.zip` y se guardan las últimas 14 (`BACKUP_DAYS`). Con `BACKUP_DIR`
  puedes mandarlas a una carpeta de OneDrive/Google Drive. Para recuperar: detén el bot, descomprime el
  zip dentro de `data\` y vuelve a arrancarlo.
- **Versiones fijas:** `requirements.txt` fija las versiones de las librerías para que una actualización no
  rompa nada sin aviso. La excepción es yt-dlp, que se actualiza solo en cada arranque.
- **Guardado seguro:** todo lo de `data/` se guarda de forma atómica (primero en un archivo temporal y
  después se reemplaza de una vez), así un apagón o un cierre inesperado nunca deja un JSON a medias.
  Si un archivo aparece dañado igual, se aparta como `archivo.json.roto` y el bot sigue funcionando.
- El estado del bot en Discord muestra la canción que suena ("Escuchando ...") o "Durmiendo 💤".
- El aviso "🎶 Reproduciendo" sale al instante; la frase de Lillia se añade unos segundos después.
- El audio de la siguiente canción se prepara 30 s antes de que termine la actual, para que no haya silencio.

## Problemas comunes

- **No responde a los comandos** → activa *MESSAGE CONTENT INTENT* (paso 1) y revisa el prefijo en `.env`.
- **Entra al canal pero no suena** → FFmpeg no se encuentra: pon la ruta completa de
  `ffmpeg.exe` en `FFMPEG_PATH` y ejecuta `reiniciar_bot.bat`.
- **"No pude reproducir..." en todas las canciones** → YouTube cambió algo; ejecuta
  `reiniciar_bot.bat` (actualiza yt-dlp) o `instalar.bat` de nuevo.
- **El personaje no responde / "no me sale responder"** → ejecuta `windows\diagnostico_ia.bat`:
  muestra si la clave funciona, qué modelos usa y si queda cupo. El error exacto sale en `logs\bot.log`.
- **La música se corta, el bot entra y sale del canal o da "Timed out connecting to voice"** → casi
  siempre son **dos copias del bot a la vez** (la tarea automática + `probar_bot.bat`). Ahora el bot
  lo impide solo, pero para probar a mano ejecuta antes `detener_bot.bat`.
- **Cualquier otra cosa** → mira `logs\launcher.log` y `logs\bot.log`.

## Futuro: Raspberry Pi

El código es el mismo; solo cambia el arranque automático (un servicio de `systemd` en lugar
del Programador de tareas). Queda pendiente para cuando llegue la Raspberry.

> Nota: reproducir YouTube con bots va contra los términos de servicio de YouTube. Úsalo en tu
> servidor privado.
