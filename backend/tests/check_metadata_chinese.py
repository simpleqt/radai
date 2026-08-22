
import redis
import sys

# Add project root to sys.path
sys.path.append('/home/inspur/myenv/langchian_project')

from backend.config.settings import settings

def check_metadata(kb_code):
    redis_client = redis.Redis(
        host=settings.redis_host or "localhost",
        port=settings.redis_port or 6379,
        password=settings.redis_password,
        db=settings.redis_db or 0,
        decode_responses=False,
    )
    
    prefix = f"{kb_code}:keyword_index"
    entity_names = redis_client.lrange(f"{prefix}:entity_names", 0, 100)
    
    for name in entity_names:
        name_str = name.decode()
        key = f"{prefix}:entity_metadata:{name_str}"
        meta = redis_client.hgetall(key)
        if meta:
            desc = meta.get(b"description", b"").decode()
            if any('\u4e00' <= char <= '\u9fff' for char in desc):
                print(f"Entity: {name_str}")
                print(f"Description: {desc[:100]}...")
                print("-" * 20)

if __name__ == "__main__":
    check_metadata("NuclearElectronicsPaperRepository")
