import json, os, sys
try:
    import redis
    url = os.getenv("REDIS_URL", "")
    if url:
        r = redis.from_url(url, decode_responses=True)
        r.delete("signal:state")
        print("Redis cleared")
except:
    print("No Redis or not reachable")
if os.path.exists("state.json"):
    os.remove("state.json")
    print("state.json deleted")
with open("state.json", "w") as f:
    json.dump({"positions": {}, "trades": []}, f)
    print("Fresh state.json written")
