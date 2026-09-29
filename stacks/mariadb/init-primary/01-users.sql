-- runs on the primary at first start; binlogged, therefore replicated to the replica as well
GRANT REPLICATION CLIENT, REPLICATION SLAVE, PROCESS, SELECT ON *.* TO 'lab'@'%';
CREATE USER IF NOT EXISTS 'maxscale'@'%' IDENTIFIED BY 'maxscalepass';
GRANT ALL PRIVILEGES ON *.* TO 'maxscale'@'%' WITH GRANT OPTION;
FLUSH PRIVILEGES;
