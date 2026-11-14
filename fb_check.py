import os, json, requests

PAGE_ID = "552481174767670"
PSID = "29863034596629805"
TOKEN = os.environ["TOKEN"]

r = requests.get(
    f"https://graph.facebook.com/v25.0/{PAGE_ID}/conversations",
    params={
        "user_id": PSID,
        "platform": "messenger",
        "fields": "id,link,updated_time,participants,senders",
        "access_token": TOKEN,
    },
    timeout=10,
)
print(r.status_code)
print(json.dumps(r.json(), indent=2, ensure_ascii=False))
