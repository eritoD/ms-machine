# SportMatch — Ms_Matching

Microservicio FastAPI independiente de Users. Gestiona solicitudes entre deportistas,
matches aceptados y el chat de cada match. Persiste en `sportmach_matching` y consulta
a `Ms_Users` por HTTP para validar cuentas activas, correo verificado y rol deportivo.
No consulta ni modifica directamente la base de Users.

## Estructura

```text
Ms_Matching/
├── app/
│   ├── api/v1/             # Endpoints; validación de entrada y dependencias
│   ├── services/           # Reglas de matching y cliente HTTP de Users
│   ├── repositories/       # Acceso a PostgreSQL y transacciones
│   ├── schemas/            # Contratos Pydantic
│   ├── core/               # Configuración y autenticación JWT
│   ├── database/           # Pool de conexiones
│   └── main.py             # Ciclo de vida de FastAPI
├── db/migrations/          # Esquema idempotente
└── tests/                  # Pruebas de API con PostgreSQL aislado
```

La app se conecta al gateway: `app → gateway:8000 → ms_matching:8002`.
Matching obtiene las cards públicas con `/api/v1/users/athletes/{id}` y
`POST /api/v1/users/athletes/cards`. Users sigue siendo dueño de cuentas,
roles, perfiles, deportes y verificación de correo.

## Flujo

1. Un corazón crea una solicitud `pending`, visible al destinatario.
2. Solo el destinatario puede aceptar (`accepted`) o rechazar (`rejected`).
3. La aceptación crea la conexión para ambos. El ID de la solicitud pasa a ser
   el ID del match y de su conversación; no se crean registros duplicados.
4. Solo los dos participantes activos de un match aceptado pueden leer o enviar
   mensajes. Una URL conocida, un ID ajeno o un JWT de una cuenta desactivada no dan acceso.
5. El rechazo cierra la solicitud sin abrir un chat. La pareja se oculta en
   Descubrir; en esta versión no se reenvían solicitudes ya rechazadas.
6. El emisor puede cancelar una solicitud pendiente desde **Enviadas**. Desaparece
   para ambos y permite enviar una nueva solicitud con otro ID. Los reintentos de
   cancelación son seguros y no afectan solicitudes posteriores ni matches aceptados.
7. Desde **Matches → Ver perfil**, o desde el nombre en el chat, se muestra el
   perfil público del otro deportista: foto, edad, biografía y deportes registrados.

Likes repetidos y simultáneos conservan una sola solicitud por pareja. Un like
cruzado conserva la solicitud pendiente: no equivale a aceptación. Las decisiones
se serializan en PostgreSQL y los reintentos de aceptación son idempotentes.
Los mensajes utilizan `client_message_id` para deduplicar reintentos de envío.

El chat es un módulo dentro de Matching para mantener los cuatro servicios previstos
(Users, Matching, Actividades y Clubes). Puede extraerse a un servicio de chat más
adelante conservando la autorización basada en el match.

## API

Todas las rutas funcionales requieren `Authorization: Bearer <JWT de Users>`.
El emisor siempre se obtiene del token; no se acepta `sender_id` en el cuerpo.

| Método | Ruta bajo `/api/v1/matching` | Uso |
|---|---|---|
| GET | `/state` | Solicitudes recibidas, enviadas, matches y pares ocultos. |
| POST | `/requests` | `{"recipient_id":"UUID"}`: enviar solicitud. |
| POST | `/requests/{id}/decision` | `{"action":"accept"}` o `{"action":"reject"}`. |
| DELETE | `/requests/{id}` | Cancelar una solicitud pendiente propia; devuelve 204. Si ya fue respondida, devuelve 409. |
| GET | `/matches/{id}` | Match aceptado y card pública del otro participante. |
| GET | `/matches/{id}/messages` | Últimos 50 mensajes; `before_id` para anteriores o `after_id` para nuevos; `limit` entre 1 y 100. |
| POST | `/matches/{id}/messages` | `{"client_message_id":"UUID","text":"¡Hola!"}`; hasta 2000 caracteres. |
| GET | `/health/ready` | Estado de la base; sin autenticación. |

