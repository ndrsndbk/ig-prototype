import os
import hmac
import hashlib
import json
import datetime
from typing import Optional, Dict, Any, Tuple

import requests
from flask import Flask, request

# -------------------------------------------------------------------
# ENV VARS
# -------------------------------------------------------------------
GRAPH_API_VERSION = os.getenv("GRAPH_API_VERSION", "v23.0")

IG_BUSINESS_ID = os.getenv("IG_BUSINESS_ID", "")
IG_TOKEN = os.getenv("IG_TOKEN", "")
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "myverifytoken")
WEBHOOK_APP_SECRET = os.getenv("WEBHOOK_APP_SECRET", "")

DASHBOARD_URL = os.getenv(
    "DASHBOARD_URL",
    "https://ndrsndbk.github.io/stamp-card-dashboard/"
)

SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_SERVICE_KEY = (
    os.getenv("SUPABASE_SERVICE_KEY")
    or os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
)

def env_diagnostics() -> None:
    print("\n🔍 ENV DIAGNOSTICS (on boot)")
    print("-------------------------------------------")
    print(f"IG_BUSINESS_ID loaded:        {bool(IG_BUSINESS_ID)}")
    print(f"IG_TOKEN loaded:              {bool(IG_TOKEN)}")
    print(f"VERIFY_TOKEN loaded:          {bool(VERIFY_TOKEN)}")
    print(f"SUPABASE_URL loaded:          {bool(SUPABASE_URL)}")
    print(f"SUPABASE_SERVICE_KEY loaded:  {bool(SUPABASE_SERVICE_KEY)}")
    print(f"WEBHOOK_APP_SECRET set:       {bool(WEBHOOK_APP_SECRET)}")
    print(f"DASHBOARD_URL:                {DASHBOARD_URL}")
    print("-------------------------------------------\n")

env_diagnostics()

# -------------------------------------------------------------------
# SUPABASE REST HELPERS (NO supabase-py client)
# -------------------------------------------------------------------

def _sb_headers() -> Dict[str, str]:
    """Common headers for Supabase REST calls."""
    return {
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
        "Content-Type": "application/json",
    }

def fetch_single_customer(customer_id: str) -> Optional[Dict[str, Any]]:
    """
    GET /rest/v1/customers?customer_id=eq.<id>&limit=1
    """
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        print("Supabase not configured")
        return None

    url = f"{SUPABASE_URL}/rest/v1/customers"
    params = {
        "customer_id": f"eq.{customer_id}",
        "limit": "1",
        "select": "*",
    }
    try:
        r = requests.get(url, headers=_sb_headers(), params=params, timeout=15)
        if r.status_code >= 400:
            print("fetch_single_customer error:", r.status_code, r.text[:500])
            return None
        rows = r.json()
        return rows[0] if rows else None
    except Exception as e:
        print("fetch_single_customer exception:", e)
        return None


def upsert_customer(payload: Dict[str, Any]) -> None:
    """
    POST /rest/v1/customers?on_conflict=customer_id
    """
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        print("Supabase not configured")
        return

    url = f"{SUPABASE_URL}/rest/v1/customers"
    params = {"on_conflict": "customer_id"}
    headers = _sb_headers()
    headers["Prefer"] = "resolution=merge-duplicates"
    try:
        r = requests.post(url, headers=headers, params=params,
                          data=json.dumps(payload), timeout=15)
        if r.status_code >= 400:
            print("upsert_customer error:", r.status_code, r.text[:500])
    except Exception as e:
        print("upsert_customer exception:", e)


