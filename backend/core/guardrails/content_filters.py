"""
内容安全过滤器 - 检测和过滤不安全内容
"""

import re
from dataclasses import dataclass
from enum import Enum
from typing import Any


class ContentSafetyLevel(Enum):
    """内容安全级别"""

    SAFE = "safe"
    WARNING = "warning"
    UNSAFE = "unsafe"


@dataclass
class FilterResult:
    """过滤结果"""

    is_safe: bool
    safety_level: ContentSafetyLevel
    issues: list[str]
    filtered_content: str
    details: dict[str, Any]


class ContentFilter:
    """内容安全过滤器"""

    # 敏感信息正则模式
    PATTERNS = {
        "phone": r"1[3-9]\d{9}",
        "email": r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}",
        "id_card": r"\d{17}[\dXx]",
        "credit_card": r"\d{4}[\s-]?\d{4}[\s-]?\d{4}[\s-]?\d{4}",
        "ip_address": r"\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}",
        "bank_account": r"\d{16,19}",
    }

    # 不安全关键词（示例，实际应该更完善）
    UNSAFE_KEYWORDS = [
        "暴力",
        "色情",
        "赌博",
        "毒品",
        "恐怖",
        "诈骗",
        "hack",
        "crack",
        "exploit",
        "malware",
        "virus",
        "bomb",
        "weapon",
        "kill",
        "murder",
        "assault",
    ]

    # Prompt Injection 检测模式
    INJECTION_PATTERNS = [
        r"ignore\s+previous\s+instructions",
        r"ignore\s+all\s+previous",
        r"disregard\s+previous",
        r"forget\s+previous",
        r"you\s+are\s+now",
        r"new\s+instructions",
        r"system\s*:\s*",
        r"assistant\s*:\s*",
        r"\[SYSTEM\]",
        r"\[INST\]",
        r"<\|im_start\|>",
        r"<\|im_end\|>",
        r"jailbreak",
        r"bypass",
        r"override",
    ]

    # OCR文本敏感信息模式
    OCR_PATTERNS = {
        "password": r"password\s*[:=]\s*\S+",
        "api_key": r"api[_-]?key\s*[:=]\s*\S+",
        "token": r"token\s*[:=]\s*\S+",
        "secret": r"secret\s*[:=]\s*\S+",
    }

    def __init__(
        self,
        enable_pii_detection: bool = True,
        enable_content_safety: bool = True,
        enable_injection_detection: bool = True,
        enable_ocr_filtering: bool = True,
        mask_pii: bool = True,
    ):
        """
        初始化内容过滤器

        Args:
            enable_pii_detection: 是否启用个人信息检测
            enable_content_safety: 是否启用内容安全检查
            enable_injection_detection: 是否启用注入检测
            enable_ocr_filtering: 是否启用OCR文本过滤
            mask_pii: 是否脱敏个人信息
        """
        self.enable_pii_detection = enable_pii_detection
        self.enable_content_safety = enable_content_safety
        self.enable_injection_detection = enable_injection_detection
        self.enable_ocr_filtering = enable_ocr_filtering
        self.mask_pii = mask_pii

    def filter_input(self, text: str) -> FilterResult:
        """
        过滤输入内容

        Args:
            text: 输入文本

        Returns:
            FilterResult: 过滤结果
        """
        issues = []
        details = {}
        filtered_text = text
        safety_level = ContentSafetyLevel.SAFE

        # 1. 检测 Prompt Injection
        if self.enable_injection_detection:
            injection_detected, injection_patterns = self._detect_injection(text)
            if injection_detected:
                issues.append("检测到可能的 Prompt Injection 攻击")
                details["injection_patterns"] = injection_patterns
                safety_level = ContentSafetyLevel.UNSAFE

        # 2. 检测个人敏感信息
        if self.enable_pii_detection:
            pii_found, pii_types = self._detect_pii(text)
            if pii_found:
                issues.append(f"检测到个人敏感信息: {', '.join(pii_types)}")
                details["pii_types"] = pii_types
                if safety_level == ContentSafetyLevel.SAFE:
                    safety_level = ContentSafetyLevel.WARNING

                # 脱敏处理
                if self.mask_pii:
                    filtered_text = self._mask_pii(filtered_text)

        # 3. 检测不安全内容
        if self.enable_content_safety:
            unsafe_detected, unsafe_keywords = self._detect_unsafe_content(text)
            if unsafe_detected:
                issues.append(f"检测到不安全内容: {', '.join(unsafe_keywords)}")
                details["unsafe_keywords"] = unsafe_keywords
                safety_level = ContentSafetyLevel.UNSAFE

        # 4. 检查长度
        if len(text) > 100000:
            issues.append("输入文本过长（超过 100000 字符）")
            if safety_level == ContentSafetyLevel.SAFE:
                safety_level = ContentSafetyLevel.WARNING

        is_safe = safety_level != ContentSafetyLevel.UNSAFE

        return FilterResult(
            is_safe=is_safe,
            safety_level=safety_level,
            issues=issues,
            filtered_content=filtered_text,
            details=details,
        )

    def filter_output(self, text: str) -> FilterResult:
        """
        过滤输出内容

        Args:
            text: 输出文本

        Returns:
            FilterResult: 过滤结果
        """
        issues = []
        details = {}
        filtered_text = text
        safety_level = ContentSafetyLevel.SAFE

        # 1. 检测个人敏感信息泄露
        if self.enable_pii_detection:
            pii_found, pii_types = self._detect_pii(text)
            if pii_found:
                issues.append(f"输出包含敏感信息: {', '.join(pii_types)}")
                details["pii_types"] = pii_types
                safety_level = ContentSafetyLevel.WARNING

                # 脱敏处理
                if self.mask_pii:
                    filtered_text = self._mask_pii(filtered_text)

        # 2. 检测不安全内容
        if self.enable_content_safety:
            unsafe_detected, unsafe_keywords = self._detect_unsafe_content(text)
            if unsafe_detected:
                issues.append(f"输出包含不安全内容: {', '.join(unsafe_keywords)}")
                details["unsafe_keywords"] = unsafe_keywords
                safety_level = ContentSafetyLevel.UNSAFE

        # 3. OCR文本敏感信息检测
        if self.enable_ocr_filtering:
            ocr_sensitive_found, ocr_types = self._detect_ocr_sensitive(text)
            if ocr_sensitive_found:
                issues.append(f"OCR文本包含敏感信息: {', '.join(ocr_types)}")
                details["ocr_sensitive_types"] = ocr_types
                if safety_level == ContentSafetyLevel.SAFE:
                    safety_level = ContentSafetyLevel.WARNING

                # 脱敏处理
                if self.mask_pii:
                    filtered_text = self._mask_ocr_sensitive(filtered_text)

        is_safe = safety_level != ContentSafetyLevel.UNSAFE

        return FilterResult(
            is_safe=is_safe,
            safety_level=safety_level,
            issues=issues,
            filtered_content=filtered_text,
            details=details,
        )

    def _detect_injection(self, text: str) -> tuple[bool, list[str]]:
        """检测 Prompt Injection"""
        detected_patterns = []
        text_lower = text.lower()

        for pattern in self.INJECTION_PATTERNS:
            if re.search(pattern, text_lower, re.IGNORECASE):
                detected_patterns.append(pattern)

        return len(detected_patterns) > 0, detected_patterns

    def _detect_pii(self, text: str) -> tuple[bool, list[str]]:
        """检测个人敏感信息"""
        found_types = []

        for pii_type, pattern in self.PATTERNS.items():
            if re.search(pattern, text):
                found_types.append(pii_type)

        return len(found_types) > 0, found_types

    def _mask_pii(self, text: str) -> str:
        """脱敏个人信息"""
        masked_text = text

        # 手机号脱敏
        masked_text = re.sub(
            self.PATTERNS["phone"],
            lambda m: m.group()[:3] + "****" + m.group()[-4:],
            masked_text,
        )

        # 邮箱脱敏
        masked_text = re.sub(
            self.PATTERNS["email"],
            lambda m: m.group().split("@")[0][:2] + "***@" + m.group().split("@")[1],
            masked_text,
        )

        # 身份证脱敏
        masked_text = re.sub(
            self.PATTERNS["id_card"],
            lambda m: m.group()[:6] + "********" + m.group()[-4:],
            masked_text,
        )

        # 信用卡脱敏
        masked_text = re.sub(
            self.PATTERNS["credit_card"],
            lambda m: "****-****-****-" + re.sub(r"[\s-]", "", m.group())[-4:],
            masked_text,
        )

        # IP 地址脱敏
        masked_text = re.sub(
            self.PATTERNS["ip_address"],
            lambda m: ".".join(m.group().split(".")[:2]) + ".***.***.***",
            masked_text,
        )

        # 银行账号脱敏
        masked_text = re.sub(
            self.PATTERNS["bank_account"],
            lambda m: "*" * (len(m.group()) - 4) + m.group()[-4:],
            masked_text,
        )

        return masked_text

    def _detect_unsafe_content(self, text: str) -> tuple[bool, list[str]]:
        """检测不安全内容"""
        found_keywords = []
        text_lower = text.lower()

        for keyword in self.UNSAFE_KEYWORDS:
            if keyword.lower() in text_lower:
                found_keywords.append(keyword)

        return len(found_keywords) > 0, found_keywords

    def _detect_ocr_sensitive(self, text: str) -> tuple[bool, list[str]]:
        """检测OCR文本敏感信息"""
        found_types = []
        text_lower = text.lower()

        for sensitive_type, pattern in self.OCR_PATTERNS.items():
            if re.search(pattern, text_lower, re.IGNORECASE):
                found_types.append(sensitive_type)

        return len(found_types) > 0, found_types

    def _mask_ocr_sensitive(self, text: str) -> str:
        """脱敏OCR敏感信息"""
        masked_text = text

        # 脱敏密码
        masked_text = re.sub(
            self.OCR_PATTERNS["password"],
            lambda m: m.group().split("=")[0] + "=****",
            masked_text,
            flags=re.IGNORECASE,
        )

        # 脱敏API密钥
        masked_text = re.sub(
            self.OCR_PATTERNS["api_key"],
            lambda m: m.group().split("=")[0] + "=****",
            masked_text,
            flags=re.IGNORECASE,
        )

        # 脱敏token
        masked_text = re.sub(
            self.OCR_PATTERNS["token"],
            lambda m: m.group().split("=")[0] + "=****",
            masked_text,
            flags=re.IGNORECASE,
        )

        # 脱敏secret
        masked_text = re.sub(
            self.OCR_PATTERNS["secret"],
            lambda m: m.group().split("=")[0] + "=****",
            masked_text,
            flags=re.IGNORECASE,
        )

        return masked_text

    def add_unsafe_keyword(self, keyword: str):
        """添加不安全关键词"""
        if keyword not in self.UNSAFE_KEYWORDS:
            self.UNSAFE_KEYWORDS.append(keyword)

    def add_injection_pattern(self, pattern: str):
        """添加注入检测模式"""
        if pattern not in self.INJECTION_PATTERNS:
            self.INJECTION_PATTERNS.append(pattern)

    def remove_unsafe_keyword(self, keyword: str):
        """移除不安全关键词"""
        if keyword in self.UNSAFE_KEYWORDS:
            self.UNSAFE_KEYWORDS.remove(keyword)

    def remove_injection_pattern(self, pattern: str):
        """移除注入检测模式"""
        if pattern in self.INJECTION_PATTERNS:
            self.INJECTION_PATTERNS.remove(pattern)


# 创建默认过滤器实例
default_filter = ContentFilter()
strict_filter = ContentFilter(
    enable_content_safety=True, enable_injection_detection=True
)
ocr_filter = ContentFilter(enable_ocr_filtering=True)
