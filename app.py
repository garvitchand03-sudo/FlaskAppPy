from flask import Flask, request, jsonify
import os
import subprocess
import json
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
import pytz
import boto3

# === CONFIGURATION ===
SLACK_BOT_TOKEN = "xoxb-3679967085-9231380700567-NigLIonbL4pjxgWSO16QTlTb"  # Replace with your bot token
CHANNEL_ID = "C098D8D02PK"  # Replace with your channel ID (not name)

app = Flask(__name__)
IST = pytz.timezone("Asia/Kolkata")
client = WebClient(token=SLACK_BOT_TOKEN)

CLUSTER_FILE = "/home/bpadmin/exclude_cluster/exclude_clusters.txt"
PENDING_FILE = "/home/bpadmin/exclude_cluster/pending_requests.json"

REMOTE_USER = "bpadmin"
REMOTE_HOST = "10.186.17.93"
REMOTE_PATH = "/home/bpadmin/exclude_clusters.txt"

# === HELPERS ===

def cluster_exists_in_regions(cluster_input):
    # If input already ends with -dev-eks-cluster, use as-is
    if cluster_input.endswith("-dev-eks-cluster"):
        expected_name = cluster_input
    else:
        expected_name = f"{cluster_input}-dev-eks-cluster"

    for region in ["us-east-1", "us-west-2"]:
        try:
            eks = boto3.client("eks", region_name=region)
            clusters = eks.list_clusters()["clusters"]
            if expected_name in clusters:
                return True
        except Exception as e:
            print(f"[ERROR] Failed to list clusters in {region}: {e}")
    return False


def transform_cluster_name(raw_input):
    cluster = raw_input.strip()
    if cluster.endswith("-dev-eks-cluster"):
        cluster = cluster[:-len("-dev-eks-cluster")]
    if not cluster.endswith("-infra"):
        cluster += "-infra"
    return cluster

def sync_file_to_remote():
    try:
        subprocess.run(
            ["scp", CLUSTER_FILE, f"{REMOTE_USER}@{REMOTE_HOST}:{REMOTE_PATH}"],
            check=True
        )
        print("[INFO] exclude_clusters.txt synced to remote")
    except subprocess.CalledProcessError as e:
        print(f"[ERROR] SCP failed: {e}")

def load_pending():
    if not os.path.exists(PENDING_FILE):
        return {}
    with open(PENDING_FILE, "r") as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            return {}

def save_pending(data):
    with open(PENDING_FILE, "w") as f:
        json.dump(data, f)
 
def get_manager(user_id):
    try:
        profile_response = client.users_profile_get(user=user_id)
        fields = profile_response["profile"].get("fields", {})

        # Look through all custom fields for a Slack user ID (starts with U)
        for field_id, field in fields.items():
            value = field.get("value", "")
            if value.startswith("U"):  # Likely a Slack user ID
                return f"<@{value}>"

        return "(Manager not set)"
    except SlackApiError as e:
        print(f"[ERROR] Failed to fetch manager: {e}")
        return "(Manager not set)"
   

def notify_requester(user_id, message):
    try:
        client.chat_postEphemeral(
            channel=user_id,
            user=user_id,
            text=message
        )
    except SlackApiError as e:
        print(f"[ERROR] Failed to notify requester: {e}")

def clear_exclude_file():
    with open(CLUSTER_FILE, "w") as f:
        f.write("")
    sync_file_to_remote()
    print("[INFO] Cleared exclusion file at 10 PM IST")

# === ROUTES ===

