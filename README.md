# Gemini Voice Agent with LiveKit & Telnyx

A voice AI agent powered by Google Gemini 2.5 Flash Native Audio, built with [LiveKit Agents](https://github.com/livekit/agents) for real-time voice conversations. Supports both local testing and telephony via Telnyx SIP trunking.

## Features

- **Native Audio**: Uses Gemini 2.5 Flash Native Audio for natural, audio-to-audio conversations
- **Telephony Ready**: Configured for inbound calls via Telnyx SIP trunk
- **Call Management**: Built-in `hang_up` tool for graceful call termination
- **Noise Cancellation**: Automatic background noise suppression
- **Fast Responses**: Thinking mode disabled for snappier replies

## Prerequisites

- Python 3.9+
- [uv](https://github.com/astral-sh/uv) package manager
- [LiveKit Cloud](https://cloud.livekit.io/) account
- [Google AI API key](https://aistudio.google.com/apikey)
- (Optional) [Telnyx](https://telnyx.com/) account with a phone number for telephony

## Quick Start

### 1. Clone the repository

```bash
git clone https://github.com/YOUR_USERNAME/lk-google-telnyx-1.git
cd lk-google-telnyx-1
```

### 2. Install dependencies

```bash
uv sync
```

### 3. Configure environment

Copy `.env.example` to `.env.local` (or create `.env.local`) with your API keys:

```bash
LIVEKIT_URL=wss://your-project.livekit.cloud
LIVEKIT_API_KEY=your_api_key
LIVEKIT_API_SECRET=your_api_secret
GOOGLE_API_KEY=your_google_api_key
```

You can also load LiveKit credentials automatically:

```bash
lk cloud auth
lk app env -w -d .env.local
```

### 4. Download required models

```bash
uv run src/agent.py download-files
```

### 5. Run locally

**Console mode** (talk directly in terminal):
```bash
uv run src/agent.py console
```

**Dev mode** (connect to LiveKit Cloud for frontend/telephony):
```bash
uv run src/agent.py dev
```

## Telephony Setup (Telnyx)

To receive inbound calls on a phone number:

### 1. Set up Telnyx

1. Purchase a phone number in [Telnyx Portal](https://portal.telnyx.com/)
2. Create an FQDN connection under Voice > SIP Trunking
3. Add your LiveKit SIP endpoint as the FQDN (e.g., `abc123.sip.livekit.cloud`)
4. Set Inbound number format to `+E.164`
5. Assign the phone number to your SIP connection

### 2. Configure LiveKit SIP

Create an inbound trunk (using LiveKit CLI):

```bash
lk sip trunk create inbound \
  --name "telnyx_trunk" \
  --numbers "+1XXXXXXXXXX"
```

Create a dispatch rule to route calls to this agent:

```bash
lk sip dispatch create \
  --name "telnyx_dispatch" \
  --trunks <TRUNK_ID> \
  --individual "call-" \
  --room-preset test_agent
```

### 3. Update agent name

Ensure `agent_name` in `src/agent.py` matches your dispatch rule's room preset:

```python
agents.WorkerOptions(
    entrypoint_fnc=entrypoint,
    prewarm_fnc=prewarm,
    agent_name="test_agent",  # Must match dispatch rule!
)
```

## Deployment to LiveKit Cloud

### 1. Create agent in cloud

```bash
lk agent create
```

### 2. Deploy

```bash
lk agent deploy
```

### 3. View logs

```bash
lk agent logs
```

## Project Structure

```
.
├── src/
│   └── agent.py          # Main agent code
├── pyproject.toml        # Dependencies
├── Dockerfile            # For cloud deployment
├── .env.local            # API keys (not committed)
└── README.md
```

## Customization

### Change the voice

Available voices for Gemini Native Audio: `Puck`, `Charon`, `Kore`, `Fenrir`, `Aoede`, `Leda`, `Orus`, `Zephyr`

```python
model = google.realtime.RealtimeModel(
    voice="Zephyr",  # Change this
    ...
)
```

### Modify the personality

Edit the `instructions` parameter in `src/agent.py`:

```python
instructions="""Your custom instructions here..."""
```

### Enable thinking mode

For more thoughtful (but slower) responses, remove or change `thinking_config`:

```python
thinking_config=types.ThinkingConfig(
    include_thoughts=True,  # Enable thinking
),
```

## Troubleshooting

| Issue | Solution |
|-------|----------|
| `tool_choice is not supported` warning | Safe to ignore - Gemini API doesn't use this parameter |
| `generate_reply timed out` | Network/API issue - check connectivity and API quotas |
| Calls not connecting | Verify dispatch rule matches `agent_name` in code |
| Agent not picking up | Ensure agent is running with `dev` or deployed to cloud |

## License

MIT
