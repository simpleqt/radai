import sys
import os
import asyncio
from pprint import pformat

# Ensure backend is in python path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../../')))

from backend.application.explore.service import PRESET_TOPICS, _llm_json
from backend.application.explore.prompts import build_map_prompt

sem = asyncio.Semaphore(10)

async def generate_topic_map(subject: str, topic: str):
    async with sem:
        print(f"Generating map for [{subject}] -> {topic}...")
        prompt = build_map_prompt(subject=subject, topic=topic, max_children=6, context="")
        for attempt in range(3):
            try:
                parsed = await _llm_json(prompt, model_name=None, timeout=50.0, max_tokens=4096, temperature=0.5)
                if isinstance(parsed, dict) and "root" in parsed and "children" in parsed:
                    print(f"Successfully generated for {topic} on attempt {attempt+1}")
                    return topic, parsed
            except Exception as e:
                print(f"Error for {topic} on attempt {attempt+1}: {e}")
            await asyncio.sleep(2)
        print(f"Failed to generate for {topic}, using fallback structure")
        # Fallback structure
        fallback_children = [
            {"title": f"{topic}基础与定义", "summary": f"介绍{topic}的基本定义、核心概念及在学科中的基础定位", "expandable": True},
            {"title": f"{topic}工作原理", "summary": f"阐述{topic}的核心工作原理、物理机制与物理过程", "expandable": True},
            {"title": f"{topic}典型结构与分类", "summary": f"分析{topic}的常见分类方法、典型电路结构或组成模块", "expandable": True},
            {"title": f"{topic}关键性能指标", "summary": f"剖析影响{topic}性能的关键参数、性能指标及测试方法", "expandable": True},
            {"title": f"{topic}实际应用与系统集成", "summary": f"探讨{topic}的典型应用场景以及在整体测量系统中的集成设计", "expandable": True},
        ]
        return topic, {
            "root": {"title": topic, "summary": f"关于{topic}的核心原理、结构设计与实际应用的系统学习路径。"},
            "children": fallback_children
        }

async def main():
    tasks = []
    for subject, topics in PRESET_TOPICS.items():
        for topic_info in topics:
            topic = topic_info["title"]
            tasks.append(generate_topic_map(subject, topic))
    
    results = await asyncio.gather(*tasks)
    
    preset_dict = {}
    for topic, parsed in results:
        preset_dict[topic] = parsed
        
    out_file = os.path.join(os.path.dirname(__file__), 'preset_maps.py')
    with open(out_file, 'w', encoding='utf-8') as f:
        f.write("# -*- coding: utf-8 -*-\n")
        f.write("\"\"\"Preset mindmaps for fast instant retrieval.\"\"\"\n\n")
        f.write("PRESET_MAPS = ")
        f.write(pformat(preset_dict, width=120, sort_dicts=False))
        f.write("\n")
        
    print(f"Done! Written preset maps to {out_file}")

if __name__ == '__main__':
    asyncio.run(main())