def get_and_update_streak(customer_id: str) -> Tuple[int, bool, bool]:
    """
    Streak logic backed by customer_streaks via REST.

    Table: customer_streaks
      - customer_id (pk)
      - streak_days
      - last_day
      - updated_at

    Returns: (new_streak, hit_2_now, hit_5_now)
    """
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        print("Supabase not configured")
        return 0, False, False

    base_url = f"{SUPABASE_URL}/rest/v1/customer_streaks"

    # 1) fetch existing
    params = {
        "customer_id": f"eq.{customer_id}",
        "limit": "1",
        "select": "*",
    }
    prev_streak = 0
    try:
        r = requests.get(base_url, headers=_sb_headers(), params=params, timeout=15)
        if r.status_code >= 400:
            print("get_and_update_streak select error:", r.status_code, r.text[:500])
        else:
            rows = r.json()
            if rows:
                prev_streak = rows[0].get("streak_days", 0) or 0
    except Exception as e:
        print("get_and_update_streak select exception:", e)

    new_streak = prev_streak + 1
    hit_2_now = (new_streak >= 2 and prev_streak < 2)
    hit_5_now = (new_streak >= 5 and prev_streak < 5)

    # 2) upsert
    now_iso = datetime.datetime.utcnow().isoformat() + "Z"
    headers = _sb_headers()
    headers["Prefer"] = "resolution=merge-duplicates"
    upsert_body = {
        "customer_id": customer_id,
        "streak_days": new_streak,
        "last_day": now_iso,
        "updated_at": now_iso,
    }
    try:
        r2 = requests.post(
            base_url,
            headers=headers,
            params={"on_conflict": "customer_id"},
            data=json.dumps(upsert_body),
            timeout=15,
        )
        if r2.status_code >= 400:
            print("get_and_update_streak upsert error:", r2.status_code, r2.text[:500])
    except Exception as e:
        print("get_and_update_streak upsert exception:", e)

    return new_streak, hit_2_now, hit_5_now

# -------------------------------------------------------------------
# IG SEND HELPERS
# -------------------------------------------------------------------

def send_instagram_message(payload: Dict[str, Any]) -> None:
    if not IG_BUSINESS_ID or not IG_TOKEN:
        print("⚠️ Missing IG_BUSINESS_ID or IG_TOKEN.")
        return

    url = f"https://graph.facebook.com/{GRAPH_API_VERSION}/{IG_BUSINESS_ID}/messages"
    headers = {
        "Authorization": f"Bearer {IG_TOKEN}",
        "Content-Type": "application/json",
    }
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=20)
        if r.status_code >= 400:
            print("❌ Instagram send error:", r.status_code, r.text[:500])
        else:
            print("✅ Instagram send ok:", r.json())
    except Exception as e:
        print("send_instagram_message exception:", e)


def send_ig_text(to_ig_user_id: str, *body_parts: str) -> None:
    if not body_parts:
        return
    body = "".join(str(p) for p in body_parts)
    payload = {
        "recipient": {"id": to_ig_user_id},
        "messaging_type": "RESPONSE",
        "message": {"text": body},
    }
    send_instagram_message(payload)


def send_ig_image(to_ig_user_id: str, image_url: str, caption: str = "") -> None:
    payload = {
        "recipient": {"id": to_ig_user_id},
        "messaging_type": "RESPONSE",
        "message": {
            "attachment": {
                "type": "image",
                "payload": {"url": image_url},
            }
        },
    }
    if caption:
        # IG DM doesn't support separate "caption" field in the same way as FB,
        # but we can just send a follow-up text.
        send_instagram_message(payload)
        send_ig_text(to_ig_user_id, caption)
    else:
        send_instagram_message(payload)


def build_stamp_card_url(visits: int) -> str:
    if visits < 0:
        visits = 0
    if visits > 10:
        visits = 10
    base = (
        "https://lhbtgjvejsnsrlstwlwl.supabase.co/storage/v1/object/public/cards/v1/"
        "Demo_Shop_"
    )
    return f"{base}{visits}.png"


def verify_meta_signature(raw_body: bytes, signature_256: str) -> bool:
    if not WEBHOOK_APP_SECRET:
        return True
    try:
        if not signature_256 or not signature_256.startswith("sha256="):
            return False
        sig_hex = signature_256.split("=", 1)[1].strip()
        mac = hmac.new(
            WEBHOOK_APP_SECRET.encode("utf-8"),
            msg=raw_body,
            digestmod=hashlib.sha256,
        )
        expected = mac.hexdigest()
        return hmac.compare_digest(sig_hex, expected)
    except Exception as e:
        print("verify_meta_signature error:", e)
        return False

# -------------------------------------------------------------------
# FLASK APP
# -------------------------------------------------------------------

app = Flask(__name__)

@app.route("/", methods=["GET"])
def health():
    return "OK", 200


@app.route("/webhook", methods=["GET"])
def verify_webhook():
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")

    if mode == "subscribe" and token == VERIFY_TOKEN:
        print("✅ Webhook verified.")
        return challenge, 200
    else:
        print("❌ Webhook verification failed.")
        return "Verification failed", 403


