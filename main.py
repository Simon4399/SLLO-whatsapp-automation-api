import os
import queue
import logging
from typing import Any, Dict, List, Optional
from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel
import requests

logger = logging.getLogger(__name__)

app = FastAPI()

# Meta credentials (from .env)
VERIFY_TOKEN = os.getenv("WHATSAPP_VERIFY_TOKEN", "my_secret_verify_token")
WHATSAPP_TOKEN = os.getenv("WHATSAPP_ACCESS_TOKEN", "")
PHONE_NUMBER_ID = os.getenv("WHATSAPP_PHONE_NUMBER_ID", "")
INTERNAL_SECRET = os.getenv("GATEWAY_SECRET", "your_internal_gateway_secret")

# In-memory FIFO queue for holding incoming messages
# (In production with multiple workers, consider Redis or SQLite)
message_queue: queue.Queue = queue.Queue()


# ================== 1. META WEBHOOK ENDPOINTS ==================


@app.get("/webhook")
async def meta_verify(request: Request):
  """Verification handshake required by Meta Dashboard."""
  params = request.query_params
  mode = params.get("hub.mode")
  token = params.get("hub.verify_token")
  challenge = params.get("hub.challenge")

  if mode == "subscribe" and token == VERIFY_TOKEN:
    return PlainTextResponse(content=challenge, status_code=200)
  return Response(status_code=403)


@app.post("/webhook")
async def meta_receive(
    request: Request, 
):
    """Meta pushes incoming WhatsApp messages here."""
    data = await request.json()

    try:
        entry = data.get("entry", [])[0]
        changes = entry.get("changes", [])[0]
        value = changes.get("value", {})

        # Check if there is an actual incoming user message
        if "messages" in value:
            raw_msg = value["messages"][0]
            sender_phone = raw_msg.get("from")
            msg_type = raw_msg.get("type")
            msg_id = raw_msg.get("id")
            timestamp = raw_msg.get("timestamp")

            content_text = ""
            if msg_type == "text":
                content_text = raw_msg.get("text", {}).get("body", "")
            elif msg_type == "interactive":
                interactive = raw_msg.get("interactive", {})
                itype = interactive.get("type")
                if itype == "button_reply":
                    content_text = interactive["button_reply"]["id"]
                elif itype == "list_reply":
                    content_text = interactive["list_reply"]["id"]

            # Queue the sanitized message for your backend to poll
            message_queue.put({
                "message_id": msg_id,
                "chat_id": sender_phone,
                "text": content_text,
                "type": msg_type,
                "timestamp": timestamp,
            })

    except Exception as e:
        print(f"Error parsing Meta payload: {e}")

    # Must always return 200 immediately to Meta
    return Response(status_code=200)


# ================== 2. BACKEND API ENDPOINTS ==================


def verify_internal_auth(authorization: Optional[str]):
  if not authorization or authorization != f"Bearer {INTERNAL_SECRET}":
    raise HTTPException(status_code=401, detail="Unauthorized")


@app.get("/api/messages/poll")
def poll_messages(authorization: Optional[str] = Header(None)):
  """Called by your backend `check_and_receive_messages()`."""
  verify_internal_auth(authorization)

  fetched = []
  while not message_queue.empty():
    try:
      fetched.append(message_queue.get_nowait())
    except queue.Empty:
      break

  return {"messages": fetched}


class OutgoingMessage(BaseModel):
  chat_id: str
  text: str
  buttons: Optional[List[Dict[str, str]]] = None


@app.post("/api/messages/send")
def send_to_whatsapp(
    msg: OutgoingMessage, authorization: Optional[str] = Header(None)
):
  """Called by your backend `send_message_to_webhook()`.

  Constructs and sends the actual JSON packet to Meta Cloud API.
  """
  verify_internal_auth(authorization)

  url = f"https://graph.facebook.com/v20.0/{PHONE_NUMBER_ID}/messages"
  headers = {
      "Authorization": f"Bearer {WHATSAPP_TOKEN}",
      "Content-Type": "application/json",
  }

  # Build JSON payload based on whether buttons are included
  if msg.buttons:
    # Construct WhatsApp Interactive Button payload (max 3 buttons)
    button_elements = []
    for b in msg.buttons[:3]:
      button_elements.append(
          {"type": "reply", "reply": {"id": b["id"], "title": b["title"][:20]}}
      )

    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": msg.chat_id,
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": msg.text},
            "action": {"buttons": button_elements},
        },
    }
  else:
    # Standard text payload
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": msg.chat_id,
        "type": "text",
        "text": {"preview_url": False, "body": msg.text},
    }

  response = requests.post(url, headers=headers, json=payload, timeout=10)
  if response.status_code in (200, 201):
    return {"status": "success", "meta_response": response.json()}
  else:
    raise HTTPException(
        status_code=response.status_code,
        detail=f"Meta API error: {response.text}",
    )