@app.route("/exclude", methods=["POST"])
def exclude_cluster():
    user_id = request.form.get("user_id")
    user_name = request.form.get("user_name")
    text = request.form.get("text", "")

    if "reason:" not in text:
        return "Usage: /exclude-cluster <cluster-name> reason: <reason>", 200

    cluster_raw, reason = text.split("reason:", 1)
    cluster_input = cluster_raw.strip()
    reason = reason.strip()

    # Validate cluster exists in AWS before proceeding
    if not cluster_exists_in_regions(cluster_input):
        return f":x: Cluster name {cluster_input} not found in *us-east-1* or *us-west-2*.", 200

    # Now transform it to internal format for exclusion tracking
    cluster = transform_cluster_name(cluster_input)


    # Load current excluded clusters
    excluded_clusters = []
    if os.path.exists(CLUSTER_FILE):
        with open(CLUSTER_FILE, "r") as f:
            content = f.read().strip()
            if content:
                excluded_clusters = content.split(",")

    if cluster in excluded_clusters:
        return f"{cluster} is already excluded.", 200

    pending = load_pending()
    if cluster in pending:
        return f"{cluster} is already pending approval.", 200

    manager_tag = get_manager(user_id)
    message = {
        "channel": CHANNEL_ID,
        "blocks": [
            {"type": "section", "text": {"type": "mrkdwn", "text":
                f"*Requester:* <@{user_id}>\n*Cluster:* {cluster}\n*Reason:* {reason}\n*Manager:* {manager_tag}"}},
            {"type": "actions", "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "✅ Accept"},
                    "style": "primary",
                    "value": cluster,
                    "action_id": "accept_cluster"
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "❌ Deny"},
                    "style": "danger",
                    "value": cluster,
                    "action_id": "deny_cluster"
                }
            ]}
        ]
    }
    try:
        response = client.chat_postMessage(**message)
        pending[cluster] = {
            "user_id": user_id,
            "message_ts": response["ts"]
        }
        save_pending(pending)
        return f":white_check_mark: Request to exclude {cluster} sent for manager approval.", 200
    except SlackApiError as e:
        print(f"[ERROR] Slack error: {e}")
        return "Failed to send approval request.", 500

@app.route("/slack/interactive", methods=["POST"])
def interactive():
    payload = json.loads(request.form["payload"])
    user = payload["user"]["id"]
    action = payload["actions"][0]["action_id"]
    cluster = payload["actions"][0]["value"]
    channel = payload["channel"]["id"]
    ts = payload["message"]["ts"]

    pending = load_pending()
    requester = pending.get(cluster, {}).get("user_id")

    if not requester:
        return "", 200

    if action == "accept_cluster":
        # Append to exclude file
        excluded_clusters = []
        if os.path.exists(CLUSTER_FILE):
            with open(CLUSTER_FILE, "r") as f:
                content = f.read().strip()
                if content:
                    excluded_clusters = content.split(",")
        if cluster not in excluded_clusters:
            excluded_clusters.append(cluster)
            with open(CLUSTER_FILE, "w") as f:
                f.write(",".join(excluded_clusters))
            sync_file_to_remote()

        # Update message in channel
        client.chat_update(
            channel=channel,
            ts=ts,
            text=f":white_check_mark: {cluster} exclusion was *approved* by <@{user}>.",
            blocks=[]
        )

        # Notify requester
        notify_requester(requester, f":white_check_mark: Your request to exclude {cluster} was *approved* by <@{user}>.")

    elif action == "deny_cluster":
        # Update message in channel
        client.chat_update(
            channel=channel,
            ts=ts,
            text=f":x: {cluster} exclusion was *denied* by <@{user}>.",
            blocks=[]
        )

        # Notify requester
        notify_requester(requester, f":x: Your request to exclude {cluster} was *denied* by <@{user}>.")

    # Clean up pending
    if cluster in pending:
        del pending[cluster]
        save_pending(pending)

    return "", 200


# === SCHEDULER ===
if __name__ == "__main__":
    scheduler = BackgroundScheduler(timezone=IST)
    trigger = CronTrigger(hour=22, minute=0)  # 10 PM IST
    scheduler.add_job(clear_exclude_file, trigger)
    scheduler.start()

    print("[INFO] Server started on http://0.0.0.0:2052")
    app.run(host="0.0.0.0", port=2052)
