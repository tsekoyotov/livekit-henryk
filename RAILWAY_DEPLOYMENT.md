# Deploying Control Service to Railway

This guide explains how to deploy the Livekit-Henryk control service to [Railway](https://railway.app) for a fully cloud-hosted solution.

## Architecture After Deployment

| Component | Location | Description |
|-----------|----------|-------------|
| Agent | LiveKit Cloud | xAI Grok voice agent (auto-scaled) |
| Control | Railway | Webhooks, transcription, room creation |

No local containers needed - everything runs in the cloud.

## Prerequisites

- [Railway account](https://railway.app) (free tier available)
- [Railway CLI](https://docs.railway.app/develop/cli) installed
- Control service working locally (test first!)

## Step 1: Install Railway CLI

```bash
# macOS
brew install railway

# Linux/WSL
curl -fsSL https://railway.app/install.sh | sh

# npm (any platform)
npm install -g @railway/cli
```

## Step 2: Login to Railway

```bash
railway login
```

This opens a browser for authentication.

## Step 3: Create Railway Project

```bash
cd /home/tseko/Docker/projects/Livekit-Henryk/control
railway init
```

When prompted:
- Select **"Empty Project"**
- Name it something like `livekit-henryk-control`

## Step 4: Configure Environment Variables

Add all required environment variables in Railway:

```bash
# LiveKit Cloud credentials
railway variables set LIVEKIT_URL=wss://your-project.livekit.cloud
railway variables set LIVEKIT_PUBLIC_URL=wss://your-project.livekit.cloud
railway variables set LIVEKIT_API_KEY=your_api_key
railway variables set LIVEKIT_API_SECRET=your_api_secret
railway variables set LIVEKIT_MEET_URL=https://meet.livekit.io
railway variables set LIVEKIT_AGENT_NAME=test_agent

# SIP/Phone (if using phone calls)
railway variables set LIVEKIT_SIP_TRUNK_ID=ST_xxxxxxxxxxxx
railway variables set TWILIO_PHONE_NUMBER=+1234567890

# Recording - Supabase Cloud Storage
railway variables set STORAGE_ACCESS_KEY=your_supabase_s3_access_key
railway variables set STORAGE_SECRET=your_supabase_s3_secret
railway variables set STORAGE_BUCKET=Recordings
railway variables set STORAGE_ENDPOINT=https://your-project.supabase.co/storage/v1/s3
railway variables set STORAGE_REGION=eu-north-1

# Transcription
railway variables set ASSEMBLYAI_API_KEY=your_assemblyai_key

# Post-call webhook
railway variables set POST_CALL_WEBHOOK_URL=https://your-n8n/webhook/your-id
```

Or set them all at once via the Railway dashboard.

## Step 5: Configure Dockerfile

Railway will auto-detect the Dockerfile. Verify it exposes the correct port.

The control service Dockerfile should have:
```dockerfile
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "9000"]
```

Railway automatically assigns a port via `$PORT` environment variable. Update the Dockerfile if needed:

```dockerfile
CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT:-9000}"]
```

## Step 6: Deploy

```bash
railway up
```

This will:
1. Build the Docker image
2. Deploy to Railway
3. Provide a public URL

## Step 7: Get Your Public URL

```bash
railway domain
```

Or check the Railway dashboard. Your URL will look like:
```
https://livekit-henryk-control-production.up.railway.app
```

## Step 8: Configure LiveKit Webhook

Update the webhook URL in LiveKit Cloud Console:

1. Go to [cloud.livekit.io](https://cloud.livekit.io)
2. Navigate to: Your Project → Settings → Webhooks
3. Update webhook URL to: `https://your-railway-url.up.railway.app/livekit-henryk/webhook`
4. Events: `egress_ended`
5. Save

## Step 9: Update Scripts (Optional)

If you want to use the `./call` and `./testcall` scripts with Railway, update the endpoint:

```bash
# In the scripts, change:
# http://localhost:9001/test-call
# to:
# https://your-railway-url.up.railway.app/test-call
```

Or create environment variable:
```bash
export CONTROL_URL=https://your-railway-url.up.railway.app
```

## Verification

### Test the health endpoint:
```bash
curl https://your-railway-url.up.railway.app/health
```

### Test room creation:
```bash
curl -X POST https://your-railway-url.up.railway.app/test-call
```

### Test phone dialing:
```bash
curl -X POST https://your-railway-url.up.railway.app/dial-lead \
  -H "Content-Type: application/json" \
  -d '{
    "phone_number": "+1234567890",
    "first_name": "Test",
    "initial_greeting": true
  }'
```

## Railway Dashboard Features

- **Logs**: View real-time logs in the dashboard
- **Metrics**: Monitor CPU, memory, and network usage
- **Deployments**: Roll back to previous versions if needed
- **Custom Domains**: Add your own domain (e.g., `control.yourdomain.com`)

## Cost Estimate

Railway pricing (as of 2024):
- **Starter**: $5/month includes $5 credit
- **Usage**: ~$0.000231/minute for compute
- **Typical control service**: ~$3-10/month depending on usage

## Troubleshooting

| Issue | Solution |
|-------|----------|
| Build fails | Check Dockerfile, ensure all dependencies are specified |
| Port issues | Use `${PORT:-9000}` in CMD to respect Railway's port |
| Webhook not received | Verify URL in LiveKit Cloud Console, check Railway logs |
| Environment variables missing | Use `railway variables` to list and verify all vars |
| Connection refused | Ensure the service is running: `railway status` |

## Updating the Service

After making code changes:

```bash
cd /home/tseko/Docker/projects/Livekit-Henryk/control
railway up
```

Railway will build and deploy the new version with zero-downtime rolling deployment.

## Removing the Deployment

```bash
railway down
```

Or delete the project from the Railway dashboard.
