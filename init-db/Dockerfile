FROM mysql:8.0

# Copy init script and SQL
COPY init.sh /init.sh
COPY schema.sql /schema.sql

RUN chmod +x /init.sh

ENTRYPOINT ["/init.sh"]


