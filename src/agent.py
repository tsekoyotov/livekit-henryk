import asyncio
import logging
import os
from datetime import datetime

import httpx
from dotenv import load_dotenv
from livekit import agents, api, rtc
from livekit.agents import (
    Agent,
    AgentSession,
    JobContext,
    RunContext,
    function_tool,
    get_job_context,
    room_io,
)
from google.genai import types
from livekit.plugins import google, noise_cancellation, silero

logger = logging.getLogger("gemini-telephony-agent")

load_dotenv(".env.local")

# Webhook for end-of-call reports (set in .env.local)
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "")

# Store transcript during the call
call_transcript: list[dict] = []
call_start_time: datetime | None = None


async def hangup_call():
    """Delete the room to end the call for all participants."""
    ctx = get_job_context()
    if ctx is None:
        return
    await ctx.api.room.delete_room(
        api.DeleteRoomRequest(room=ctx.room.name)
    )


async def send_end_of_call_report():
    """Send the call transcript to the webhook (fallback for shutdown callback)."""
    global call_transcript, call_start_time
    
    if not call_transcript:
        logger.info("No transcript to send (shutdown callback)")
        return
    
    call_end_time = datetime.now()
    duration_seconds = (call_end_time - call_start_time).total_seconds() if call_start_time else 0
    
    report = {
        "call_start": call_start_time.isoformat() if call_start_time else None,
        "call_end": call_end_time.isoformat(),
        "duration_seconds": duration_seconds,
        "transcript": call_transcript,
        "message_count": len(call_transcript),
    }
    
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(WEBHOOK_URL, json=report, timeout=10.0)
            logger.info(f"End-of-call report sent (shutdown): {response.status_code}")
    except Exception as e:
        logger.error(f"Failed to send end-of-call report: {e}")
    
    # Reset for next call
    call_transcript = []
    call_start_time = None


async def send_end_of_call_report_from_session(session: AgentSession):
    """Send the call transcript from session chat context - called BEFORE room closes."""
    global call_start_time, call_transcript
    
    call_end_time = datetime.now()
    duration_seconds = (call_end_time - call_start_time).total_seconds() if call_start_time else 0
    
    # Try to get transcript from chat context
    transcript = []
    try:
        if hasattr(session, 'chat_ctx') and session.chat_ctx:
            for msg in session.chat_ctx.messages:
                content = msg.text_content() if hasattr(msg, 'text_content') else str(msg.content)
                transcript.append({
                    "role": msg.role,
                    "content": content,
                })
            logger.info(f"Extracted {len(transcript)} messages from chat_ctx")
    except Exception as e:
        logger.error(f"Failed to extract chat context: {e}")
    
    # Fall back to event-collected transcript if chat_ctx is empty
    if not transcript and call_transcript:
        transcript = call_transcript
        logger.info(f"Using event-collected transcript: {len(transcript)} messages")
    
    if not transcript:
        logger.warning("No transcript available from chat_ctx or events")
        return
    
    report = {
        "call_start": call_start_time.isoformat() if call_start_time else None,
        "call_end": call_end_time.isoformat(),
        "duration_seconds": duration_seconds,
        "transcript": transcript,
        "message_count": len(transcript),
    }
    
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(WEBHOOK_URL, json=report, timeout=10.0)
            logger.info(f"End-of-call report sent: {response.status_code}")
    except Exception as e:
        logger.error(f"Failed to send end-of-call report: {e}")


class Assistant(Agent):
    def __init__(self) -> None:
        super().__init__(instructions="")  # Main instructions are in RealtimeModel

    @function_tool
    async def hang_up(self, ctx: RunContext):
        """Hang up the phone call. Use when the user says goodbye or wants to end the call."""
        # Send end-of-call report BEFORE hanging up (while we still have context)
        await send_end_of_call_report_from_session(ctx.session)
        
        # Use generate_reply instead of say (no TTS with native audio realtime model)
        await ctx.session.generate_reply(
            instructions="Say a brief, warm goodbye like 'Goodbye! Have a great day!'"
        )
        await asyncio.sleep(2)  # Give time for the goodbye to play
        await hangup_call()


def prewarm(proc: agents.JobProcess):
    proc.userdata["vad"] = silero.VAD.load()


async def entrypoint(ctx: JobContext):
    global call_transcript, call_start_time
    
    # Reset transcript for this call
    call_transcript = []
    call_start_time = datetime.now()
    
    await ctx.connect()
    
    logger.info(f"Call started - Room: {ctx.room.name}")

    model = google.realtime.RealtimeModel(
        model="gemini-2.5-flash-native-audio-preview-09-2025",
        voice="Zephyr",
        instructions="""You are playful and on a phone call.
            Your responses should be conversational and without any complex formatting or punctuation 
            including emojis, asterisks, or other symbols.
            When the user says goodbye or wants to end the call, use the hang_up tool.
            Keep the responses short (under like 60 words).""",
        temperature=0.6,
        thinking_config=types.ThinkingConfig(
            include_thoughts=False,  # Disable thinking for faster responses
        ),
    )

    session = AgentSession(
        llm=model,
        vad=ctx.proc.userdata["vad"],
    )

    # --- Event listeners for transcript logging ---
    # Set to False to disable transcript collection (for debugging latency)
    ENABLE_TRANSCRIPT = True
    
    if ENABLE_TRANSCRIPT:
        @session.on("conversation_item_added")
        def on_conversation_item(event):
            """Log and store conversation items (works with realtime models)."""
            msg = getattr(event, 'item', event)
            role = getattr(msg, 'role', 'unknown')
            
            # Extract content
            if hasattr(msg, 'text_content') and callable(msg.text_content):
                content = msg.text_content()
            elif hasattr(msg, 'content'):
                if isinstance(msg.content, list) and len(msg.content) > 0:
                    content = msg.content[0] if isinstance(msg.content[0], str) else str(msg.content[0])
                else:
                    content = str(msg.content)
            else:
                content = str(msg)
            
            # Log
            if role == 'user':
                logger.info(f"👤 USER: {content}")
            elif role == 'assistant':
                logger.info(f"🤖 AGENT: {content}")
            
            call_transcript.append({"role": role, "content": content})
    
    # --- End event listeners ---

    await session.start(
        agent=Assistant(),
        room=ctx.room,
        room_options=room_io.RoomOptions(
            audio_input=room_io.AudioInputOptions(
                noise_cancellation=lambda params: noise_cancellation.BVCTelephony()
                if params.participant.kind == rtc.ParticipantKind.PARTICIPANT_KIND_SIP
                else noise_cancellation.BVC(),
            ),
        ),
    )

    await session.generate_reply(
        instructions="Answer the phone warmly, like 'Hello! Thanks for calling. How can I help you today?'"
    )
    
    # Wait for the session to end, then send the report
    # The room will close when the participant disconnects
    ctx.add_shutdown_callback(send_end_of_call_report)


if __name__ == "__main__":
    agents.cli.run_app(
        agents.WorkerOptions(
            entrypoint_fnc=entrypoint,
            prewarm_fnc=prewarm,
            agent_name="test_agent",  # Must match dispatch rule!
        )
    )
