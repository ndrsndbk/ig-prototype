import os
import hmac
import hashlib
import json
import datetime
from typing import Optional, Dict, Any, Tuple

import requests
from flask import Flask, request, jsonify

# ----------------------------- ENV VARS ---------------------------------------
# This app is Instagram-only, but we re-use a similar pattern to your WhatsApp prototype.

GRAPH_API_VERSION = os.getenv("GRAPH_API_VERSION", "v23.0")

# Instagram business account ID (the one that receives DMs)
IG_BUSINESS_ID = os.getenv("IG_BUSINESS_ID", "")

# Page access token / system user token with instagram_manage_messages, pages_messaging, etc.
IG_TOKEN = os.getenv("IG_TOKEN", "")

# Webhook verification token used during the Meta webhook "GET" handshake.
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "myverifytoken")

# Optional HMAC secret from Meta (x-hub-signature-256). If unset, signature check is skipped.
WEBHOOK_APP_SECRET = os.getenv("WEBHOOK_APP_SECRET", "")

# ---------------- Dashboard URL (for REPORT shortcut) ----------------
DASHBOARD_URL = os.getenv(
    "DASHBOARD_URL",
    "https://ndrsndbk.github.io/stamp-card-dashboard/"
)

# ---------------- Supabase ----------------
SUPABASE_URL = os.getenv("SUPABASE_URL", "https://lhbtgjvejsnsrlstwlwl.supabase.co")
SUPABASE_SERVICE_KEY = (
    os.getenv("SUPABASE_SERVICE_KEY")
    or os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
)

if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
    print("⚠️ Missing SUPABASE_URL or SUPABASE_SERVICE_KEY / SUPABASE_SERVICE_ROLE_KEY in env!")


def env_diagnostics() -> None:
    """
    Print a one-shot summary of which critical env vars are loaded.
    Values are not printed (only booleans) so secrets never leak.
    """
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

# ----------------------------- SUPABASE CLIENT --------------------------------
from supabase import create_client, Client

sb: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

# ----------------------------- FLASK APP --------------------------------------
app = Flask(__name__)

# ----------------------------- HELPERS ----------------------------------------
def send_instagram_message(payload: Dict[str, Any]) -> None:
    """
    Low-level sender using Messenger API for Instagram.

    Endpoint:
      POST https://graph.facebook.com/{GRAPH_API_VERSION}/{IG_BUSINESS_ID}/messages

    Payload should follow Messenger-style structure:
      { "recipient": {"id": "<IGSID>"}, "message": {...} }
    """
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
    """
    Convenience wrapper to send a plain Instagram DM text message.

    `to_ig_user_id` is the IG Scoped ID (IGSID) we get in `sender.id` in the webhook.
    """
    if not body_parts:
        print("send_ig_text called without body; skipping")
        return

    body = "".join(str(part) for part in body_parts)

    payload = {
        "recipient": {"id": to_ig_user_id},
        "messaging_type": "RESPONSE",
        "message": {"text": body},
    }
    send_instagram_message(payload)


def send_ig_image(to_ig_user_id: str, image_url: str, caption: str = "") -> None:
    """
    Send an image to an Instagram user via the Messenger API.
    """
    payload = {
        "recipient": {"id": to_ig_user_id},
        "messaging_type": "RESPONSE",
        "message": {
            "attachment": {
                "type": "image",
                "payload": {
                    "url": image_url,
                },
            },
            **({"text": caption} if caption else {}),
        },
    }
    send_instagram_message(payload)


def build_stamp_card_url(visits: int) -> str:
    """
    Map visit count → correct static PNG URL.
    Same logic as your WhatsApp prototype:
      Demo_Shop_0.png ... Demo_Shop_10.png
    """
    if visits < 0:
        visits = 0
    if visits > 10:
        visits = 10
    base = "https://lhbtgjvejsnsrlstwlwl.supabase.co/storage/v1/object/public/cards/v1/Demo_Shop_"
    return f"{base}{visits}.png"


