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
WebSockets, notificaciones push, confirmaciones de lectura ni envío de archivos.

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

`tests/test_matching.py` utiliza esquemas temporales `matching_test_<uuid>` y los
elimina al finalizar. No utiliza las tablas activas de `matching_api` ni los seeds.
Requiere un rol con permiso para crear esquemas en la base de pruebas.

```sh
MATCHING_TEST_DATABASE_URL='postgresql://usuario:clave@127.0.0.1:5432/base_pruebas' .venv/bin/python -m pytest -q tests
```

Se comprueban permisos, roles mediante Users, auto-solicitudes, concurrencia,
rechazo, aceptación idempotente, persistencia entre instancias y paginación del chat.
Las pruebas del directorio de deportistas viven en `../Ms_Users/tests/test_athlete_directory.py`;
las del gateway en `../Ms_gateway/gateway/tests/test_matching_proxy.py`.
