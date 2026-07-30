from dotenv import load_dotenv
from pathlib import Path
import httpx
ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / '.env')
import os
import io
import csv
import uuid
import secrets
import logging
import bcrypt
import jwt
from datetime import datetime, timezone, timedelta
from dateutil import parser as dateutil_parser
from typing import List, Optional, Literal
from fastapi import FastAPI, APIRouter, HTTPException, Depends, Request, Response, UploadFile, File, Form, Query
from fastapi.responses import StreamingResponse
from starlette.middleware.cors import CORSMiddleware
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from motor.motor_asyncio import AsyncIOMotorClient
from pydantic import BaseModel, Field, EmailStr
from google.oauth2 import id_token as google_id_token
from google.auth.transport import requests as google_requests

# LLM providers — supports both Emergent LLM Key and direct Anthropic API key
try:
    from emergentintegrations.llm.chat import LlmChat, UserMessage
    _HAS_EMERGENT = True
except Exception:
    _HAS_EMERGENT = False

try:
    from anthropic import AsyncAnthropic
    _HAS_ANTHROPIC = True
except Exception:
    _HAS_ANTHROPIC = False

try:
    import pdfplumber
    _HAS_PDFPLUMBER = True
except Exception:
    _HAS_PDFPLUMBER = False

try:
    from groq import AsyncGroq
    _HAS_GROQ = True
except Exception:
    _HAS_GROQ = False

# ----- Config -----
JWT_ALGORITHM = "HS256"
JWT_SECRET = os.environ["JWT_SECRET"]
GOOGLE_CLIENT_ID = os.environ["GOOGLE_CLIENT_ID"]
CRON_SECRET = os.environ.get("CRON_SECRET", "")
COOKIE_DOMAIN = os.environ.get("COOKIE_DOMAIN", "").strip() or None
EMERGENT_LLM_KEY = os.environ.get("EMERGENT_LLM_KEY", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
LLM_MODEL = os.environ.get("LLM_MODEL", "claude-sonnet-4-5-20250929")
GROQ_MODEL = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")

# ----- Premium / Monetization Config -----
TRIAL_DAYS = 90
TRIAL_REMINDER_DAYS_BEFORE = 7
MAX_ADS_PER_DAY = 3
FREE_AI_CHAT_DAILY_LIMIT = 5
FREE_AI_INSIGHTS_DAILY_LIMIT = 1
FREE_PDF_EXPORT_DAILY_LIMIT = 2

PREMIUM_PLANS = {
    "monthly": {"id": "monthly", "label": "Monthly", "price": 99, "duration_days": 30},
    "half_yearly": {"id": "half_yearly", "label": "6 Months", "price": 499, "duration_days": 182},
    "yearly": {"id": "yearly", "label": "1 Year", "price": 999, "duration_days": 365},
    "two_yearly": {"id": "two_yearly", "label": "2 Years", "price": 1900, "duration_days": 730},
    "lifetime": {"id": "lifetime", "label": "Lifetime", "price": 2999, "duration_days": None},
}
PREMIUM_FEATURE_LIST = [
    "Unlimited AI Assistant", "Unlimited PDF Export", "Advanced Reports",
    "Business Analytics", "Unlimited Backup & Restore", "Excel Export",
    "Cloud Sync", "Advanced Search", "Ad-Free Experience", "Priority Support",
]

mongo_url = os.environ["MONGO_URL"].strip().strip('"').strip("'")
if not (mongo_url.startswith("mongodb://") or mongo_url.startswith("mongodb+srv://")):
    raise RuntimeError(
        f"MONGO_URL must start with 'mongodb://' or 'mongodb+srv://'. "
        f"Got: {mongo_url[:30]!r}... "
        f"Check your environment variable — for MongoDB Atlas it should look like "
        f"'mongodb+srv://<user>:<password>@<cluster>.mongodb.net/?retryWrites=true&w=majority'"
    )
client = AsyncIOMotorClient(mongo_url)
db = client[os.environ["DB_NAME"]]

app = FastAPI(title="Apka Munim API")

limiter = Limiter(key_func=get_remote_address)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)


async def llm_json_call(system_msg: str, user_msg: str, session_id: str) -> Optional[str]:
    """
    Portable LLM call. Priority: Anthropic → Groq (free) → Emergent.
    Returns raw text or None if no provider is configured.
    """
    if ANTHROPIC_API_KEY and _HAS_ANTHROPIC:
        try:
            ac = AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
            resp = await ac.messages.create(
                model=LLM_MODEL,
                max_tokens=1024,
                system=system_msg,
                messages=[{"role": "user", "content": user_msg}],
            )
            return resp.content[0].text
        except Exception as e:
            logging.warning("Anthropic direct call failed: %s", e)

    if GROQ_API_KEY and _HAS_GROQ:
        try:
            gc = AsyncGroq(api_key=GROQ_API_KEY)
            resp = await gc.chat.completions.create(
                model=GROQ_MODEL,
                max_tokens=1024,
                messages=[
                    {"role": "system", "content": system_msg},
                    {"role": "user", "content": user_msg},
                ],
            )
            return resp.choices[0].message.content
        except Exception as e:
            logging.warning("Groq call failed: %s", e)

    if EMERGENT_LLM_KEY and _HAS_EMERGENT:
        try:
            chat = LlmChat(
                api_key=EMERGENT_LLM_KEY,
                session_id=session_id,
                system_message=system_msg,
            ).with_model("anthropic", LLM_MODEL)
            return await chat.send_message(UserMessage(text=user_msg))
        except Exception as e:
            logging.warning("Emergent LLM call failed: %s", e)

    return None
api = APIRouter(prefix="/api")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("paisabook")


# ----- Auth Helpers -----
def hash_password(pw: str) -> str:
    return bcrypt.hashpw(pw.encode(), bcrypt.gensalt()).decode()


