import json
import logging
import os
import signal
import sys
from threading import Event

from kafka import KafkaConsumer
from prometheus_client import Counter, start_http_server


logger = logging.getLogger("shop-consumer")
logger.setLevel(logging.INFO)

_formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

_stream_handler = logging.StreamHandler(sys.stdout)
_stream_handler.setFormatter(_formatter)
logger.addHandler(_stream_handler)

LOG_FILE_PATH = os.getenv("LOG_FILE_PATH", "/var/log/shop-consumer/consumer.log")
try:
    os.makedirs(os.path.dirname(LOG_FILE_PATH), exist_ok=True)
    _file_handler = logging.FileHandler(LOG_FILE_PATH)
    _file_handler.setFormatter(_formatter)
    logger.addHandler(_file_handler)
except Exception as e:
    # If file logging fails, continue with stdout logging only
    logger.warning("Failed to set up file logging at %s: %s", LOG_FILE_PATH, e)


KAFKA_BOOTSTRAP_SERVERS = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "kafka:9092")
KAFKA_TOPIC = os.getenv("KAFKA_TOPIC", "shop-db-data-changes")
KAFKA_GROUP_ID = os.getenv("KAFKA_GROUP_ID", "shop-consumer-group")

METRICS_PORT = int(os.getenv("METRICS_PORT", "8000"))


db_changes_counter = Counter(
    "db_changes_total",
    "Number of database row changes, labeled by table and operation",
    ["table", "op"],
)


stop_event = Event()


def handle_signal(signum, frame):
    logger.info("Received signal %s, shutting down...", signum)
    stop_event.set()


for sig in (signal.SIGINT, signal.SIGTERM):
    signal.signal(sig, handle_signal)


def parse_canal_json(msg_value: bytes):
    """
    Parse a TiCDC canal-json message to extract table and operation.
    Fallbacks to 'unknown' if structure is not as expected.
    """
    text = None
    try:
        text = msg_value.decode("utf-8")
        payload = json.loads(msg_value.decode("utf-8"))
    except Exception as e:
        logger.warning("Failed to decode JSON message: %s", e)
        return "unknown", "unknown", text

    table = payload.get("table") or "unknown"
    op_raw = payload.get("type") or payload.get("eventType") or "unknown"
    op = op_raw.lower()

    # Normalize op to insert/update/delete/other
    if op.startswith("insert"):
        op = "insert"
    elif op.startswith("update"):
        op = "update"
    elif op.startswith("delete"):
        op = "delete"

    return table, op, payload


def main():
    logger.info(
        "Starting shop-consumer. Kafka: %s, topic: %s, group: %s",
        KAFKA_BOOTSTRAP_SERVERS,
        KAFKA_TOPIC,
        KAFKA_GROUP_ID,
    )

    # Start Prometheus metrics HTTP server
    start_http_server(METRICS_PORT)
    logger.info("Prometheus metrics server listening on port %d", METRICS_PORT)

    consumer = KafkaConsumer(
        KAFKA_TOPIC,
        bootstrap_servers=KAFKA_BOOTSTRAP_SERVERS,
        group_id=KAFKA_GROUP_ID,
        enable_auto_commit=True,
        auto_offset_reset="earliest",
        value_deserializer=lambda v: v,  # keep as bytes, we decode manually
    )

    try:
        for message in consumer:
            if stop_event.is_set():
                break

            value = message.value
            table, op, data = parse_canal_json(value)

            # Prepare log fields
            raw_value = value.decode("utf-8", errors="replace")
            if isinstance(data, dict):
                try:
                    data_str = json.dumps(data, ensure_ascii=False)
                except Exception:
                    data_str = str(data)
            else:
                data_str = str(data) if data is not None else ""

            # Compact, CDC event log line (tagged so Filebeat can filter)
            logger.info(
                "CDC_EVENT topic=%s partition=%s table=%s op=%s value=%s data=%s",
                message.topic,
                message.partition,
                table,
                op,
                raw_value,
                data_str,
            )

            # Increment Prometheus counter
            db_changes_counter.labels(table=table, op=op).inc()

    except Exception as e:
        logger.exception("Error in consumer loop: %s", e)
        sys.exit(1)
    finally:
        logger.info("Closing Kafka consumer")
        consumer.close()


if __name__ == "__main__":
    main()


