"""
Control service for creating LiveKit rooms and generating JWT tokens.
Allows testing the xAI Grok agent via LiveKit Meet UI.
"""

import json
import os
import uuid
import logging
import aiohttp
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from livekit.api import AccessToken, VideoGrants, RoomAgentDispatch, CreateRoomRequest
from livekit.api.room_service import RoomService

# LiveKit configuration
LIVEKIT_URL = os.environ["LIVEKIT_URL"]
LIVEKIT_PUBLIC_URL = os.getenv("LIVEKIT_PUBLIC_URL", LIVEKIT_URL)
LIVEKIT_API_KEY = os.environ["LIVEKIT_API_KEY"]
LIVEKIT_API_SECRET = os.environ["LIVEKIT_API_SECRET"]
LIVEKIT_MEET_URL = os.getenv("LIVEKIT_MEET_URL", "http://localhost:3000")

# Agent name must match the agent's registered name
AGENT_NAME = os.getenv("LIVEKIT_AGENT_NAME", "test_agent")

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


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=9000)
