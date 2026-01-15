"""
Control service for creating LiveKit rooms and generating JWT tokens.
Allows testing the xAI Grok agent via LiveKit Meet UI.
Supports outbound phone calls via LiveKit SIP.
"""

import json
import os
import uuid
import logging
from datetime import timedelta
import aiohttp
from fastapi import FastAPI, Request
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

app = FastAPI(title="Livekit-Henryk Control")

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


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


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=9000)
