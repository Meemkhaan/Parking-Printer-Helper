python - <<'PY'
import base64
import requests

url = "https://hscqvjxkbdmehngbpony.supabase.co/rest/v1/print_jobs?id=eq.10d80e05-7dea-40ee-a9db-b6b4651bbc4e&select=payload_base64"

headers = {
    "apikey": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImhzY3F2anhrYmRtZWhuZ2Jwb255Iiwicm9sZSI6ImFub24iLCJpYXQiOjE3ODY0NjY0NzYsImV4cCI6MjEwMjA0MjQ3Nn0.4WfHrBmhVf9jz2FR2aPsl4zZ3N1QO5qOo0BZN2KhaCM",
    "Authorization": "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImhzY3F2anhrYmRtZWhuZ2Jwb255Iiwicm9sZSI6ImFub24iLCJpYXQiOjE3ODY0NjY0NzYsImV4cCI6MjEwMjA0MjQ3Nn0.4WfHrBmhVf9jz2FR2aPsl4zZ3N1QO5qOo0BZN2KhaCM",
}

r = requests.get(url, headers=headers)
r.raise_for_status()

payload = r.json()[0]["payload_base64"]
data = base64.b64decode(payload)

print("Total bytes:", len(data))
print("First 100:", data[:100].hex(" "))
print("Last 100:", data[-100:].hex(" "))
PY