def fetch_single_customer(customer_id: str) -> Optional[Dict[str, Any]]:
    """
    Fetch one row from the `customers` table by `customer_id`.
    Uses the same table as the WhatsApp app – we just use the IG sender ID
    instead of a phone number.
    """
    try:
        resp = (
            sb.table("customers")
            .select("*")
            .eq("customer_id", customer_id)
            .limit(1)
            .execute()
        )
        rows = getattr(resp, "data", None) or []
        return rows[0] if rows else None
    except Exception as e:
        print("fetch_single_customer error:", e)
        return None


def verify_meta_signature(raw_body: bytes, signature_256: str) -> bool:
    """
    Validate x-hub-signature-256 from Meta if WEBHOOK_APP_SECRET is set.
    Signature format: 'sha256=...'
    """
    if not WEBHOOK_APP_SECRET:
        return True  # skip if not configured

    try:
        if not signature_256 or not signature_256.startswith("sha256="):
            return False

        sig_hex = signature_256.split("=", 1)[1].strip()
        mac = hmac.new(WEBHOOK_APP_SECRET.encode("utf-8"), msg=raw_body, digestmod=hashlib.sha256)
        expected = mac.hexdigest()
        return hmac.compare_digest(sig_hex, expected)
    except Exception as e:
        print("verify_meta_signature error:", e)
        return False


# ----------------------------- STREAK LOGIC -----------------------------------
def get_and_update_streak(customer_id: str) -> Tuple[int, bool, bool]:
    """
    Same streak logic as your WhatsApp app, but used for Instagram users too.

    Backs onto `customer_streaks`:
      - `customer_id`
      - `streak_days`
      - `last_day`
      - `updated_at`

    Returns (new_streak, hit_2_now, hit_5_now).
    """

    try:
        resp = (
            sb.table("customer_streaks")
            .select("*")
            .eq("customer_id", customer_id)
            .limit(1)
            .execute()
        )
        rows = getattr(resp, "data", None) or []
        row = rows[0] if rows else None
    except Exception as e:
        print("get_and_update_streak: select error:", e)
        row = None

    prev_streak = row.get("streak_days", 0) if row else 0
    new_streak = (prev_streak or 0) + 1

    hit_2_now = (new_streak >= 2 and prev_streak < 2)
    hit_5_now = (new_streak >= 5 and prev_streak < 5)

    try:
        now_iso = datetime.datetime.utcnow().isoformat() + "Z"
        sb.table("customer_streaks").upsert(
            {
                "customer_id": customer_id,
                "streak_days": new_streak,
                "last_day": now_iso,
                "updated_at": now_iso,
            }
        ).execute()
    except Exception as e:
        print("get_and_update_streak: upsert error:", e)

    return new_streak, hit_2_now, hit_5_now


# ----------------------------- ROUTES -----------------------------------------
@app.route("/", methods=["GET"])
def health():
    """
    Simple healthcheck endpoint.
    """
    return "OK", 200


