"""
Kafka 管理脚本
用于创建主题、测试连接、验证集成等操作
"""

import asyncio
import json
import logging
import sys
from datetime import datetime

from aiokafka import AIOKafkaConsumer, AIOKafkaProducer
from aiokafka.admin import AIOKafkaAdminClient, NewTopic
from aiokafka.errors import KafkaError, TopicAlreadyExistsError

from backend.config.loggers import get_logger
from backend.config.settings import settings

logging.basicConfig(level=logging.INFO)
logger = get_logger(__name__)


async def _close_client(client, method_name: str) -> None:
    """Best-effort Kafka client cleanup that does not mask the original error."""
    if client is None:
        return
    try:
        await getattr(client, method_name)()
    except Exception:
        logger.warning("Failed to close Kafka client", exc_info=True)


async def create_topics():
    """创建所有配置的主题"""
    logger.info("Creating Kafka topics...")

    admin_client = None
    try:
        admin_client = AIOKafkaAdminClient(
            bootstrap_servers=settings.kafka_bootstrap_servers,
        )
        await admin_client.start()

        topic_configs = {
            "max.message.bytes": str(settings.kafka_topic_max_message_bytes),
        }
        topics = [
            NewTopic(
                name=settings.kafka_task_topic,
                num_partitions=settings.kafka_num_partitions,
                replication_factor=settings.kafka_replication_factor,
                topic_configs=topic_configs,
            ),
            NewTopic(
                name=settings.kafka_status_topic,
                num_partitions=settings.kafka_num_partitions,
                replication_factor=settings.kafka_replication_factor,
                topic_configs=topic_configs,
            ),
            NewTopic(
                name=settings.kafka_result_topic,
                num_partitions=settings.kafka_num_partitions,
                replication_factor=settings.kafka_replication_factor,
                topic_configs=topic_configs,
            ),
            NewTopic(
                name=settings.kafka_event_topic,
                num_partitions=3,
                replication_factor=settings.kafka_replication_factor,
                topic_configs=topic_configs,
            ),
            NewTopic(
                name=settings.kafka_retry_topic,
                num_partitions=3,
                replication_factor=settings.kafka_replication_factor,
                topic_configs=topic_configs,
            ),
            NewTopic(
                name=settings.kafka_dead_letter_topic,
                num_partitions=1,
                replication_factor=settings.kafka_replication_factor,
                topic_configs=topic_configs,
            ),
            NewTopic(
                name=settings.log_kafka_topic,
                num_partitions=3,
                replication_factor=settings.kafka_replication_factor,
                topic_configs=topic_configs,
            ),
            NewTopic(
                name=settings.log_kafka_error_topic,
                num_partitions=1,
                replication_factor=settings.kafka_replication_factor,
                topic_configs=topic_configs,
            ),
        ]

        try:
            created = await admin_client.create_topics(new_topics=topics)
            logger.info(f"Topics created successfully: {created}")
        except TopicAlreadyExistsError:
            logger.info("Topics already exist")
        except KafkaError as e:
            logger.error(f"Error creating topics: {e}")

    except Exception as e:
        logger.error(f"Failed to create topics: {e}")
        raise
    finally:
        await _close_client(admin_client, "close")


async def list_topics():
    """列出所有主题"""
    logger.info("Listing Kafka topics...")

    admin_client = None
    try:
        admin_client = AIOKafkaAdminClient(
            bootstrap_servers=settings.kafka_bootstrap_servers,
        )
        await admin_client.start()

        topics = await admin_client.list_topics()
        logger.info(f"Available topics: {topics}")

    except Exception as e:
        logger.error(f"Failed to list topics: {e}")
        raise
    finally:
        await _close_client(admin_client, "close")


async def delete_topics():
    """删除所有配置的主题"""
    logger.warning("Deleting Kafka topics...")

    admin_client = None
    try:
        admin_client = AIOKafkaAdminClient(
            bootstrap_servers=settings.kafka_bootstrap_servers,
        )
        await admin_client.start()

        topics_to_delete = [
            settings.kafka_task_topic,
            settings.kafka_status_topic,
            settings.kafka_result_topic,
            settings.kafka_event_topic,
            settings.kafka_retry_topic,
            settings.kafka_dead_letter_topic,
        ]

        deleted = await admin_client.delete_topics(topics_to_delete)
        logger.info(f"Topics deleted: {deleted}")

    except Exception as e:
        logger.error(f"Failed to delete topics: {e}")
        raise
    finally:
        await _close_client(admin_client, "close")