def verify_password(pw: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(pw.encode(), hashed.encode())
    except Exception:
        return False


def create_access_token(user_id: str, email: str) -> str:
    payload = {
        "sub": user_id,
        "email": email,
        "exp": datetime.now(timezone.utc) + timedelta(days=7),
        "type": "access",
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def gen_invite_code() -> str:
    # Human-friendly 6-char code
    alphabet = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(6))


async def ensure_personal_ledger(user_doc: dict) -> str:
    """Ensure user has a personal ledger + backfill legacy docs. Returns ledger id."""
    uid = user_doc["id"]
    personal_id = user_doc.get("personal_ledger_id")
    if not personal_id:
        personal_id = f"pl_{uid}"
        await db.ledgers.update_one(
            {"id": personal_id},
            {
                "$setOnInsert": {
                    "id": personal_id,
                    "name": "Personal",
                    "type": "personal",
                    "owner_user_id": uid,
                    "members": [uid],
                    "invite_code": None,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                }
            },
            upsert=True,
        )
        await db.users.update_one(
            {"id": uid},
            {"$set": {
                "personal_ledger_id": personal_id,
                "current_ledger_id": user_doc.get("current_ledger_id") or personal_id,
            }},
        )
        # backfill: legacy docs with no owner_id get user's personal_ledger_id
        for coll in ("accounts", "transactions", "udhaar", "recurring", "budgets"):
            await db[coll].update_many(
                {"user_id": uid, "owner_id": {"$exists": False}},
                {"$set": {"owner_id": personal_id}},
            )
    return personal_id


async def get_current_user(request: Request) -> dict:
    token = request.cookies.get("access_token")
    if not token:
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            token = auth[7:]
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")

    user = await db.users.find_one({"id": payload["sub"]}, {"password_hash": 0, "_id": 0})
    if not user:
        raise HTTPException(status_code=401, detail="User not found")
    await ensure_personal_ledger(user)
    # reload after backfill to pick up new fields
    user = await db.users.find_one({"id": payload["sub"]}, {"password_hash": 0, "_id": 0})

    # verify user is still a member of their current_ledger_id; else fall back to personal
    cur_id = user.get("current_ledger_id") or user["personal_ledger_id"]
    lg = await db.ledgers.find_one({"id": cur_id, "members": user["id"]}, {"_id": 0})
    if not lg:
        cur_id = user["personal_ledger_id"]
        await db.users.update_one({"id": user["id"]}, {"$set": {"current_ledger_id": cur_id}})
        lg = await db.ledgers.find_one({"id": cur_id}, {"_id": 0})
    user["current_ledger_id"] = cur_id
    user["current_ledger"] = lg
    return user


def scope(user: dict) -> dict:
    """MongoDB filter to scope by user's active ledger."""
    return {"owner_id": user["current_ledger_id"]}


# ----- Premium / Monetization helpers -----
def _parse_dt(value) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        dt = dateutil_parser.isoparse(value)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


async def _sync_premium_status(user: dict) -> dict:
    """
    Recomputes the user's trial/subscription state against 'now' and persists
    any transition (trial -> free, subscription -> free) or backfill for
    accounts created before this system existed. Cheap and idempotent — safe
    to call on every request; only writes to the DB when something changed.
    """
    now = datetime.now(timezone.utc)
    updates = {}

    if not user.get("trialStart"):
        created = _parse_dt(user.get("created_at")) or now
        trial_end = created + timedelta(days=TRIAL_DAYS)
        updates.update({
            "registrationDate": user.get("registrationDate") or created.isoformat(),
            "trialStart": created.isoformat(),
            "trialEnd": trial_end.isoformat(),
            "subscriptionStatus": user.get("subscriptionStatus") or "trial",
            "subscriptionPlan": user.get("subscriptionPlan"),
            "subscriptionStart": user.get("subscriptionStart"),
            "subscriptionEnd": user.get("subscriptionEnd"),
            "premiumActive": True,
            "adsShownToday": user.get("adsShownToday", 0),
            "lastAdResetDate": user.get("lastAdResetDate") or now.date().isoformat(),
        })
        user = {**user, **updates}

    status = user.get("subscriptionStatus", "trial")
    plan = user.get("subscriptionPlan")
    sub_end = _parse_dt(user.get("subscriptionEnd"))
    trial_end = _parse_dt(user.get("trialEnd"))

    if status == "premium" and plan == "lifetime":
        premium_active = True
    elif status == "premium":
        if sub_end and now > sub_end:
            status, premium_active = "free", False
            updates["subscriptionStatus"] = "free"
            updates["premiumActive"] = False
        else:
            premium_active = True
    elif status == "trial":
        if trial_end and now > trial_end:
            status, premium_active = "free", False
            updates["subscriptionStatus"] = "free"
            updates["premiumActive"] = False
        else:
            premium_active = True
    else:
        status, premium_active = "free", False

    if updates:
        await db.users.update_one({"id": user["id"]}, {"$set": {**updates, "updatedAt": now.isoformat()}})

    trial_days_left = max(0, (trial_end.date() - now.date()).days) if trial_end else 0

    return {
        "status": status,  # trial | premium | free
        "premium_active": premium_active,
        "plan": plan,
        "registration_date": user.get("registrationDate"),
        "trial_start": user.get("trialStart"),
        "trial_end": user.get("trialEnd"),
        "trial_days_left": trial_days_left if status == "trial" else 0,
        "subscription_start": user.get("subscriptionStart"),
        "subscription_end": user.get("subscriptionEnd"),
        "show_trial_reminder": status == "trial" and trial_days_left <= TRIAL_REMINDER_DAYS_BEFORE,
        "max_ads_per_day": MAX_ADS_PER_DAY,
    }


async def require_premium(user=Depends(get_current_user)) -> dict:
    """Dependency for endpoints that are entirely Premium-only (e.g. cloud backup)."""
    pstatus = await _sync_premium_status(user)
    if not pstatus["premium_active"]:
        raise HTTPException(status_code=402, detail={
            "code": "PREMIUM_REQUIRED",
            "message": "This feature is available only for Premium Members.",
        })
    return user


async def _check_daily_free_limit(user: dict, field: str, limit: int) -> bool:
    """
    True if a free-tier user is still under their daily limit for `field`
    (and increments the counter). Resets automatically on a new UTC day.
    Only call this after confirming the user is NOT premium_active.
    """
    today = datetime.now(timezone.utc).date().isoformat()
    reset_field = f"{field}ResetDate"
    count = user.get(field, 0) if user.get(reset_field) == today else 0
    if count >= limit:
        return False
    await db.users.update_one(
        {"id": user["id"]},
        {"$set": {field: count + 1, reset_field: today}},
    )
    return True


# ----- Models -----
class RegisterIn(BaseModel):
    name: str
    email: EmailStr
    password: str
    currency: str = "INR"
    referral_code: Optional[str] = None


class LoginIn(BaseModel):
    email: EmailStr
    password: str


class PinSetIn(BaseModel):
    pin: str  # 4-6 digits
    password: str  # current password to authorize


class PinVerifyIn(BaseModel):
    email: EmailStr
    pin: str


class ForgotPasswordIn(BaseModel):
    email: EmailStr


class GoogleAuthIn(BaseModel):
    credential: str  # JWT id_token from Google
    currency: str = "INR"


class ResetPasswordIn(BaseModel):
    token: str
    new_password: str


def validate_password_strength(password: str) -> tuple[bool, str]:
    """Returns (is_valid, error_message)"""
    import re
    if len(password) < 8:
        return False, "Password kam se kam 8 characters ka hona chahiye"
    if not re.search(r"[A-Z]", password):
        return False, "Password me ek uppercase letter (A-Z) hona chahiye"
    if not re.search(r"[a-z]", password):
        return False, "Password me ek lowercase letter (a-z) hona chahiye"
    if not re.search(r"[0-9]", password):
        return False, "Password me ek number (0-9) hona chahiye"
    if not re.search(r"[!@#$%^&*()_+\-=\[\]{};':\"\\|,.<>/?~`]", password):
        return False, "Password me ek special character (!@#$%^&* etc.) hona chahiye"
    return True, ""


def validate_pin(pin: str) -> tuple[bool, str]:
    if not pin.isdigit():
        return False, "PIN sirf numbers ka hona chahiye"
    if len(pin) < 4 or len(pin) > 6:
        return False, "PIN 4 se 6 digits ka hona chahiye"
    return True, ""


class AccountIn(BaseModel):
    name: str
    type: Literal["savings", "current", "cash", "wallet", "credit_card", "emergency", "investment", "other"] = "savings"
    opening_balance: float = 0.0
    currency: str = "INR"
    color: str = "#2A4F4F"


class TransactionIn(BaseModel):
    account_id: str
    type: Literal["income", "expense"]
    amount: float = Field(gt=0)
    category: str
    note: Optional[str] = ""
    date: Optional[str] = None


class TransactionUpdate(BaseModel):
    account_id: Optional[str] = None
    type: Optional[Literal["income", "expense"]] = None
    amount: Optional[float] = Field(default=None, gt=0)
    category: Optional[str] = None
    note: Optional[str] = None
    date: Optional[str] = None


class UdhaarIn(BaseModel):
    person_name: str
    phone: Optional[str] = ""
    type: Literal["lene", "dene"]
    amount: float = Field(gt=0)
    note: Optional[str] = ""
    due_date: Optional[str] = None


class UdhaarUpdate(BaseModel):
    person_name: Optional[str] = None
    phone: Optional[str] = None
    amount: Optional[float] = Field(default=None, gt=0)
    note: Optional[str] = None
    due_date: Optional[str] = None
    status: Optional[Literal["pending", "settled"]] = None


class RecurringIn(BaseModel):
    account_id: str
    type: Literal["income", "expense"]
    amount: float = Field(gt=0)
    category: str
    note: Optional[str] = ""
    frequency: Literal["daily", "weekly", "monthly"] = "monthly"
    day_of_month: Optional[int] = None
    start_date: Optional[str] = None
    active: bool = True


class BudgetIn(BaseModel):
    category: str
    amount: float = Field(gt=0)


class InvestmentIn(BaseModel):
    name: str
    type: Literal["mutual_fund", "stock", "sip", "fd", "rd", "other"] = "mutual_fund"
    invested_amount: float = Field(gt=0)
    current_value: float = Field(ge=0)
    units: Optional[float] = None
    purchase_date: Optional[str] = None
    maturity_date: Optional[str] = None
    notes: Optional[str] = ""


class InvestmentUpdate(BaseModel):
    name: Optional[str] = None
    type: Optional[Literal["mutual_fund", "stock", "sip", "fd", "rd", "other"]] = None
    invested_amount: Optional[float] = Field(default=None, gt=0)
    current_value: Optional[float] = Field(default=None, ge=0)
    units: Optional[float] = None
    purchase_date: Optional[str] = None
    maturity_date: Optional[str] = None
    notes: Optional[str] = None


class SplitParticipant(BaseModel):
    name: str
    share_amount: float = Field(gt=0)
    settled: bool = False


class SplitIn(BaseModel):
    title: str
    total_amount: float = Field(gt=0)
    paid_by: str = "You"
    participants: List[SplitParticipant]
    date: Optional[str] = None
    notes: Optional[str] = ""


class ParsedStatementRow(BaseModel):
    date: str
    type: Literal["income", "expense"]
    amount: float = Field(gt=0)
    category: str = "Other"
    note: str = ""


class ConfirmImportIn(BaseModel):
    account_id: str
    transactions: List[ParsedStatementRow]


class TaxEstimateIn(BaseModel):
    annual_income: float
    regime: Literal["old", "new"] = "new"
    section_80c: float = 0.0
    section_80d: float = 0.0
    age_below_60: bool = True


class LedgerCreate(BaseModel):
    name: str


class LedgerJoin(BaseModel):
    invite_code: str


# ----- Auth Endpoints -----
@api.post("/auth/register")
@limiter.limit("5/hour")
async def register(request: Request, body: RegisterIn, response: Response):
    email = body.email.lower()
    if await db.users.find_one({"email": email}):
        raise HTTPException(status_code=400, detail="Email already registered")

    # Enforce strong password
    ok, err = validate_password_strength(body.password)
    if not ok:
        raise HTTPException(status_code=400, detail=err)

    uid = str(uuid.uuid4())
    personal_id = f"pl_{uid}"
    now = datetime.now(timezone.utc).isoformat()

    referred_by = None
    if body.referral_code:
        referrer = await db.users.find_one({"referral_code": body.referral_code.upper()})
        if referrer:
            referred_by = referrer["referral_code"]
            await db.users.update_one({"id": referrer["id"]}, {"$inc": {"referral_credits": 50}})

    trial_end_iso = (datetime.now(timezone.utc) + timedelta(days=TRIAL_DAYS)).isoformat()
    await db.users.insert_one({
        "id": uid,
        "email": email,
        "name": body.name,
        "password_hash": hash_password(body.password),
        "currency": body.currency,
        "personal_ledger_id": personal_id,
        "current_ledger_id": personal_id,
        "referred_by": referred_by,
        "created_at": now,
        # ----- Monetization: 90-day Premium trial starts at registration -----
        "registrationDate": now,
        "trialStart": now,
        "trialEnd": trial_end_iso,
        "subscriptionStatus": "trial",
        "subscriptionPlan": None,
        "subscriptionStart": None,
        "subscriptionEnd": None,
        "premiumActive": True,
        "adsShownToday": 0,
        "lastAdResetDate": datetime.now(timezone.utc).date().isoformat(),
        "updatedAt": now,
    })
    await db.ledgers.insert_one({
        "id": personal_id,
        "name": "Personal",
        "type": "personal",
        "owner_user_id": uid,
        "members": [uid],
        "invite_code": None,
        "created_at": now,
    })
    token = create_access_token(uid, email)
    response.set_cookie("access_token", token, httponly=True, secure=True, samesite="none",
                        domain=COOKIE_DOMAIN, max_age=60 * 60 * 24 * 7, path="/")
    return {"id": uid, "email": email, "name": body.name, "currency": body.currency, "token": token}


# ----- Login activity helper -----
async def log_login_activity(request: Request, user_id: str, method: str):
    await db.login_activity.insert_one({
        "id": str(uuid.uuid4()),
        "user_id": user_id,
        "method": method,
        "ip": request.client.host if request.client else "unknown",
        "user_agent": request.headers.get("user-agent", "unknown"),
        "created_at": datetime.now(timezone.utc).isoformat(),
    })


@api.post("/auth/login")
@limiter.limit("10/15minutes")
async def login(request: Request, body: LoginIn, response: Response):
    email = body.email.lower()
    user = await db.users.find_one({"email": email})
    if not user or not verify_password(body.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Invalid email or password")

    # OTP wala lafda hata diya gaya hai taaki direct login ho
    token = create_access_token(user["id"], email)
    response.set_cookie("access_token", token, httponly=True, secure=True, samesite="none",
                        domain=COOKIE_DOMAIN, max_age=60 * 60 * 24 * 7, path="/")
    await log_login_activity(request, user["id"], "password")
    return {"id": user["id"], "email": email, "name": user["name"],
            "currency": user.get("currency", "INR"), "token": token}

@api.post("/auth/google")
@limiter.limit("15/hour")
async def google_auth(request: Request, body: GoogleAuthIn, response: Response):
    try:
        idinfo = google_id_token.verify_oauth2_token(
            body.credential, google_requests.Request(), GOOGLE_CLIENT_ID
        )
    except ValueError:
        raise HTTPException(status_code=401, detail="Invalid Google token")

    email = idinfo["email"].lower()
    name = idinfo.get("name", email.split("@")[0])

    user = await db.users.find_one({"email": email})
    if not user:
        uid = str(uuid.uuid4())
        personal_id = f"pl_{uid}"
        now = datetime.now(timezone.utc).isoformat()
        trial_end_iso = (datetime.now(timezone.utc) + timedelta(days=TRIAL_DAYS)).isoformat()
        await db.users.insert_one({
            "id": uid,
            "email": email,
            "name": name,
            "password_hash": None,
            "currency": "INR",
            "personal_ledger_id": personal_id,
            "current_ledger_id": personal_id,
            "created_at": now,
            # ----- Monetization: 90-day Premium trial starts at registration -----
            "registrationDate": now,
            "trialStart": now,
            "trialEnd": trial_end_iso,
            "subscriptionStatus": "trial",
            "subscriptionPlan": None,
            "subscriptionStart": None,
            "subscriptionEnd": None,
            "premiumActive": True,
            "adsShownToday": 0,
            "lastAdResetDate": datetime.now(timezone.utc).date().isoformat(),
            "updatedAt": now,
        })
        await db.ledgers.insert_one({
            "id": personal_id,
            "name": "Personal",
            "type": "personal",
            "owner_user_id": uid,
            "members": [uid],
            "invite_code": None,
            "created_at": now,
        })
        uid_final, name_final = uid, name
    else:
        uid_final, name_final = user["id"], user["name"]

    token = create_access_token(uid_final, email)
    response.set_cookie("access_token", token, httponly=True, secure=True, samesite="none",
                        domain=COOKIE_DOMAIN, max_age=60 * 60 * 24 * 7, path="/")
    await log_login_activity(request, uid_final, "google")
    return {"id": uid_final, "email": email, "name": name_final, "token": token}


@api.get("/auth/login-activity")
async def get_login_activity(user=Depends(get_current_user)):
    """Recent login history for the current account — Gmail-style 'where you're logged in'."""
    rows = await db.login_activity.find(
        {"user_id": user["id"]}, {"_id": 0}
    ).sort("created_at", -1).to_list(20)
    return {"activity": rows}


# ----- 2FA (email OTP) -----
class TwoFAVerifyIn(BaseModel):
    email: str
    code: str


@api.post("/auth/2fa/enable")
async def enable_2fa(user=Depends(get_current_user)):
    await db.users.update_one({"id": user["id"]}, {"$set": {"two_factor_enabled": True}})
    return {"ok": True, "two_factor_enabled": True}


@api.post("/auth/2fa/disable")
async def disable_2fa(user=Depends(get_current_user)):
    await db.users.update_one({"id": user["id"]}, {"$set": {"two_factor_enabled": False}})
    return {"ok": True, "two_factor_enabled": False}


@api.post("/auth/2fa/send-code")
@limiter.limit("5/15minutes")
async def send_2fa_code(request: Request, body: TwoFAVerifyIn):
    email = body.email.lower()
    user = await db.users.find_one({"email": email})
    if not user:
        return {"ok": True}  # don't reveal existence
    code = f"{secrets.randbelow(900000) + 100000}"
    await db.otp_codes.insert_one({
        "email": email,
        "code": code,
        "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(),
        "used": False,
        "created_at": datetime.now(timezone.utc).isoformat(),
    })
    _send_simple_email(email, "🔐 Apka Munim — Login Code", "Your login code",
                        f"Aapka one-time login code hai:\n\n{code}\n\nYe 10 minute tak valid hai. Kisi ke saath share mat karo.")
    return {"ok": True}


@api.post("/auth/2fa/verify")
@limiter.limit("10/15minutes")
async def verify_2fa_code(request: Request, body: TwoFAVerifyIn, response: Response):
    email = body.email.lower()
    row = await db.otp_codes.find_one(
        {"email": email, "code": body.code, "used": False}, sort=[("created_at", -1)]
    )
    if not row:
        raise HTTPException(status_code=401, detail="Invalid code")
    if datetime.fromisoformat(row["expires_at"]) < datetime.now(timezone.utc):
        raise HTTPException(status_code=401, detail="Code expired")
    await db.otp_codes.update_one({"_id": row["_id"]}, {"$set": {"used": True}})

    user = await db.users.find_one({"email": email})
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    token = create_access_token(user["id"], email)
    response.set_cookie("access_token", token, httponly=True, secure=True, samesite="none",
                        domain=COOKIE_DOMAIN, max_age=60 * 60 * 24 * 7, path="/")
    await log_login_activity(request, user["id"], "2fa")
    return {"id": user["id"], "email": email, "name": user["name"],
            "currency": user.get("currency", "INR"), "token": token}


@api.post("/auth/logout")
async def logout(response: Response):
    response.delete_cookie("access_token", path="/", domain=COOKIE_DOMAIN)
    return {"ok": True}


# ----- PIN Authentication -----
@api.post("/auth/pin/set")
async def set_pin(body: PinSetIn, user=Depends(get_current_user)):
    """Set or update 4-6 digit PIN. Requires current password to authorize."""
    ok, err = validate_pin(body.pin)
    if not ok:
        raise HTTPException(status_code=400, detail=err)
    # Verify password
    full = await db.users.find_one({"id": user["id"]})
    if not full or not verify_password(body.password, full["password_hash"]):
        raise HTTPException(status_code=401, detail="Password galat hai")
    pin_hash = hash_password(body.pin)
    await db.users.update_one({"id": user["id"]}, {"$set": {"pin_hash": pin_hash}})
    return {"ok": True, "message": "PIN set ho gaya!"}


@api.delete("/auth/pin")
async def delete_pin(user=Depends(get_current_user)):
    await db.users.update_one({"id": user["id"]}, {"$unset": {"pin_hash": ""}})
    return {"ok": True}


@api.get("/auth/pin/status")
async def pin_status(user=Depends(get_current_user)):
    full = await db.users.find_one({"id": user["id"]}, {"pin_hash": 1})
    return {"enabled": bool(full and full.get("pin_hash"))}


@api.post("/auth/pin/verify")
async def verify_pin(body: PinVerifyIn, response: Response):
    """Login via email + PIN combination."""
    email = body.email.lower()
    user = await db.users.find_one({"email": email})
    if not user or not user.get("pin_hash"):
        raise HTTPException(status_code=401, detail="Invalid credentials or PIN not set")
    # Rate limit: track failed PIN attempts
    now_ts = datetime.now(timezone.utc)
    attempts_doc = await db.pin_attempts.find_one({"email": email})
    if attempts_doc and attempts_doc.get("locked_until"):
        locked_until = datetime.fromisoformat(attempts_doc["locked_until"])
        if now_ts < locked_until:
            wait_min = int((locked_until - now_ts).total_seconds() / 60) + 1
            raise HTTPException(status_code=429, detail=f"Bahut galat PIN — {wait_min} min me try karo")

    if not verify_password(body.pin, user["pin_hash"]):
        # Increment failed attempts
        fails = (attempts_doc.get("count", 0) if attempts_doc else 0) + 1
        update = {"count": fails, "email": email, "last_at": now_ts.isoformat()}
        if fails >= 5:
            update["locked_until"] = (now_ts + timedelta(minutes=15)).isoformat()
            update["count"] = 0
        await db.pin_attempts.update_one({"email": email}, {"$set": update}, upsert=True)
        raise HTTPException(status_code=401, detail=f"Galat PIN ({fails}/5)")

    # Success — clear attempts
    await db.pin_attempts.delete_one({"email": email})
    token = create_access_token(user["id"], email)
    response.set_cookie("access_token", token, httponly=True, secure=True, samesite="none",
                        max_age=60 * 60 * 24 * 7, path="/")
    return {"id": user["id"], "email": email, "name": user["name"],
            "currency": user.get("currency", "INR"), "token": token}


# ----- Forgot / Reset Password -----
def _send_reset_email(to_email: str, name: str, reset_link: str):
    """Send password reset email via Resend if configured. Fallback: log to console."""
    api_key = os.environ.get("RESEND_API_KEY", "").strip()
    sender = os.environ.get("SENDER_EMAIL", "onboarding@resend.dev").strip()

    if not api_key or api_key == "your_resend_key_here":
        logger.warning("Resend not configured — reset link (dev mode): %s -> %s", to_email, reset_link)
        return {"dev_link": reset_link}

    try:
        import resend as resend_lib
        resend_lib.api_key = api_key
        html = f"""
        <div style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Arial, sans-serif; max-width: 480px; margin: 0 auto; padding: 32px 24px; background: #F5F2ED; color: #1C1917;">
          <div style="text-align: center; margin-bottom: 24px;">
            <div style="display: inline-block; padding: 12px 20px; background: #2A4F4F; color: #E8B365; border-radius: 12px; font-size: 22px; font-weight: 800;">
              Apka Munim 🎩
            </div>
          </div>
          <div style="background: white; padding: 32px 24px; border-radius: 16px; box-shadow: 0 4px 12px rgba(0,0,0,0.05);">
            <h1 style="margin: 0 0 12px; font-size: 24px; color: #1C1917;">Namaste {name or 'friend'}! 👋</h1>
            <p style="font-size: 15px; line-height: 1.6; color: #57534E;">
              Aapne apne <strong>Apka Munim</strong> account ka password reset karne ke liye request bheji hai.
            </p>
            <p style="font-size: 15px; line-height: 1.6; color: #57534E;">
              Neeche wale button pe click karke naya password set karo. Yeh link <strong>60 minute</strong> tak valid hai.
            </p>
            <div style="text-align: center; margin: 32px 0;">
              <a href="{reset_link}" style="background: #2A4F4F; color: white; padding: 14px 32px; text-decoration: none; border-radius: 999px; font-weight: 600; display: inline-block; font-size: 15px;">
                Password Reset Karo
              </a>
            </div>
            <p style="font-size: 13px; color: #78716C; margin-top: 24px;">
              Ya yeh link copy karke browser me paste karo:<br>
              <span style="color: #2A4F4F; word-break: break-all; font-size: 12px;">{reset_link}</span>
            </p>
            <hr style="border: none; border-top: 1px solid #E7E5DF; margin: 24px 0;">
            <p style="font-size: 12px; color: #A8A29E; line-height: 1.5;">
              ⚠️ Agar aapne yeh request nahi bheji, toh is email ko ignore karo — aapka account safe hai.
            </p>
          </div>
          <div style="text-align: center; margin-top: 24px; font-size: 12px; color: #A8A29E;">
            Made with ❤️ in India · <a href="https://apkamunim.com" style="color: #2A4F4F;">apkamunim.com</a>
          </div>
        </div>
        """
        params = {
            "from": sender,
            "to": [to_email],
            "subject": "🔐 Apka Munim — Password Reset",
            "html": html,
        }
        result = resend_lib.Emails.send(params)
        logger.info("Reset email sent to %s (id=%s)", to_email, result.get("id"))
        return {"sent": True, "id": result.get("id")}
    except Exception as e:
        logger.exception("Failed to send reset email: %s", e)
        return {"error": str(e), "dev_link": reset_link}


def _send_simple_email(to_email: str, subject: str, heading: str, body_text: str):
    """Generic transactional email via Resend — used for OTP codes and bill reminders."""
    api_key = os.environ.get("RESEND_API_KEY", "").strip()
    sender = os.environ.get("SENDER_EMAIL", "onboarding@resend.dev").strip()
    if not api_key or api_key == "your_resend_key_here":
        logger.warning("Resend not configured — email (dev mode) to %s: %s / %s", to_email, subject, body_text)
        return {"dev_mode": True}
    try:
        import resend as resend_lib
        resend_lib.api_key = api_key
        html = f"""
        <div style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Arial, sans-serif; max-width: 480px; margin: 0 auto; padding: 32px 24px; background: #F5F2ED; color: #1C1917;">
          <div style="text-align: center; margin-bottom: 24px;">
            <div style="display: inline-block; padding: 12px 20px; background: #2A4F4F; color: #E8B365; border-radius: 12px; font-size: 22px; font-weight: 800;">
              Apka Munim 🎩
            </div>
          </div>
          <div style="background: white; padding: 32px 24px; border-radius: 16px; box-shadow: 0 4px 12px rgba(0,0,0,0.05);">
            <h1 style="margin: 0 0 12px; font-size: 22px; color: #1C1917;">{heading}</h1>
            <p style="font-size: 15px; line-height: 1.6; color: #57534E; white-space: pre-line;">{body_text}</p>
          </div>
          <div style="text-align: center; margin-top: 24px; font-size: 12px; color: #A8A29E;">
            Made with ❤️ in India · <a href="https://apkamunim.com" style="color: #2A4F4F;">apkamunim.com</a>
          </div>
        </div>
        """
        result = resend_lib.Emails.send({"from": sender, "to": [to_email], "subject": subject, "html": html})
        return {"sent": True, "id": result.get("id")}
    except Exception as e:
        logger.exception("Failed to send email: %s", e)
        return {"error": str(e)}


@api.post("/auth/forgot-password")
@limiter.limit("5/hour")
async def forgot_password(body: ForgotPasswordIn, request: Request):
    """Generate reset token + send email. Always returns success even if email doesn't exist (privacy)."""
    email = body.email.lower()
    user = await db.users.find_one({"email": email})
    if not user:
        # Do not reveal whether email exists
        return {"ok": True, "message": "Agar yeh email registered hai, toh reset link bhej diya."}

    import secrets
    token = secrets.token_urlsafe(32)
    expires = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    await db.password_reset_tokens.insert_one({
        "token": token,
        "user_id": user["id"],
        "email": email,
        "expires_at": expires,
        "used": False,
        "created_at": datetime.now(timezone.utc).isoformat(),
    })
    # Build reset link — use frontend URL from env
    frontend_url = os.environ.get("FRONTEND_URL", "https://apkamunim.com").rstrip("/")
    reset_link = f"{frontend_url}/reset-password?token={token}"
    result = _send_reset_email(email, user.get("name", ""), reset_link)

    resp = {"ok": True, "message": "Reset link bhej diya! Email check karo."}
    # Only return dev_link in localhost/development mode
    frontend_url = os.environ.get("FRONTEND_URL", "")
    is_dev = "localhost" in frontend_url or "127.0.0.1" in frontend_url
    if result.get("dev_link") and is_dev:
        resp["dev_link"] = result["dev_link"]
    return resp


@api.post("/auth/reset-password")
async def reset_password(body: ResetPasswordIn, response: Response):
    """Verify token + set new password."""
    ok, err = validate_password_strength(body.new_password)
    if not ok:
        raise HTTPException(status_code=400, detail=err)

    doc = await db.password_reset_tokens.find_one({"token": body.token, "used": False})
    if not doc:
        raise HTTPException(status_code=400, detail="Invalid ya used token")
    try:
        exp = datetime.fromisoformat(doc["expires_at"])
        if datetime.now(timezone.utc) > exp:
            raise HTTPException(status_code=400, detail="Token expire ho gaya — dobara request karo")
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid token")

    await db.users.update_one({"id": doc["user_id"]},
                              {"$set": {"password_hash": hash_password(body.new_password)}})
    await db.password_reset_tokens.update_one({"token": body.token},
                                              {"$set": {"used": True}})
    return {"ok": True, "message": "Password reset ho gaya! Ab login karo."}


# ----- Auth (rest) -----


@api.get("/auth/me")
async def me(user=Depends(get_current_user)):
    premium = await _sync_premium_status(user)
    return {
        "id": user["id"], "email": user["email"], "name": user["name"],
        "currency": user.get("currency", "INR"),
        "personal_ledger_id": user["personal_ledger_id"],
        "current_ledger_id": user["current_ledger_id"],
        "current_ledger": user["current_ledger"],
        "premium": premium,
    }


@api.patch("/auth/currency")
async def update_currency(payload: dict, user=Depends(get_current_user)):
    currency = payload.get("currency", "INR")
    await db.users.update_one({"id": user["id"]}, {"$set": {"currency": currency}})
    return {"ok": True, "currency": currency}


@api.get("/auth/me/export")
async def export_my_data(user=Depends(get_current_user)):
    """Export ALL of the current user's data across their ledgers (compliance/GDPR-ready)."""
    uid = user["id"]
    my_ledgers = await db.ledgers.find({"members": uid}, {"_id": 0}).to_list(50)
    ledger_ids = [l["id"] for l in my_ledgers]

    async def _all(coll):
        return await db[coll].find({"$or": [{"user_id": uid}, {"owner_id": {"$in": ledger_ids}}]}, {"_id": 0}).to_list(10000)

    return {
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "user": {
            "id": user["id"], "email": user["email"], "name": user["name"],
            "currency": user.get("currency", "INR"),
            "created_at": user.get("created_at"),
        },
        "ledgers": my_ledgers,
        "accounts": await _all("accounts"),
        "transactions": await _all("transactions"),
        "udhaar": await _all("udhaar"),
        "recurring": await _all("recurring"),
        "budgets": await _all("budgets"),
    }


@api.delete("/auth/me")
async def delete_my_account(response: Response, user=Depends(get_current_user)):
    """Permanently delete the current user's account and all their data (Play Store 'Data Safety' compliance)."""
    uid = user["id"]
    # Delete personal-scoped data
    personal_ledger_id = user["personal_ledger_id"]
    for coll in ("accounts", "transactions", "udhaar", "recurring", "budgets"):
        await db[coll].delete_many({"owner_id": personal_ledger_id})
        # legacy docs
        await db[coll].delete_many({"user_id": uid, "owner_id": {"$exists": False}})
    # Remove from shared ledgers
    shared = await db.ledgers.find({"members": uid, "type": "shared"}, {"_id": 0}).to_list(50)
    for lg in shared:
        if lg.get("owner_user_id") == uid and len(lg.get("members", [])) == 1:
            # sole owner: delete ledger + data
            for coll in ("accounts", "transactions", "udhaar", "recurring", "budgets"):
                await db[coll].delete_many({"owner_id": lg["id"]})
            await db.ledgers.delete_one({"id": lg["id"]})
        else:
            await db.ledgers.update_one({"id": lg["id"]}, {"$pull": {"members": uid}})
    # Delete personal ledger
    await db.ledgers.delete_one({"id": personal_ledger_id})
    # Delete user
    await db.users.delete_one({"id": uid})
    response.delete_cookie("access_token", path="/", domain=COOKIE_DOMAIN)
    return {"ok": True}


# ----- Ledgers (Family / Shared) -----
async def _decorate_ledger(lg: dict, user_id: str) -> dict:
    """Attach member details to a ledger doc."""
    members = await db.users.find(
        {"id": {"$in": lg.get("members", [])}}, {"_id": 0, "id": 1, "name": 1, "email": 1}
    ).to_list(20)
    lg["members_detail"] = members
    lg["is_owner"] = lg.get("owner_user_id") == user_id
    return lg


@api.get("/ledgers")
async def list_ledgers(user=Depends(get_current_user)):
    rows = await db.ledgers.find({"members": user["id"]}, {"_id": 0}).to_list(50)
    for r in rows:
        await _decorate_ledger(r, user["id"])
    return rows


@api.post("/ledgers")
async def create_ledger(body: LedgerCreate, user=Depends(get_current_user)):
    lid = str(uuid.uuid4())
    code = gen_invite_code()
    doc = {
        "id": lid,
        "name": body.name.strip() or "Shared",
        "type": "shared",
        "owner_user_id": user["id"],
        "members": [user["id"]],
        "invite_code": code,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    await db.ledgers.insert_one(doc)
    doc.pop("_id", None)
    await _decorate_ledger(doc, user["id"])
    return doc


@api.post("/ledgers/join")
async def join_ledger(body: LedgerJoin, user=Depends(get_current_user)):
    code = body.invite_code.strip().upper()
    lg = await db.ledgers.find_one({"invite_code": code}, {"_id": 0})
    if not lg:
        raise HTTPException(status_code=404, detail="Invalid invite code")
    if user["id"] in lg.get("members", []):
        return await _decorate_ledger(lg, user["id"])
    await db.ledgers.update_one({"id": lg["id"]}, {"$addToSet": {"members": user["id"]}})
    lg["members"] = list(set(lg.get("members", []) + [user["id"]]))
    await _decorate_ledger(lg, user["id"])
    return lg


@api.post("/ledgers/{ledger_id}/switch")
async def switch_ledger(ledger_id: str, user=Depends(get_current_user)):
    lg = await db.ledgers.find_one({"id": ledger_id, "members": user["id"]}, {"_id": 0})
    if not lg:
        raise HTTPException(status_code=404, detail="Ledger not found")
    await db.users.update_one({"id": user["id"]}, {"$set": {"current_ledger_id": ledger_id}})
    return {"ok": True, "current_ledger_id": ledger_id}


@api.post("/ledgers/{ledger_id}/leave")
async def leave_ledger(ledger_id: str, user=Depends(get_current_user)):
    lg = await db.ledgers.find_one({"id": ledger_id, "members": user["id"]})
    if not lg:
        raise HTTPException(status_code=404, detail="Ledger not found")
    if lg.get("type") == "personal":
        raise HTTPException(status_code=400, detail="Cannot leave personal ledger")
    if lg.get("owner_user_id") == user["id"] and len(lg.get("members", [])) > 1:
        raise HTTPException(status_code=400, detail="Transfer ownership first")
    await db.ledgers.update_one({"id": ledger_id}, {"$pull": {"members": user["id"]}})
    # if empty & owner left, delete ledger + its data
    remaining = await db.ledgers.find_one({"id": ledger_id})
    if not remaining.get("members"):
        for coll in ("accounts", "transactions", "udhaar", "recurring", "budgets"):
            await db[coll].delete_many({"owner_id": ledger_id})
        await db.ledgers.delete_one({"id": ledger_id})
    # switch to personal
    await db.users.update_one(
        {"id": user["id"]},
        {"$set": {"current_ledger_id": user["personal_ledger_id"]}},
    )
    return {"ok": True}


@api.get("/analytics/net-worth")
async def get_net_worth(user=Depends(get_current_user)):
    """Total assets minus liabilities: all account balances (credit cards counted as debt)
    plus pending udhaar you're owed, minus pending udhaar you owe."""
    accs = await db.accounts.find(scope(user), {"_id": 0}).to_list(500)
    assets = 0.0
    liabilities = 0.0
    breakdown = []
    for a in accs:
        pipeline = [
            {"$match": {"owner_id": user["current_ledger_id"], "account_id": a["id"]}},
            {"$group": {"_id": "$type", "total": {"$sum": "$amount"}}},
        ]
        agg = await db.transactions.aggregate(pipeline).to_list(10)
        income = sum(x["total"] for x in agg if x["_id"] == "income")
        expense = sum(x["total"] for x in agg if x["_id"] == "expense")
        balance = round(a.get("opening_balance", 0.0) + income - expense, 2)
        if a.get("type") == "credit_card":
            liabilities += abs(balance)
        else:
            assets += balance
        breakdown.append({"name": a["name"], "type": a.get("type"), "balance": balance})

    udhaar_rows = await db.udhaar.find(
        {"owner_id": user["current_ledger_id"], "status": "pending"}, {"_id": 0}
    ).to_list(1000)
    udhaar_owed_to_you = sum(u["amount"] for u in udhaar_rows if u["type"] == "lene")
    udhaar_you_owe = sum(u["amount"] for u in udhaar_rows if u["type"] == "dene")
    assets += udhaar_owed_to_you
    liabilities += udhaar_you_owe

    investment_rows = await db.investments.find(scope(user), {"_id": 0}).to_list(500)
    total_investments = sum(i["current_value"] for i in investment_rows)
    assets += total_investments

    return {
        "net_worth": round(assets - liabilities, 2),
        "total_assets": round(assets, 2),
        "total_liabilities": round(liabilities, 2),
        "total_investments": round(total_investments, 2),
        "udhaar_owed_to_you": round(udhaar_owed_to_you, 2),
        "udhaar_you_owe": round(udhaar_you_owe, 2),
        "accounts": breakdown,
    }


# ----- Accounts -----
@api.get("/accounts")
async def list_accounts(user=Depends(get_current_user)):
    accs = await db.accounts.find(scope(user), {"_id": 0}).to_list(500)
    for a in accs:
        pipeline = [
            {"$match": {"owner_id": user["current_ledger_id"], "account_id": a["id"]}},
            {"$group": {"_id": "$type", "total": {"$sum": "$amount"}}},
        ]
        agg = await db.transactions.aggregate(pipeline).to_list(10)
        income = sum(x["total"] for x in agg if x["_id"] == "income")
        expense = sum(x["total"] for x in agg if x["_id"] == "expense")
        a["balance"] = round(a.get("opening_balance", 0.0) + income - expense, 2)
    return accs


@api.post("/accounts")
async def create_account(body: AccountIn, user=Depends(get_current_user)):
    doc = {
        "id": str(uuid.uuid4()),
        "user_id": user["id"],
        "owner_id": user["current_ledger_id"],
        "name": body.name,
        "type": body.type,
        "opening_balance": body.opening_balance,
        "currency": body.currency,
        "color": body.color,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    await db.accounts.insert_one(doc)
    doc.pop("_id", None)
    doc["balance"] = doc["opening_balance"]
    return doc


@api.delete("/accounts/{account_id}")
async def delete_account(account_id: str, user=Depends(get_current_user)):
    await db.accounts.delete_one({"id": account_id, "owner_id": user["current_ledger_id"]})
    await db.transactions.delete_many({"account_id": account_id, "owner_id": user["current_ledger_id"]})
    return {"ok": True}


# ----- Budget helper -----
async def compute_budget_alerts(user: dict, category: str) -> list:
    """Return list of alerts if the given category budget is >=80% or exceeded."""
    b = await db.budgets.find_one({"owner_id": user["current_ledger_id"], "category": category})
    if not b:
        return []
    now = datetime.now(timezone.utc)
    prefix = now.strftime("%Y-%m")
    rows = await db.transactions.find(
        {"owner_id": user["current_ledger_id"], "type": "expense", "category": category},
        {"_id": 0, "amount": 1, "date": 1},
    ).to_list(5000)
    spent = sum(r["amount"] for r in rows if r.get("date", "").startswith(prefix))
    percent = (spent / b["amount"] * 100) if b["amount"] > 0 else 0
    if percent < 80:
        return []
    level = "over" if percent >= 100 else "warning"
    return [{
        "category": category,
        "budget": b["amount"],
        "spent": round(spent, 2),
        "percent": round(percent, 1),
        "level": level,
    }]


@api.get("/budgets/alerts")
async def get_budget_alerts(user=Depends(get_current_user)):
    """All current-month budget alerts across every category (dashboard widget)."""
    budgets = await db.budgets.find({"owner_id": user["current_ledger_id"]}, {"_id": 0}).to_list(200)
    all_alerts = []
    for b in budgets:
        alerts = await compute_budget_alerts(user, b["category"])
        all_alerts.extend(alerts)
    all_alerts.sort(key=lambda a: a["percent"], reverse=True)
    return {"alerts": all_alerts, "count_over": sum(1 for a in all_alerts if a["level"] == "over")}


# ----- Investments -----
@api.get("/investments")
async def list_investments(user=Depends(get_current_user)):
    rows = await db.investments.find(scope(user), {"_id": 0}).sort("created_at", -1).to_list(500)
    return rows


@api.post("/investments")
async def create_investment(body: InvestmentIn, user=Depends(get_current_user)):
    doc = {
        "id": str(uuid.uuid4()),
        "user_id": user["id"],
        "owner_id": user["current_ledger_id"],
        **body.dict(),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    await db.investments.insert_one(doc)
    doc.pop("_id", None)
    return doc


@api.put("/investments/{investment_id}")
async def update_investment(investment_id: str, body: InvestmentUpdate, user=Depends(get_current_user)):
    updates = {k: v for k, v in body.dict().items() if v is not None}
    res = await db.investments.update_one({"id": investment_id, **scope(user)}, {"$set": updates})
    if res.matched_count == 0:
        raise HTTPException(status_code=404, detail="Investment not found")
    doc = await db.investments.find_one({"id": investment_id, **scope(user)}, {"_id": 0})
    return doc


@api.delete("/investments/{investment_id}")
async def delete_investment(investment_id: str, user=Depends(get_current_user)):
    await db.investments.delete_one({"id": investment_id, "owner_id": user["current_ledger_id"]})
    return {"ok": True}


@api.get("/investments/summary")
async def investments_summary(user=Depends(get_current_user)):
    rows = await db.investments.find(scope(user), {"_id": 0}).to_list(500)
    invested = sum(r["invested_amount"] for r in rows)
    current = sum(r["current_value"] for r in rows)
    return {
        "total_invested": round(invested, 2),
        "total_current_value": round(current, 2),
        "total_gain": round(current - invested, 2),
        "gain_percent": round(((current - invested) / invested * 100) if invested > 0 else 0, 2),
        "count": len(rows),
    }


# ----- Expense Splitting -----
@api.get("/splits")
async def list_splits(user=Depends(get_current_user)):
    rows = await db.splits.find(scope(user), {"_id": 0}).sort("created_at", -1).to_list(500)
    return rows


@api.post("/splits")
async def create_split(body: SplitIn, user=Depends(get_current_user)):
    doc = {
        "id": str(uuid.uuid4()),
        "user_id": user["id"],
        "owner_id": user["current_ledger_id"],
        "title": body.title,
        "total_amount": body.total_amount,
        "paid_by": body.paid_by,
        "participants": [p.dict() for p in body.participants],
        "date": body.date or datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "notes": body.notes,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    await db.splits.insert_one(doc)
    doc.pop("_id", None)
    return doc


@api.patch("/splits/{split_id}/settle/{participant_index}")
async def settle_split_participant(split_id: str, participant_index: int, user=Depends(get_current_user)):
    split = await db.splits.find_one({"id": split_id, **scope(user)})
    if not split:
        raise HTTPException(status_code=404, detail="Split not found")
    participants = split["participants"]
    if participant_index < 0 or participant_index >= len(participants):
        raise HTTPException(status_code=400, detail="Invalid participant")
    participants[participant_index]["settled"] = True
    await db.splits.update_one({"id": split_id}, {"$set": {"participants": participants}})
    return {"ok": True}


@api.delete("/splits/{split_id}")
async def delete_split(split_id: str, user=Depends(get_current_user)):
    await db.splits.delete_one({"id": split_id, "owner_id": user["current_ledger_id"]})
    return {"ok": True}


# ----- Tax Estimator (India, rough estimate — not tax advice) -----
@api.post("/tax/estimate")
async def estimate_tax(body: TaxEstimateIn, user=Depends(get_current_user)):
    """Rough India income-tax estimate (FY2025-26 slabs). Informational only, not tax advice."""
    income = body.annual_income

    if body.regime == "new":
        # New regime: no 80C/80D deduction, standard deduction 75000 (salaried assumption)
        taxable = max(0, income - 75000)
        slabs = [(400000, 0), (800000, 0.05), (1200000, 0.10), (1600000, 0.15),
                 (2000000, 0.20), (2400000, 0.25), (float("inf"), 0.30)]
    else:
        # Old regime: standard deduction 50000 + 80C (cap 150000) + 80D (cap 25000/50000)
        cap_80d = 25000 if body.age_below_60 else 50000
        deductions = 50000 + min(body.section_80c, 150000) + min(body.section_80d, cap_80d)
        taxable = max(0, income - deductions)
        slabs = [(250000, 0), (500000, 0.05), (1000000, 0.20), (float("inf"), 0.30)]

    tax = 0.0
    prev_limit = 0
    for limit, rate in slabs:
        if taxable > prev_limit:
            tax += (min(taxable, limit) - prev_limit) * rate
        prev_limit = limit
        if taxable <= limit:
            break

    cess = tax * 0.04
    total_tax = round(tax + cess, 2)

    return {
        "regime": body.regime,
        "taxable_income": round(taxable, 2),
        "tax_before_cess": round(tax, 2),
        "cess": round(cess, 2),
        "total_tax_payable": total_tax,
        "effective_rate_percent": round((total_tax / income * 100) if income > 0 else 0, 2),
        "disclaimer": "Ye rough estimate hai, CA se confirm zaroor karo — final tax filing ke liye.",
    }


# ----- Referral Program -----
@api.get("/referral/me")
async def get_my_referral(user=Depends(get_current_user)):
    full = await db.users.find_one({"id": user["id"]}, {"_id": 0})
    code = full.get("referral_code")
    if not code:
        code = full["id"][:8].upper()
        await db.users.update_one({"id": user["id"]}, {"$set": {"referral_code": code}})
    referred_count = await db.users.count_documents({"referred_by": code})
    return {
        "referral_code": code,
        "referral_link": f"https://apkamunim.com/register?ref={code}",
        "referred_count": referred_count,
        "credits_earned": full.get("referral_credits", 0),
    }


# ----- Bulk CSV Import (bank statement / manual bulk entry) -----
# ----- Bank Statement Import (CSV / PDF, any bank format) -----

_EXPENSE_KEYWORDS = {
    "Food": ["swiggy", "zomato", "restaurant", "cafe", "dine", "eatery", "food"],
    "Groceries": ["grocery", "bigbasket", "blinkit", "zepto", "dmart", "grofers", "supermarket", "instamart"],
    "Rent": ["rent", "landlord"],
    "Transport": ["uber", "ola", "petrol", "diesel", "fuel", "metro", "irctc", "railway", "fastag", "parking"],
    "Shopping": ["amazon", "flipkart", "myntra", "ajio", "mall", "shopping", "nykaa"],
    "Bills": ["electricity", "water bill", "recharge", "broadband", "wifi", "gas bill", "dth", "bill payment", "bses", "postpaid", "prepaid"],
    "Entertainment": ["netflix", "prime video", "hotstar", "bookmyshow", "spotify", "movie", "pvr", "inox", "youtube"],
    "Health": ["hospital", "pharmacy", "medical", "apollo", "clinic", "doctor", "medplus"],
    "Education": ["school", "college", "tuition fee", "udemy", "coursera", "byju", "unacademy"],
    "Travel": ["makemytrip", "goibibo", "airlines", "indigo", "spicejet", "oyo", "airbnb", "flight", "yatra"],
}
_INCOME_KEYWORDS = {
    "Salary": ["salary", "payroll", "sal credit"],
    "Business": ["sales", "invoice", "business income"],
    "Freelance": ["freelance", "upwork", "fiverr"],
    "Investment": ["dividend", "interest credit", "redemption", "mutual fund"],
    "Gift": ["gift"],
}
_IGNORE_ROW_KEYWORDS = ["opening balance", "closing balance", "balance b/f", "balance c/f", "carried forward"]


def guess_category(description: str, txn_type: str) -> str:
    desc = (description or "").lower()
    table = _INCOME_KEYWORDS if txn_type == "income" else _EXPENSE_KEYWORDS
    for category, keywords in table.items():
        if any(k in desc for k in keywords):
            return category
    return "Other Income" if txn_type == "income" else "Other"


def parse_flexible_date(value: str) -> Optional[str]:
    value = (value or "").strip()
    if not value:
        return None
    try:
        dt = dateutil_parser.parse(value, dayfirst=True, fuzzy=True)
        return dt.strftime("%Y-%m-%d")
    except Exception:
        return None


def clean_amount(value: str) -> Optional[float]:
    if value is None:
        return None
    v = str(value).strip().replace(",", "").replace("₹", "").replace("Rs.", "").replace("Rs", "").strip()
    if not v or v in ("-", "--"):
        return None
    negative = False
    if v.startswith("(") and v.endswith(")"):
        negative = True
        v = v[1:-1]
    if v.upper().endswith("DR"):
        negative = True
        v = v[:-2].strip()
    if v.upper().endswith("CR"):
        v = v[:-2].strip()
    try:
        num = float(v)
    except Exception:
        return None
    return -abs(num) if negative else num


def find_col(headers: List[str], keywords: List[str]) -> Optional[str]:
    for h in headers:
        low = h.lower().strip()
        if any(k in low for k in keywords):
            return h
    return None


def normalize_statement_rows(raw_rows: List[dict]) -> List[dict]:
    """Takes list of raw dict rows (header -> string value) from ANY bank's CSV/PDF
    and turns them into clean transaction rows: {date, type, amount, category, note}."""
    if not raw_rows:
        return []
    headers = list(raw_rows[0].keys())
    date_col = find_col(headers, ["date"])
    desc_col = find_col(headers, ["narration", "description", "particular", "details", "remarks", "transaction"])
    debit_col = find_col(headers, ["debit", "withdrawal", "dr amount", "amount debited"])
    credit_col = find_col(headers, ["credit", "deposit", "cr amount", "amount credited"])
    amount_col = None if (debit_col or credit_col) else find_col(headers, ["amount"])

    results = []
    for row in raw_rows:
        desc = (row.get(desc_col) or "").strip() if desc_col else ""
        if any(k in desc.lower() for k in _IGNORE_ROW_KEYWORDS):
            continue

        date_str = parse_flexible_date(row.get(date_col)) if date_col else None
        if not date_str:
            continue

        txn_type, amount = None, None
        if debit_col or credit_col:
            debit_val = clean_amount(row.get(debit_col)) if debit_col else None
            credit_val = clean_amount(row.get(credit_col)) if credit_col else None
            if debit_val and debit_val != 0:
                txn_type, amount = "expense", abs(debit_val)
            elif credit_val and credit_val != 0:
                txn_type, amount = "income", abs(credit_val)
        elif amount_col:
            val = clean_amount(row.get(amount_col))
            if val is not None and val != 0:
                txn_type = "expense" if val < 0 else "income"
                amount = abs(val)

        if not txn_type or not amount:
            continue

        results.append({
            "date": date_str,
            "type": txn_type,
            "amount": round(amount, 2),
            "category": guess_category(desc, txn_type),
            "note": desc[:140],
        })
    return results


def extract_csv_rows(raw_bytes: bytes) -> List[dict]:
    text = raw_bytes.decode("utf-8", errors="ignore")
    lines = text.splitlines()
    header_idx = 0
    for i, line in enumerate(lines[:40]):
        low = line.lower()
        if "date" in low and any(k in low for k in ["amount", "debit", "credit", "withdrawal", "deposit", "narration"]):
            header_idx = i
            break
    trimmed = "\n".join(lines[header_idx:])
    reader = csv.DictReader(io.StringIO(trimmed))
    return [dict(row) for row in reader if any((v or "").strip() for v in row.values())]


def extract_pdf_rows(raw_bytes: bytes) -> List[dict]:
    if not _HAS_PDFPLUMBER:
        raise HTTPException(status_code=400, detail="PDF parsing not available on server right now.")
    headers = None
    rows = []
    with pdfplumber.open(io.BytesIO(raw_bytes)) as pdf:
        for page in pdf.pages:
            tables = page.extract_tables()
            for table in tables:
                if not table or len(table) < 2:
                    continue
                for r_idx, raw_row in enumerate(table):
                    cells = [(c or "").strip() for c in raw_row]
                    low_joined = " ".join(cells).lower()
                    looks_like_header = "date" in low_joined and any(
                        k in low_joined for k in ["amount", "debit", "credit", "withdrawal", "deposit", "narration", "balance"]
                    )
                    if looks_like_header:
                        headers = cells
                        continue
                    if headers and len(cells) >= 1:
                        row_dict = {headers[i]: (cells[i] if i < len(cells) else "") for i in range(len(headers))}
                        rows.append(row_dict)
    return rows


@api.post("/transactions/parse-statement")
async def parse_statement(account_id: str = Form(...), file: UploadFile = File(...), user=Depends(get_current_user)):
    """Upload a bank statement (CSV or PDF, any bank's format) and get back a preview
    of detected transactions. Nothing is saved yet — call /transactions/confirm-import
    with the (possibly edited) list to actually create them."""
    acc = await db.accounts.find_one({"id": account_id, "owner_id": user["current_ledger_id"]})
    if not acc:
        raise HTTPException(status_code=404, detail="Account not found")

    raw = await file.read()
    filename = (file.filename or "").lower()

    if filename.endswith(".pdf"):
        raw_rows = extract_pdf_rows(raw)
    else:
        raw_rows = extract_csv_rows(raw)

    parsed = normalize_statement_rows(raw_rows)

    # Basic duplicate check against existing transactions on this account (same date+amount+type)
    existing = await db.transactions.find(
        {"account_id": account_id, "owner_id": user["current_ledger_id"]},
        {"_id": 0, "date": 1, "amount": 1, "type": 1},
    ).to_list(2000)
    existing_keys = {(e.get("date", "")[:10], round(e.get("amount", 0), 2), e.get("type")) for e in existing}
    for row in parsed:
        row["possible_duplicate"] = (row["date"], row["amount"], row["type"]) in existing_keys

    if not parsed:
        return {
            "transactions": [],
            "count": 0,
            "message": "Koi transaction detect nahi hua. File format alag ho sakta hai — thoda column headers check kar lo.",
        }
    return {"transactions": parsed, "count": len(parsed)}


@api.post("/transactions/confirm-import")
async def confirm_import(body: ConfirmImportIn, user=Depends(get_current_user)):
    """Bulk-create transactions from a (user-reviewed) parsed statement."""
    acc = await db.accounts.find_one({"id": body.account_id, "owner_id": user["current_ledger_id"]})
    if not acc:
        raise HTTPException(status_code=404, detail="Account not found")

    inserted = 0
    docs = []
    for row in body.transactions:
        docs.append({
            "id": str(uuid.uuid4()),
            "user_id": user["id"],
            "owner_id": user["current_ledger_id"],
            "account_id": body.account_id,
            "account_name": acc["name"],
            "type": row.type,
            "amount": float(row.amount),
            "category": row.category or "Other",
            "note": row.note or "",
            "date": row.date,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "source": "statement_import",
        })
    if docs:
        await db.transactions.insert_many(docs)
        inserted = len(docs)
    return {"inserted": inserted}


@api.post("/transactions/import-csv")
async def import_transactions_csv(account_id: str = Form(...), file: UploadFile = File(...), user=Depends(get_current_user)):
    """Expects a CSV file with header: date,type,amount,category,note
    date format YYYY-MM-DD, type is income/expense."""
    acc = await db.accounts.find_one({"id": account_id, "owner_id": user["current_ledger_id"]})
    if not acc:
        raise HTTPException(status_code=404, detail="Account not found")

    raw = await file.read()
    text = raw.decode("utf-8", errors="ignore")
    reader = csv.DictReader(io.StringIO(text))
    inserted, skipped = 0, 0
    for row in reader:
        try:
            amount = float(row.get("amount", 0))
            txn_type = row.get("type", "").strip().lower()
            if txn_type not in ("income", "expense") or amount <= 0:
                skipped += 1
                continue
            doc = {
                "id": str(uuid.uuid4()),
                "user_id": user["id"],
                "owner_id": user["current_ledger_id"],
                "account_id": account_id,
                "type": txn_type,
                "amount": amount,
                "category": row.get("category", "Other").strip() or "Other",
                "note": row.get("note", "").strip(),
                "date": row.get("date", "").strip() or datetime.now(timezone.utc).isoformat(),
                "created_at": datetime.now(timezone.utc).isoformat(),
                "source": "csv_import",
            }
            await db.transactions.insert_one(doc)
            inserted += 1
        except Exception:
            skipped += 1
    return {"inserted": inserted, "skipped": skipped}


# ----- Transactions -----
@api.get("/transactions")
async def list_transactions(user=Depends(get_current_user), limit: int = 500):
    rows = await db.transactions.find(scope(user), {"_id": 0}) \
        .sort("date", -1).to_list(limit)
    # attach creator name for shared ledgers
    if user["current_ledger"].get("type") == "shared":
        uids = list({r.get("user_id") for r in rows if r.get("user_id")})
        umap = {}
        if uids:
            people = await db.users.find({"id": {"$in": uids}}, {"_id": 0, "id": 1, "name": 1}).to_list(50)
            umap = {u["id"]: u["name"] for u in people}
        for r in rows:
            r["created_by"] = umap.get(r.get("user_id"), "")
    return rows


@api.post("/transactions")
async def create_transaction(body: TransactionIn, user=Depends(get_current_user)):
    acc = await db.accounts.find_one({"id": body.account_id, "owner_id": user["current_ledger_id"]})
    if not acc:
        raise HTTPException(status_code=404, detail="Account not found")
    doc = {
        "id": str(uuid.uuid4()),
        "user_id": user["id"],
        "owner_id": user["current_ledger_id"],
        "account_id": body.account_id,
        "account_name": acc["name"],
        "type": body.type,
        "amount": float(body.amount),
        "category": body.category,
        "note": body.note or "",
        "date": body.date or datetime.now(timezone.utc).isoformat(),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    await db.transactions.insert_one(doc)
    doc.pop("_id", None)

    alerts = []
    if body.type == "expense":
        alerts = await compute_budget_alerts(user, body.category)
    return {"transaction": doc, "budget_alerts": alerts}


@api.patch("/transactions/{txn_id}")
async def update_transaction(txn_id: str, body: TransactionUpdate, user=Depends(get_current_user)):
    updates = {k: v for k, v in body.model_dump().items() if v is not None}
    if not updates:
        return {"ok": True}
    if "account_id" in updates:
        acc = await db.accounts.find_one({"id": updates["account_id"], "owner_id": user["current_ledger_id"]})
        if not acc:
            raise HTTPException(status_code=404, detail="Account not found")
        updates["account_name"] = acc["name"]
    await db.transactions.update_one({"id": txn_id, "owner_id": user["current_ledger_id"]}, {"$set": updates})
    return {"ok": True}


@api.delete("/transactions/{txn_id}")
async def delete_transaction(txn_id: str, user=Depends(get_current_user)):
    await db.transactions.delete_one({"id": txn_id, "owner_id": user["current_ledger_id"]})
    return {"ok": True}


# ----- Global Search -----
@api.get("/search")
async def global_search(q: str = "", user=Depends(get_current_user)):
    """Search across transactions, udhaar, accounts, goals, subscriptions, splits, investments."""
    query = (q or "").strip()
    empty = {"transactions": [], "udhaar": [], "accounts": [], "goals": [], "subscriptions": [], "splits": [], "investments": []}
    if len(query) < 1:
        return empty

    import re
    safe = re.escape(query)
    rx = {"$regex": safe, "$options": "i"}
    owner = {"owner_id": user["current_ledger_id"]}

    amount_match = None
    try:
        amount_match = float(query.replace(",", ""))
    except ValueError:
        amount_match = None

    txn_or = [{"note": rx}, {"category": rx}, {"account_name": rx}, {"type": rx}]
    if amount_match is not None:
        txn_or.append({"amount": amount_match})
    txns = await db.transactions.find({**owner, "$or": txn_or}, {"_id": 0}) \
        .sort("date", -1).to_list(15)

    udh_or = [{"person_name": rx}, {"note": rx}, {"type": rx}, {"phone": rx}]
    if amount_match is not None:
        udh_or.append({"amount": amount_match})
    udhaar = await db.udhaar.find({**owner, "$or": udh_or}, {"_id": 0}) \
        .sort("created_at", -1).to_list(15)

    accounts = await db.accounts.find({**owner, "$or": [{"name": rx}, {"type": rx}]}, {"_id": 0}) \
        .to_list(15)

    goals = await db.goals.find({**owner, "name": rx}, {"_id": 0}).to_list(15) \
        if hasattr(db, "goals") else []

    subs = await db.subscriptions.find({**owner, "$or": [{"name": rx}, {"category": rx}]}, {"_id": 0}) \
        .to_list(15) if hasattr(db, "subscriptions") else []

    splits = []
    try:
        splits = await db.splits.find({**owner, "$or": [{"title": rx}, {"paid_by": rx}]}, {"_id": 0}) \
            .sort("created_at", -1).to_list(15)
    except Exception:
        splits = []

    investments = []
    try:
        inv_or = [{"name": rx}, {"type": rx}, {"note": rx}]
        if amount_match is not None:
            inv_or.append({"invested_amount": amount_match})
            inv_or.append({"current_value": amount_match})
        investments = await db.investments.find({**owner, "$or": inv_or}, {"_id": 0}) \
            .sort("created_at", -1).to_list(15)
    except Exception:
        investments = []

    return {
        "transactions": txns,
        "udhaar": udhaar,
        "accounts": accounts,
        "goals": goals,
        "subscriptions": subs,
        "splits": splits,
        "investments": investments,
    }


# ----- Udhaar -----
@api.get("/udhaar")
async def list_udhaar(user=Depends(get_current_user)):
    rows = await db.udhaar.find(scope(user), {"_id": 0}).sort("created_at", -1).to_list(500)
    return rows


@api.post("/udhaar")
async def create_udhaar(body: UdhaarIn, user=Depends(get_current_user)):
    doc = {
        "id": str(uuid.uuid4()),
        "user_id": user["id"],
        "owner_id": user["current_ledger_id"],
        "person_name": body.person_name,
        "phone": body.phone or "",
        "type": body.type,
        "amount": float(body.amount),
        "note": body.note or "",
        "due_date": body.due_date or "",
        "status": "pending",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    await db.udhaar.insert_one(doc)
    doc.pop("_id", None)
    return doc


@api.patch("/udhaar/{udhaar_id}")
async def update_udhaar(udhaar_id: str, body: UdhaarUpdate, user=Depends(get_current_user)):
    updates = {k: v for k, v in body.model_dump().items() if v is not None}
    if not updates:
        return {"ok": True}
    await db.udhaar.update_one({"id": udhaar_id, "owner_id": user["current_ledger_id"]}, {"$set": updates})
    return {"ok": True}


@api.delete("/udhaar/{udhaar_id}")
async def delete_udhaar(udhaar_id: str, user=Depends(get_current_user)):
    await db.udhaar.delete_one({"id": udhaar_id, "owner_id": user["current_ledger_id"]})
    return {"ok": True}


# ----- SMS / UPI Parser -----
import re

CATEGORY_KEYWORDS = {
    "Food": ["zomato", "swiggy", "dominos", "domino", "pizza", "mcd", "mcdonald", "kfc",
             "burger", "starbucks", "cafe", "restaurant", "food", "eatsure", "eazydiner"],
    "Groceries": ["bigbasket", "blinkit", "zepto", "instamart", "grofers", "dmart",
                  "reliance fresh", "grocery", "kirana"],
    "Transport": ["uber", "ola", "rapido", "yulu", "namma metro", "irctc", "railway",
                  "petrol", "hpcl", "iocl", "bpcl", "indianoil", "fastag", "metro"],
    "Shopping": ["amazon", "flipkart", "myntra", "ajio", "meesho", "nykaa", "tatacliq",
                 "shoppers stop", "reliance trends", "croma"],
    "Bills": ["airtel", "jio", "vi ", "vodafone", "electricity", "bescom", "msedcl",
              "adani electricity", "torrent power", "gas bill", "mahanagar gas", "igl",
              "water bill", "broadband", "act fibernet", "recharge", "postpaid", "dth"],
    "Entertainment": ["netflix", "hotstar", "prime video", "amazon prime", "spotify",
                      "youtube premium", "sonyliv", "bookmyshow", "pvr", "inox"],
    "Health": ["pharmeasy", "netmeds", "1mg", "apollo", "practo", "medlife", "hospital",
               "clinic", "pharmacy", "medicine"],
    "Education": ["byju", "unacademy", "vedantu", "coursera", "udemy", "school", "college",
                  "fees"],
    "Travel": ["makemytrip", "goibibo", "yatra", "irctc", "airbnb", "oyo", "cleartrip",
               "ixigo", "indigo", "vistara", "spicejet", "airindia"],
    "Rent": ["rent"],
    "Salary": ["salary", "sal cr", "salary credit"],
    "Business": ["invoice", "payment received", "business"],
    "Freelance": ["upwork", "fiverr", "freelance"],
    "Investment": ["mutual fund", "sip", "zerodha", "groww", "kite", "coin", "smallcase"],
    "Gift": ["gift"],
}


def _guess_category(text: str, txn_type: str) -> str:
    t = text.lower()
    for cat, kws in CATEGORY_KEYWORDS.items():
        for kw in kws:
            if kw in t:
                if txn_type == "income" and cat not in ("Salary", "Business", "Freelance", "Investment", "Gift"):
                    continue
                if txn_type == "expense" and cat in ("Salary", "Business", "Freelance", "Investment", "Gift"):
                    continue
                return cat
    return "Other Income" if txn_type == "income" else "Other"


def _detect_type(text: str) -> Optional[str]:
    t = text.lower()
    debit_words = ["debited", "debit", "paid", "sent", "deducted", "withdrawn", "spent", "purchase"]
    credit_words = ["credited", "credit", "received", "deposited", "refund", "salary"]
    d = any(w in t for w in debit_words)
    c = any(w in t for w in credit_words)
    if d and not c:
        return "expense"
    if c and not d:
        return "income"
    if d:
        return "expense"
    if c:
        return "income"
    return None


def _extract_merchant(text: str) -> Optional[str]:
    """Try multiple patterns; pick most specific."""
    patterns = [
        r"UPI[/\-]([A-Za-z][A-Za-z0-9 &.\-]{2,40}?)(?:/|\s+on\s|\s+ref|\.|$)",
        r"paid to\s+([A-Za-z][A-Za-z0-9 &.\-]{2,40}?)(?:\s+via|\s+on|\.|,|$)",
        r"to\s+([a-zA-Z][a-zA-Z0-9._\-]{2,40})@[a-zA-Z]+",
        r"received from\s+([A-Za-z][A-Za-z0-9 &.\-]{2,40}?)(?:\s+on|\.|,|$)",
        r"at\s+([A-Z][A-Za-z0-9 &.\-]{2,40}?)(?:\s+on|\.|,|$)",
    ]
    skip = {"your", "the", "a/c", "salary", "credit", "debit", "customer", "account", "hdfc", "sbi", "icici", "axis", "kotak"}
    for pat in patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            name = m.group(1).strip().rstrip(".,/-").strip()
            if name and name.lower() not in skip and len(name) >= 2:
                return name[:40]
    return None


AMOUNT_RE = re.compile(
    r"(?:rs\.?|inr|rupees|₹)\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)",
    re.IGNORECASE,
)
ACC_LAST4_RE = re.compile(r"a/?c[^0-9]*([0-9]{4})", re.IGNORECASE)
XX_LAST4_RE = re.compile(r"x{2,}(\d{4})", re.IGNORECASE)


def parse_sms_regex(text: str) -> dict:
    result = {
        "type": None, "amount": None, "merchant": None,
        "account_last4": None, "raw": text.strip(),
        "confidence": 0.0,
    }
    if not text or len(text) < 8:
        return result

    m = AMOUNT_RE.search(text)
    if m:
        try:
            result["amount"] = float(m.group(1).replace(",", "").replace(" ", ""))
            result["confidence"] += 0.4
        except Exception:
            pass

    result["type"] = _detect_type(text)
    if result["type"]:
        result["confidence"] += 0.3

    m = ACC_LAST4_RE.search(text) or XX_LAST4_RE.search(text)
    if m:
        result["account_last4"] = m.group(1)
        result["confidence"] += 0.15

    merchant = _extract_merchant(text)
    if merchant:
        result["merchant"] = merchant
        result["confidence"] += 0.15
    return result


class SmsParseIn(BaseModel):
    text: str


@api.post("/sms/parse")
async def parse_sms(body: SmsParseIn, user=Depends(get_current_user)):
    text = (body.text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="Empty SMS")

    parsed = parse_sms_regex(text)

    # match account by last-4 digits in name
    account = None
    if parsed["account_last4"]:
        accs = await db.accounts.find(scope(user), {"_id": 0}).to_list(500)
        for a in accs:
            if parsed["account_last4"] in (a.get("name", "") + " " + a.get("note", "")):
                account = a
                break

    if not account:
        accs = await db.accounts.find(scope(user), {"_id": 0}).to_list(1)
        account = accs[0] if accs else None

    # If we couldn't extract essentials via regex, ask the LLM as a fallback
    llm_used = False
    if parsed["confidence"] < 0.5 or parsed["amount"] is None or not parsed["type"]:
        try:
            raw = await llm_json_call(
                system_msg=(
                    "You are an Indian bank/UPI SMS parser. Given raw SMS text, extract structured JSON. "
                    "Return ONLY a JSON object with keys: type ('income' or 'expense'), amount (number), "
                    "merchant (string, empty if unknown), account_last4 (4-digit string or empty). "
                    "No markdown, no code fences, JSON only."
                ),
                user_msg=f"Parse this SMS: {text}",
                session_id=f"sms-{user['id']}",
            )
            if raw:
                import json
                s = raw.strip()
                if s.startswith("```"):
                    s = s.strip("`")
                    if s.lower().startswith("json"):
                        s = s[4:].strip()
                i, j = s.find("{"), s.rfind("}")
                if i != -1 and j != -1:
                    data = json.loads(s[i:j + 1])
                    if not parsed["amount"] and data.get("amount"):
                        parsed["amount"] = float(data["amount"])
                    if not parsed["type"] and data.get("type"):
                        parsed["type"] = data["type"]
                    if not parsed["merchant"] and data.get("merchant"):
                        parsed["merchant"] = data["merchant"]
                    if not parsed["account_last4"] and data.get("account_last4"):
                        parsed["account_last4"] = data["account_last4"]
                    parsed["confidence"] = max(parsed["confidence"], 0.75)
                    llm_used = True
        except Exception as e:
            logger.warning("SMS LLM fallback failed: %s", e)

    txn_type = parsed["type"] or "expense"
    category = _guess_category(text, txn_type)

    return {
        "type": txn_type,
        "amount": parsed["amount"] or 0.0,
        "merchant": parsed["merchant"] or "",
        "account_last4": parsed["account_last4"] or "",
        "suggested_account_id": account["id"] if account else None,
        "suggested_account_name": account["name"] if account else None,
        "category": category,
        "note": (parsed["merchant"] or "SMS") + (f" · ...{parsed['account_last4']}" if parsed["account_last4"] else ""),
        "confidence": round(parsed["confidence"], 2),
        "llm_used": llm_used,
        "raw": text,
    }


# ----- Recurring -----
def _next_due(from_dt: datetime, frequency: str, day_of_month: Optional[int]) -> datetime:
    if frequency == "daily":
        return from_dt + timedelta(days=1)
    if frequency == "weekly":
        return from_dt + timedelta(days=7)
    year = from_dt.year
    month = from_dt.month + 1
    if month > 12:
        month = 1
        year += 1
    day = min(day_of_month or from_dt.day, 28)
    return from_dt.replace(year=year, month=month, day=day)


@api.get("/recurring")
async def list_recurring(user=Depends(get_current_user)):
    return await db.recurring.find(scope(user), {"_id": 0}).to_list(500)


@api.post("/recurring")
async def create_recurring(body: RecurringIn, user=Depends(get_current_user)):
    acc = await db.accounts.find_one({"id": body.account_id, "owner_id": user["current_ledger_id"]})
    if not acc:
        raise HTTPException(status_code=404, detail="Account not found")
    now = datetime.now(timezone.utc)
    start = now
    if body.start_date:
        try:
            start = datetime.fromisoformat(body.start_date.replace("Z", "+00:00"))
        except Exception:
            start = now
    doc = {
        "id": str(uuid.uuid4()),
        "user_id": user["id"],
        "owner_id": user["current_ledger_id"],
        "account_id": body.account_id,
        "account_name": acc["name"],
        "type": body.type,
        "amount": float(body.amount),
        "category": body.category,
        "note": body.note or "",
        "frequency": body.frequency,
        "day_of_month": body.day_of_month,
        "active": body.active,
        "start_date": start.isoformat(),
        "next_due": start.isoformat(),
        "last_run": None,
        "created_at": now.isoformat(),
    }
    await db.recurring.insert_one(doc)
    doc.pop("_id", None)
    return doc


@api.patch("/recurring/{rec_id}")
async def update_recurring(rec_id: str, body: dict, user=Depends(get_current_user)):
    allowed = {"active", "amount", "category", "note", "day_of_month", "frequency"}
    updates = {k: v for k, v in body.items() if k in allowed and v is not None}
    if updates:
        await db.recurring.update_one({"id": rec_id, "owner_id": user["current_ledger_id"]}, {"$set": updates})
    return {"ok": True}


@api.delete("/recurring/{rec_id}")
async def delete_recurring(rec_id: str, user=Depends(get_current_user)):
    await db.recurring.delete_one({"id": rec_id, "owner_id": user["current_ledger_id"]})
    return {"ok": True}


@api.post("/recurring/send-due-reminders")
async def send_due_reminders(request: Request):
    """System job — call this once a day from an external scheduler (Railway Cron / GitHub Action)
    with header X-Cron-Secret matching CRON_SECRET. Emails users about bills due in the next 2 days."""
    if not CRON_SECRET or request.headers.get("x-cron-secret") != CRON_SECRET:
        raise HTTPException(status_code=403, detail="Forbidden")

    now = datetime.now(timezone.utc)
    window_end = now + timedelta(days=2)
    upcoming = await db.recurring.find({"active": True}, {"_id": 0}).to_list(2000)
    sent = 0
    for r in upcoming:
        try:
            due = datetime.fromisoformat(r["next_due"].replace("Z", "+00:00"))
        except Exception:
            continue
        if now <= due <= window_end:
            user = await db.users.find_one({"id": r["user_id"]}, {"_id": 0})
            if not user:
                continue
            due_str = due.strftime("%d %b %Y")
            body_text = (
                f"Aapka '{r['category']}' bill ({r['account_name']}) ka amount "
                f"₹{r['amount']:.0f} hai, jo {due_str} ko due hai.\n\nTime pe pay karna na bhoolo!"
            )
            _send_simple_email(user["email"], f"⏰ Bill Reminder: {r['category']} due {due_str}",
                                "Bill Due Reminder 💰", body_text)
            sent += 1
    return {"reminders_sent": sent}



@api.post("/recurring/run")
async def run_recurring(user=Depends(get_current_user)):
    now = datetime.now(timezone.utc)
    created = 0
    rows = await db.recurring.find({"owner_id": user["current_ledger_id"], "active": True}, {"_id": 0}).to_list(500)
    for r in rows:
        try:
            due = datetime.fromisoformat(r["next_due"].replace("Z", "+00:00"))
        except Exception:
            continue
        for _ in range(24):
            if due > now:
                break
            await db.transactions.insert_one({
                "id": str(uuid.uuid4()),
                "user_id": user["id"],
                "owner_id": user["current_ledger_id"],
                "account_id": r["account_id"],
                "account_name": r["account_name"],
                "type": r["type"],
                "amount": r["amount"],
                "category": r["category"],
                "note": f"{r.get('note', '')} (recurring)".strip(),
                "date": due.isoformat(),
                "created_at": now.isoformat(),
                "recurring_id": r["id"],
            })
            created += 1
            due = _next_due(due, r["frequency"], r.get("day_of_month"))
        await db.recurring.update_one(
            {"id": r["id"], "owner_id": user["current_ledger_id"]},
            {"$set": {"next_due": due.isoformat(), "last_run": now.isoformat()}},
        )
    return {"created": created}


# ----- Budgets -----
@api.get("/budgets")
async def list_budgets(user=Depends(get_current_user)):
    rows = await db.budgets.find(scope(user), {"_id": 0}).to_list(200)
    now = datetime.now(timezone.utc)
    month_prefix = now.strftime("%Y-%m")
    spent_by_cat = {}
    txns = await db.transactions.find(
        {"owner_id": user["current_ledger_id"], "type": "expense"}, {"_id": 0}
    ).to_list(5000)
    for t in txns:
        d = t.get("date", "")
        if d.startswith(month_prefix):
            spent_by_cat[t["category"]] = spent_by_cat.get(t["category"], 0.0) + t["amount"]
    for b in rows:
        b["spent"] = round(spent_by_cat.get(b["category"], 0.0), 2)
        b["remaining"] = round(b["amount"] - b["spent"], 2)
        b["percent"] = round((b["spent"] / b["amount"] * 100) if b["amount"] > 0 else 0, 1)
    return rows


@api.post("/budgets")
async def upsert_budget(body: BudgetIn, user=Depends(get_current_user)):
    existing = await db.budgets.find_one({"owner_id": user["current_ledger_id"], "category": body.category})
    if existing:
        await db.budgets.update_one(
            {"id": existing["id"]},
            {"$set": {"amount": float(body.amount)}},
        )
        return {"ok": True, "id": existing["id"]}
    doc = {
        "id": str(uuid.uuid4()),
        "user_id": user["id"],
        "owner_id": user["current_ledger_id"],
        "category": body.category,
        "amount": float(body.amount),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    await db.budgets.insert_one(doc)
    return {"ok": True, "id": doc["id"]}


@api.delete("/budgets/{budget_id}")
async def delete_budget(budget_id: str, user=Depends(get_current_user)):
    await db.budgets.delete_one({"id": budget_id, "owner_id": user["current_ledger_id"]})
    return {"ok": True}


# ----- Analytics -----
@api.get("/analytics/summary")
async def analytics_summary(user=Depends(get_current_user)):
    pipeline = [
        {"$match": {"owner_id": user["current_ledger_id"]}},
        {"$group": {"_id": "$type", "total": {"$sum": "$amount"}}},
    ]
    agg = await db.transactions.aggregate(pipeline).to_list(10)
    income = sum(x["total"] for x in agg if x["_id"] == "income")
    expense = sum(x["total"] for x in agg if x["_id"] == "expense")

    accs = await db.accounts.find(scope(user), {"_id": 0}).to_list(500)
    total_balance = 0.0
    per_type = {}
    for a in accs:
        pipeline2 = [
            {"$match": {"owner_id": user["current_ledger_id"], "account_id": a["id"]}},
            {"$group": {"_id": "$type", "total": {"$sum": "$amount"}}},
        ]
        agg2 = await db.transactions.aggregate(pipeline2).to_list(10)
        inc = sum(x["total"] for x in agg2 if x["_id"] == "income")
        exp = sum(x["total"] for x in agg2 if x["_id"] == "expense")
        bal = round(a.get("opening_balance", 0.0) + inc - exp, 2)
        total_balance += bal
        per_type[a["type"]] = per_type.get(a["type"], 0.0) + bal

    ud = await db.udhaar.find({"owner_id": user["current_ledger_id"], "status": "pending"}, {"_id": 0}).to_list(500)
    lene = sum(x["amount"] for x in ud if x["type"] == "lene")
    dene = sum(x["amount"] for x in ud if x["type"] == "dene")

    pipeline3 = [
        {"$match": {"owner_id": user["current_ledger_id"], "type": "expense"}},
        {"$group": {"_id": "$category", "total": {"$sum": "$amount"}}},
        {"$sort": {"total": -1}},
    ]
    cats = await db.transactions.aggregate(pipeline3).to_list(50)
    categories = [{"category": c["_id"], "total": round(c["total"], 2)} for c in cats]

    return {
        "total_income": round(income, 2),
        "total_expense": round(expense, 2),
        "net_balance": round(total_balance, 2),
        "per_account_type": {k: round(v, 2) for k, v in per_type.items()},
        "udhaar_lene": round(lene, 2),
        "udhaar_dene": round(dene, 2),
        "expense_by_category": categories,
    }


@api.get("/analytics/monthly")
async def analytics_monthly(user=Depends(get_current_user)):
    rows = await db.transactions.find(scope(user), {"_id": 0}).to_list(5000)
    buckets = {}
    for r in rows:
        d = r.get("date", "")
        if not d:
            continue
        try:
            dt = datetime.fromisoformat(d.replace("Z", "+00:00"))
        except Exception:
            continue
        key = dt.strftime("%Y-%m")
        b = buckets.setdefault(key, {"month": key, "income": 0.0, "expense": 0.0})
        if r["type"] == "income":
            b["income"] += r["amount"]
        else:
            b["expense"] += r["amount"]
    out = sorted(buckets.values(), key=lambda x: x["month"])
    for b in out:
        b["income"] = round(b["income"], 2)
        b["expense"] = round(b["expense"], 2)
        b["savings"] = round(b["income"] - b["expense"], 2)
    return out[-12:]


# ----- AI Insights -----
@api.post("/ai/insights")
async def ai_insights(user=Depends(get_current_user)):
    pstatus = await _sync_premium_status(user)
    if not pstatus["premium_active"]:
        if not await _check_daily_free_limit(user, "aiInsightsCount", FREE_AI_INSIGHTS_DAILY_LIMIT):
            raise HTTPException(status_code=402, detail={
                "code": "PREMIUM_REQUIRED",
                "message": "Free plan allows 1 AI insight refresh a day. Upgrade to Premium for unlimited Advanced Reports.",
            })
    summary = await analytics_summary(user)
    monthly = await analytics_monthly(user)
    currency = user.get("currency", "INR")

    if summary["total_income"] == 0 and summary["total_expense"] == 0:
        return {
            "headline": "Abhi tak koi data nahi hai — pehla transaction add karo!",
            "summary": "Jaise hi aap kuch income ya kharcha add karenge, main aapko personalized insights aur savings tips dunga.",
            "tips": [
                "Pehle apne primary Savings aur Current accounts add karo.",
                "Har chhoti-badi kharcha turant record karo — habit ban jayegi.",
                "Udhaar lene/dene bhi Apka Munim mein daalte raho.",
            ],
        }

    context = {"currency": currency, "summary": summary, "monthly_trend": monthly}

    system_msg = (
        "You are a friendly Indian personal finance coach called 'Munim Ji'. "
        "Speak in warm Hinglish (Hindi + English mix). Be concise, practical and non-judgmental. "
        "Return ONLY a JSON object with keys: headline (string, max 90 chars), "
        "summary (string, 2-3 sentences), tips (array of 3-5 short actionable tips). "
        "No markdown, no code fences, only pure JSON."
    )

    try:
        raw = await llm_json_call(
            system_msg=system_msg,
            user_msg=f"Currency: {currency}. Data:\n{context}",
            session_id=f"insights-{user['id']}",
        )
        if not raw:
            raise RuntimeError("No LLM provider configured")

        import json
        text = raw.strip()
        if text.startswith("```"):
            text = text.strip("`")
            if text.lower().startswith("json"):
                text = text[4:].strip()
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1:
            text = text[start:end + 1]
        return json.loads(text)
    except Exception as e:
        logger.exception("AI insights failed: %s", e)
        net = summary["total_income"] - summary["total_expense"]
        top_cat = summary["expense_by_category"][0]["category"] if summary["expense_by_category"] else None
        tips = []
        if net < 0:
            tips.append("Aapke kharche income se zyada hain — is mahine budget tight karna padega.")
        else:
            tips.append(f"Shabaash! Aap {currency} {round(net, 2)} save kar chuke ho.")
        if top_cat:
            tips.append(f"Sabse zyada kharcha '{top_cat}' pe ho raha hai.")
        if summary["udhaar_lene"] > 0:
            tips.append(f"Logon se {currency} {summary['udhaar_lene']} lena hai — reminder bhej do.")
        if summary["udhaar_dene"] > 0:
            tips.append(f"Aapko {currency} {summary['udhaar_dene']} dena hai.")
        tips.append("Har hafte ek baar Apka Munim check karo.")
        return {
            "headline": "Aapka Financial Snapshot",
            "summary": f"Total income {currency} {summary['total_income']}, kharcha {currency} {summary['total_expense']}, net {currency} {round(net, 2)}.",
            "tips": tips[:5],
        }


# ----- Exports (CSV / PDF) -----
async def _month_transactions(user: dict, month: Optional[str]) -> tuple[list, str]:
    """Fetch transactions for a YYYY-MM month (default: current)."""
    if not month:
        month = datetime.now(timezone.utc).strftime("%Y-%m")
    rows = await db.transactions.find(scope(user), {"_id": 0}).sort("date", -1).to_list(5000)
    rows = [r for r in rows if r.get("date", "").startswith(month)]
    return rows, month


@api.get("/export/csv")
async def export_csv(month: Optional[str] = None, user=Depends(get_current_user)):
    rows, month = await _month_transactions(user, month)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Date", "Type", "Category", "Account", "Amount", "Note"])
    for r in rows:
        d = r.get("date", "")
        try:
            d = datetime.fromisoformat(d.replace("Z", "+00:00")).strftime("%Y-%m-%d")
        except Exception:
            pass
        w.writerow([d, r["type"], r["category"], r.get("account_name", ""), r["amount"], r.get("note", "")])
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="paisabook-{month}.csv"'},
    )


@api.get("/export/pdf")
async def export_pdf(month: Optional[str] = None, user=Depends(get_current_user)):
    pstatus = await _sync_premium_status(user)
    if not pstatus["premium_active"]:
        if not await _check_daily_free_limit(user, "pdfExportCount", FREE_PDF_EXPORT_DAILY_LIMIT):
            raise HTTPException(status_code=402, detail={
                "code": "PREMIUM_REQUIRED",
                "message": f"Free plan allows {FREE_PDF_EXPORT_DAILY_LIMIT} PDF exports a day. Upgrade to Premium for unlimited PDF Export.",
            })
    from reportlab.lib.pagesizes import A4
    from reportlab.lib import colors
    from reportlab.lib.units import cm
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.enums import TA_LEFT

    rows, month = await _month_transactions(user, month)
    summary = await analytics_summary(user)
    currency = user.get("currency", "INR")
    cur_sym = {"INR": "Rs.", "USD": "$", "EUR": "EUR ", "GBP": "GBP ", "AED": "AED "}.get(currency, "")
    ledger_name = user["current_ledger"]["name"]

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=1.6 * cm, rightMargin=1.6 * cm,
                            topMargin=1.6 * cm, bottomMargin=1.6 * cm, title=f"Apka Munim Report {month}")
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name="TitleBig", fontSize=22, leading=26, textColor=colors.HexColor("#2A4F4F"), spaceAfter=6))
    styles.add(ParagraphStyle(name="Sub", fontSize=10, textColor=colors.HexColor("#57534E"), spaceAfter=14))
    styles.add(ParagraphStyle(name="H2", fontSize=13, leading=16, textColor=colors.HexColor("#1C1917"), spaceBefore=8, spaceAfter=6))

    story = []
    story.append(Paragraph("Apka Munim", styles["TitleBig"]))
    story.append(Paragraph(f"Monthly Report &middot; {month} &middot; Ledger: {ledger_name}", styles["Sub"]))

    m_income = sum(r["amount"] for r in rows if r["type"] == "income")
    m_expense = sum(r["amount"] for r in rows if r["type"] == "expense")
    m_net = m_income - m_expense

    story.append(Paragraph("This Month Summary", styles["H2"]))
    st = Table(
        [["Income", "Expense", "Net"],
         [f"{cur_sym}{m_income:,.2f}", f"{cur_sym}{m_expense:,.2f}", f"{cur_sym}{m_net:,.2f}"]],
        colWidths=[5.5 * cm, 5.5 * cm, 5.5 * cm],
    )
    st.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#2A4F4F")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("PADDING", (0, 0), (-1, -1), 8),
        ("BACKGROUND", (0, 1), (0, 1), colors.HexColor("#EAF3EC")),
        ("BACKGROUND", (1, 1), (1, 1), colors.HexColor("#FAE9E3")),
        ("BACKGROUND", (2, 1), (2, 1), colors.HexColor("#FDF5E7")),
        ("FONTSIZE", (0, 1), (-1, 1), 14),
        ("FONTNAME", (0, 1), (-1, 1), "Helvetica-Bold"),
    ]))
    story.append(st)
    story.append(Spacer(1, 10))

    # Overall snapshot
    story.append(Paragraph("Overall Snapshot", styles["H2"]))
    ot = Table(
        [["Net Balance", "Udhaar Lene", "Udhaar Dene"],
         [f"{cur_sym}{summary['net_balance']:,.2f}",
          f"{cur_sym}{summary['udhaar_lene']:,.2f}",
          f"{cur_sym}{summary['udhaar_dene']:,.2f}"]],
        colWidths=[5.5 * cm, 5.5 * cm, 5.5 * cm],
    )
    ot.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#F2F0EA")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor("#57534E")),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("PADDING", (0, 0), (-1, -1), 8),
        ("FONTSIZE", (0, 1), (-1, 1), 12),
    ]))
    story.append(ot)
    story.append(Spacer(1, 12))

    # Transactions table
    story.append(Paragraph(f"Transactions ({len(rows)})", styles["H2"]))
    data = [["Date", "Type", "Category", "Account", "Amount"]]
    for r in rows[:200]:
        d = r.get("date", "")
        try:
            d = datetime.fromisoformat(d.replace("Z", "+00:00")).strftime("%d %b")
        except Exception:
            pass
        data.append([
            d,
            "Income" if r["type"] == "income" else "Expense",
            r.get("category", ""),
            r.get("account_name", "")[:20],
            f"{cur_sym}{r['amount']:,.2f}",
        ])
    if len(data) == 1:
        data.append(["-", "-", "No transactions", "-", "-"])
    tbl = Table(data, colWidths=[2.4 * cm, 2 * cm, 4 * cm, 4.6 * cm, 3.5 * cm], repeatRows=1)
    tbl.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#2A4F4F")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.HexColor("#FAF9F5"), colors.white]),
        ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#E7E5DF")),
        ("PADDING", (0, 0), (-1, -1), 5),
        ("ALIGN", (4, 1), (4, -1), "RIGHT"),
    ]))
    # colour expense rows
    for idx, r in enumerate(rows[:200], start=1):
        color = colors.HexColor("#B15039") if r["type"] == "expense" else colors.HexColor("#3B6446")
        tbl.setStyle(TableStyle([("TEXTCOLOR", (4, idx), (4, idx), color)]))
    story.append(tbl)

    story.append(Spacer(1, 12))
    story.append(Paragraph(
        f"<font color='#78716C'><i>Generated by Apka Munim on {datetime.now(timezone.utc).strftime('%d %b %Y')}</i></font>",
        styles["Normal"],
    ))

    doc.build(story)
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="paisabook-{month}.pdf"'},
    )


