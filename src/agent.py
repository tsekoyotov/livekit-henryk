import asyncio
import json
import logging
import os
from datetime import datetime
from zoneinfo import ZoneInfo

import aiohttp
from dotenv import load_dotenv
from livekit import api, agents, rtc
from livekit.agents import (
    Agent,
    AgentSession,
    JobContext,
    RunContext,
    cli,
    function_tool,
    get_job_context,
    llm,
)
from livekit.api.egress_service import EgressService
from livekit.api import RoomCompositeEgressRequest, EncodedFileOutput, S3Upload
from livekit.protocol.egress import AudioMixing, EgressStatus, StopEgressRequest
from livekit.plugins.xai.realtime import (
    RealtimeModel,
    WebSearch,
)  # Using xAI plugin for Grok Voice API
from prompt_loader import get_system_prompt

logger = logging.getLogger("xai-telephony-agent")

load_dotenv(".env.local")

# Supabase Cloud Storage configuration (for dual-channel egress)
STORAGE_ACCESS_KEY = os.getenv("STORAGE_ACCESS_KEY", "")
STORAGE_SECRET = os.getenv("STORAGE_SECRET", "")
STORAGE_BUCKET = os.getenv("STORAGE_BUCKET", "Recordings")
STORAGE_ENDPOINT = os.getenv("STORAGE_ENDPOINT", "")
STORAGE_REGION = os.getenv("STORAGE_REGION", "eu-north-1")

# Check if recording is configured
RECORDING_ENABLED = all([STORAGE_ACCESS_KEY, STORAGE_SECRET, STORAGE_BUCKET, STORAGE_ENDPOINT])

# Web search configuration
EXA_API_KEY = os.getenv("EXA_API_KEY", "")

# Timezone configuration (IANA timezone, e.g., "America/New_York", "Europe/London")
AGENT_TIMEZONE = os.getenv("AGENT_TIMEZONE", "UTC")


async def hangup_call():
    """Delete the room to end the call for all participants."""
    ctx = get_job_context()
    if ctx is None:
        return
    await ctx.api.room.delete_room(api.DeleteRoomRequest(room=ctx.room.name))


class Assistant(Agent):
    """Agent class that provides tools and instructions for the xAI RealtimeModel."""

    def __init__(self, time_str: str, timezone: str) -> None:
        # Load instructions from Agent_prompt.md via prompt_loader
        instructions = get_system_prompt(time_str, timezone)

        # Following xAI plugin examples - instructions go in Agent, not ChatContext
        super().__init__(instructions=instructions)
        logger.info(
            f"✅ Agent initialized with instructions from Agent_prompt.md (length: {len(instructions)})"
        )

    # Removed custom search_web tool - using xAI's built-in WebSearch tool

    @function_tool()
    async def hang_up(self, ctx: RunContext):
        """Hang up the phone call. Use when the user says goodbye or wants to end the call."""
        logger.info("Hang up tool called - initiating call termination")

        # Give a moment for any pending audio to finish
        await asyncio.sleep(0.5)

        # Delete room to end the SIP call
        await hangup_call()

        logger.info("Room deleted - call ended")

        # Wait for SIP disconnect to complete before function returns
        await asyncio.sleep(2.0)

        return "Call ended successfully"


async def start_dual_channel_recording(ctx: JobContext) -> str | None:
    """
    Start dual-channel egress recording to Supabase Cloud.

    Uses AudioMixing.DUAL_CHANNEL_AGENT:
    - Left channel (FL): Agent audio
    - Right channel (FR): Human audio

    This allows post-call transcription of both speakers separately.
    """
    if not RECORDING_ENABLED:
        logger.info("Recording disabled - configure STORAGE_* env vars to enable")
        return None

    logger.info(f"Starting dual-channel egress to s3://{STORAGE_BUCKET}/{ctx.room.name}/")

    try:
        # Configure S3 upload to Supabase Cloud
        s3_upload = S3Upload(
            access_key=STORAGE_ACCESS_KEY,
            secret=STORAGE_SECRET,
            region=STORAGE_REGION,
            bucket=STORAGE_BUCKET,
            endpoint=STORAGE_ENDPOINT,
            force_path_style=True,
        )

        # File output configuration (OGG format for better compression)
        file_output = EncodedFileOutput(
            file_type=api.EncodedFileType.OGG,
            filepath=f"{ctx.room.name}/recording-{{time}}.ogg",
            s3=s3_upload,
        )

        # Create egress request with dual-channel audio mixing
        egress_request = RoomCompositeEgressRequest(
            room_name=ctx.room.name,
            audio_only=True,
            audio_mixing=AudioMixing.DUAL_CHANNEL_AGENT,  # Agent=Left, Human=Right
            file_outputs=[file_output],
        )

        # Start egress recording
        async with aiohttp.ClientSession() as http_session:
            egress_service = EgressService(
                http_session,
                os.environ["LIVEKIT_URL"],
                os.environ["LIVEKIT_API_KEY"],
                os.environ["LIVEKIT_API_SECRET"],
            )
            egress_info = await egress_service.start_room_composite_egress(egress_request)

        logger.info(f"✅ Dual-channel recording started - Egress ID: {egress_info.egress_id}")
        logger.info(f"📁 Recording to: s3://{STORAGE_BUCKET}/{ctx.room.name}/recording-*.ogg")
        return egress_info.egress_id

    except Exception as e:
        logger.error(f"Failed to start dual-channel recording: {e}")
        return None