async def test_connection():
    """测试 Kafka 连接"""
    logger.info("Testing Kafka connection...")

    producer = None
    try:
        producer = AIOKafkaProducer(
            bootstrap_servers=settings.kafka_bootstrap_servers,
        )
        await producer.start()
        logger.info(f"Producer connected to {settings.kafka_bootstrap_servers}")
        logger.info("Kafka connection test successful!")
        return True

    except Exception as e:
        logger.error(f"Kafka connection test failed: {e}")
        return False
    finally:
        await _close_client(producer, "stop")


async def test_produce_consume():
    """测试生产和消费"""
    logger.info("Testing produce and consume...")

    test_topic = f"test-{datetime.now().strftime('%Y%m%d%H%M%S')}"
    test_message = {
        "message_id": "test-123",
        "timestamp": int(datetime.now().timestamp() * 1000),
        "content": "Test message",
    }

    admin_client = None
    producer = None
    consumer = None
    try:
        admin_client = AIOKafkaAdminClient(
            bootstrap_servers=settings.kafka_bootstrap_servers,
        )
        await admin_client.start()

        test_topic_obj = NewTopic(
            name=test_topic,
            num_partitions=1,
            replication_factor=1,
        )

        try:
            await admin_client.create_topics([test_topic_obj])
            logger.info(f"Test topic created: {test_topic}")
        except TopicAlreadyExistsError:
            logger.info(f"Test topic already exists: {test_topic}")

        producer = AIOKafkaProducer(
            bootstrap_servers=settings.kafka_bootstrap_servers,
            value_serializer=lambda v: json.dumps(v).encode("utf-8"),
        )
        await producer.start()

        await producer.send_and_wait(
            topic=test_topic,
            value=test_message,
            key="test-key",
        )
        logger.info("Test message sent")

        consumer = AIOKafkaConsumer(
            test_topic,
            bootstrap_servers=settings.kafka_bootstrap_servers,
            group_id="test-group",
            auto_offset_reset="earliest",
            value_deserializer=lambda v: json.loads(v.decode("utf-8")),
        )
        await consumer.start()

        received = await asyncio.wait_for(
            consumer.getone(),
            timeout=10.0,
        )
        logger.info(f"Test message received: {received.value}")

        logger.info("Produce and consume test successful!")
        return True

    except asyncio.TimeoutError:
        logger.error("Test timed out waiting for message")
        return False
    except Exception as e:
        logger.error(f"Produce and consume test failed: {e}")
        return False
    finally:
        await _close_client(consumer, "stop")
        await _close_client(producer, "stop")
        await _close_client(admin_client, "close")


async def describe_topics():
    """描述主题信息"""
    logger.info("Describing Kafka topics...")

    admin_client = None
    try:
        admin_client = AIOKafkaAdminClient(
            bootstrap_servers=settings.kafka_bootstrap_servers,
        )
        await admin_client.start()

        topic_descriptions = await admin_client.describe_topics(
            [
                settings.kafka_task_topic,
                settings.kafka_status_topic,
                settings.kafka_result_topic,
                settings.kafka_event_topic,
                settings.kafka_retry_topic,
                settings.kafka_dead_letter_topic,
            ]
        )

        for desc in topic_descriptions:
            logger.info(f"Topic: {desc.topic}")
            logger.info(f"  Partitions: {len(desc.partitions)}")
            for partition in desc.partitions:
                logger.info(
                    f"    Partition {partition.partition_id}: "
                    f"leader={partition.leader}, "
                    f"replicas={partition.replicas}, "
                    f"isr={partition.isr}"
                )

    except Exception as e:
        logger.error(f"Failed to describe topics: {e}")
        raise
    finally:
        await _close_client(admin_client, "close")


async def describe_consumer_groups():
    """描述消费者组"""
    logger.info("Describing Kafka consumer groups...")

    admin_client = None
    try:
        admin_client = AIOKafkaAdminClient(
            bootstrap_servers=settings.kafka_bootstrap_servers,
        )
        await admin_client.start()

        groups = [
            settings.kafka_task_group_id,
            settings.kafka_status_group_id,
            settings.kafka_result_group_id,
            settings.kafka_event_group_id,
            settings.kafka_dead_letter_group_id,
        ]

        for group_id in groups:
            try:
                descriptions = await admin_client.describe_consumer_groups([group_id])
                for desc in descriptions:
                    logger.info(f"Group: {desc.group_id}")
                    logger.info(f"  State: {desc.state}")
                    logger.info(f"  Members: {len(desc.members)}")
                    for member in desc.members:
                        logger.info(
                            f"    Member: {member.member_id}, "
                            f"client={member.client_id}, "
                            f"host={member.host}"
                        )
            except Exception as e:
                logger.warning(f"Could not describe group {group_id}: {e}")

    except Exception as e:
        logger.error(f"Failed to describe consumer groups: {e}")
        raise
    finally:
        await _close_client(admin_client, "close")


