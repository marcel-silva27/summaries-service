"""Configuración leída desde variables de entorno."""
import os

MONGODB_URL = os.environ.get("MONGODB_URL", "mongodb://summaries-mongodb:27017")
MONGODB_DATABASE = os.environ.get("MONGODB_DATABASE", "summaries")
MONGODB_TIMEOUT_MS = int(os.environ.get("MONGODB_TIMEOUT_MS", "5000"))
BROKER_URL = os.environ.get("BROKER_URL", "amqp://summaries-rabbitmq:5672")
BROKER_TIMEOUT_SECONDS = float(os.environ.get("BROKER_TIMEOUT_SECONDS", "2"))
BROKER_RETRY_SECONDS = float(os.environ.get("BROKER_RETRY_SECONDS", "1"))
# Respaldo: la outbox se publica al ser notificada; este sondeo cubre caídas entre la escritura y el aviso.
OUTBOX_POLL_SECONDS = float(os.environ.get("OUTBOX_POLL_SECONDS", "5"))

USERS_URL = os.environ.get("USERS_URL", "http://users:8000")
CHANNELS_URL = os.environ.get("CHANNELS_URL", "http://channels:8000")
MESSAGES_URL = os.environ.get("MESSAGES_URL", "http://messages:8000")
CORE_TIMEOUT_SECONDS = float(os.environ.get("CORE_TIMEOUT_SECONDS", "2"))

# LLM: `mock` (desarrollo/pruebas) o `gemini` (se conecta al final del paso 7).
LLM_PROVIDER = os.environ.get("LLM_PROVIDER", "mock")
LLM_API_URL = os.environ.get("LLM_API_URL", "https://generativelanguage.googleapis.com/v1beta")
LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
LLM_MODEL = os.environ.get("LLM_MODEL", "")
LLM_FALLBACK_MODEL = os.environ.get("LLM_FALLBACK_MODEL", "")
LLM_TIMEOUT_SECONDS = float(os.environ.get("LLM_TIMEOUT_SECONDS", "60"))
# Espera máxima para un único reintento inmediato ante 429 (si el proveedor indica cuánto esperar).
LLM_RETRY_MAX_WAIT_SECONDS = float(os.environ.get("LLM_RETRY_MAX_WAIT_SECONDS", "10"))

# Límites de la ventana enviada al LLM (lo que ocurra primero). Se aplican desde los mensajes
# más recientes; lo omitido se registra en el digest, nunca se recorta en silencio.
LLM_MAX_DAYS = int(os.environ.get("LLM_MAX_DAYS", "7"))
LLM_MAX_MESSAGES = int(os.environ.get("LLM_MAX_MESSAGES", "500"))
LLM_MAX_INPUT_CHARS = int(os.environ.get("LLM_MAX_INPUT_CHARS", "400000"))

# Worker de generación. La concesión (lease) debe superar al timeout del LLM.
WORKER_POLL_SECONDS = float(os.environ.get("WORKER_POLL_SECONDS", "5"))
WORKER_RETRY_SECONDS = float(os.environ.get("WORKER_RETRY_SECONDS", "5"))
WORKER_LEASE_SECONDS = float(os.environ.get("WORKER_LEASE_SECONDS", "120"))
WORKER_MAX_ATTEMPTS = int(os.environ.get("WORKER_MAX_ATTEMPTS", "3"))

MESSAGE_THRESHOLD = int(os.environ.get("MESSAGE_THRESHOLD", "50"))

# Exchange del chat base: topic y NO durable (debe coincidir con `messages`).
CHAT_EXCHANGE = "chat"
SUMMARIES_EXCHANGE = "summaries"
# El Gateway (aún inexistente) publica aquí la entrada de un usuario a un canal. Durable.
GATEWAY_EXCHANGE = "gateway"
