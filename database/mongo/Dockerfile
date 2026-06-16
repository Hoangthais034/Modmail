FROM mongo:7

COPY custom-entrypoint.sh /usr/local/bin/custom-entrypoint.sh
COPY mongorestore.sh /docker-entrypoint-initdb.d/mongorestore.sh

RUN chmod +x /usr/local/bin/custom-entrypoint.sh /docker-entrypoint-initdb.d/mongorestore.sh

ENTRYPOINT ["/usr/local/bin/custom-entrypoint.sh"]