La app actualiza solicitudes cada 5 segundos mientras la pantalla está abierta,
y mensajes cada 4 segundos mientras el chat está abierto. La consulta se pausa
cuando la app pasa a segundo plano. Esta primera versión usa HTTP; no incluye
WebSockets ni notificaciones push. Admite ubicación, imágenes y archivos como enlaces (ver «Ubicación, imágenes y archivos»), pero no los almacena. El estado de lectura y la lista de
chats con mensajes no leídos se describen en «Lista de chats y mensajes no leídos».

## Lista de chats y mensajes no leídos (HU-49)

Una conversación es un match aceptado, así que no hay tablas de conversaciones ni de
participantes aparte: `matching_api.requests` ya los modela. La migración
`004_message_read_state.sql` agrega `messages.read_at` (nulo mientras el destinatario no
lo ha leído) y un índice parcial para contar. Los mensajes anteriores a la migración
quedan como leídos, para que ningún contador arranque inflado. Es la 004 y no la 003 para
no chocar con la migración del outbox de notificaciones.

Un mensaje no está leído para una persona cuando **no lo envió ella** y su `read_at` es nulo.

| Método | Ruta | Uso |
|---|---|---|
| GET | `/api/v1/chats` | Mis conversaciones activas, la de actividad más reciente primero. `limit` (1–100, por defecto 50) y `offset`. |
| GET | `/api/v1/chats/{id}/messages` | Igual que `/matching/matches/{id}/messages`, y además marca como leídos los mensajes de la otra persona. |
| PATCH | `/api/v1/chats/{id}/read` | Marca como leído todo lo que envió la otra persona. Idempotente. |

Cada elemento de `GET /api/v1/chats` es exactamente:

```json
{
  "conversationId": "uuid", "unreadCount": 3,
  "participant": {"userId": "uuid", "name": "Juan P.", "avatarUrl": "https://..."},
  "lastMessage": {"content": "Nos vemos en la cancha", "senderId": "uuid", "createdAt": "2026-10-14T14:30:00Z"}
}
```

`lastMessage` es `null` si aún no hay mensajes (el match igual aparece, como en la pestaña
de chats actual). `name` es el nombre más la inicial del apellido, porque es lo único que
Users entrega de otra persona. Las cuentas desactivadas no aparecen.

**Leer marca como leído, sin perder mensajes.** Al pedir mensajes se marcan los de la otra
persona hasta el más reciente **que se entregó en esa respuesta**. Uno que llega mientras
tanto tiene un id mayor y sigue sin leer. Una consulta de actualización que no encuentra
nada nuevo no marca nada, y pedir historia anterior (`before_id`) no marca lo más nuevo.
La ruta antigua `/matching/matches/{id}/messages` marca igual, así que la app actual ya
limpia los contadores al abrir el chat. En cada mensaje, `read_at` e `is_read` indican si
el destinatario lo leyó: para los que envías tú, es el acuse de lectura.

El prefijo `/api/v1/chats` es nuevo: el gateway necesita una ruta que lo reenvíe a este
servicio (hoy solo reenvía `/api/v1/matching`).

## Ubicación, imágenes y archivos (HU-50)

`POST /api/v1/chats/{id}/messages` envía un mensaje de cualquier tipo a los dos participantes de un
match aceptado. La ruta antigua `POST /matching/matches/{id}/messages` sigue enviando solo texto.
Acepta camelCase (el contrato de la historia) y también snake_case.

| `type` | Obligatorio | Opcional | No admite |
|---|---|---|---|
| `TEXT` (por defecto) | `content` (1–2000) | `clientMessageId` | adjunto ni coordenadas |
| `IMAGE` | `attachmentUrl` | `content` (leyenda) | coordenadas, `locationName`, `attachmentName` |
| `FILE` | `attachmentUrl` | `content`, `attachmentName` | coordenadas, `locationName` |
| `LOCATION` | `latitude`, `longitude` | `locationName` (≤120), `content` | `attachmentUrl`, `attachmentName` |

```json
{ "type": "LOCATION", "content": "Punto de encuentro en la cancha principal",
  "latitude": -33.5833, "longitude": -70.6333, "locationName": "Cancha 2 - Complejo Deportivo" }
```

