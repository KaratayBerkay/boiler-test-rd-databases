-- the entrypoint already ran CHANGE MASTER TO (from MARIADB_MASTER_HOST); pin GTID mode and start
CHANGE MASTER TO MASTER_USE_GTID = slave_pos;
START REPLICA;
