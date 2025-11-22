## README

### Overview

This project implements a local TiDB + Kafka + CDC + monitoring stack using **one `docker-compose.yml`** at the repository root that defines **all services together** (DB, CDC, Kafka, consumer, logging, and monitoring).  
Bringing the stack up with a single command gives you a fully wired demo environment.

```bash
docker compose up -d --build
```
---

## How to run and verify

From the repo root:

```bash
docker compose up -d --build
```

### 1. Generate CDC events in TiDB

Run the following to insert/update data in `shop` (using `shop_admin`).  
You will also see the 3 initial `CREATE TABLE` DDL events in the CDC stream (and 1 'unknown' which is probably another DDL), in addition to the **3 inserts** and **1 update** generated here:

```bash
mysql -h127.0.0.1 -P4000 -ushop_admin -ppassword <<'SQL'
USE shop;

INSERT INTO suppliers (name, contact_email, phone)
VALUES ('CDC Supplier', 'cdc-supplier@example.com', '050-0000000');

INSERT INTO customers (name, email, phone)
VALUES ('CDC Customer', 'cdc-customer@example.com', '050-1111111');

INSERT INTO products (name, price, stock, supplier_id)
SELECT 'CDC Product', 42.50, 7, id
FROM suppliers
WHERE contact_email = 'cdc-supplier@example.com';

UPDATE products
SET price = 55.00, stock = 3
WHERE name = 'CDC Product';
SQL
```

### 2. Check Prometheus metrics from the consumer

- **Prometheus UI**: `http://localhost:9090`
- **Metric to look for**: `db_changes_total`

From the host you can also hit the consumer’s metrics endpoint directly:

```bash
curl http://localhost:8000/metrics | grep db_changes_total
```

You should see `db_changes_total{table="...",op="insert"}` / `update` etc. increase after running the SQL above.

### 3. Check CDC events in Elasticsearch

```bash
curl 'http://localhost:9200/cdc-events-*/_search?pretty&size=20'
```

You should see documents corresponding to the `CDC_EVENT` log lines from the consumer for the changes you just made.

### 4. Open Grafana dashboard

- **Grafana UI**: `http://localhost:3000`
- **Default credentials**: user `admin`, password `admin`

After logging in, you can explore the pre-provisioned `Shop CDC Dashboard` (if it loaded correctly) or create your own panels using:
- Prometheus datasource with the `db_changes_total` metric.
- Elasticsearch datasource with index pattern `cdc-events-*`.

---

## Project structure

Top-level layout (this directory):

```text
.
  docker-compose.yml          # All services in one compose file

  config/
    cdc-config.toml           # TiCDC filter config (captures only shop.* tables)

  init-db/
    Dockerfile                # Image that waits for TiDB and initializes schema
    init.sh                   # Waits for TiDB, applies schema and default user
    schema.sql                # shop DB, tables, shop_admin user

  shop-consumer/
    Dockerfile                # Python consumer image (Kafka + Prometheus client)
    requirements.txt          # Python dependencies
    consumer.py               # Kafka consumer + Prometheus metrics + logging

  filebeat/
    filebeat.yml              # Tails consumer log file and ships CDC events to Elasticsearch

  monitoring/
    prometheus.yml            # Scrapes shop-consumer /metrics
    grafana/
      provisioning/
        datasources/
          datasources.yml     # Prometheus + Elasticsearch datasources
        dashboards/
          dashboards.yml      # Points Grafana to the dashboards folder
      dashboards/
        shop-dashboard.json   # Pre-provisioned CDC dashboard (table + pie chart)

```

---

## Docker Compose layout

All services are defined in **one `docker-compose.yml`**, grouped logically.

### 1. TiDB core services

- `pd`: Placement driver for TiDB.
- `tikv`: TiKV storage node.
- `tidb`: TiDB SQL front-end (MySQL protocol, exposed on `localhost:4000`).
- `init-db`:
  - Custom image built from `init-db/Dockerfile`.
  - `init.sh` waits for TiDB to become ready, then runs `schema.sql`.
  - Creates `shop` database, tables, and the `shop_admin` DB user.

### 2. TiCDC / CDC init

- `ticdc`:
  - Runs `/cdc server` pointing at PD (`--pd=http://pd:2379`) and listens on `ticdc:8300`.
  - This is the long-running TiCDC server.