Responde `200` con el mensaje (el mismo `Message` de siempre, ahora con `message_type`,
`attachment_url`, `attachment_name`, `latitude`, `longitude` y `location_name`). Un campo que el tipo no
usa se rechaza con 422. Las coordenadas deben estar entre ±90 y ±180, no ser infinitas, y se guardan con
6 decimales (unos 10 cm). `clientMessageId` es opcional: con él, reintentar el mismo envío devuelve el
mismo mensaje, y reutilizarlo con otro contenido es 409; sin él, cada envío es un mensaje nuevo.

**Los archivos no se guardan aquí.** Un adjunto es el enlace a un archivo que la app ya subió a un
almacenamiento de confianza. Para que nadie pueda hacer que la app de otra persona cargue contenido de un
servidor ajeno (lo que le revelaría su dirección IP), el enlace debe cumplir todo esto:

- ser `https`, sin usuario ni contraseña y sin puerto distinto de 443;
- tener un host listado en `CHAT_ATTACHMENT_HOSTS` (separados por coma; `*.cdn.ejemplo.cl` permite
  subdominios, pero no el dominio base). **Vacío, los mensajes IMAGE y FILE responden 503**;
- terminar en una extensión permitida: imágenes `jpg, jpeg, png, webp, gif`; archivos `pdf, doc, docx, xls,
  xlsx, ppt, pptx, txt, csv`. Se excluyen a propósito `svg` (puede llevar scripts), `html`, ejecutables y
  comprimidos. La ruta no puede terminar en `/`.

Solo se valida el enlace: no se descarga el archivo ni se comprueba su tipo real o su tamaño; eso le
corresponde a quien lo sube. `attachmentName` (hasta 120, sin barras ni caracteres de control) se toma
del enlace si no se envía.

En la lista de chats y en `/matching/state`, un mensaje sin texto se resume como «Imagen», «Archivo» o
«Ubicación»; `lastMessage.type` indica el tipo. Un chat sin mensajes sigue teniendo `lastMessage` nulo.

La migración `005_message_attachments.sql` agrega las columnas, reemplaza la regla «todo mensaje tiene
texto» por «solo el texto lo exige» y agrega una regla por tipo (una imagen siempre con enlace, una ubicación
siempre con coordenadas), de modo que la base también la hace cumplir. Los mensajes anteriores quedan como
`TEXT`. Se aplica una sola vez, aunque el servicio reinicie.

## Tiempo real con WebSockets (HU-52)

El servicio que guarda los mensajes los anuncia al instante a quienes están conectados. Es un canal
adicional: **todo sigue funcionando por REST** sin él, y un fallo del tiempo real nunca impide guardar un
mensaje (se registra en el log y el emisor ni se entera).

`wss://<gateway>/api/v1/chats/ws`. Tramas JSON de texto, de hasta 4 KB:

| Dirección | Trama | Cuándo |
|---|---|---|
| cliente → servidor | `{"type":"auth","token":"<JWT>"}` | Primera trama, dentro de 5 s. El token va aquí y no en la URL, para que no quede en los logs de acceso. |
| cliente → servidor | `{"type":"join","conversationId":"<uuid>"}` | La conversación está abierta en pantalla (una por conexión). Se exigen las mismas reglas que para abrir el chat por REST: participante de un match aceptado y sin bloqueo. |
| cliente → servidor | `{"type":"leave"}` / `{"type":"ping"}` | Se cerró el chat / latido (responde `{"type":"pong"}`). |
| servidor → cliente | `{"type":"ready","userId":"…"}` | Autenticado. |
| servidor → cliente | `{"type":"new_message","conversationId":"…","message":{…}}` | A todas las conexiones **de los dos participantes que tienen esa conversación abierta**, incluidos los otros dispositivos del emisor. `message` es el mismo objeto que devuelve REST. |
| servidor → cliente | `{"type":"unread_update","conversationId":"…","unreadCount":3,"totalUnread":7,"lastMessage":{"content","senderId","createdAt","type"}}` | A las conexiones **del destinatario que no tienen esa conversación abierta**, para actualizar la insignia y la lista. `totalUnread` suma todas sus conversaciones. El emisor nunca recibe uno por su propio mensaje. |
| servidor → cliente | `{"type":"error","code":"…","message":"…"}` | `invalid_frame`, `unknown_type`, `invalid_conversation`, `conversation_unavailable`, `already_authenticated`, `token_expired`. |

