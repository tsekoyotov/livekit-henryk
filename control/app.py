"""
Control service for creating LiveKit rooms and generating JWT tokens.
Allows testing the xAI Grok agent via LiveKit Meet UI.
Supports outbound phone calls via LiveKit SIP.
Handles post-call transcription and webhooks.
"""

import json
import os
import uuid
import logging
import subprocess
import tempfile
from datetime import timedelta
from pathlib import Path
import aiohttp
import boto3
import assemblyai as aai
from fastapi import FastAPI, Request, BackgroundTasks
from fastapi.responses import JSONResponse

from livekit.api import AccessToken, VideoGrants, RoomAgentDispatch, CreateRoomRequest
from livekit.api.room_service import RoomService
from livekit.api.sip_service import SipService
from livekit.protocol.sip import CreateSIPParticipantRequest

# LiveKit configuration
LIVEKIT_URL = os.environ["LIVEKIT_URL"]
LIVEKIT_PUBLIC_URL = os.getenv("LIVEKIT_PUBLIC_URL", LIVEKIT_URL)
LIVEKIT_API_KEY = os.environ["LIVEKIT_API_KEY"]
LIVEKIT_API_SECRET = os.environ["LIVEKIT_API_SECRET"]
LIVEKIT_MEET_URL = os.getenv("LIVEKIT_MEET_URL", "http://localhost:3000")

# Agent name must match the agent's registered name
AGENT_NAME = os.getenv("LIVEKIT_AGENT_NAME", "test_agent")

# SIP configuration for phone calls
LIVEKIT_SIP_TRUNK_ID = os.getenv("LIVEKIT_SIP_TRUNK_ID", "")
TWILIO_PHONE_NUMBER = os.getenv("TWILIO_PHONE_NUMBER", "")

# Supabase Cloud Storage configuration
STORAGE_ACCESS_KEY = os.getenv("STORAGE_ACCESS_KEY", "")
STORAGE_SECRET = os.getenv("STORAGE_SECRET", "")
STORAGE_BUCKET = os.getenv("STORAGE_BUCKET", "Recordings")
STORAGE_ENDPOINT = os.getenv("STORAGE_ENDPOINT", "")
STORAGE_REGION = os.getenv("STORAGE_REGION", "eu-north-1")

# AssemblyAI configuration
ASSEMBLYAI_API_KEY = os.getenv("ASSEMBLYAI_API_KEY", "")
if ASSEMBLYAI_API_KEY:
    aai.settings.api_key = ASSEMBLYAI_API_KEY

# n8n webhook URL
POST_CALL_WEBHOOK_URL = os.getenv("POST_CALL_WEBHOOK_URL", "")

# Track processed egresses to avoid duplicates (LiveKit sends webhook 3x)
processed_egresses = set()

# Store session data for webhook payload
active_sessions = {}

app = FastAPI(title="Livekit-Henryk Control")

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


# ============================================================================
# Recording Processing Functions
# ============================================================================

def split_dual_channel_audio(input_file: Path) -> tuple[Path, Path]:
    """
    Split dual-channel OGG into separate WAV files for agent and human.

    Channel mapping (DUAL_CHANNEL_AGENT):
        - Channel 0 (left/FL): Agent audio
        - Channel 1 (right/FR): Human audio

    Returns:
        Tuple of (agent_wav_path, human_wav_path)
    """
    temp_dir = input_file.parent
    agent_wav = temp_dir / f"{input_file.stem}_agent.wav"
    human_wav = temp_dir / f"{input_file.stem}_human.wav"

    # Extract agent channel (left, channel 0)
    cmd_agent = [
        "ffmpeg", "-y", "-i", str(input_file),
        "-af", "pan=mono|c0=FL,aresample=16000",
        str(agent_wav)
    ]

    # Extract human channel (right, channel 1)
    cmd_human = [
        "ffmpeg", "-y", "-i", str(input_file),
        "-af", "pan=mono|c0=FR,aresample=16000",
        str(human_wav)
    ]

    try:
        subprocess.run(cmd_agent, check=True, capture_output=True)
        logger.info(f"Extracted agent channel: {agent_wav}")

        subprocess.run(cmd_human, check=True, capture_output=True)
        logger.info(f"Extracted human channel: {human_wav}")

        return agent_wav, human_wav
    except subprocess.CalledProcessError as e:
        logger.error(f"FFmpeg error: {e.stderr.decode()}")
        raise


