from contextlib import asynccontextmanager
import logging
from dotenv import load_dotenv

# 1. Load environment variables first
load_dotenv()

from fastapi import FastAPI
from src.config import Settings as settings
from src.send_message import router as send_router
from src.webhook import router as webhook_router

# 2. Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
  """Startup and shutdown events."""
  logger.info("SLLO WhatsApp API Webhook Server is starting up...")
  logger.info(f"Graph API version: {settings.GRAPH_API_VERSION}")
  yield
  logger.info("SLLO WhatsApp API Webhook Server is shutting down...")


# 3. Instantiate FastAPI with root_path for reverse proxy awareness
app = FastAPI(
    title="SLLO WhatsApp API Webhook Server",
    description=(
        "A WhatsApp Cloud API with FastAPI for SLLO Emergency Response System"
    ),
    version="1.0.0",
    lifespan=lifespan,
    root_path="/sllo/bridge/whatsapp-api",
)

app.include_router(webhook_router, tags=["Webhook"])
app.include_router(send_router, prefix="/api", tags=["Messaging"])


@app.get("/", tags=["Health"])
async def home():
  """Health check endpoint."""
  return {
      "status": "running",
      "service": "SLLO WhatsApp API Webhook Server",
      "version": "1.0.0",
  }


@app.get("/health", tags=["Health"])
async def health_check():
  """Detailed health check endpoint."""
  return {
      "status": "healthy",
      "api_version": settings.GRAPH_API_VERSION,
      "webhook_configured": bool(settings.VERIFY_TOKEN),
      "whatsapp_configured": bool(
          settings.WHATSAPP_TOKEN and settings.WHATSAPP_PHONE_NUMBER_ID
      ),
  }