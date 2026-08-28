import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import redis
from app.config import settings

def test_redis():
    print("=" * 60)
    print(f"Testing Redis Connection to: {settings.redis_url}")
    print("=" * 60)

    try:
        r = redis.Redis.from_url(
            settings.redis_url,
            socket_connect_timeout=5,
            socket_timeout=5,
        )
        ping_response = r.ping()
        print(f"SUCCESS: Redis is CONNECTED! PING response: {ping_response}")
        info = r.info()
        print(f"  Redis Version: {info.get('redis_version')}")
        print(f"  Used Memory:   {info.get('used_memory_human')}")
        print(f"  Connected Clients: {info.get('connected_clients')}")
    except redis.ConnectionError as err:
        print(f"FAILED to connect to Redis: {err}")
        print("\nNote: 'email-automation-adira-ryw3zd' is an internal Docker container hostname.")
        print("If running scripts locally outside Docker, update REDIS_URL in .env to use the external IP or localhost.")
    except Exception as err:
        print(f"ERROR testing Redis: {err}")

if __name__ == "__main__":
    test_redis()
