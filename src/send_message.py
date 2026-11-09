import logging
import os
from typing import Any, Dict, Optional

import httpx
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from src.config import Settings

import src.models as models

# Instantiate Settings() so the @property `BASE_URL` evaluates to a string
# instead of returning a `<property object>` descriptor on the class.
settings = Settings() if isinstance(Settings, type) else Settings

logger = logging.getLogger(__name__)

router = APIRouter()

# Cache if WHATSAPP_PHONE_NUMBER_ID was accidentally set to the WABA ID and auto-resolved
_resolved_phone_number_id: Optional[str] = None


def _verify_internal_auth(authorization: Optional[str]) -> None:
    """Validates `INTERNAL_SECRET` between backend and webhook server.

    If `INTERNAL_SECRET` is empty in `.env`, skips strict check so a stripped 'Bearer'
    header does not trigger a false 401 Unauthorized.
    """
    internal_secret = (getattr(settings, "INTERNAL_SECRET", "") or os.getenv("INTERNAL_SECRET", "")).strip()
    if not internal_secret:
        return

    if not authorization:
        raise HTTPException(status_code=401, detail="Missing Authorization header")

    scheme, _, token = authorization.strip().partition(" ")
    if scheme.lower() != "bearer" or token.strip() != internal_secret:
        raise HTTPException(status_code=401, detail="Unauthorized: invalid INTERNAL_SECRET token")


def _get_api_version() -> str:
    version = (
        getattr(settings, "GRAPH_API_VERSION", "")
        or os.getenv("GRAPH_API_VERSION", "v21.0")
    ).strip("/")
    return version if version.startswith("v") else f"v{version}"


def _get_configured_phone_id() -> str:
    if _resolved_phone_number_id:
        return _resolved_phone_number_id
    return (
        getattr(settings, "WHATSAPP_PHONE_NUMBER_ID", "")
        or os.getenv("WHATSAPP_PHONE_NUMBER_ID", "")
    ).strip("/")


def _get_messages_endpoint_url(override_phone_id: Optional[str] = None) -> str:
    """Resolves the Meta Graph API `/messages` endpoint URL from the instantiated `settings`."""
    version = _get_api_version()
    phone_id = (override_phone_id or _get_configured_phone_id()).strip("/")

    if override_phone_id or _resolved_phone_number_id:
        return f"https://graph.facebook.com/{version}/{phone_id}/messages"

    base_url = getattr(settings, "BASE_URL", "")
    if isinstance(base_url, property) or not isinstance(base_url, str) or not base_url.startswith(("http://", "https://")):
        base_url = f"https://graph.facebook.com/{version}/{phone_id}"

    base_url = base_url.rstrip("/")
    if base_url.endswith("/messages"):
        return base_url
    return f"{base_url}/messages"


async def _try_resolve_waba_to_phone_id(
    client: httpx.AsyncClient, configured_id: str, headers: Dict[str, str]
) -> Optional[str]:
    """If `WHATSAPP_PHONE_NUMBER_ID` was accidentally set to a WhatsApp Business Account (WABA) ID,
    queries `GET /{WABA_ID}/phone_numbers` to auto-discover the true Phone Number ID.
    """
    global _resolved_phone_number_id
    if not configured_id:
        return None
    version = _get_api_version()
    lookup_url = f"https://graph.facebook.com/{version}/{configured_id}/phone_numbers"
    try:
        resp = await client.get(lookup_url, headers=headers)
        if resp.status_code == 200:
            body = resp.json()
            numbers = body.get("data") or []
            if numbers and numbers[0].get("id"):
                real_id = str(numbers[0]["id"])
                display_num = numbers[0].get("display_phone_number", "")
                _resolved_phone_number_id = real_id
                logger.warning(
                    f"Configured ID '{configured_id}' is a WhatsApp Business Account (WABA) ID, "
                    f"not a Phone Number ID. Auto-resolved to Phone Number ID '{real_id}' ({display_num}). "
                    f"Please update WHATSAPP_PHONE_NUMBER_ID={real_id} in your .env file."
                )
                return real_id
    except Exception as e:
        logger.debug(f"WABA phone_numbers lookup failed for {configured_id}: {e}")
    return None


