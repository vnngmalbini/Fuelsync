"""Supabase-backed ESP32 camera frame transport."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from django.conf import settings

from config.supabase_client import get_admin_client


class CameraStreamError(RuntimeError):
    """Raised when the Supabase camera stream cannot be used."""


def _client():
    if not settings.SUPABASE_URL or not settings.SUPABASE_SERVICE_ROLE_KEY:
        raise CameraStreamError("Supabase camera storage is not configured")
    if not settings.CAMERA_DEVICE_KEY:
        raise CameraStreamError("CAMERA_DEVICE_KEY is not configured")
    return get_admin_client()


def save_frame(image_bytes: bytes, camera_id: str, user) -> dict:
    """Upload a JPEG to Storage and record its metadata in the Supabase table."""
    client = _client()
    captured_at = datetime.now(timezone.utc)
    object_path = f"{camera_id}/{captured_at:%Y/%m/%d}/{uuid4().hex}.jpg"

    client.storage.from_(settings.SUPABASE_CAMERA_BUCKET).upload(
        object_path,
        image_bytes,
        {"content-type": "image/jpeg", "cache-control": "60", "upsert": "false"},
    )
    row = {
        "camera_id": camera_id,
        "object_path": object_path,
        "content_type": "image/jpeg",
        "captured_at": captured_at.isoformat(),
        "captured_by_user_id": str(user.pk) if user and user.is_authenticated else None,
        "captured_by_email": user.email if user and user.is_authenticated else None,
        "captured_by_username": user.get_username() if user and user.is_authenticated else None,
    }
    result = client.table(settings.SUPABASE_CAMERA_TABLE).insert(row).execute()
    return result.data[0] if result.data else row


def latest_frame(camera_id: str) -> dict | None:
    """Return the latest frame metadata with a short-lived signed URL."""
    client = _client()
    result = (
        client.table(settings.SUPABASE_CAMERA_TABLE)
        .select("*")
        .eq("camera_id", camera_id)
        .order("captured_at", desc=True)
        .limit(1)
        .execute()
    )
    if not result.data:
        return None

    frame = result.data[0]
    signed = client.storage.from_(settings.SUPABASE_CAMERA_BUCKET).create_signed_url(
        frame["object_path"], 60
    )
    frame["signed_url"] = signed.get("signedURL") or signed.get("signed_url")
    return frame


def record_viewer_access(camera_id: str, user) -> None:
    """Audit which authenticated dashboard user requested the camera stream."""
    client = _client()
    client.table(settings.SUPABASE_CAMERA_ACCESS_TABLE).insert({
        "camera_id": camera_id,
        "user_id": str(user.pk),
        "email": user.email,
        "username": user.get_username(),
    }).execute()
