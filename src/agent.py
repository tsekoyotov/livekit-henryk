import asyncio
import json
import logging
import os
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
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
from livekit.plugins.xai.realtime import (
    RealtimeModel,
    WebSearch,
)  # Using xAI plugin for Grok Voice API
from prompt_loader import get_system_prompt

logger = logging.getLogger("xai-telephony-agent")

load_dotenv(".env.local")

# Egress configuration (set in .env.local)
ENABLE_RECORDING = os.getenv("ENABLE_RECORDING", "false").lower() == "true"
S3_BUCKET = os.getenv("S3_BUCKET", "")  # Your Supabase bucket name
S3_REGION = os.getenv("S3_REGION", "eu-central-1")
S3_ACCESS_KEY = os.getenv("ACCESS_SUPABASE", "")  # Supabase access key
S3_SECRET_KEY = os.getenv("SECRET_SUPABASE", "")  # Supabase secret key
S3_ENDPOINT = os.getenv(
    "ENDPOINT_SUPABASE",
    "https://rexdoyxjqixzchgaadum.storage.supabase.co/storage/v1/s3",
)

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


async def start_recording(ctx: JobContext) -> str | None:
    """Start egress recording for the call."""
    if not ENABLE_RECORDING:
        logger.info("Recording disabled - set ENABLE_RECORDING=true to enable")
        return None

    logger.info(f"Recording enabled - checking credentials (bucket={S3_BUCKET})")

    if not all([S3_BUCKET, S3_REGION, S3_ACCESS_KEY, S3_SECRET_KEY]):
        logger.warning(
            f"S3 credentials incomplete: bucket={bool(S3_BUCKET)}, region={bool(S3_REGION)}, access_key={bool(S3_ACCESS_KEY)}, secret_key={bool(S3_SECRET_KEY)}"
        )
        return None

    try:
        # Start audio-only room composite egress
        logger.info(
            f"Starting egress recording to s3://{S3_BUCKET}/calls/{ctx.room.name}.mp3"
        )

        egress_info = await ctx.api.egress.start_room_composite_egress(
            api.RoomCompositeEgressRequest(
                room_name=ctx.room.name,
                audio_only=True,  # Only record audio
                file_outputs=[
                    api.EncodedFileOutput(
                        filepath=f"calls/{ctx.room.name}.mp3",  # Save as MP3
                        s3=api.S3Upload(
                            access_key=S3_ACCESS_KEY,
                            secret=S3_SECRET_KEY,
                            bucket=S3_BUCKET,
                            region=S3_REGION,
                            endpoint=S3_ENDPOINT,  # Supabase S3 endpoint
                            force_path_style=True,  # Required for Supabase
                        ),
                    )
                ],
            )
        )

        logger.info(
            f"✅ Recording started successfully - Egress ID: {egress_info.egress_id}"
        )
        logger.info(
            f"📁 File will be saved to: s3://{S3_BUCKET}/calls/{ctx.room.name}.mp3"
        )
        return egress_info.egress_id

    except Exception as e:
        logger.error(f"Failed to start recording: {e}")
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

    # Start recording the call
    egress_id = await start_recording(ctx)
    if egress_id:
        logger.info(f"Call recording to S3: s3://{S3_BUCKET}/calls/{ctx.room.name}.mp3")

    # Use xAI RealtimeModel - instructions loaded via Agent class from Agent_prompt.md
    model = RealtimeModel(
        voice="eve",  # xAI voice: Ara, Rex, Sal, Eve, Leo
        api_key=os.getenv("XAI_API_KEY"),
    )
    logger.info("✅ Created xAI RealtimeModel with Grok Voice API")

    # Create session with xAI RealtimeModel
    session = AgentSession(llm=model)

    # Track greeting state (dict allows modification in nested functions)
    greeting_said = {"value": False}

    async def greet_participant():
        """Generate the initial greeting."""
        if greeting_said["value"]:
            return
        greeting_said["value"] = True
        logger.info("Generating initial greeting...")
        await session.generate_reply()
        logger.info("✅ Initial greeting sent")

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

    # Register event handlers
    ctx.room.on("participant_attributes_changed", on_participant_attributes_changed)
    ctx.room.on("participant_connected", on_participant_connected)

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
