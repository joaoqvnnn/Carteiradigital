import os

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
RENDER_EXTERNAL_URL = os.environ.get("RENDER_EXTERNAL_URL", "https://seu-app.onrender.com").rstrip("/")
PORT = int(os.environ.get("PORT", 8443))
DB_PATH = os.environ.get("DB_PATH", "onboarding.db")
KYC_BASE_URL = os.environ.get("KYC_BASE_URL", "https://sua-instituicao.com/kyc")

ADMIN_IDS = [
    int(x.strip()) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip().isdigit()
]