# ----- AI Chat (Munim Ji) — conversational -----
class ChatMessageIn(BaseModel):
    role: Literal["user", "assistant"]
    content: str


class ChatIn(BaseModel):
    messages: List[ChatMessageIn]


@api.post("/ai/chat")
async def ai_chat(body: ChatIn, user=Depends(get_current_user)):
    """Conversational chat with Munim Ji. Uses last 6 messages + user's financial context."""
    if not body.messages:
        raise HTTPException(status_code=400, detail="No messages")

    pstatus = await _sync_premium_status(user)
    if not pstatus["premium_active"]:
        if not await _check_daily_free_limit(user, "aiChatCount", FREE_AI_CHAT_DAILY_LIMIT):
            raise HTTPException(status_code=402, detail={
                "code": "PREMIUM_REQUIRED",
                "message": f"Free plan allows {FREE_AI_CHAT_DAILY_LIMIT} AI chats a day. Upgrade to Premium for unlimited AI Assistant.",
            })

    # Get user's financial snapshot for context
    summary = await analytics_summary(user)
    currency = user.get("currency", "INR")

    context_lines = [
        f"User's name: {user.get('name', 'friend')}",
        f"Currency: {currency}",
        f"This month income: {currency}{summary.get('total_income', 0):.0f}",
        f"This month expense: {currency}{summary.get('total_expense', 0):.0f}",
        f"Net balance: {currency}{summary.get('net_balance', 0):.0f}",
        f"Udhaar lena (to receive): {currency}{summary.get('udhaar_lene', 0):.0f}",
        f"Udhaar dena (to pay): {currency}{summary.get('udhaar_dene', 0):.0f}",
    ]
    top_cats = summary.get("expense_by_category", [])[:3]
    if top_cats:
        context_lines.append("Top expense categories: " + ", ".join(
            f"{c['category']} ({currency}{c['total']:.0f})" for c in top_cats
        ))
    context = "\n".join(context_lines)

    system_msg = (
        "You are 'Munim Ji' — a friendly, witty Indian personal finance advisor. "
        "You speak in warm Hinglish (Hindi mixed with English). Keep replies SHORT (2-4 sentences max) "
        "and conversational. Use emojis occasionally. Be practical, non-judgmental, encouraging. "
        "If user asks about their finances, use the CONTEXT below. If unclear, ask a clarifying question. "
        "If asked to add a transaction, tell them to use the 'Transaction' button (you cannot add for them). "
        "Never make up numbers not in context. Never give investment advice for specific stocks/funds. "
        f"\n\nCURRENT USER CONTEXT:\n{context}"
    )

    # Take last 6 messages (for token efficiency)
    recent = body.messages[-6:]
    user_msg_parts = []
    for m in recent[:-1]:
        prefix = "User: " if m.role == "user" else "Munim Ji: "
        user_msg_parts.append(f"{prefix}{m.content}")
    user_msg_parts.append(f"User: {recent[-1].content}")
    user_msg_parts.append("Munim Ji:")
    user_msg = "\n".join(user_msg_parts)

    try:
        reply = await llm_json_call(
            system_msg=system_msg,
            user_msg=user_msg,
            session_id=f"chat-{user['id']}",
        )
        if not reply:
            reply = "Bhai abhi thoda dimag out of order hai 😅 — thodi der baad try karo!"
        # Clean any markdown/quotes
        reply = reply.strip().strip('"').strip()
        if reply.startswith("Munim Ji:"):
            reply = reply[len("Munim Ji:"):].strip()
        return {"reply": reply}
    except Exception as e:
        logger.exception("AI chat failed: %s", e)
        # Fallback simple response
        last_user = recent[-1].content.lower()
        if "kharcha" in last_user or "expense" in last_user:
            fallback = f"Iss mahine total kharcha {currency}{summary.get('total_expense', 0):.0f} hua hai. Kya specific poochna hai?"
        elif "income" in last_user or "aaya" in last_user or "salary" in last_user:
            fallback = f"Iss mahine {currency}{summary.get('total_income', 0):.0f} aayi hai. Savings kaisi chal rahi hai?"
        elif "udhaar" in last_user:
            fallback = f"Aapko {currency}{summary.get('udhaar_lene', 0):.0f} lene hain aur {currency}{summary.get('udhaar_dene', 0):.0f} dene hain."
        else:
            fallback = "Namaste! Main Munim Ji. Aap finance ke baare me kuch bhi puch sakte ho — kharcha, savings, udhaar, budget."
        return {"reply": fallback, "fallback": True}


