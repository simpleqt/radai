import asyncio
import json
import sys
from pathlib import Path

# Add project root to path
sys.path.append(str(Path(__file__).resolve().parents[3]))

from backend.application.explore.service import PRESET_TOPICS, generate_map

async def generate_all():
    preset_maps = {}
    
    output_path = Path(__file__).resolve().parent / "preset_maps.json"
    if output_path.exists():
        try:
            with open(output_path, "r", encoding="utf-8") as f:
                preset_maps = json.load(f)
            print(f"Loaded {len(preset_maps)} existing maps from {output_path}")
        except Exception as e:
            print(f"Error loading existing maps: {e}")

    # Semaphores to control concurrency. We can use 3 for faster generation.
    sem = asyncio.Semaphore(3)

    async def process_topic(subject, topic):
        key = f"{subject}:{topic}"
        # If already exists and is valid (not fallback, i.e., child title doesn't end with "·基本概念" or summary is not empty)
        if key in preset_maps:
            children = preset_maps[key].get("children", [])
            if children and not children[0]["title"].endswith("·基本概念") and children[0].get("summary"):
                print(f"Skipping already generated valid topic: {key}")
                return

        async with sem:
            for attempt in range(5):
                try:
                    print(f"Generating map for {key} (attempt {attempt+1})...")
                    import backend.application.explore.service as service_mod
                    # Clear cache to avoid getting cached fallback or cached results
                    service_mod._mindmap_cache.clear()
                    service_mod.TIMEOUT_MAP = 60.0
                    
                    res = await generate_map(topic=topic, kb_id=None, subject=subject)
                    if res.get("source") == "llm" and res.get("children"):
                        # Success! Check if it's a real LLM map (summary should be non-empty)
                        children = res["children"]
                        if children and children[0].get("summary"):
                            clean_res = {
                                "root": res["root"],
                                "children": res["children"]
                            }
                            preset_maps[key] = clean_res
                            print(f"Successfully generated map for {key}")
                            # Save periodically
                            with open(output_path, "w", encoding="utf-8") as f:
                                json.dump(preset_maps, f, ensure_ascii=False, indent=2)
                            return
                    print(f"Attempt {attempt+1} for {key} returned fallback or invalid map. Retrying...")
                except Exception as e:
                    print(f"Error generating map for {key} on attempt {attempt+1}: {e}")
                await asyncio.sleep(3)
            print(f"WARNING: Failed to generate non-fallback map for {key} after all attempts.")

    tasks = []
    for subject, topics in PRESET_TOPICS.items():
        for t in topics:
            tasks.append(process_topic(subject, t["title"]))

    await asyncio.gather(*tasks)
    
    # Final save
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(preset_maps, f, ensure_ascii=False, indent=2)
    print(f"Done! Saved all preset maps to {output_path}")

if __name__ == "__main__":
    asyncio.run(generate_all())