async def entrypoint(ctx: JobContext):
    await ctx.connect()

    logger.info(f"Call started - Room: {ctx.room.name}")

    # Parse room metadata to check if this is a phone call
    room_metadata = {}
    try:
        if ctx.room.metadata:
            room_metadata = json.loads(ctx.room.metadata)
    except json.JSONDecodeError:
        pass

    is_phone_call = room_metadata.get("phone_call", False)
    initial_greeting_enabled = room_metadata.get("initial_greeting", True)
    caller_first_name = room_metadata.get("first_name", "")

    logger.info(f"Room metadata: phone_call={is_phone_call}, initial_greeting={initial_greeting_enabled}, first_name={caller_first_name}")

    # Get current time in configured timezone
    try:
        tz = ZoneInfo(AGENT_TIMEZONE)
        current_time = datetime.now(tz)
        time_str = current_time.strftime("%A, %B %d, %Y at %I:%M %p")
        timezone_name = AGENT_TIMEZONE
        logger.info(f"Agent timezone: {timezone_name}, Current time: {time_str}")
    except Exception as e:
        logger.warning(f"Invalid timezone '{AGENT_TIMEZONE}', falling back to UTC: {e}")
        current_time = datetime.now(ZoneInfo("UTC"))
        time_str = current_time.strftime("%A, %B %d, %Y at %I:%M %p")
        timezone_name = "UTC"

    # Use xAI RealtimeModel - instructions loaded via Agent class from Agent_prompt.md
    model = RealtimeModel(
        voice="eve",  # xAI voice: Ara, Rex, Sal, Eve, Leo
        api_key=os.getenv("XAI_API_KEY"),
    )
    logger.info("✅ Created xAI RealtimeModel with Grok Voice API")

    # Create session with xAI RealtimeModel
    session = AgentSession(llm=model)

    # Track greeting and recording state (dict allows modification in nested functions)
    greeting_said = {"value": False}
    egress_started = {"value": False}
    egress_stopped = {"value": False}
    current_egress_id = {"value": None}

    async def greet_participant():
        """Generate the initial greeting and start recording."""
        if greeting_said["value"]:
            return
        greeting_said["value"] = True

        # Start recording when call is answered (not during ringing)
        if not egress_started["value"]:
            egress_started["value"] = True
            egress_id = await start_dual_channel_recording(ctx)
            if egress_id:
                current_egress_id["value"] = egress_id
                logger.info(f"📼 Recording started - Egress ID: {egress_id}")

        logger.info("Generating initial greeting...")
        await session.generate_reply()
        logger.info("✅ Initial greeting sent")

    async def stop_egress_and_cleanup():
        """Stop egress recording and delete room when participant disconnects."""
        if egress_stopped["value"]:
            return
        egress_stopped["value"] = True

        egress_id = current_egress_id["value"]

        # Stop egress first
        if egress_id:
            try:
                async with aiohttp.ClientSession() as http_session:
                    egress_service = EgressService(
                        http_session,
                        os.environ["LIVEKIT_URL"],
                        os.environ["LIVEKIT_API_KEY"],
                        os.environ["LIVEKIT_API_SECRET"],
                    )
                    await egress_service.stop_egress(StopEgressRequest(egress_id=egress_id))
                    logger.info(f"⏹️ Egress stopped - ID: {egress_id}")
            except Exception as e:
                logger.error(f"Failed to stop egress: {e}")

        # Small delay to ensure egress stop is processed
        await asyncio.sleep(0.5)

        # Delete room to force complete cleanup (same as hang_up tool)
        try:
            await ctx.api.room.delete_room(api.DeleteRoomRequest(room=ctx.room.name))
            logger.info(f"🗑️ Room deleted: {ctx.room.name}")
        except Exception as e:
            logger.error(f"Failed to delete room: {e}")

    # Handler for participant attributes changed (SIP call status updates)
    def on_participant_attributes_changed(changed_attributes: dict, participant: rtc.Participant):
        """Detect when SIP call transitions to 'active' (answered)."""
        if participant.kind == rtc.ParticipantKind.PARTICIPANT_KIND_SIP:
            call_status = changed_attributes.get("sip.callStatus", "")
            logger.info(f"SIP status change: {participant.identity} -> {call_status}")

            if call_status == "active" and not greeting_said["value"] and initial_greeting_enabled:
                logger.info(f"📞 SIP call answered: {participant.identity}")
                asyncio.create_task(greet_participant())

    # Handler for participant connected
    def on_participant_connected(participant: rtc.Participant):
        """Handle new participant joining the room."""
        logger.info(f"Participant connected: {participant.identity} (kind={participant.kind})")

        if participant.kind == rtc.ParticipantKind.PARTICIPANT_KIND_SIP:
            # SIP participant (phone call) - check if already answered
            call_status = participant.attributes.get("sip.callStatus", "")
            logger.info(f"SIP participant: {participant.identity}, callStatus={call_status}")

            if call_status == "active" and not greeting_said["value"] and initial_greeting_enabled:
                logger.info(f"📞 SIP call already active: {participant.identity}")
                asyncio.create_task(greet_participant())
            else:
                logger.info(f"📞 SIP call dialing: {participant.identity} - waiting for answer...")
        else:
            # Browser participant - greet immediately
            if not greeting_said["value"] and initial_greeting_enabled:
                logger.info(f"🌐 Browser participant: {participant.identity} - greeting...")
                asyncio.create_task(greet_participant())

    # Handler for participant disconnected - stop egress and cleanup room
    def on_participant_disconnected(participant: rtc.RemoteParticipant):
        """Stop egress and delete room when participant disconnects."""
        logger.info(f"👋 Participant disconnected: {participant.identity} - stopping egress and cleaning up")
        asyncio.create_task(stop_egress_and_cleanup())

    # Register event handlers
    ctx.room.on("participant_attributes_changed", on_participant_attributes_changed)
    ctx.room.on("participant_connected", on_participant_connected)
    ctx.room.on("participant_disconnected", on_participant_disconnected)

    # Add event listeners for debugging
    @session.on("user_started_speaking")
    def on_user_started():
        logger.info("🎤 User started speaking")

    @session.on("user_stopped_speaking")
    def on_user_stopped():
        logger.info("🎤 User stopped speaking")

    @session.on("agent_started_speaking")
    def on_agent_started():
        logger.info("🔊 Agent started speaking")

    @session.on("agent_stopped_speaking")
    def on_agent_stopped():
        logger.info("🔊 Agent stopped speaking")

    # Start session with Agent - instructions come from Agent class
    agent = Assistant(time_str=time_str, timezone=timezone_name)
    await session.start(
        room=ctx.room,
        agent=agent,
    )
    logger.info("✅ Session started")

    # Update instructions on the realtime session (workaround for xAI plugin)
    try:
        await session.llm.update_instructions(agent.instructions)
        logger.info(f"✅ Instructions sent to xAI model (length: {len(agent.instructions)})")
    except Exception as e:
        logger.warning(f"Could not update instructions: {e}")

    # Check for existing participants (race condition: they may have joined before handlers registered)
    if initial_greeting_enabled and not greeting_said["value"]:
        for participant in ctx.room.remote_participants.values():
            if greeting_said["value"]:
                break

            if participant.kind == rtc.ParticipantKind.PARTICIPANT_KIND_SIP:
                call_status = participant.attributes.get("sip.callStatus", "")
                logger.info(f"Found existing SIP participant: {participant.identity}, callStatus={call_status}")
                if call_status == "active":
                    logger.info(f"📞 Existing SIP participant active - greeting...")
                    asyncio.create_task(greet_participant())
            else:
                logger.info(f"Found existing browser participant: {participant.identity} - greeting...")
                asyncio.create_task(greet_participant())


if __name__ == "__main__":
    cli.run_app(
        agents.WorkerOptions(
            entrypoint_fnc=entrypoint,
            agent_name="test_agent",  # Must match dispatch rule!
        )
    )