# ----- Financial Goals (Sapno ka Wallet) -----
class GoalIn(BaseModel):
    name: str
    target_amount: float
    saved_amount: float = 0.0
    target_date: Optional[str] = None
    emoji: str = "🎯"
    color: str = "#4A7C59"
    account_id: Optional[str] = None


class GoalUpdate(BaseModel):
    name: Optional[str] = None
    target_amount: Optional[float] = None
    saved_amount: Optional[float] = None
    target_date: Optional[str] = None
    emoji: Optional[str] = None
    color: Optional[str] = None
    account_id: Optional[str] = None


@api.get("/goals")
async def list_goals(user=Depends(get_current_user)):
    rows = await db.goals.find(scope(user), {"_id": 0}).sort("created_at", -1).to_list(500)
    # Enrich each goal with savings breakdown
    now = datetime.now(timezone.utc).date()
    for g in rows:
        target_amt = float(g.get("target_amount", 0))
        saved_amt = float(g.get("saved_amount", 0))
        remaining = max(0, target_amt - saved_amt)
        pct = round((saved_amt / target_amt * 100) if target_amt > 0 else 0, 1)

        breakdown = {
            "remaining": round(remaining, 2),
            "percent": pct,
            "days_left": None,
            "per_day": None,
            "per_week": None,
            "per_month": None,
            "status": "on_track",
        }

        target_date_str = g.get("target_date")
        if target_date_str and remaining > 0:
            try:
                tgt = datetime.strptime(target_date_str, "%Y-%m-%d").date()
                days = (tgt - now).days
                if days > 0:
                    breakdown["days_left"] = days
                    breakdown["per_day"] = round(remaining / days, 2)
                    breakdown["per_week"] = round(remaining / (days / 7), 2)
                    breakdown["per_month"] = round(remaining / (days / 30.44), 2)
                    if days < 30:
                        breakdown["status"] = "urgent"
                    elif days < 90:
                        breakdown["status"] = "soon"
                else:
                    breakdown["status"] = "overdue"
                    breakdown["days_left"] = 0
            except Exception:
                pass
        elif remaining == 0:
            breakdown["status"] = "achieved"

        g["breakdown"] = breakdown
    return rows