async def _send_whatsapp_message(payload: Dict[str, Any]) -> models.MessageResponse:
    """Send a message or read-receipt payload via the WhatsApp Cloud API using httpx (async).

    Args:
        payload: The WhatsApp API message payload.

    Returns:
        MessageResponse with success status and message ID.
    """
    override_phone_id = payload.pop("phone_number_id", None)
    url = _get_messages_endpoint_url(override_phone_id)
    token = (getattr(settings, "WHATSAPP_TOKEN", "") or os.getenv("WHATSAPP_TOKEN", "")).strip()
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            response = await client.post(url, headers=headers, json=payload)
            try:
                data = response.json()
            except ValueError:
                data = {}

            # If Meta returns 400 "Unsupported post request", check if WHATSAPP_PHONE_NUMBER_ID is actually a WABA ID
            error_obj = data.get("error", {}) if isinstance(data, dict) else {}
            error_msg = error_obj.get("message", "")
            if (
                response.status_code == 400
                and "Unsupported post request" in error_msg
                and not override_phone_id
                and not _resolved_phone_number_id
            ):
                configured_id = _get_configured_phone_id()
                real_phone_id = await _try_resolve_waba_to_phone_id(client, configured_id, headers)
                if real_phone_id:
                    retry_url = _get_messages_endpoint_url(real_phone_id)
                    response = await client.post(retry_url, headers=headers, json=payload)
                    try:
                        data = response.json()
                    except ValueError:
                        data = {}
                    error_obj = data.get("error", {}) if isinstance(data, dict) else {}
                    error_msg = error_obj.get("message", "")

            if response.status_code in (200, 201):
                # Read receipts (`status: "read"`) return `{"success": true}` without a `messages` list
                if payload.get("status") == "read":
                    msg_id = str(payload.get("message_id", ""))
                    logger.info(f"Marked message as read: {msg_id}")
                    return models.MessageResponse(success=True, message_id=msg_id)

                messages_list = data.get("messages") or [{}]
                message_id = messages_list[0].get("id", "") if messages_list else ""
                logger.info(f"Message sent successfully: {message_id}")
                return models.MessageResponse(success=True, message_id=message_id)

            error_msg = (
                error_msg
                or error_obj.get("error_user_msg")
                or f"HTTP {response.status_code}: {response.text[:200]}"
            )
            logger.error(f"WhatsApp API error ({response.status_code}): {error_msg}")
            return models.MessageResponse(success=False, error=error_msg)

        except httpx.TimeoutException:
            logger.error("WhatsApp API request timed out")
            return models.MessageResponse(success=False, error="Request timed out")
        except httpx.RequestError as e:
            logger.error(f"Request error calling {url}: {e}")
            return models.MessageResponse(success=False, error=str(e))


@router.post("/send", response_model=models.MessageResponse)
@router.post("/send/", response_model=models.MessageResponse)
async def forward_to_whatsapp(
    payload: Dict[str, Any], authorization: Optional[str] = Header(None)
) -> models.MessageResponse:
    """Receives an outgoing WhatsApp JSON payload from your backend and relays it to Meta Graph API.

    Supports all WhatsApp Cloud API message types (text, interactive buttons/lists/CTA URL/location request/
    address/flow/voice call, image, document, audio, video, sticker, location, contacts, reaction, template),
    contextual replies (`context.message_id`), and read receipts (`status: "read"`).
    """
    _verify_internal_auth(authorization)

    # Ensure messaging_product is always set to "whatsapp"
    payload["messaging_product"] = "whatsapp"

    # Allow shorthand `reply_to_message_id` from backend callers and convert to WhatsApp `context`
    reply_to_id = payload.pop("reply_to_message_id", None)
    if reply_to_id and "context" not in payload and payload.get("type") != "reaction":
        payload["context"] = {"message_id": reply_to_id}

    # For regular messages (not status="read"), default recipient_type to "individual"
    if payload.get("status") != "read":
        payload.setdefault("recipient_type", "individual")

    return await _send_whatsapp_message(payload)


@router.post("/send-message", response_model=models.MessageResponse)
async def send_text_message(
    payload: models.SendTextRequest, authorization: Optional[str] = Header(None)
) -> models.MessageResponse:
    """Send a text message (with optional contextual reply) to a WhatsApp number."""
    _verify_internal_auth(authorization)
    data: Dict[str, Any] = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": payload.to,
        "type": "text",
        "text": {"body": payload.message},
    }
    reply_id = getattr(payload, "reply_to_message_id", None)
    if reply_id:
        data["context"] = {"message_id": reply_id}
    return await _send_whatsapp_message(data)


@router.post("/send-image", response_model=models.MessageResponse)
async def send_image_message(
    payload: models.SendImageRequest, authorization: Optional[str] = Header(None)
) -> models.MessageResponse:
    """Send an image message to a WhatsApp number."""
    _verify_internal_auth(authorization)
    image_data: Dict[str, Any] = {"link": payload.image_url}
    if payload.caption:
        image_data["caption"] = payload.caption

    data: Dict[str, Any] = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": payload.to,
        "type": "image",
        "image": image_data,
    }
    reply_id = getattr(payload, "reply_to_message_id", None)
    if reply_id:
        data["context"] = {"message_id": reply_id}
    return await _send_whatsapp_message(data)


@router.post("/send-document", response_model=models.MessageResponse)
async def send_document_message(
    payload: models.SendDocumentRequest, authorization: Optional[str] = Header(None)
) -> models.MessageResponse:
    """Send a document message to a WhatsApp number."""
    _verify_internal_auth(authorization)
    doc_data: Dict[str, Any] = {"link": payload.document_url}
    if payload.filename:
        doc_data["filename"] = payload.filename
    if payload.caption:
        doc_data["caption"] = payload.caption

    data: Dict[str, Any] = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": payload.to,
        "type": "document",
        "document": doc_data,
    }
    reply_id = getattr(payload, "reply_to_message_id", None)
    if reply_id:
        data["context"] = {"message_id": reply_id}
    return await _send_whatsapp_message(data)


@router.post("/send-template", response_model=models.MessageResponse)
async def send_template_message(
    payload: models.SendTemplateRequest, authorization: Optional[str] = Header(None)
) -> models.MessageResponse:
    """Send a template message to a WhatsApp number (works outside the 24h customer service window)."""
    _verify_internal_auth(authorization)
    template_data: Dict[str, Any] = {
        "name": payload.template_name,
        "language": {"code": payload.language_code},
    }
    if payload.components:
        template_data["components"] = [
            c.model_dump() if hasattr(c, "model_dump") else c
            for c in payload.components
        ]

    data: Dict[str, Any] = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": payload.to,
        "type": "template",
        "template": template_data,
    }
    return await _send_whatsapp_message(data)