# Copy to secrets.local.ps1 (gitignored), fill in, then dot-source before running:
#
#   . .\secrets.local.ps1
#   python -m edge_engine.scan scan
#
# Never put these in config.yaml - that file is tracked by git.

$env:TELEGRAM_BOT_TOKEN = "paste-your-botfather-token-here"
$env:TELEGRAM_CHAT_ID   = "paste-your-chat-id-here"

# Optional - free key at https://the-odds-api.com
# $env:ODDS_API_KEY = ""

# Optional - Supabase connection string, to use the cloud database locally
# $env:DATABASE_URL = ""