def transcribe_with_assemblyai(audio_file: Path, speaker: str) -> list[dict]:
    """
    Transcribe audio file using AssemblyAI.

    Args:
        audio_file: Path to WAV file
        speaker: "ai" or "human"

    Returns:
        List of segments: [{"speaker": "ai", "t_start": 0.8, "t_end": 2.1, "text": "..."}]
    """
    if not ASSEMBLYAI_API_KEY:
        logger.warning("AssemblyAI API key not configured")
        return []

    try:
        logger.info(f"Transcribing {speaker} channel with AssemblyAI...")

        transcriber = aai.Transcriber()
        transcript = transcriber.transcribe(str(audio_file))

        if transcript.status == aai.TranscriptStatus.error:
            logger.error(f"AssemblyAI error: {transcript.error}")
            return []

        # Convert words to segments (group by 1-second gaps)
        segments = []
        current_segment = None

        for word in transcript.words or []:
            if current_segment is None:
                current_segment = {
                    "speaker": speaker,
                    "t_start": word.start / 1000.0,
                    "t_end": word.end / 1000.0,
                    "text": word.text
                }
            else:
                gap = (word.start / 1000.0) - current_segment["t_end"]
                if gap > 1.0:
                    # Start new segment
                    segments.append(current_segment)
                    current_segment = {
                        "speaker": speaker,
                        "t_start": word.start / 1000.0,
                        "t_end": word.end / 1000.0,
                        "text": word.text
                    }
                else:
                    # Continue current segment
                    current_segment["t_end"] = word.end / 1000.0
                    current_segment["text"] += " " + word.text

        if current_segment:
            segments.append(current_segment)

        logger.info(f"Transcribed {speaker}: {len(segments)} segments")
        return segments

    except Exception as e:
        logger.error(f"AssemblyAI transcription failed: {e}")
        return []


def merge_transcript_segments(agent_segments: list[dict], human_segments: list[dict]) -> list[dict]:
    """
    Merge agent and human transcript segments by timestamp.

    Rules:
        1. Combine all segments
        2. Sort by t_start
        3. Remove duplicates (cross-talk detection)
        4. Merge adjacent segments from same speaker if gap < 1 second
    """
    # Combine all segments
    all_segments = agent_segments + human_segments

    # Remove duplicates
    deduplicated = []
    seen = set()
    for seg in all_segments:
        key = (round(seg["t_start"], 2), round(seg["t_end"], 2), seg["text"].strip())
        if key not in seen:
            deduplicated.append(seg)
            seen.add(key)

    # Sort by start time
    deduplicated.sort(key=lambda x: x["t_start"])

    # Merge adjacent segments from same speaker
    merged = []
    for seg in deduplicated:
        if not merged:
            merged.append(seg)
            continue

        last = merged[-1]
        gap = seg["t_start"] - last["t_end"]

        if last["speaker"] == seg["speaker"] and gap < 1.0:
            last["text"] += " " + seg["text"]
            last["t_end"] = seg["t_end"]
        else:
            merged.append(seg)

    logger.info(f"Merged transcripts: {len(all_segments)} -> {len(merged)} segments")
    return merged


