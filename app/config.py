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

LLM_API_URL = os.environ.get("LLM_API_URL", "")
LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
LLM_TIMEOUT_SECONDS = float(os.environ.get("LLM_TIMEOUT_SECONDS", "60"))

MESSAGE_THRESHOLD = int(os.environ.get("MESSAGE_THRESHOLD", "50"))

# Exchange del chat base: topic y NO durable (debe coincidir con `messages`).
CHAT_EXCHANGE = "chat"
SUMMARIES_EXCHANGE = "summaries"