- `ticdc-init` (container name `init-ticdc`):
  - One-shot container.
  - After a small sleep, runs  
    `/cdc cli --server=http://ticdc:8300 changefeed create ...`.
  - Creates (or resumes) a changefeed that streams **all changes from `shop.*`** to Kafka topic `shop-db-data-changes` in **Canal-JSON** format.
  - Uses `config/cdc-config.toml` to filter only `shop.*`.

### 3. Kafka stack

- `zookeeper`: Zookeeper for Kafka.
- `kafka`: Single Kafka broker on `kafka:9092` (mapped to `localhost:9092`).
- `kafka-ui`:
  - Web UI at `localhost:8080`.
  - Lets you inspect topics like `shop-db-data-changes`.

### 4. Consumer / processing stack

- `shop-consumer`:
  - Built from `shop-consumer/Dockerfile`.
  - Python app that:
    - Consumes `shop-db-data-changes` from Kafka.
    - Parses Canal-JSON messages to extract `table` and `op` (insert/update/delete).
    - Logs a compact line per CDC event:
      - `CDC_EVENT topic=... partition=... table=... op=... value=... data=...`.
    - Exposes Prometheus metrics on port `8000`:
      - `db_changes_total{table="<table>", op="<insert|update|delete|unknown>"}`.
  - Also writes logs to `/var/log/shop-consumer/consumer.log`, which Filebeat tails.

### 5. Monitoring & logging stack

- `elasticsearch`:
  - Single-node Elasticsearch on `localhost:9200`.
  - Stores CDC event logs in indices like `cdc-events-YYYY.MM.DD`.

- `filebeat`:
  - Tails `/var/log/shop-consumer/consumer.log`.
  - Uses a processor to keep only lines containing `CDC_EVENT`.
  - Sends those to Elasticsearch with index pattern `cdc-events-*`.

- `prometheus`:
  - Uses `monitoring/prometheus.yml`.
  - Scrapes `shop-consumer:8000/metrics`.
  - Provides `db_changes_total` time-series per `table` and `op`.

- `grafana`:
  - Auto-provisioned via:
    - `monitoring/grafana/provisioning/datasources/datasources.yml` (Prometheus + Elasticsearch datasources).
    - `monitoring/grafana/provisioning/dashboards/dashboards.yml` (loads dashboards from `/var/lib/grafana/dashboards`).
    - `monitoring/grafana/dashboards/shop-dashboard.json`.
  - Exposed at `localhost:3000` (admin/admin).
  - Contains a single dashboard:
    - Table panel: raw CDC events from Elasticsearch (`cdc-events-*`).
    - Pie chart: share of insert/update/delete over last 1h using  
      `sum by (op) (increase(db_changes_total[1h]))`.

---

## Implementation per requirement

### Part 1: TiDB Implementation

#### 1. Database Setup (TiDB + Docker)

- **Use TiDB database**:
  - TiDB cluster: `pd`, `tikv`, `tidb` services.
  - TiDB exposed on `localhost:4000` using MySQL protocol.

- **Configure TiDB in Docker environment**:
  - All TiDB services in the root `docker-compose.yml`.
  - Persistent storage via named volumes (`tikv-data`) and bind mounts (`./data`, `./logs`).

#### 2. Message Queue Integration (Kafka)

- **Implement Apache Kafka as message broker**:
  - `kafka` service (Confluent Kafka image) + `zookeeper`.
- **Set up Kafka brokers in Docker environment**:
  - Kafka listens on `kafka:9092` for internal services, `localhost:9092` for host tools.
  - `kafka-ui` for inspecting topics via ui.

#### 3. Database Initialization (auto schema + user)

- **When Docker loads, automatically:**
  - **Import database tables structure**:
    - `init-db` waits for TiDB and runs `schema.sql`.
    - Creates database `shop` and tables:
      - `customers` (id PK, name, email, phone, created_at).
      - `suppliers` (id PK, name, contact_email, phone, created_at).
      - `products` (id PK, name, price, stock, supplier_id FK → `suppliers.id`, created_at).
  - **Create a default user with password**:
    - In `schema.sql`:
      - `CREATE USER IF NOT EXISTS 'shop_admin'@'%' IDENTIFIED BY 'password';`
      - `GRANT ALL PRIVILEGES ON shop.* TO 'shop_admin'@'%';`.
