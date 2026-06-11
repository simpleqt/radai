"""
实体名称归一化工具

在实体提取阶段对实体名称进行归一化处理，避免同一实体因名称变体而被分别存储。

归一化规则：
1. 统一转换为小写
2. 去除首尾空格
3. 将多个连续空格替换为单个空格
"""

import re


def normalize_entity_name(name: str) -> str:
    """
    归一化实体名称
    
    Args:
        name: 原始实体名称
        
    Returns:
        归一化后的实体名称
        
    Examples:
        >>> normalize_entity_name("  GEANT4  ")
        "geant4"
        >>> normalize_entity_name("Picosec  MicroMegas   Detector")
        "picosec micromegas detector"
        >>> normalize_entity_name("COMPASS")
        "compass"
    """
    if not name:
        return ""
    
    normalized = name.strip()
    
    if not normalized:
        return ""
    
    normalized = normalized.lower()
    
    normalized = re.sub(r'\s+', ' ', normalized)
    
    return normalized
