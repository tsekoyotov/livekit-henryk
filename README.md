# xAI Grok Voice Agent with LiveKit

A voice AI agent powered by xAI's Grok Voice API for real-time speech-to-speech conversations, built with [LiveKit Agents](https://github.com/livekit/agents).

## Features

- **Speech-to-Speech**: Uses xAI Grok Voice API for natural, audio-to-audio conversations
- **Phone Calls**: Outbound dialing via LiveKit SIP + Twilio
- **Dual-Channel Recording**: Records agent (left) and human (right) on separate channels
- **Auto Transcription**: AssemblyAI transcribes both channels, merged by timestamp
- **Webhook Integration**: Sends transcript and recording URL to n8n after call ends
- **Browser Testing**: Test via LiveKit Meet with JWT tokens
- **External Prompt Loading**: Prompts stored in markdown files for easy editing
- **Variable Substitution**: Supports `{{current_time}}` and `{{timezone}}` in prompts
- **Call Management**: Built-in `hang_up` tool for graceful call termination
- **Docker Ready**: Includes Dockerfile and docker-compose.yml

## Prerequisites

- Python 3.13+
- [uv](https://github.com/astral-sh/uv) package manager
- [LiveKit Cloud](https://cloud.livekit.io/) account
- [xAI API key](https://x.ai/) for Grok Voice API
- Docker (optional, for containerized deployment)

## Quick Start

### 1. Clone and install

```bash
git clone https://github.com/henryklunaris/lk-google-telnyx-1.git
cd lk-google-telnyx-1
uv sync
```

### 2. Configure environment

Create `.env` with your API keys:

```bash
LIVEKIT_URL=wss://your-project.livekit.cloud
LIVEKIT_PUBLIC_URL=wss://your-project.livekit.cloud
LIVEKIT_API_KEY=your_api_key
LIVEKIT_API_SECRET=your_api_secret
LIVEKIT_MEET_URL=https://meet.livekit.io
LIVEKIT_AGENT_NAME=test_agent
XAI_API_KEY=your_xai_api_key
AGENT_TIMEZONE=UTC
```

### 3. Run with Docker

```bash
docker compose up -d --build
```

### 4. Test the agent

Run the test call script:

```bash
./call
```

This will:
1. Create a LiveKit room with agent dispatch
2. Generate a JWT token for you to join
3. Display the URL and token

Then either:
- **Option A**: Open the `meet_url` directly in your browser
- **Option B**: Go to [meet.livekit.io](https://meet.livekit.io), paste URL + Token

## Project Structure

```
.
├── src/
│   ├── agent.py           # Main agent code
│   ├── Agent_prompt.md    # Rachel's prompt (edit this!)
│   └── prompt_loader.py   # Loads prompts with variable substitution
├── control/
│   ├── app.py             # Control service (room/token/SIP)
│   └── Dockerfile
├── call                   # Browser test call script
├── dial                   # Phone dialing script
├── docker-compose.yml     # Docker configuration
├── Dockerfile
├── pyproject.toml
└── .env                   # API keys (not committed)
```

## Docker Services

| Service | Container | Port | Description |
|---------|-----------|------|-------------|
| Agent | Livekit-Henryk | - | xAI Grok voice agent |
| Control | Livekit-Henryk-Control | 9001 | Room creation & JWT tokens |

## Testing the Agent

### Method 1: Using the `./call` script (Recommended)

```bash
./call
```

The script will output:
```
═══════════════════════════════════════════════════════
   Join the Call
═══════════════════════════════════════════════════════

Option 1: Open Meet URL directly
https://meet.livekit.io/?url=wss://...&token=eyJ...

Option 2: Manual join at https://meet.livekit.io

URL: wss://your-project.livekit.cloud
Token: eyJ...
```

### Method 2: Using LiveKit Cloud Sandbox

1. Go to [cloud.livekit.io](https://cloud.livekit.io)
2. Select your project → **Sandbox**
3. Create a **Web Voice Agent** sandbox
4. Set **Agent name** to `test_agent`
5. Launch and start call

### Method 3: Direct API call

```bash
curl -X POST http://localhost:9001/test-call
```

Returns:
```json
{
  "status": "success",
  "room_name": "call_abc12345",
  "meet_url": "https://meet.livekit.io/?url=wss://...&token=...",
  "livekit_url": "wss://...",
  "token": "eyJ..."
}
```

## Phone Calls (Outbound Dialing)

Make real phone calls to any number using LiveKit SIP + Twilio.

### Prerequisites for Phone Calls

1. **Twilio Account**: Sign up at [twilio.com](https://twilio.com)
2. **Twilio Phone Number**: Purchase a number (E.164 format: +1234567890)
3. **Twilio SIP Trunk**: Create in Twilio Console with credentials
4. **LiveKit Cloud SIP Trunk**: Configure in LiveKit Cloud Console
   - Go to [cloud.livekit.io](https://cloud.livekit.io) → Your Project → Settings → SIP
   - Add your Twilio trunk credentials
   - Copy the `LIVEKIT_SIP_TRUNK_ID` (starts with `ST_`)

### Setup

Add to your `.env`:

```bash
LIVEKIT_SIP_TRUNK_ID=ST_xxxxxxxxxxxx
TWILIO_PHONE_NUMBER=+1234567890
```

Rebuild containers:

```bash
docker compose up -d --build
```

### Dialing a Phone Number

Use the `./dial` script:

```bash
./dial +1234567890 "John"
```

Or call the API directly:

```bash
curl -X POST http://localhost:9001/dial-lead \
  -H "Content-Type: application/json" \
  -d '{
    "phone_number": "+1234567890",
    "first_name": "John",
    "last_name": "Smith",
    "initial_greeting": true
  }'
```

Returns:
```json
{
  "status": "success",
  "room_name": "call_abc12345",
  "sip_call_id": "SC_xyz789",
  "phone_number": "+1234567890",
  "message": "Dialing +1234567890..."
}
```

### Call Flow

```
1. ./dial +1234567890 "John"
       ↓
2. Control service creates LiveKit room
       ↓
3. SIP call initiated via LiveKit Cloud
       ↓
4. Twilio routes call to PSTN
       ↓
5. Phone rings → User answers
       ↓
6. Agent detects answer (sip.callStatus=active)
       ↓
7. Recording starts + Rachel greets
       ↓
8. Call ends → Transcription → Webhook to n8n
```

## Recording & Transcription

Calls are automatically recorded and transcribed when configured.

### Recording Flow

```
Call Answered (sip.callStatus=active)
       ↓
Dual-Channel Recording Starts
(Agent=Left channel, Human=Right channel)
       ↓
Recording → Supabase Cloud S3 (stereo OGG)
       ↓
Call Ends → LiveKit sends egress_ended webhook
       ↓
Control service downloads recording
       ↓
FFmpeg splits channels → AssemblyAI transcribes both
       ↓
Transcripts merged by timestamp
       ↓
Webhook sent to n8n with transcript + recording URL
```

### Setup Recording

Add to your `.env`:

```bash
# Supabase Cloud Storage (S3-compatible)
STORAGE_ACCESS_KEY=your_supabase_s3_access_key
STORAGE_SECRET=your_supabase_s3_secret
STORAGE_BUCKET=Recordings
STORAGE_ENDPOINT=https://your-project.supabase.co/storage/v1/s3
STORAGE_REGION=eu-north-1

# AssemblyAI (for transcription)
ASSEMBLYAI_API_KEY=your_assemblyai_key

# n8n Webhook (receives transcript after call)
POST_CALL_WEBHOOK_URL=https://your-n8n/webhook/your-id
```

Configure LiveKit webhook in [cloud.livekit.io](https://cloud.livekit.io):
- URL: `https://your-control-service/livekit-henryk/webhook`
- Events: `egress_ended`

### Webhook Payload

After transcription, n8n receives:

```json
{
  "room_name": "call_abc123",
  "phone_number": "+1234567890",
  "first_name": "John",
  "recording_url": "https://...supabase.co/.../recording.ogg",
  "transcript": "AI: Hello...\nHUMAN: Hi...",
  "transcript_structured": [
    {"speaker": "ai", "t_start": 0.8, "t_end": 2.1, "text": "Hello..."},
    {"speaker": "human", "t_start": 2.5, "t_end": 3.2, "text": "Hi..."}
  ]
}
```

## Customizing the Agent

### Change the prompt

Edit `src/Agent_prompt.md`:

```markdown
# Rachel Voice Agent

## System Prompt

You are [your custom personality]...

The current time is {{current_time}} ({{timezone}}).
```

Supported variables:
- `{{current_time}}` - Formatted datetime (e.g., "Thursday, January 15, 2026 at 07:30 PM")
- `{{timezone}}` - IANA timezone (e.g., "UTC", "America/New_York")

### Change the voice

Available xAI voices: `Ara`, `Eve`, `Leo`, `Rex`, `Sal`, `Mika`, `Valentin`

Edit `src/agent.py`:

```python
model = RealtimeModel(
    voice="eve",  # Change this
    api_key=os.getenv("XAI_API_KEY"),
)
```

### Change agent name

The agent name must match your LiveKit dispatch rule and `.env`:

1. Update `.env`:
   ```bash
   LIVEKIT_AGENT_NAME=your_agent_name
   ```

2. Update `src/agent.py`:
   ```python
   cli.run_app(
       agents.WorkerOptions(
           entrypoint_fnc=entrypoint,
           agent_name="your_agent_name",
       )
   )
   ```

## xAI Plugin Notes

### Known Limitation

The LiveKit xAI plugin (`livekit-plugins-xai>=1.3.10`) does **not** accept an `instructions` parameter directly in `RealtimeModel()`. This is a known issue ([#4305](https://github.com/livekit/agents/issues/4305)).

### Workaround

Instructions are loaded through the `Agent` class:

```python
class Assistant(Agent):
    def __init__(self, time_str: str, timezone: str) -> None:
        instructions = get_system_prompt(time_str, timezone)  # From prompt_loader
        super().__init__(instructions=instructions)
```

Then call `session.generate_reply()` to trigger the initial greeting with the personality:

```python
session = AgentSession(llm=model)
agent = Assistant(time_str=time_str, timezone=timezone_name)
await session.start(room=ctx.room, agent=agent)
await session.generate_reply()  # Triggers greeting with loaded instructions
```

## Troubleshooting

| Issue | Solution |
|-------|----------|
| Agent not responding after greeting | Ensure `generate_reply()` is called after `session.start()` |
| `'RealtimeModel' object has no attribute 'update_instructions'` | Expected - xAI plugin doesn't support this method |
| Calls not connecting | Verify **Agent name** matches `LIVEKIT_AGENT_NAME` in `.env` |
| Prompt changes not applied | Rebuild container: `docker compose up -d --build` |
| Agent not registered | Check `.env` has correct `LIVEKIT_URL`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET` |
| Control service not responding | Check port 9001 is available, restart with `docker compose restart control` |
| `./call` script fails | Ensure control service is running: `docker ps \| grep Control` |
| `./dial` fails with "SIP trunk not configured" | Set `LIVEKIT_SIP_TRUNK_ID` in `.env` |
| Phone doesn't ring | Check Twilio SIP trunk credentials, verify trunk is configured in LiveKit Cloud |
| Agent doesn't greet on phone call | Check agent logs: `docker logs Livekit-Henryk` - look for "SIP call answered" |

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `LIVEKIT_URL` | Yes | LiveKit Cloud WebSocket URL |
| `LIVEKIT_PUBLIC_URL` | Yes | Public URL for Meet (usually same as LIVEKIT_URL) |
| `LIVEKIT_API_KEY` | Yes | LiveKit API key |
| `LIVEKIT_API_SECRET` | Yes | LiveKit API secret |
| `LIVEKIT_MEET_URL` | No | Meet URL (default: https://meet.livekit.io) |
| `LIVEKIT_AGENT_NAME` | No | Agent name (default: test_agent) |
| `XAI_API_KEY` | Yes | xAI API key for Grok Voice |
| `AGENT_TIMEZONE` | No | IANA timezone (default: UTC) |
| `LIVEKIT_SIP_TRUNK_ID` | For calls | SIP trunk ID from LiveKit Cloud |
| `TWILIO_PHONE_NUMBER` | For calls | Your Twilio phone number (caller ID) |
| `EXA_API_KEY` | No | Exa API key for web search |
| `STORAGE_ACCESS_KEY` | For recording | Supabase S3 access key |
| `STORAGE_SECRET` | For recording | Supabase S3 secret |
| `STORAGE_BUCKET` | For recording | S3 bucket name (default: Recordings) |
| `STORAGE_ENDPOINT` | For recording | Supabase S3 endpoint URL |
| `STORAGE_REGION` | For recording | S3 region (default: eu-north-1) |
| `ASSEMBLYAI_API_KEY` | For transcription | AssemblyAI API key |
| `POST_CALL_WEBHOOK_URL` | For webhook | n8n webhook URL for transcripts |

## License

MIT