@api.post("/goals")
async def create_goal(body: GoalIn, user=Depends(get_current_user)):
    doc = {
        "id": str(uuid.uuid4()),
        "owner_id": user["current_ledger_id"],
        "user_id": user["id"],
        "name": body.name,
        "target_amount": float(body.target_amount),
        "saved_amount": float(body.saved_amount or 0.0),
        "target_date": body.target_date,
        "emoji": body.emoji or "🎯",
        "color": body.color or "#4A7C59",
        "account_id": body.account_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    await db.goals.insert_one(doc)
    doc.pop("_id", None)
    return doc


@api.patch("/goals/{goal_id}")
async def update_goal(goal_id: str, body: GoalUpdate, user=Depends(get_current_user)):
    updates = {k: v for k, v in body.model_dump(exclude_unset=True).items() if v is not None}
    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update")
    res = await db.goals.update_one({"id": goal_id, **scope(user)}, {"$set": updates})
    if res.matched_count == 0:
        raise HTTPException(status_code=404, detail="Goal not found")
    doc = await db.goals.find_one({"id": goal_id}, {"_id": 0})
    return doc


@api.delete("/goals/{goal_id}")
async def delete_goal(goal_id: str, user=Depends(get_current_user)):
    res = await db.goals.delete_one({"id": goal_id, **scope(user)})
    if res.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Goal not found")
    return {"ok": True}


@api.post("/goals/{goal_id}/contribute")
async def contribute_to_goal(goal_id: str, amount: float = Query(gt=0), user=Depends(get_current_user)):
    goal = await db.goals.find_one({"id": goal_id, **scope(user)})
    if not goal:
        raise HTTPException(status_code=404, detail="Goal not found")
    new_saved = float(goal.get("saved_amount", 0)) + float(amount)
    await db.goals.update_one({"id": goal_id}, {"$set": {"saved_amount": new_saved}})
    return {"ok": True, "saved_amount": new_saved, "target_amount": goal["target_amount"]}


# ----- Subscription Tracker -----
class SubscriptionIn(BaseModel):
    name: str
    amount: float = Field(gt=0)
    category: str = "Entertainment"
    billing_cycle: Literal["monthly", "quarterly", "yearly", "weekly"] = "monthly"
    next_billing_date: Optional[str] = None
    account_id: Optional[str] = None
    emoji: str = "💳"
    color: str = "#D96C52"
    active: bool = True
    website: Optional[str] = ""


class SubscriptionUpdate(BaseModel):
    name: Optional[str] = None
    amount: Optional[float] = None
    category: Optional[str] = None
    billing_cycle: Optional[Literal["monthly", "quarterly", "yearly", "weekly"]] = None
    next_billing_date: Optional[str] = None
    account_id: Optional[str] = None
    emoji: Optional[str] = None
    color: Optional[str] = None
    active: Optional[bool] = None
    website: Optional[str] = None


@api.get("/subscriptions")
async def list_subscriptions(user=Depends(get_current_user)):
    rows = await db.subscriptions.find(scope(user), {"_id": 0}).sort("next_billing_date", 1).to_list(500)
    # compute monthly total
    monthly_total = 0.0
    for r in rows:
        if not r.get("active"):
            continue
        amt = float(r.get("amount", 0))
        cycle = r.get("billing_cycle", "monthly")
        if cycle == "monthly":
            monthly_total += amt
        elif cycle == "yearly":
            monthly_total += amt / 12
        elif cycle == "quarterly":
            monthly_total += amt / 3
        elif cycle == "weekly":
            monthly_total += amt * 4.33
    return {"subscriptions": rows, "monthly_total": round(monthly_total, 2)}


@api.post("/subscriptions")
async def create_subscription(body: SubscriptionIn, user=Depends(get_current_user)):
    doc = {
        "id": str(uuid.uuid4()),
        "owner_id": user["current_ledger_id"],
        "user_id": user["id"],
        **body.model_dump(),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    await db.subscriptions.insert_one(doc)
    doc.pop("_id", None)
    return doc


@api.patch("/subscriptions/{sub_id}")
async def update_subscription(sub_id: str, body: SubscriptionUpdate, user=Depends(get_current_user)):
    updates = {k: v for k, v in body.model_dump(exclude_unset=True).items() if v is not None}
    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update")
    res = await db.subscriptions.update_one({"id": sub_id, **scope(user)}, {"$set": updates})
    if res.matched_count == 0:
        raise HTTPException(status_code=404, detail="Subscription not found")
    doc = await db.subscriptions.find_one({"id": sub_id}, {"_id": 0})
    return doc


@api.delete("/subscriptions/{sub_id}")
async def delete_subscription(sub_id: str, user=Depends(get_current_user)):
    res = await db.subscriptions.delete_one({"id": sub_id, **scope(user)})
    if res.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Subscription not found")
    return {"ok": True}


# ----- Financial Health Score -----
@api.get("/analytics/health-score")
async def health_score(user=Depends(get_current_user)):
    """Calculate 0-100 financial health score based on:
    - Savings rate (40%)
    - Budget adherence (25%)
    - Udhaar balance (15%)
    - Diversification (10%)
    - Activity/tracking (10%)
    """
    now = datetime.now(timezone.utc)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat()

    # Get this month's txns
    txns = await db.transactions.find(
        {**scope(user), "date": {"$gte": month_start}}, {"_id": 0}
    ).to_list(5000)
    total_income = sum(t["amount"] for t in txns if t["type"] == "income")
    total_expense = sum(t["amount"] for t in txns if t["type"] == "expense")
    savings = total_income - total_expense
    savings_rate = (savings / total_income * 100) if total_income > 0 else 0

    # Score components
    scores = {}

    # 1. Savings rate (40 pts) — ideally save >20%
    if savings_rate >= 30:
        scores["savings"] = 40
    elif savings_rate >= 20:
        scores["savings"] = 32
    elif savings_rate >= 10:
        scores["savings"] = 20
    elif savings_rate >= 0:
        scores["savings"] = 10
    else:
        scores["savings"] = 0

    # 2. Budget adherence (25 pts)
    budgets = await db.budgets.find(scope(user), {"_id": 0}).to_list(200)
    if budgets:
        breach_count = 0
        for b in budgets:
            spent = sum(t["amount"] for t in txns if t["type"] == "expense" and t["category"] == b["category"])
            if spent > b["amount"]:
                breach_count += 1
        adherence = (len(budgets) - breach_count) / len(budgets)
        scores["budget"] = int(adherence * 25)
    else:
        scores["budget"] = 12  # partial credit for no budgets yet

    # 3. Udhaar balance (15 pts) — less pending is better
    udhaars = await db.udhaar.find({**scope(user), "status": "pending"}, {"_id": 0}).to_list(200)
    dene_amt = sum(u["amount"] for u in udhaars if u["type"] == "dene")
    lene_amt = sum(u["amount"] for u in udhaars if u["type"] == "lene")
    net_udhaar = dene_amt - lene_amt  # positive = more to give, negative = more to receive
    if total_income > 0:
        udhaar_ratio = abs(net_udhaar) / total_income
        if udhaar_ratio < 0.1:
            scores["udhaar"] = 15
        elif udhaar_ratio < 0.3:
            scores["udhaar"] = 10
        elif udhaar_ratio < 0.5:
            scores["udhaar"] = 5
        else:
            scores["udhaar"] = 0
    else:
        scores["udhaar"] = 10

    # 4. Diversification (10 pts) — multiple accounts
    accounts = await db.accounts.find(scope(user), {"_id": 0}).to_list(50)
    if len(accounts) >= 3:
        scores["diversification"] = 10
    elif len(accounts) == 2:
        scores["diversification"] = 7
    elif len(accounts) == 1:
        scores["diversification"] = 4
    else:
        scores["diversification"] = 0

    # 5. Activity/tracking (10 pts) — txns this month
    if len(txns) >= 20:
        scores["activity"] = 10
    elif len(txns) >= 10:
        scores["activity"] = 7
    elif len(txns) >= 5:
        scores["activity"] = 4
    else:
        scores["activity"] = 1

    total_score = sum(scores.values())

    # Grade & Motto
    if total_score >= 85:
        grade = "A+"
        motto = "Paisa ka Baadshah 👑"
        message = "Bhai tum toh Ambani ban rahe ho — ekdum solid financial habits!"
    elif total_score >= 70:
        grade = "A"
        motto = "Money Master 💪"
        message = "Wah bhai wah! Financial planning ekdum sahi track pe hai."
    elif total_score >= 55:
        grade = "B"
        motto = "Sudhaar Chahiye 📈"
        message = "Achha kar rahe ho, but thoda aur bachao — future ka sochke."
    elif total_score >= 40:
        grade = "C"
        motto = "Kharcha King 💸"
        message = "Bhai kharcha kam karo — budget follow karne se score improve hoga."
    else:
        grade = "D"
        motto = "Munim Ji ki Zaroorat 😅"
        message = "Bhai ekdum se hisab-kitab shuru karo, budget banao — abhi improve karne ka time hai!"

    return {
        "score": total_score,
        "grade": grade,
        "motto": motto,
        "message": message,
        "breakdown": scores,
        "stats": {
            "total_income": round(total_income, 2),
            "total_expense": round(total_expense, 2),
            "savings": round(savings, 2),
            "savings_rate": round(savings_rate, 1),
            "transaction_count": len(txns),
            "accounts_count": len(accounts),
            "pending_udhaar_dene": round(dene_amt, 2),
            "pending_udhaar_lene": round(lene_amt, 2),
        },
    }


# ----- Streaks -----
@api.get("/analytics/badges")
async def get_badges(user=Depends(get_current_user)):
    """Achievement badges computed from existing activity — no new data needed."""
    txn_count = await db.transactions.count_documents(scope(user))
    goals = await db.goals.find(scope(user), {"_id": 0}).to_list(500)
    completed_goals = sum(1 for g in goals if g.get("saved_amount", 0) >= g.get("target_amount", 0) and g.get("target_amount", 0) > 0)
    udhaar_settled = await db.udhaar.count_documents({"owner_id": user["current_ledger_id"], "status": "settled"})
    streak_data = await get_streak(user=user)
    longest_streak = streak_data.get("longest_streak", 0)
    budgets_count = await db.budgets.count_documents({"owner_id": user["current_ledger_id"]})

    badges = [
        {"id": "first_step", "emoji": "👣", "name": "Pehla Kadam", "desc": "Pehla transaction add kiya",
         "earned": txn_count >= 1},
        {"id": "century", "emoji": "💯", "name": "Century", "desc": "100 transactions track kiye",
         "earned": txn_count >= 100},
        {"id": "streak_7", "emoji": "🔥", "name": "1 Hafta Streak", "desc": "7 din lagatar tracking",
         "earned": longest_streak >= 7},
        {"id": "streak_30", "emoji": "🏆", "name": "Consistency King", "desc": "30 din lagatar tracking",
         "earned": longest_streak >= 30},
        {"id": "goal_getter", "emoji": "🎯", "name": "Goal Getter", "desc": "Pehla saving goal poora kiya",
         "earned": completed_goals >= 1},
        {"id": "clean_slate", "emoji": "🤝", "name": "Clean Slate", "desc": "Ek udhaar settle kiya",
         "earned": udhaar_settled >= 1},
        {"id": "planner", "emoji": "📋", "name": "Planner", "desc": "Pehla budget set kiya",
         "earned": budgets_count >= 1},
    ]
    return {"badges": badges, "earned_count": sum(1 for b in badges if b["earned"]), "total_count": len(badges)}



@api.get("/analytics/streak")
async def get_streak(user=Depends(get_current_user)):
    """Calculate current tracking streak (consecutive days with at least one transaction)"""
    txns = await db.transactions.find(
        scope(user), {"_id": 0, "date": 1}
    ).sort("date", -1).to_list(500)

    if not txns:
        return {"current_streak": 0, "longest_streak": 0, "today_tracked": False, "message": "Aaj kuch add karo — streak shuru karo!"}

    # Get unique dates (YYYY-MM-DD)
    dates = set()
    for t in txns:
        d = t.get("date", "")
        if d:
            dates.add(d[:10])
    dates_sorted = sorted(dates, reverse=True)

    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    yesterday_str = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")

    today_tracked = today_str in dates
    # Streak counts from today (if tracked) or yesterday (still active)
    if today_str in dates:
        current_date = datetime.now(timezone.utc)
    elif yesterday_str in dates:
        current_date = datetime.now(timezone.utc) - timedelta(days=1)
    else:
        return {"current_streak": 0, "longest_streak": 0, "today_tracked": False,
                "message": "Streak toot gayi! Aaj se dobara shuru karo."}

    current_streak = 0
    while current_date.strftime("%Y-%m-%d") in dates:
        current_streak += 1
        current_date -= timedelta(days=1)

    # Longest streak
    longest_streak = 0
    temp_streak = 0
    prev_date = None
    for d_str in sorted(dates):
        d = datetime.strptime(d_str, "%Y-%m-%d")
        if prev_date is None or (d - prev_date).days == 1:
            temp_streak += 1
        else:
            temp_streak = 1
        longest_streak = max(longest_streak, temp_streak)
        prev_date = d

    # Fun message
    if current_streak >= 30:
        msg = f"🔥 {current_streak} din streak! Tum toh Money Master ho gaye!"
    elif current_streak >= 7:
        msg = f"🔥 {current_streak} din straight — kamaal ka discipline!"
    elif current_streak >= 3:
        msg = f"🎯 {current_streak} din streak — keep going bhai!"
    elif current_streak >= 1:
        msg = f"✨ Streak shuru! Kal bhi track karna mat bhoolna."
    else:
        msg = "Aaj kuch add karo — streak shuru karo!"

    return {
        "current_streak": current_streak,
        "longest_streak": longest_streak,
        "today_tracked": today_tracked,
        "message": msg,
    }


# ----- Meme Alerts (Fun Notifications) -----
@api.get("/analytics/vibe-check")
async def vibe_check(user=Depends(get_current_user)):
    """Return a funny/motivational one-liner based on current state"""
    import random
    now = datetime.now(timezone.utc)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat()

    txns = await db.transactions.find(
        {**scope(user), "date": {"$gte": month_start}}, {"_id": 0}
    ).to_list(2000)
    total_income = sum(t["amount"] for t in txns if t["type"] == "income")
    total_expense = sum(t["amount"] for t in txns if t["type"] == "expense")

    # Category-specific memes
    cat_totals = {}
    for t in txns:
        if t["type"] == "expense":
            cat_totals[t["category"]] = cat_totals.get(t["category"], 0) + t["amount"]
    top_cat = max(cat_totals.items(), key=lambda x: x[1]) if cat_totals else None

    memes = []

    # Balance-based memes
    if total_income == 0 and total_expense == 0:
        memes = [
            {"emoji": "😴", "text": "Iss mahine kuch bhi track nahi kiya. Munim Ji so gaya hai."},
            {"emoji": "🤔", "text": "Bhai kharcha kaha kar rahe ho? App ko batao!"},
        ]
    elif total_expense > total_income:
        memes = [
            {"emoji": "😱", "text": "Kharcha income se zyada! Warren Buffet ne kuch aur socha hoga."},
            {"emoji": "💸", "text": "Paisa udd raha hai — udhaar lene ki nobat na aa jaye."},
            {"emoji": "🚨", "text": "RED ALERT: Kharcha > Income. Kuch to gadbad hai."},
        ]
    elif total_income > 0 and (total_income - total_expense) / total_income > 0.3:
        memes = [
            {"emoji": "👑", "text": "30%+ bachat! Ambani beta ban rahe ho."},
            {"emoji": "🎉", "text": "Great savings this month — Munim Ji proud hai!"},
            {"emoji": "💰", "text": "Solid bachat — SIP shuru karo, karod pati ban jao."},
        ]
    else:
        memes = [
            {"emoji": "😊", "text": "Chal raha hai... but aur bachao yaar."},
            {"emoji": "📊", "text": "Steady kharcha — but savings rate improve karna hai."},
        ]

    # Category-specific meme
    if top_cat:
        cat, amt = top_cat
        if cat.lower() == "food" and amt > 5000:
            memes.append({"emoji": "🍕", "text": f"Food pe ₹{int(amt)}! Kitchen kis din se band hai?"})
        if cat.lower() == "shopping" and amt > 3000:
            memes.append({"emoji": "🛍️", "text": f"Shopping pe ₹{int(amt)}! Amazon ke shareholder ban rahe ho."})
        if cat.lower() == "entertainment" and amt > 2000:
            memes.append({"emoji": "🎬", "text": f"Entertainment pe ₹{int(amt)}! Netflix + Prime + Hotstar sab liya hai kya?"})
        if cat.lower() == "transport" and amt > 4000:
            memes.append({"emoji": "🚗", "text": f"Transport ₹{int(amt)} — Uber ka VIP customer banoge."})

    chosen = random.choice(memes) if memes else {"emoji": "💡", "text": "Track your money, master your life!"}
    return chosen


# ----- Voice Parse (accepts spoken transaction, returns structured) -----
class VoiceParseIn(BaseModel):
    text: str


@api.post("/voice/parse-transaction")
async def voice_parse_transaction(body: VoiceParseIn, user=Depends(get_current_user)):
    """
    Parse spoken/typed text like "500 rupaye zomato pe" into a transaction dict.
    Uses regex first, LLM as fallback.
    """
    import re
    text = body.text.strip().lower()
    if not text:
        raise HTTPException(status_code=400, detail="Empty text")

    # Regex: try to extract amount
    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:rupaye|rupees|rs\.?|₹|inr)?", text)
    amount = float(m.group(1)) if m else None

    # Detect income keywords
    income_kw = ["mila", "aaya", "salary", "earned", "received", "credit", "income", "bonus"]
    is_income = any(k in text for k in income_kw)

    # Category detection (simple keyword mapping)
    cat_map = {
        "Food": ["zomato", "swiggy", "dominos", "khana", "food", "restaurant", "cafe", "bhojan", "lunch", "dinner", "breakfast"],
        "Groceries": ["dmart", "bigbasket", "grocery", "sabzi", "vegetable", "kirana"],
        "Transport": ["uber", "ola", "petrol", "diesel", "cab", "auto", "rickshaw", "metro", "bus", "train"],
        "Shopping": ["amazon", "flipkart", "myntra", "shopping", "kapde", "clothes", "meesho"],
        "Bills": ["bill", "electricity", "recharge", "airtel", "jio", "vi", "gas", "water", "internet"],
        "Entertainment": ["netflix", "prime", "hotstar", "movie", "cinema", "spotify"],
        "Health": ["medicine", "doctor", "hospital", "pharmacy", "medical"],
        "Salary": ["salary", "vetan", "tankha"],
    }
    detected_cat = None
    for cat, keywords in cat_map.items():
        if any(k in text for k in keywords):
            detected_cat = cat
            break

    if not detected_cat:
        detected_cat = "Other" if not is_income else "Other Income"

    if amount is None:
        # Try LLM fallback
        llm_prompt = (
            "Extract transaction details from this Hinglish text. Return ONLY valid JSON with keys: "
            "amount (number), type ('income' or 'expense'), category, merchant, note. "
            f"Text: {body.text}"
        )
        try:
            out = await llm_json_call(
                system_msg="You are a finance transaction parser. Return only JSON.",
                user_msg=llm_prompt,
                session_id=f"voice-{user['id']}",
            )
            if out:
                import json as _json
                # extract JSON block
                jm = re.search(r"\{[\s\S]*\}", out)
                if jm:
                    parsed = _json.loads(jm.group(0))
                    return {
                        "amount": float(parsed.get("amount") or 0),
                        "type": parsed.get("type", "expense"),
                        "category": parsed.get("category", "Other"),
                        "note": parsed.get("note") or parsed.get("merchant", ""),
                        "confidence": "llm",
                    }
        except Exception as e:
            logging.warning("voice LLM parse failed: %s", e)
        raise HTTPException(status_code=400, detail="Amount detect nahi hua. Fir se boliye ya type kariye.")

    # Extract note (words minus category keywords)
    note = body.text
    return {
        "amount": amount,
        "type": "income" if is_income else "expense",
        "category": detected_cat,
        "note": note,
        "confidence": "regex",
    }


# =========================
# ===== v13: 5 New Features
# =========================

# ----- What-If Simulator -----
class WhatIfIn(BaseModel):
    reduce_category: Optional[str] = None
    reduce_amount_monthly: float = 0
    goal_id: Optional[str] = None


@api.post("/whatif/simulate")
async def whatif_simulate(body: WhatIfIn, user=Depends(get_current_user)):
    """Simulate: agar main X category ka Y/month kam karu to 1yr/goal completion mein kitna asar."""
    monthly = max(0.0, float(body.reduce_amount_monthly or 0))
    yearly = monthly * 12
    five_year = monthly * 60

    goal_impact = None
    if body.goal_id:
        g = await db.goals.find_one({"id": body.goal_id, "owner_id": user["current_ledger_id"]}, {"_id": 0})
        if g:
            target = float(g.get("target_amount") or 0)
            saved = float(g.get("saved_amount") or 0)
            remaining = max(0.0, target - saved)
            months_faster = 0
            if monthly > 0 and remaining > 0:
                months_faster = int(remaining / monthly)
            goal_impact = {
                "goal_name": g.get("name"),
                "remaining": remaining,
                "months_faster_to_complete": months_faster,
                "would_complete_by": (datetime.now(timezone.utc) + timedelta(days=months_faster * 30)).date().isoformat() if months_faster > 0 else None,
            }

    # Current category avg
    current_monthly_avg = None
    if body.reduce_category:
        pipeline = [
            {"$match": {"owner_id": user["current_ledger_id"], "category": body.reduce_category, "type": "expense"}},
            {"$group": {"_id": None, "total": {"$sum": "$amount"}, "count": {"$sum": 1}}},
        ]
        docs = await db.transactions.aggregate(pipeline).to_list(1)
        if docs and docs[0].get("total"):
            # Divide by months elapsed (min 1)
            oldest = await db.transactions.find(
                {"owner_id": user["current_ledger_id"], "category": body.reduce_category, "type": "expense"},
                {"date": 1, "_id": 0}
            ).sort("date", 1).limit(1).to_list(1)
            months_span = 1
            if oldest:
                try:
                    d = oldest[0]["date"]
                    if isinstance(d, str):
                        d = datetime.fromisoformat(d.replace("Z", "+00:00"))
                    delta_days = max(1, (datetime.now(timezone.utc) - d).days)
                    months_span = max(1, delta_days / 30)
                except Exception:
                    months_span = 1
            current_monthly_avg = round(docs[0]["total"] / months_span, 2)

    return {
        "reduce_category": body.reduce_category,
        "reduce_amount_monthly": monthly,
        "current_monthly_avg": current_monthly_avg,
        "yearly_savings": yearly,
        "five_year_savings": five_year,
        "goal_impact": goal_impact,
    }


# ----- Category Auto-Suggestion (rule-based learning) -----
@api.get("/categories/suggest")
async def suggest_category(q: str = "", user=Depends(get_current_user)):
    """Returns top 3 categories user has used before for similar note keywords."""
    q = (q or "").strip().lower()
    if len(q) < 2:
        return {"suggestions": []}

    import re
    safe = re.escape(q)
    rx = {"$regex": safe, "$options": "i"}

    # Aggregate: find transactions where note or category contains query, group by category, sort by count
    pipeline = [
        {"$match": {"owner_id": user["current_ledger_id"], "$or": [{"note": rx}, {"category": rx}]}},
        {"$group": {"_id": "$category", "count": {"$sum": 1}, "last_used": {"$max": "$date"}}},
        {"$sort": {"count": -1, "last_used": -1}},
        {"$limit": 3},
    ]
    docs = await db.transactions.aggregate(pipeline).to_list(3)
    suggestions = [{"category": d["_id"], "count": d["count"]} for d in docs if d.get("_id")]
    return {"suggestions": suggestions}


# ----- Emergency Fund Health Check -----
@api.get("/analytics/emergency-fund")
async def emergency_fund_health(user=Depends(get_current_user)):
    """Calculate months of expense coverage from savings + emergency accounts."""
    # Sum balances of savings + emergency accounts
    accounts = await db.accounts.find(
        {"owner_id": user["current_ledger_id"], "type": {"$in": ["savings", "emergency"]}},
        {"_id": 0}
    ).to_list(200)
    total_saved = sum(float(a.get("balance") or 0) for a in accounts)

    # Avg monthly expense over last 3 months
    three_months_ago = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()
    pipeline = [
        {"$match": {"owner_id": user["current_ledger_id"], "type": "expense", "date": {"$gte": three_months_ago}}},
        {"$group": {"_id": None, "total": {"$sum": "$amount"}}},
    ]
    docs = await db.transactions.aggregate(pipeline).to_list(1)
    total_expense_3mo = float(docs[0]["total"]) if docs else 0.0
    avg_monthly_expense = round(total_expense_3mo / 3, 2) if total_expense_3mo > 0 else 0

    months_covered = 0
    if avg_monthly_expense > 0:
        months_covered = round(total_saved / avg_monthly_expense, 1)

    ideal_months = 6
    ideal_fund = round(avg_monthly_expense * ideal_months, 0)
    shortfall = max(0, ideal_fund - total_saved)

    # Status
    if months_covered >= 6:
        status = "excellent"
        message = f"Bhai zabardast! Aapke paas {months_covered} mahine ka emergency fund hai. Peace of mind! 🎉"
    elif months_covered >= 3:
        status = "good"
        message = f"{months_covered} mahine cover ho jaate hain. Ideal 6 mahine hai — ₹{int(shortfall):,} aur bacha lo."
    elif months_covered >= 1:
        status = "warning"
        message = f"Sirf {months_covered} mahine ka fund hai. Emergency mein tight ho jayega — target 3-6 mahine karo."
    else:
        status = "critical"
        message = "Bhai emergency fund almost zero hai! Chhota sa bhi shuru karo — ₹1000/mahina bhi kaafi hai starting mein."

    return {
        "total_saved": total_saved,
        "avg_monthly_expense": avg_monthly_expense,
        "months_covered": months_covered,
        "ideal_months": ideal_months,
        "ideal_fund_amount": ideal_fund,
        "shortfall": shortfall,
        "status": status,
        "message": message,
    }


# ----- Warranty & Bill Vault -----
class WarrantyIn(BaseModel):
    item_name: str
    category: Optional[str] = "Electronics"
    purchase_date: str
    warranty_months: int = 12
    amount: Optional[float] = 0
    store: Optional[str] = None
    note: Optional[str] = None
    receipt_image: Optional[str] = None  # base64 data URL


@api.get("/warranties")
async def list_warranties(user=Depends(get_current_user)):
    rows = await db.warranties.find({"owner_id": user["current_ledger_id"]}, {"_id": 0}) \
        .sort("purchase_date", -1).to_list(200)
    # Compute days_left for each
    today = datetime.now(timezone.utc).date()
    for w in rows:
        try:
            pd = datetime.fromisoformat(w["purchase_date"]).date() if isinstance(w["purchase_date"], str) else w["purchase_date"]
            expiry = pd + timedelta(days=int(w.get("warranty_months", 12)) * 30)
            w["expiry_date"] = expiry.isoformat()
            w["days_left"] = (expiry - today).days
            w["status"] = "active" if w["days_left"] > 30 else ("expiring" if w["days_left"] > 0 else "expired")
        except Exception:
            w["expiry_date"] = None
            w["days_left"] = None
            w["status"] = "unknown"
    return rows


@api.post("/warranties")
async def create_warranty(body: WarrantyIn, user=Depends(get_current_user)):
    w_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    doc = {
        "id": w_id,
        "owner_id": user["current_ledger_id"],
        "item_name": body.item_name,
        "category": body.category or "Electronics",
        "purchase_date": body.purchase_date,
        "warranty_months": int(body.warranty_months or 12),
        "amount": float(body.amount or 0),
        "store": body.store,
        "note": body.note,
        "receipt_image": body.receipt_image,
        "created_at": now,
    }
    await db.warranties.insert_one(doc)
    doc.pop("_id", None)
    return doc


@api.patch("/warranties/{warranty_id}")
async def update_warranty(warranty_id: str, body: WarrantyIn, user=Depends(get_current_user)):
    updates = {k: v for k, v in body.dict().items() if v is not None}
    await db.warranties.update_one(
        {"id": warranty_id, "owner_id": user["current_ledger_id"]},
        {"$set": updates}
    )
    return {"ok": True}


@api.delete("/warranties/{warranty_id}")
async def delete_warranty(warranty_id: str, user=Depends(get_current_user)):
    await db.warranties.delete_one({"id": warranty_id, "owner_id": user["current_ledger_id"]})
    return {"ok": True}


# ----- Kids Pocket Money Tracker -----
class KidIn(BaseModel):
    name: str
    emoji: Optional[str] = "🧒"
    monthly_allowance: float = 0
    balance: float = 0


class KidEntryIn(BaseModel):
    kid_id: str
    type: str  # "allowance" | "spend" | "save"
    amount: float = Field(gt=0)
    note: Optional[str] = None
    category: Optional[str] = None


@api.get("/kids")
async def list_kids(user=Depends(get_current_user)):
    return await db.kids.find({"owner_id": user["current_ledger_id"]}, {"_id": 0}) \
        .sort("created_at", -1).to_list(50)


@api.post("/kids")
async def create_kid(body: KidIn, user=Depends(get_current_user)):
    kid_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    doc = {
        "id": kid_id,
        "owner_id": user["current_ledger_id"],
        "name": body.name,
        "emoji": body.emoji or "🧒",
        "monthly_allowance": float(body.monthly_allowance or 0),
        "balance": float(body.balance or 0),
        "total_saved": 0.0,
        "total_spent": 0.0,
        "created_at": now,
    }
    await db.kids.insert_one(doc)
    doc.pop("_id", None)
    return doc


@api.delete("/kids/{kid_id}")
async def delete_kid(kid_id: str, user=Depends(get_current_user)):
    await db.kids.delete_one({"id": kid_id, "owner_id": user["current_ledger_id"]})
    await db.kid_entries.delete_many({"kid_id": kid_id, "owner_id": user["current_ledger_id"]})
    return {"ok": True}


@api.get("/kids/{kid_id}/entries")
async def kid_entries(kid_id: str, user=Depends(get_current_user)):
    return await db.kid_entries.find(
        {"kid_id": kid_id, "owner_id": user["current_ledger_id"]}, {"_id": 0}
    ).sort("created_at", -1).to_list(200)


@api.post("/kids/entry")
async def add_kid_entry(body: KidEntryIn, user=Depends(get_current_user)):
    kid = await db.kids.find_one({"id": body.kid_id, "owner_id": user["current_ledger_id"]})
    if not kid:
        raise HTTPException(status_code=404, detail="Kid not found")

    amount = float(body.amount or 0)
    entry_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()

    inc = {}
    if body.type == "allowance":
        inc = {"balance": amount}
    elif body.type == "spend":
        inc = {"balance": -amount, "total_spent": amount}
    elif body.type == "save":
        inc = {"total_saved": amount}
    else:
        raise HTTPException(status_code=400, detail="Invalid type. Use allowance/spend/save.")

    await db.kids.update_one({"id": body.kid_id}, {"$inc": inc})
    entry = {
        "id": entry_id,
        "kid_id": body.kid_id,
        "owner_id": user["current_ledger_id"],
        "type": body.type,
        "amount": amount,
        "note": body.note,
        "category": body.category,
        "created_at": now,
    }
    await db.kid_entries.insert_one(entry)
    entry.pop("_id", None)
    return entry


# ================================================================
# ===== v14: BILLING MODULE
# ================================================================

class ProductIn(BaseModel):
    name: str
    sku: Optional[str] = None
    hsn: Optional[str] = None
    barcode: Optional[str] = None
    category: Optional[str] = "General"
    brand: Optional[str] = None
    unit: Optional[str] = "PCS"
    price: float = 0
    mrp: Optional[float] = 0
    purchase_price: Optional[float] = 0
    gst_rate: float = 0
    stock: float = 0
    low_stock_alert: Optional[float] = 5
    image: Optional[str] = None
    active: bool = True


class PartyIn(BaseModel):
    name: str
    phone: Optional[str] = None
    email: Optional[str] = None
    gstin: Optional[str] = None
    address: Optional[str] = None
    opening_balance: float = 0


class InvoiceItemIn(BaseModel):
    product_id: Optional[str] = None
    name: str
    hsn: Optional[str] = None
    qty: float
    unit: Optional[str] = "PCS"
    price: float
    discount_pct: Optional[float] = 0
    gst_rate: Optional[float] = 0


class InvoiceIn(BaseModel):
    invoice_type: str = "tax"
    customer_id: Optional[str] = None
    customer_name: Optional[str] = None
    items: List[InvoiceItemIn]
    discount_amount: Optional[float] = 0
    shipping: Optional[float] = 0
    gst_mode: str = "exclusive"
    payment_mode: Optional[str] = "cash"
    account_id: Optional[str] = None
    paid_amount: Optional[float] = None
    notes: Optional[str] = None
    terms: Optional[str] = None
    invoice_date: Optional[str] = None
    due_date: Optional[str] = None
    status: Optional[str] = "final"


class PurchaseIn(BaseModel):
    supplier_id: Optional[str] = None
    supplier_name: Optional[str] = None
    items: List[InvoiceItemIn]
    discount_amount: Optional[float] = 0
    shipping: Optional[float] = 0
    gst_mode: str = "exclusive"
    payment_mode: Optional[str] = "credit"
    account_id: Optional[str] = None
    paid_amount: Optional[float] = None
    notes: Optional[str] = None
    purchase_date: Optional[str] = None


def _compute_invoice_totals(items, discount_amount, shipping, gst_mode):
    subtotal = 0.0
    taxable = 0.0
    tax = 0.0
    for it in items:
        line = float(it.get("qty", 0)) * float(it.get("price", 0))
        disc_pct = float(it.get("discount_pct") or 0)
        line_after_disc = line * (1 - disc_pct / 100)
        rate = float(it.get("gst_rate") or 0)
        if gst_mode == "inclusive":
            line_taxable = line_after_disc / (1 + rate / 100) if rate else line_after_disc
            line_tax = line_after_disc - line_taxable
        else:
            line_taxable = line_after_disc
            line_tax = line_after_disc * (rate / 100)
        subtotal += line_after_disc
        taxable += line_taxable
        tax += line_tax
        it["_line_total"] = round(line_taxable + line_tax, 2)
    total = round(taxable + tax + float(shipping or 0) - float(discount_amount or 0), 2)
    return {
        "subtotal": round(subtotal, 2), "taxable": round(taxable, 2),
        "tax": round(tax, 2), "shipping": float(shipping or 0),
        "discount_amount": float(discount_amount or 0), "total": total,
    }


async def _next_invoice_number(owner_id, invoice_type):
    prefix_map = {"tax": "INV", "gst": "GST", "proforma": "PRF", "quotation": "QTN", "challan": "CHL", "credit": "CN", "debit": "DN"}
    prefix = prefix_map.get(invoice_type, "INV")
    year = datetime.now(timezone.utc).strftime("%y")
    last = await db.invoices.find_one(
        {"owner_id": owner_id, "invoice_type": invoice_type},
        sort=[("created_at", -1)]
    )
    seq = 1
    if last and last.get("invoice_number"):
        try:
            seq = int(str(last["invoice_number"]).split("-")[-1]) + 1
        except Exception:
            seq = 1
    return f"{prefix}-{year}-{seq:04d}"


@api.get("/billing/products")
async def list_products(user=Depends(get_current_user), q: str = ""):
    query = {"owner_id": user["current_ledger_id"]}
    if q:
        import re
        rx = {"$regex": re.escape(q), "$options": "i"}
        query["$or"] = [{"name": rx}, {"sku": rx}, {"barcode": rx}, {"category": rx}]
    return await db.products.find(query, {"_id": 0}).sort("name", 1).to_list(500)


@api.post("/billing/products")
async def create_product(body: ProductIn, user=Depends(get_current_user)):
    pid = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    doc = {"id": pid, "owner_id": user["current_ledger_id"], **body.dict(), "created_at": now}
    await db.products.insert_one(doc)
    doc.pop("_id", None)
    return doc


@api.patch("/billing/products/{product_id}")
async def update_product(product_id: str, body: ProductIn, user=Depends(get_current_user)):
    await db.products.update_one(
        {"id": product_id, "owner_id": user["current_ledger_id"]},
        {"$set": body.dict()}
    )
    return {"ok": True}


@api.delete("/billing/products/{product_id}")
async def delete_product(product_id: str, user=Depends(get_current_user)):
    await db.products.delete_one({"id": product_id, "owner_id": user["current_ledger_id"]})
    return {"ok": True}


async def _party_create(kind, body, user):
    pid = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    doc = {
        "id": pid, "owner_id": user["current_ledger_id"], "kind": kind,
        **body.dict(), "outstanding": float(body.opening_balance or 0),
        "created_at": now,
    }
    await db.parties.insert_one(doc)
    doc.pop("_id", None)
    return doc


@api.get("/billing/customers")
async def list_customers(user=Depends(get_current_user)):
    return await db.parties.find(
        {"owner_id": user["current_ledger_id"], "kind": "customer"}, {"_id": 0}
    ).sort("name", 1).to_list(500)


@api.post("/billing/customers")
async def add_customer(body: PartyIn, user=Depends(get_current_user)):
    return await _party_create("customer", body, user)


@api.get("/billing/suppliers")
async def list_suppliers(user=Depends(get_current_user)):
    return await db.parties.find(
        {"owner_id": user["current_ledger_id"], "kind": "supplier"}, {"_id": 0}
    ).sort("name", 1).to_list(500)


@api.post("/billing/suppliers")
async def add_supplier(body: PartyIn, user=Depends(get_current_user)):
    return await _party_create("supplier", body, user)


@api.patch("/billing/parties/{party_id}")
async def update_party(party_id: str, body: PartyIn, user=Depends(get_current_user)):
    await db.parties.update_one(
        {"id": party_id, "owner_id": user["current_ledger_id"]},
        {"$set": body.dict()}
    )
    return {"ok": True}


@api.delete("/billing/parties/{party_id}")
async def delete_party(party_id: str, user=Depends(get_current_user)):
    await db.parties.delete_one({"id": party_id, "owner_id": user["current_ledger_id"]})
    return {"ok": True}


@api.get("/billing/invoices")
async def list_invoices(user=Depends(get_current_user), status: Optional[str] = None):
    q = {"owner_id": user["current_ledger_id"]}
    if status:
        q["status"] = status
    return await db.invoices.find(q, {"_id": 0}).sort("created_at", -1).to_list(500)


@api.get("/billing/invoices/{invoice_id}")
async def get_invoice(invoice_id: str, user=Depends(get_current_user)):
    inv = await db.invoices.find_one({"id": invoice_id, "owner_id": user["current_ledger_id"]}, {"_id": 0})
    if not inv:
        raise HTTPException(status_code=404, detail="Invoice not found")
    return inv


@api.post("/billing/invoices")
async def create_invoice(body: InvoiceIn, user=Depends(get_current_user)):
    owner = user["current_ledger_id"]
    inv_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    inv_no = await _next_invoice_number(owner, body.invoice_type)

    items = [it.dict() for it in body.items]
    totals = _compute_invoice_totals(items, body.discount_amount or 0, body.shipping or 0, body.gst_mode)

    paid_amt = body.paid_amount if body.paid_amount is not None else totals["total"]
    if body.payment_mode == "credit":
        paid_amt = 0

    doc = {
        "id": inv_id, "owner_id": owner, "invoice_number": inv_no,
        "invoice_type": body.invoice_type,
        "customer_id": body.customer_id, "customer_name": body.customer_name,
        "items": items, "gst_mode": body.gst_mode, **totals,
        "paid_amount": float(paid_amt),
        "balance_due": round(totals["total"] - float(paid_amt), 2),
        "payment_mode": body.payment_mode, "account_id": body.account_id,
        "notes": body.notes, "terms": body.terms,
        "invoice_date": body.invoice_date or now, "due_date": body.due_date,
        "status": body.status or "final", "created_at": now,
    }
    await db.invoices.insert_one(doc)

    if body.invoice_type in ("tax", "gst") and doc["status"] == "final":
        for it in items:
            if it.get("product_id"):
                await db.products.update_one(
                    {"id": it["product_id"], "owner_id": owner},
                    {"$inc": {"stock": -float(it.get("qty", 0))}}
                )
        if body.customer_id and doc["balance_due"] > 0:
            await db.parties.update_one(
                {"id": body.customer_id, "owner_id": owner},
                {"$inc": {"outstanding": doc["balance_due"]}}
            )
        if body.account_id and float(paid_amt) > 0:
            acc = await db.accounts.find_one({"id": body.account_id, "owner_id": owner})
            if acc:
                await db.accounts.update_one({"id": body.account_id}, {"$inc": {"balance": float(paid_amt)}})
                await db.transactions.insert_one({
                    "id": str(uuid.uuid4()), "owner_id": owner,
                    "account_id": body.account_id, "account_name": acc.get("name"),
                    "type": "income", "amount": float(paid_amt),
                    "category": "Sales", "note": f"Invoice {inv_no}",
                    "date": now, "created_at": now,
                })

    doc.pop("_id", None)
    return doc


@api.patch("/billing/invoices/{invoice_id}")
async def update_invoice(invoice_id: str, body: InvoiceIn, user=Depends(get_current_user)):
    owner = user["current_ledger_id"]
    old = await db.invoices.find_one({"id": invoice_id, "owner_id": owner})
    if not old:
        raise HTTPException(status_code=404, detail="Invoice not found")

    # Reverse old effects if it was a real sales invoice
    if old.get("invoice_type") in ("tax", "gst") and old.get("status") == "final":
        for it in old.get("items", []):
            if it.get("product_id"):
                await db.products.update_one(
                    {"id": it["product_id"], "owner_id": owner},
                    {"$inc": {"stock": float(it.get("qty", 0))}}
                )
        if old.get("customer_id") and old.get("balance_due", 0) > 0:
            await db.parties.update_one(
                {"id": old["customer_id"], "owner_id": owner},
                {"$inc": {"outstanding": -float(old["balance_due"])}}
            )
        if old.get("account_id") and old.get("paid_amount", 0) > 0:
            await db.accounts.update_one(
                {"id": old["account_id"], "owner_id": owner},
                {"$inc": {"balance": -float(old["paid_amount"])}}
            )
        # Delete linked transaction record if exists
        await db.transactions.delete_many({
            "owner_id": owner, "note": f"Invoice {old.get('invoice_number')}"
        })

    # Compute new totals
    items = [it.dict() for it in body.items]
    totals = _compute_invoice_totals(items, body.discount_amount or 0, body.shipping or 0, body.gst_mode)
    paid_amt = body.paid_amount if body.paid_amount is not None else totals["total"]
    if body.payment_mode == "credit":
        paid_amt = 0

    now = datetime.now(timezone.utc).isoformat()
    updates = {
        "invoice_type": body.invoice_type,
        "customer_id": body.customer_id, "customer_name": body.customer_name,
        "items": items, "gst_mode": body.gst_mode, **totals,
        "paid_amount": float(paid_amt),
        "balance_due": round(totals["total"] - float(paid_amt), 2),
        "payment_mode": body.payment_mode, "account_id": body.account_id,
        "notes": body.notes, "terms": body.terms,
        "invoice_date": body.invoice_date or old.get("invoice_date"),
        "due_date": body.due_date, "status": body.status or old.get("status", "final"),
        "updated_at": now,
    }
    await db.invoices.update_one({"id": invoice_id}, {"$set": updates})

    # Re-apply business logic
    if updates["invoice_type"] in ("tax", "gst") and updates["status"] == "final":
        for it in items:
            if it.get("product_id"):
                await db.products.update_one(
                    {"id": it["product_id"], "owner_id": owner},
                    {"$inc": {"stock": -float(it.get("qty", 0))}}
                )
        if body.customer_id and updates["balance_due"] > 0:
            await db.parties.update_one(
                {"id": body.customer_id, "owner_id": owner},
                {"$inc": {"outstanding": updates["balance_due"]}}
            )
        if body.account_id and float(paid_amt) > 0:
            acc = await db.accounts.find_one({"id": body.account_id, "owner_id": owner})
            if acc:
                await db.accounts.update_one({"id": body.account_id}, {"$inc": {"balance": float(paid_amt)}})
                await db.transactions.insert_one({
                    "id": str(uuid.uuid4()), "owner_id": owner,
                    "account_id": body.account_id, "account_name": acc.get("name"),
                    "type": "income", "amount": float(paid_amt),
                    "category": "Sales", "note": f"Invoice {old.get('invoice_number')}",
                    "date": now, "created_at": now,
                })

    updated = await db.invoices.find_one({"id": invoice_id}, {"_id": 0})
    return updated


@api.delete("/billing/invoices/{invoice_id}")
async def delete_invoice(invoice_id: str, user=Depends(get_current_user)):
    await db.invoices.delete_one({"id": invoice_id, "owner_id": user["current_ledger_id"]})
    return {"ok": True}


@api.get("/billing/dashboard")
async def billing_dashboard(user=Depends(get_current_user)):
    owner = user["current_ledger_id"]
    now = datetime.now(timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat()

    async def _sum(match):
        pipe = [{"$match": match}, {"$group": {"_id": None, "total": {"$sum": "$total"}, "count": {"$sum": 1}}}]
        docs = await db.invoices.aggregate(pipe).to_list(1)
        return {"total": (docs[0]["total"] if docs else 0), "count": (docs[0]["count"] if docs else 0)}

    today_sales = await _sum({"owner_id": owner, "invoice_type": {"$in": ["tax", "gst"]}, "status": "final", "created_at": {"$gte": today_start}})
    month_sales = await _sum({"owner_id": owner, "invoice_type": {"$in": ["tax", "gst"]}, "status": "final", "created_at": {"$gte": month_start}})

    pending_pipe = [
        {"$match": {"owner_id": owner, "balance_due": {"$gt": 0}, "status": "final"}},
        {"$group": {"_id": None, "total": {"$sum": "$balance_due"}, "count": {"$sum": 1}}}
    ]
    pending_docs = await db.invoices.aggregate(pending_pipe).to_list(1)
    pending = {"total": pending_docs[0]["total"] if pending_docs else 0, "count": pending_docs[0]["count"] if pending_docs else 0}

    recent = await db.invoices.find({"owner_id": owner}, {"_id": 0}).sort("created_at", -1).to_list(8)

    low_stock = await db.products.find(
        {"owner_id": owner, "$expr": {"$lte": ["$stock", "$low_stock_alert"]}}, {"_id": 0}
    ).limit(10).to_list(10)

    total_products = await db.products.count_documents({"owner_id": owner})
    total_customers = await db.parties.count_documents({"owner_id": owner, "kind": "customer"})

    return {
        "today_sales": today_sales,
        "month_sales": month_sales,
        "pending": pending,
        "recent_invoices": recent,
        "low_stock_items": low_stock,
        "total_products": total_products,
        "total_customers": total_customers,
    }


# ----- Premium / Monetization -----
class SubscribeIn(BaseModel):
    plan: Literal["monthly", "half_yearly", "yearly", "two_yearly", "lifetime"]
    # Kept free-form so any gateway (Google Play Billing / Razorpay / UPI /
    # PhonePe / Paytm) can attach its own verification payload later without
    # changing this contract.
    payment_method: Optional[str] = "manual"
    payment_reference: Optional[str] = None


@api.get("/premium/plans")
async def get_premium_plans():
    return {"plans": list(PREMIUM_PLANS.values()), "features": PREMIUM_FEATURE_LIST}


@api.get("/premium/status")
async def get_premium_status(user=Depends(get_current_user)):
    return await _sync_premium_status(user)


@api.post("/premium/subscribe")
@limiter.limit("10/hour")
async def subscribe_premium(request: Request, body: SubscribeIn, user=Depends(get_current_user)):
    """
    Activates a plan on the account. Payment capture is intentionally
    pluggable: in production, verify `payment_reference` against the
    relevant gateway (Play Billing / Razorpay / UPI / PhonePe / Paytm)
    BEFORE trusting it here. This endpoint is the single place that flips
    subscriptionStatus, so wiring in a real gateway later only means adding
    a verification call at the top of this function.
    """
    if body.plan not in PREMIUM_PLANS:
        raise HTTPException(status_code=400, detail="Unknown plan")
    plan = PREMIUM_PLANS[body.plan]
    now = datetime.now(timezone.utc)
    sub_end = None if plan["duration_days"] is None else now + timedelta(days=plan["duration_days"])
    updates = {
        "subscriptionPlan": plan["id"],
        "subscriptionStart": now.isoformat(),
        "subscriptionEnd": sub_end.isoformat() if sub_end else None,
        "subscriptionStatus": "premium",
        "premiumActive": True,
        "lastPaymentMethod": body.payment_method,
        "lastPaymentReference": body.payment_reference,
        "updatedAt": now.isoformat(),
    }
    await db.users.update_one({"id": user["id"]}, {"$set": updates})
    fresh = await db.users.find_one({"id": user["id"]}, {"password_hash": 0, "_id": 0})
    return await _sync_premium_status(fresh)


@api.post("/premium/restore")
async def restore_purchase(user=Depends(get_current_user)):
    """Re-checks current status against the DB — useful after reinstall / relogin."""
    fresh = await db.users.find_one({"id": user["id"]}, {"password_hash": 0, "_id": 0})
    return await _sync_premium_status(fresh)


# ----- Advertisements -----
@api.get("/ads/status")
async def ads_status(user=Depends(get_current_user)):
    pstatus = await _sync_premium_status(user)
    if pstatus["premium_active"]:
        return {"ads_enabled": False, "shown_today": 0, "max_per_day": 0, "remaining": 0}
    today = datetime.now(timezone.utc).date().isoformat()
    shown = user.get("adsShownToday", 0) if user.get("lastAdResetDate") == today else 0
    return {
        "ads_enabled": True,
        "shown_today": shown,
        "max_per_day": MAX_ADS_PER_DAY,
        "remaining": max(0, MAX_ADS_PER_DAY - shown),
    }


@api.post("/ads/track")
async def track_ad_shown(user=Depends(get_current_user)):
    """Called by the client right after an ad impression completes."""
    pstatus = await _sync_premium_status(user)
    if pstatus["premium_active"]:
        return {"ok": False, "ads_enabled": False, "shown_today": 0, "remaining": 0}
    today = datetime.now(timezone.utc).date().isoformat()
    shown = user.get("adsShownToday", 0) if user.get("lastAdResetDate") == today else 0
    if shown >= MAX_ADS_PER_DAY:
        return {"ok": False, "shown_today": shown, "remaining": 0}
    shown += 1
    await db.users.update_one(
        {"id": user["id"]},
        {"$set": {"adsShownToday": shown, "lastAdResetDate": today}},
    )
    return {"ok": True, "shown_today": shown, "remaining": max(0, MAX_ADS_PER_DAY - shown)}


# ----- Cloud Backup & Restore (Premium) -----
BACKUP_COLLECTIONS = [
    "accounts", "transactions", "udhaar", "recurring", "budgets", "goals",
    "subscriptions", "investments", "warranties", "products", "parties", "invoices",
]


@api.get("/backup/export")
async def backup_export(user=Depends(require_premium)):
    owner = user["current_ledger_id"]
    data = {}
    for coll in BACKUP_COLLECTIONS:
        data[coll] = await db[coll].find({"owner_id": owner}, {"_id": 0}).to_list(10000)
    return {"exported_at": datetime.now(timezone.utc).isoformat(), "data": data}


@api.post("/backup/restore")
@limiter.limit("5/hour")
async def backup_restore(request: Request, payload: dict, user=Depends(require_premium)):
    owner = user["current_ledger_id"]
    data = payload.get("data") or {}
    restored = {}
    for coll in BACKUP_COLLECTIONS:
        rows = data.get(coll) or []
        if not isinstance(rows, list) or not rows:
            continue
        for r in rows:
            r["owner_id"] = owner
            r.setdefault("id", str(uuid.uuid4()))
            r.pop("_id", None)
        await db[coll].delete_many({"owner_id": owner})
        await db[coll].insert_many(rows)
        restored[coll] = len(rows)
    return {"ok": True, "restored": restored}


# ----- Excel Export (Premium) -----
@api.get("/export/excel")
async def export_excel(month: Optional[str] = None, user=Depends(require_premium)):
    from openpyxl import Workbook
    rows, month = await _month_transactions(user, month)
    wb = Workbook()
    ws = wb.active
    ws.title = "Transactions"
    ws.append(["Date", "Type", "Category", "Account", "Amount", "Note"])
    for r in rows:
        d = r.get("date", "")
        try:
            d = datetime.fromisoformat(d.replace("Z", "+00:00")).strftime("%Y-%m-%d")
        except Exception:
            pass
        ws.append([d, r["type"], r["category"], r.get("account_name", ""), r["amount"], r.get("note", "")])
    for col in ws.columns:
        width = max((len(str(c.value)) for c in col if c.value is not None), default=10)
        ws.column_dimensions[col[0].column_letter].width = min(40, max(10, width + 2))
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="apka-munim-{month}.xlsx"'},
    )


# ----- Health -----
@api.get("/")
async def root():
    return {"app": "Apka Munim", "status": "ok"}


app.include_router(api)

app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=[o.strip() for o in os.environ.get("CORS_ORIGINS", "").split(",") if o.strip()],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def startup():
    await db.users.create_index("email", unique=True)
    await db.accounts.create_index([("owner_id", 1)])
    await db.transactions.create_index([("owner_id", 1), ("date", -1)])
    await db.udhaar.create_index([("owner_id", 1)])
    await db.recurring.create_index([("owner_id", 1)])
    await db.budgets.create_index([("owner_id", 1), ("category", 1)], unique=True)
    await db.ledgers.create_index("invite_code")
    await db.ledgers.create_index("members")
    await db.goals.create_index([("owner_id", 1)])
    await db.subscriptions.create_index([("owner_id", 1), ("next_billing_date", 1)])
    await db.password_reset_tokens.create_index("token", unique=True)
    await db.password_reset_tokens.create_index("expires_at")
    await db.pin_attempts.create_index("email", unique=True)
    logger.info("Apka Munim API started")


@app.on_event("shutdown")
async def shutdown():
    client.close()
    # --- Account Deletion Endpoints ---

@app.delete("/api/user/delete-account")
async def delete_user_account(current_user: dict = Depends(get_current_user)):
    user_id = current_user["_id"]
    
    # Invalidate and delete user data securely
    await db.users.delete_one({"_id": user_id})
    await db.transactions.delete_many({"user_id": user_id})
    await db.invoices.delete_many({"user_id": user_id})
    await db.parties.delete_many({"user_id": user_id})
    await db.products.delete_many({"user_id": user_id})
    
    return {"status": "success", "message": "Account and associated data deleted permanently."}

@app.post("/api/public/delete-account-request")
async def public_delete_account_request(request_data: dict):
    email = request_data.get("email")
    reason = request_data.get("reason", "")
    
    if not email:
        raise HTTPException(status_code=400, detail="Email is required")
        
    await db.deletion_requests.insert_one({
        "email": email,
        "reason": reason,
        "status": "Pending Verification",
        "created_at": datetime.utcnow()
    })
    
    return {"status": "success", "message": "Request recorded successfully."}