@app.route("/webhook", methods=["POST"])
def webhook():
    if WEBHOOK_APP_SECRET:
        sig256 = request.headers.get("x-hub-signature-256", "")
        if not verify_meta_signature(request.data, sig256):
            return "invalid signature", 403

    data = request.get_json(silent=True) or {}
    print("Incoming IG webhook:", json.dumps(data)[:1200], "...")

    entries = data.get("entry") or []
    events = []
    for entry in entries:
        events.extend(entry.get("messaging") or [])

    if not events:
        return "ok", 200

    for event in events:
        sender = event.get("sender") or {}
        ig_user_id = sender.get("id")
        msg = event.get("message") or {}
        if not ig_user_id or not msg:
            continue

        text = (msg.get("text") or "").strip()
        text_upper = text.upper()

        # ---------------- SIGNUP ----------------
        if text_upper == "SIGNUP":
            now_iso = datetime.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
            customer = fetch_single_customer(ig_user_id)
            if not customer:
                upsert_customer(
                    {
                        "customer_id": ig_user_id,
                        "number_of_visits": 0,
                        "last_visit_at": now_iso,
                    }
                )

            send_ig_text(
                ig_user_id,
                "👋 *Welcome to the Demo Coffee Shop stamp card!*\n\n",
                "You’re now signed up via Instagram.\n\n",
                "You can send:\n",
                "• *STAMP* – log a visit and collect a stamp\n",
                "• *CARD* – see your current stamp card\n",
                "• *REPORT* – open the live dashboard\n\n",
                "_(Prototype in testing mode)_",
            )
            continue

        # ---------------- STAMP ----------------
        if text_upper == "STAMP":
            customer = fetch_single_customer(ig_user_id)
            current_visits = customer.get("number_of_visits", 0) if customer else 0

            streak_days, hit_2_now, hit_5_now = get_and_update_streak(ig_user_id)
            add_stamps = 1

            if hit_2_now:
                send_ig_text(
                    ig_user_id,
                    "🔥 *You’re on a 2-visit streak!* 🔥\n\n",
                    "Keep it going — reach *5 visits* and earn an *extra stamp* 🏆",
                )

            if hit_5_now:
                add_stamps = 2
                send_ig_text(
                    ig_user_id,
                    "🏆 *5-Visit Streak!* 🏆\n\n",
                    "You’ve unlocked *double stamps today* — this check-in counts as *+2* and "
                    "your exclusive *coffee bag reward*!\n",
                    "Keep the momentum going!\n",
                    "_(Double applies to this visit only.)_",
                )

            new_visits = current_visits + add_stamps
            now_iso = datetime.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
            upsert_customer(
                {
                    "customer_id": ig_user_id,
                    "number_of_visits": new_visits,
                    "last_visit_at": now_iso,
                }
            )

            media_url = build_stamp_card_url(new_visits)
            caption = (
                f"You now have *{new_visits}* stamp(s). "
                "10 stamps = 1 free coffee ☕"
            )
            send_ig_image(ig_user_id, media_url, caption)
            continue

        # ---------------- CARD ----------------
        if text_upper == "CARD":
            customer = fetch_single_customer(ig_user_id)
            visits = customer.get("number_of_visits", 0) if customer else 0
            media_url = build_stamp_card_url(visits)
            caption = (
                f"You currently have *{visits}* stamp(s). "
                "10 stamps = 1 free coffee ☕"
            )
            send_ig_image(ig_user_id, media_url, caption)
            continue

        # ---------------- REPORT ----------------
        if text_upper == "REPORT":
            send_ig_text(
                ig_user_id,
                "📊 *Here’s your dashboard*\n\n",
                f"{DASHBOARD_URL}\n\n",
                "You can see:\n",
                "• Total cards\n",
                "• Stamps issued & redeemed\n",
                "• Redemption rate & ROI\n",
            )
            continue

        # ---------------- DEFAULT / HELP ----------------
        send_ig_text(
            ig_user_id,
            "👋 *Demo Coffee Shop stamp card (Instagram)*\n\n",
            "You can send:\n",
            "• *SIGNUP* – register and start your card\n",
            "• *STAMP* – log a visit and collect a stamp\n",
            "• *CARD* – see your current stamp card\n",
            "• *REPORT* – open the live dashboard\n\n",
            "_Prototype currently in testing mode._",
        )

    return "ok", 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8080")), debug=True)