async def run_integration_test():
    """运行完整的集成测试"""
    logger.info("Running Kafka integration test...")

    tests = [
        ("Connection Test", test_connection),
        ("Topic Creation", create_topics),
        ("List Topics", list_topics),
        ("Describe Topics", describe_topics),
        ("Produce/Consume Test", test_produce_consume),
    ]

    results = []
    for name, test_func in tests:
        logger.info(f"\n{'='*50}")
        logger.info(f"Running: {name}")
        logger.info(f"{'='*50}")

        try:
            success = await test_func()
            results.append((name, success))
        except Exception as e:
            logger.error(f"Test {name} failed: {e}")
            results.append((name, False))

    logger.info(f"\n{'='*50}")
    logger.info("Integration Test Results:")
    logger.info(f"{'='*50}")

    for name, success in results:
        status = "✓ PASS" if success else "✗ FAIL"
        logger.info(f"{status}: {name}")

    all_passed = all(success for _, success in results)
    logger.info(
        f"\nOverall: {'All tests passed!' if all_passed else 'Some tests failed!'}"
    )

    return all_passed


async def print_config():
    """打印当前 Kafka 配置"""
    logger.info("Current Kafka Configuration:")
    logger.info(f"{'='*50}")

    config_items = [
        ("Bootstrap Servers", settings.kafka_bootstrap_servers),
        ("Task Topic", settings.kafka_task_topic),
        ("Status Topic", settings.kafka_status_topic),
        ("Result Topic", settings.kafka_result_topic),
        ("Event Topic", settings.kafka_event_topic),
        ("Retry Topic", settings.kafka_retry_topic),
        ("Dead Letter Topic", settings.kafka_dead_letter_topic),
        ("Task Group ID", settings.kafka_task_group_id),
        ("Status Group ID", settings.kafka_status_group_id),
        ("Result Group ID", settings.kafka_result_group_id),
        ("Event Group ID", settings.kafka_event_group_id),
        ("Num Partitions", settings.kafka_num_partitions),
        ("Replication Factor", settings.kafka_replication_factor),
        ("Producer Acks", settings.kafka_producer_acks),
        ("Consumer Auto Offset Reset", settings.kafka_auto_offset_reset),
    ]

    for key, value in config_items:
        logger.info(f"  {key}: {value}")


def main():
    """主函数"""
    import argparse

    parser = argparse.ArgumentParser(description="Kafka Management Script")
    parser.add_argument(
        "command",
        choices=[
            "create-topics",
            "delete-topics",
            "list-topics",
            "describe-topics",
            "describe-groups",
            "test-connection",
            "test-produce-consume",
            "test-integration",
            "print-config",
        ],
        help="Command to execute",
    )

    args = parser.parse_args()

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    try:
        if args.command == "create-topics":
            loop.run_until_complete(create_topics())
        elif args.command == "delete-topics":
            loop.run_until_complete(delete_topics())
        elif args.command == "list-topics":
            loop.run_until_complete(list_topics())
        elif args.command == "describe-topics":
            loop.run_until_complete(describe_topics())
        elif args.command == "describe-groups":
            loop.run_until_complete(describe_consumer_groups())
        elif args.command == "test-connection":
            success = loop.run_until_complete(test_connection())
            sys.exit(0 if success else 1)
        elif args.command == "test-produce-consume":
            success = loop.run_until_complete(test_produce_consume())
            sys.exit(0 if success else 1)
        elif args.command == "test-integration":
            success = loop.run_until_complete(run_integration_test())
            sys.exit(0 if success else 1)
        elif args.command == "print-config":
            loop.run_until_complete(print_config())

    except KeyboardInterrupt:
        logger.info("Operation cancelled")
    except Exception as e:
        logger.error(f"Operation failed: {e}")
        sys.exit(1)
    finally:
        loop.close()


if __name__ == "__main__":
    main()