async def send_webhook_to_n8n(room_name: str, recording_url: str, transcript_structured: list[dict], session_data: dict):
    """Send post-call webhook to n8n."""
    if not POST_CALL_WEBHOOK_URL:
        logger.info("POST_CALL_WEBHOOK_URL not configured - skipping webhook")
        return

    # Format transcript as text
    transcript_formatted = "\n".join([
        f"{seg['speaker'].upper()}: {seg['text']}"
        for seg in transcript_structured
    ])

    webhook_payload = {
        "room_name": room_name,
        "phone_number": session_data.get("phone_number", ""),
        "first_name": session_data.get("first_name", ""),
        "last_name": session_data.get("last_name", ""),
        "recording_url": recording_url,
        "transcript": transcript_formatted,
        "transcript_structured": transcript_structured,
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(POST_CALL_WEBHOOK_URL, json=webhook_payload) as resp:
                if resp.status == 200:
                    logger.info(f"✅ Webhook sent to n8n: {room_name}")
                else:
                    logger.error(f"Webhook failed: HTTP {resp.status}")
    except Exception as e:
        logger.error(f"Webhook error: {e}")


async def process_recording(file_url: str, room_name: str):
    """
    Download recording, split channels, transcribe, and send webhook.

    Args:
        file_url: S3 URL of the recording (e.g., s3://bucket/room/recording.ogg)
        room_name: Name of the LiveKit room
    """
    logger.info(f"Processing recording: {file_url}")

    # Get session data
    session_data = active_sessions.get(room_name, {})

    try:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            recording_file = temp_path / "recording.ogg"

            # Download from Supabase Cloud S3
            logger.info(f"Downloading from Supabase Cloud S3...")

            # Parse S3 URL to get bucket and key
            # URL format: https://xxx.storage.supabase.co/storage/v1/s3/bucket/path/file.ogg
            # or s3://bucket/path/file.ogg
            if file_url.startswith("s3://"):
                parts = file_url[5:].split("/", 1)
                bucket = parts[0]
                key = parts[1] if len(parts) > 1 else ""
            elif ".supabase.co" in file_url and "/storage/v1/s3/" in file_url:
                # Extract bucket and key from Supabase URL
                after_s3 = file_url.split("/storage/v1/s3/")[1]
                parts = after_s3.split("/", 1)
                bucket = parts[0]
                key = parts[1] if len(parts) > 1 else ""
            else:
                logger.error(f"Unknown URL format: {file_url}")
                return

            # Create S3 client for Supabase
            s3_client = boto3.client(
                's3',
                endpoint_url=STORAGE_ENDPOINT,
                aws_access_key_id=STORAGE_ACCESS_KEY,
                aws_secret_access_key=STORAGE_SECRET,
                region_name=STORAGE_REGION,
            )

            s3_client.download_file(bucket, key, str(recording_file))
            logger.info(f"Downloaded recording: {recording_file}")

            # Split dual-channel audio
            agent_wav, human_wav = split_dual_channel_audio(recording_file)

            # Transcribe both channels
            agent_segments = transcribe_with_assemblyai(agent_wav, "ai")
            human_segments = transcribe_with_assemblyai(human_wav, "human")

            # Merge transcripts
            transcript_structured = merge_transcript_segments(agent_segments, human_segments)

            # Generate public URL for recording
            recording_public_url = file_url
            if ".supabase.co" in file_url:
                # Convert to public URL format
                recording_public_url = file_url.replace("/storage/v1/s3/", "/storage/v1/object/public/")

            # Send webhook to n8n
            await send_webhook_to_n8n(room_name, recording_public_url, transcript_structured, session_data)

            logger.info(f"✅ Recording processed: {room_name}")

    except Exception as e:
        logger.error(f"Failed to process recording: {e}", exc_info=True)


# ============================================================================
# API Endpoints
# ============================================================================

@app.get("/health")
async def health():
    """Health check endpoint."""
    return {"status": "ok", "agent_name": AGENT_NAME}


@app.post("/test-call")
async def test_call():
    """
    Create a test call room and return Meet URL with JWT token.

    Creates a LiveKit room with agent dispatch, generates a participant token,
    and returns a URL that can be used to join via LiveKit Meet.

    Returns:
    {
        "status": "success",
        "room_name": "call_abc12345",
        "meet_url": "http://localhost:3000/?url=wss://...&token=...",
        "livekit_url": "wss://...",
        "token": "eyJ..."
    }
    """
    try:
        # Create unique room name
        room_name = f"call_{uuid.uuid4().hex[:8]}"

        logger.info(f"Creating test call room: {room_name}")

        # Create LiveKit API client
        async with aiohttp.ClientSession() as session:
            room_service = RoomService(session, LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET)

            # Agent metadata (can be used by agent for context)
            agent_metadata = {
                "test_call": True,
                "room_name": room_name,
            }
            metadata_json = json.dumps(agent_metadata)

            # Create room with agent dispatch
            create_room_request = CreateRoomRequest(
                name=room_name,
                empty_timeout=300,  # 5 minutes
                max_participants=10,
                metadata=metadata_json,
            )

            # Dispatch agent to room
            agent_dispatch = RoomAgentDispatch(
                agent_name=AGENT_NAME,
                metadata=metadata_json,
            )
            create_room_request.agents.append(agent_dispatch)

            await room_service.create_room(create_room_request)
            logger.info(f"Created room: {room_name} with agent: {AGENT_NAME}")

            # Generate participant JWT token
            token = (
                AccessToken(LIVEKIT_API_KEY, LIVEKIT_API_SECRET)
                .with_identity(f"tester_{uuid.uuid4().hex[:6]}")
                .with_name("Test User")
                .with_metadata(json.dumps({"test_call": True}))
                .with_grants(
                    VideoGrants(
                        room_join=True,
                        room=room_name,
                        can_publish=True,
                        can_subscribe=True,
                        can_publish_data=True,
                    )
                )
            ).to_jwt()

            # Generate Meet URL
            meet_url = f"{LIVEKIT_MEET_URL}/?url={LIVEKIT_PUBLIC_URL}&token={token}"

            logger.info(f"Test call ready: {room_name}")

        return JSONResponse(content={
            "status": "success",
            "room_name": room_name,
            "meet_url": meet_url,
            "livekit_url": LIVEKIT_PUBLIC_URL,
            "token": token,
            "message": f"Room created. Open {LIVEKIT_MEET_URL} and paste URL + Token, or use the meet_url directly."
        })

    except Exception as e:
        logger.error(f"Error creating test call: {e}", exc_info=True)
        return JSONResponse(
            status_code=500,
            content={"error": str(e)}
        )


@app.post("/dial-lead")
async def dial_lead(request: Request):
    """
    Dial a phone number and connect to xAI Grok agent.

    Creates a LiveKit room with agent dispatch, then initiates an outbound
    SIP call to the specified phone number.

    Body:
    {
        "phone_number": "+1234567890",  # Required, E.164 format
        "first_name": "John",            # Optional
        "last_name": "Smith",            # Optional
        "initial_greeting": true         # Optional, default true
    }

    Returns:
    {
        "status": "success",
        "room_name": "call_abc12345",
        "sip_call_id": "SC_xyz789",
        "phone_number": "+1234567890",
        "message": "Dialing..."
    }
    """
    try:
        # Parse request body
        body = await request.json()
        phone_number = body.get("phone_number")
        first_name = body.get("first_name", "Caller")
        last_name = body.get("last_name", "")
        initial_greeting = body.get("initial_greeting", True)

        # Validate phone number (E.164 format)
        if not phone_number:
            return JSONResponse(
                status_code=400,
                content={"error": "phone_number is required"}
            )

        if not phone_number.startswith("+") or not phone_number[1:].replace(" ", "").isdigit():
            return JSONResponse(
                status_code=400,
                content={"error": "phone_number must be in E.164 format (+1234567890)"}
            )

        # Check SIP trunk configuration
        if not LIVEKIT_SIP_TRUNK_ID:
            logger.error("LIVEKIT_SIP_TRUNK_ID not configured")
            return JSONResponse(
                status_code=500,
                content={"error": "SIP trunk not configured. Set LIVEKIT_SIP_TRUNK_ID in .env"}
            )

        # Create unique room name
        room_name = f"call_{uuid.uuid4().hex[:8]}"

        logger.info(f"Dialing {phone_number} -> Room: {room_name}")

        async with aiohttp.ClientSession() as session:
            room_service = RoomService(session, LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET)
            sip_service = SipService(session, LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET)

            # Agent metadata (passed to agent for context)
            agent_metadata = {
                "phone_call": True,
                "phone_number": phone_number,
                "first_name": first_name,
                "last_name": last_name,
                "initial_greeting": initial_greeting,
                "room_name": room_name,
            }
            metadata_json = json.dumps(agent_metadata)

            # Create room with agent dispatch
            create_room_request = CreateRoomRequest(
                name=room_name,
                empty_timeout=60,  # 1 minute (shorter for phone calls)
                max_participants=10,
                metadata=metadata_json,
            )

            # Dispatch agent to room
            agent_dispatch = RoomAgentDispatch(
                agent_name=AGENT_NAME,
                metadata=metadata_json,
            )
            create_room_request.agents.append(agent_dispatch)

            await room_service.create_room(create_room_request)
            logger.info(f"Created room: {room_name} with agent: {AGENT_NAME}")

            # Create participant display name
            participant_name = f"{first_name} {last_name}".strip() or "Phone Caller"

            # Initiate SIP call to phone number
            sip_request = CreateSIPParticipantRequest(
                sip_trunk_id=LIVEKIT_SIP_TRUNK_ID,
                sip_call_to=phone_number,
                room_name=room_name,
                participant_identity=f"phone_{phone_number.replace('+', '')}",
                participant_name=participant_name,
                play_ringtone=True,
                ringing_timeout=timedelta(seconds=30),
                max_call_duration=timedelta(seconds=600),  # 10 minutes max
            )

            sip_response = await sip_service.create_sip_participant(sip_request)
            sip_call_id = sip_response.sip_call_id

            logger.info(f"SIP call initiated: {sip_call_id} -> {phone_number}")

            # Store session data for webhook
            active_sessions[room_name] = {
                "phone_number": phone_number,
                "first_name": first_name,
                "last_name": last_name,
                "sip_call_id": sip_call_id,
            }

        return JSONResponse(content={
            "status": "success",
            "room_name": room_name,
            "sip_call_id": sip_call_id,
            "phone_number": phone_number,
            "message": f"Dialing {phone_number}..."
        })

    except Exception as e:
        logger.error(f"Error dialing lead: {e}", exc_info=True)
        return JSONResponse(
            status_code=500,
            content={"error": str(e)}
        )


@app.post("/livekit-henryk/webhook")
async def livekit_webhook(request: Request, background_tasks: BackgroundTasks):
    """
    Handle LiveKit webhook events.

    This endpoint receives webhooks from LiveKit Cloud when egress recordings complete.
    Configure in LiveKit Cloud Console: Settings -> Webhooks -> Add this URL.

    Events handled:
        - egress_ended: Recording completed, triggers transcription pipeline
    """
    try:
        body = await request.json()
        event_type = body.get("event")

        logger.info(f"LiveKit webhook received: {event_type}")

        if event_type == "egress_ended":
            egress = body.get("egressInfo", {})
            egress_id = egress.get("egressId")
            room_name = egress.get("roomName", "")

            # Deduplicate (LiveKit sends webhook 3x for reliability)
            if egress_id in processed_egresses:
                logger.info(f"Ignoring duplicate egress webhook: {egress_id}")
                return {"ok": True, "message": "duplicate_ignored"}

            processed_egresses.add(egress_id)

            # Get file results from egress
            file_results = egress.get("fileResults", [])

            if not file_results:
                logger.warning(f"Egress ended but no file results: {egress_id}")
                return {"ok": True, "message": "no_files"}

            for file_result in file_results:
                file_url = file_result.get("location", "")
                if file_url:
                    logger.info(f"Processing recording: {file_url}")
                    # Process in background to avoid webhook timeout
                    background_tasks.add_task(process_recording, file_url, room_name)

            return {"ok": True, "message": "processing_started", "egress_id": egress_id}

        return {"ok": True, "message": f"event_ignored: {event_type}"}

    except Exception as e:
        logger.error(f"Webhook error: {e}", exc_info=True)
        return JSONResponse(
            status_code=500,
            content={"error": str(e)}
        )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=9000)
