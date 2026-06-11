"""
Kafka 自定义异常定义
"""


class KafkaException(Exception):
    """Kafka 基础异常"""

    pass


class ProducerException(KafkaException):
    """生产者异常"""

    pass


class ConsumerException(KafkaException):
    """消费者异常"""

    pass


class TopicNotFoundException(KafkaException):
    """主题不存在异常"""

    pass


class MessageSendException(ProducerException):
    """消息发送异常"""

    pass


class MessageConsumeException(ConsumerException):
    """消息消费异常"""

    pass


class ConsumerGroupNotFoundException(KafkaException):
    """消费者组不存在异常"""

    pass


class PartitionAssignmentException(KafkaException):
    """分区分配异常"""

    pass


class OffsetCommitException(ConsumerException):
    """Offset 提交异常"""

    pass


class ConnectionException(KafkaException):
    """连接异常"""

    pass


class AuthenticationException(KafkaException):
    """认证异常"""

    pass


class AuthorizationException(KafkaException):
    """授权异常"""

    pass


class RetriableException(KafkaException):
    """可重试异常"""

    pass


class NonRetriableException(KafkaException):
    """不可重试异常"""

    pass