Cierres: `4401` sin autenticar, token inválido, cuenta desactivada o **token vencido** (la conexión no
sobrevive al token: reconectar con uno nuevo); `4409` desplazada por una conexión más nueva (máximo 5 por
persona); `1008` más de 30 tramas en 10 s; `1009` trama de más de 4 KB; `1013` Usuarios no responde o el
cliente no lee y su cola se llenó (se le suelta en vez de frenar a los demás).

Cómo usarlo desde la app: conectar al iniciar sesión (con reintentos espaciados), `join` al abrir un chat y
`leave` al salir. Al recibir `new_message` con el chat abierto, mostrarlo y llamar a
`PATCH /api/v1/chats/{id}/read`; al recibir `unread_update`, actualizar la insignia. Los eventos no se
reenvían a quien se conecta después: al conectar o reconectar hay que cargar `GET /api/v1/chats` por REST.

**Límites de esta versión.** Los sockets viven en memoria de un solo proceso: con varias réplicas del
servicio, un evento solo llega a quienes estén conectados a la réplica que guardó el mensaje (haría falta
PostgreSQL `LISTEN/NOTIFY` o Redis). Hace falta la librería `websockets` (ya está en `requirements.txt`; sin
ella uvicorn no atiende WebSockets). El gateway todavía no reenvía WebSockets.

**Push.** Este servicio no envía push: el aviso al celular de un mensaje lo genera el gateway (Expo Push) a
partir de los eventos de la base. Para que un envío de push se abstenga mientras la persona mira el chat,
`RealtimeHub.is_active(usuario, conversación)` indica si tiene esa conversación abierta.

## Ejecución

Desde `../Ms_gateway`, con Docker Desktop y PostgreSQL local iniciados:

```sh
sh scripts/dev-lan.sh
```

El archivo local `../Ms_gateway/.env` debe contener `MATCHING_DATABASE_URL`,
usando `host.docker.internal` desde Docker. El servicio comparte emisor, audiencia
y secreto JWT con Users y el gateway. Las credenciales reales están excluidas de Git.
La migración se aplica al iniciar el servicio, bajo un bloqueo transaccional.

El esquema `matching_api` contiene `requests` y `messages`. Las tablas anteriores
de `sportmach_matching` contienen ejemplos históricos y no se importan como
consentimientos reales ni conversaciones de los deportistas.

Para ejecutar sin Docker:

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
# Completar .env a partir de .env.example y arrancar Users en el puerto 8001.
.venv/bin/uvicorn app.main:app --env-file .env --port 8002
```

## Pruebas

`tests/test_matching.py` y `tests/test_chats.py` utilizan esquemas temporales `matching_test_<uuid>` y los
elimina al finalizar. No utiliza las tablas activas de `matching_api` ni los seeds.
Requiere un rol con permiso para crear esquemas en la base de pruebas.

```sh
MATCHING_TEST_DATABASE_URL='postgresql://usuario:clave@127.0.0.1:5432/base_pruebas' .venv/bin/python -m pytest -q tests
```

Se comprueban permisos, roles mediante Users, auto-solicitudes, concurrencia,
rechazo, aceptación idempotente, persistencia entre instancias y paginación del chat.
`tests/test_realtime.py` cubre la autenticación del socket, los eventos, los límites y los fallos aislados.
`tests/test_attachments.py` cubre los cuatro tipos de mensaje, la seguridad de los enlaces, los reintentos,
las reglas de la base y la migración sobre datos anteriores. `tests/test_chats.py` cubre la lista, el orden, los contadores, el marcado de lectura (incluido
con lectores y emisores simultáneos) y la migración sobre datos anteriores.
Las pruebas del directorio de deportistas viven en `../Ms_Users/tests/test_athlete_directory.py`;
las del gateway en `../Ms_gateway/gateway/tests/test_matching_proxy.py`.