- **Why a separate `init-db` service?**
  - Keeps the TiDB image standard (no schema baked in).
  - Simple, idempotent init that:
    - Waits for the DB to be reachable.
    - Applies schema and user creation safely on every `docker compose up`.
  - Easy to reason about and rerun without dropping data.

#### 4. Database Change Monitoring (CDC with TiDB)

- **Every update/insert/delete operation in the database should be logged**:
  - TiCDC changefeed streams row changes from `shop.*` into Kafka topic `shop-db-data-changes`.

- **Implement using Change Data Capture (CDC) with TiDB**:
  - `ticdc` server: `/cdc server --pd=http://pd:2379 ... --addr=0.0.0.0:8300 --advertise-addr=ticdc:8300`.

- **Run the TiDB CDC component and configure CDC task**:
  - `ticdc-init` (`init-ticdc`) is a one-shot container that runs:
    - `/cdc cli --server=http://ticdc:8300 changefeed create ...`.
  - Uses `config/cdc-config.toml` with `[filter] rules = ['shop.*']` to capture only the `shop` database.
- **Why a separate `ticdc-init` service?**
  - Keeps the `ticdc` container focused on the long-running server only.
  - Avoids mixing lifecycle concerns (server vs. CLI) and simplifies restarts.
  - The init container can safely retry creation/resume of the changefeed without impacting the TiCDC server itself.

- **TiCDC component included and configured in `docker-compose.yml`**:
  - Both `ticdc` and `ticdc-init` are services declared in the same compose file.
  - Changefeed sink:  
    `kafka://kafka:9092/shop-db-data-changes?protocol=canal-json&partition-num=1&replication-factor=1`.

- **Ensure CDC task is automatically started when Docker environment loads**:
  - `ticdc-init` runs on startup, creating (or resuming) the changefeed after TiCDC and Kafka are available.

---

### Part 2: Monitoring & Logging

#### 1. Real-time Data Processing (Kafka consumer + Prometheus)

> The original requirement asked for Node.js; here it is implemented as a **Python** containerized app using `kafka-python` and `prometheus_client`. The behavior matches the requirements.

- **Consumes database change messages from Kafka**:
  - `shop-consumer` subscribes to `shop-db-data-changes` from `kafka:9092`.

- **Processes these changes and logs them**:
  - For each message:
    - Decodes Canal-JSON payload.
    - Extracts `table` and `op` (insert/update/delete).
    - Logs a single line with `CDC_EVENT` prefix and key fields.

- **For each consumed message, increase a Prometheus counter**:
  - Prometheus client `db_changes_total`:
    - Metric: `db_changes_total`.
    - Labels: `table`, `op` (`insert` / `update` / `delete` / `unknown`).
    - Endpoint: `/metrics` on port `8000`.

#### 2. Real-time Data Processing (Elasticsearch, Filebeat, Prometheus, Grafana)

- **Dockerize Elasticsearch, Filebeat, Prometheus, Grafana**:
  - All four are defined as services in the same `docker-compose.yml` on the shared `shop-net` network.

- **Logs collector (Filebeat) setup**:
  - Filebeat tails `shop-consumer`’s log file:
    - `/var/log/shop-consumer/consumer.log` via `shop-consumer-logs` volume.
  - Uses a processor to keep only CDC lines:
    - `drop_event` for lines that do not contain `"CDC_EVENT"`.
  - Sends events to Elasticsearch with index pattern `cdc-events-*`.

- **Grafana automatically configured**:
  - **Data sources**:
    - Prometheus at `http://prometheus:9090`.
    - Elasticsearch at `http://elasticsearch:9200` with index `cdc-events-*`, time field `@timestamp`.
  - <span style="color:red">Dashboard note: I didn't fully get the pre-provisioned Grafana dashboard working within the assignment time-frame. The dashboards fails to show data.</span>  
    - Intended dashboard (`Shop CDC Dashboard`), provisioned on startup:
      - Table panel: raw CDC events from Elasticsearch (`cdc-events-*`).
      - Pie chart panel: distribution of insert/update/delete over last 1h using  
        `sum by (op) (increase(db_changes_total[1h]))`.

---