@app.route("/webhook", methods=["GET"])
def verify_webhook():
    """
    Webhook verification (GET) for Messenger API for Instagram.
    Meta will send hub.mode, hub.verify_token, hub.challenge.
    """
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
    """
    Main webhook handler for incoming Instagram messages.

    Messenger API for Instagram payload shape (simplified):

    {
      "object": "instagram",
      "entry": [
        {
          "id": "<IG_BUSINESS_ID>",
          "time": 1234567890,
          "messaging": [
            {
              "sender": {"id": "<IGSID>"},
              "recipient": {"id": "<IG_BUSINESS_ID>"},
              "timestamp": 1234567890,
              "message": {
                "mid": "...",
                "text": "SIGNUP"
              }
            }
          ]
        }
      ]
    }
    """

    # Optional signature check
    if WEBHOOK_APP_SECRET:
        sig256 = request.headers.get("x-hub-signature-256", "")
        if not verify_meta_signature(request.data, sig256):
            return "invalid signature", 403

    data = request.get_json(silent=True) or {}
    print("Incoming IG webhook:", json.dumps(data)[:1200], "...")

    entries = data.get("entry") or []
    if not entries:
        return "ok", 200

    # Flatten all messaging events across entries
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

        # -------------------- SIGNUP FLOW ------------------------------------
        if text_upper == "SIGNUP":
            # Ensure the customer exists with 0 visits (if new)
            now_iso = datetime.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
            customer = fetch_single_customer(ig_user_id)
            if not customer:
                try:
                    sb.table("customers").upsert(
                        {
                            "customer_id": ig_user_id,
                            "number_of_visits": 0,
                            "last_visit_at": now_iso,
                        }
                    ).execute()
                except Exception as e:
                    print("SIGNUP upsert error:", e)

            # Send welcome + instructions (same experience as WhatsApp)
            send_ig_text(
                ig_user_id,
                "👋 *Welcome to the Demo Coffee Shop stamp card!*\n\n",
                "You’re now signed up via Instagram.\n\n",
                "You can send:\n",
                "• *STAMP* – log a visit and collect a stamp\n",
                "• *CARD* – see your current stamp card\n",
                "• *REPORT* – open the live dashboard\n\n",
                "_(Prototype in testing mode)_"
            )
            continue

        # -------------------- STAMP FLOW -------------------------------------
        if text_upper == "STAMP":
            customer = fetch_single_customer(ig_user_id)
            current_visits = customer.get("number_of_visits", 0) if customer else 0

            streak_days, hit_2_now, hit_5_now = get_and_update_streak(ig_user_id)

            add_stamps = 1

            if hit_2_now:
                send_ig_text(
                    ig_user_id,
                    "🔥 *You’re on a 2-visit streak!* 🔥\n\n",
                    "Keep it going — reach *5 visits* and earn an *extra stamp* 🏆"
                )

            if hit_5_now:
                add_stamps = 2
                send_ig_text(
                    ig_user_id,
                    "🏆 *5-Visit Streak!* 🏆\n\n",
                    "You’ve unlocked *double stamps today* — this check-in counts as *+2* and "
                    "your exclusive *coffee bag reward*!\n",
                    "Keep the momentum going!\n",
                    "_(Double applies to this visit only.)_"
                )

            new_visits = current_visits + add_stamps
            now_iso = datetime.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
            try:
                sb.table("customers").upsert(
                    {
                        "customer_id": ig_user_id,
                        "number_of_visits": new_visits,
                        "last_visit_at": now_iso,
                    }
                ).execute()
            except Exception as e:
                print("STAMP upsert error (IG):", e)

            media_url = build_stamp_card_url(new_visits)
            caption = (
                f"You now have *{new_visits}* stamp(s). "
                "10 stamps = 1 free coffee ☕"
            )
            send_ig_image(ig_user_id, media_url, caption)
            continue

        # -------------------- CARD FLOW --------------------------------------
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

        # -------------------- REPORT FLOW ------------------------------------
        if text_upper == "REPORT":
            send_ig_text(
                ig_user_id,
                "📊 *Here’s your dashboard*\n\n",
                f"{DASHBOARD_URL}\n\n",
                "You can see:\n",
                "• Total cards\n",
                "• Stamps issued & redeemed\n",
                "• Redemption rate & ROI\n"
            )
            continue

        # -------------------- HELP / DEFAULT ---------------------------------
        send_ig_text(
            ig_user_id,
            "👋 *Demo Coffee Shop stamp card (Instagram)*\n\n",
            "You can send:\n",
            "• *SIGNUP* – register and start your card\n",
            "• *STAMP* – log a visit and collect a stamp\n",
            "• *CARD* – see your current stamp card\n",
            "• *REPORT* – open the live dashboard\n\n",
            "_Prototype currently in testing mode._"
        )

    return "ok", 200


# ----------------------------- WSGI ENTRYPOINT -------------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8080")), debug=True)
