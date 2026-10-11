import pymysql

import aurora_store as a

conn = pymysql.connect(
    host=a.AURORA_HOST, port=a.AURORA_PORT, user=a.AURORA_USER,
    password=a.AURORA_PASSWORD, database=a.AURORA_DATABASE,
    connect_timeout=8, autocommit=True,
)
with conn.cursor() as cur:
    cur.execute("SHOW VARIABLES LIKE 'max_allowed_packet'")
    print('before=' + str(cur.fetchone()))
    for target in (67108864, 16777216, 4194304):
        try:
            cur.execute(f"SET GLOBAL max_allowed_packet={target}")
            print(f"set_global_ok={target}")
            break
        except Exception as err:  # noqa: BLE001
            print(f"set_global {target} failed: {err}")
    cur.execute("SHOW VARIABLES LIKE 'max_allowed_packet'")
    print('after(global)=' + str(cur.fetchone()))
    cur.execute("SELECT @@max_allowed_packet")
    print('session_after=' + str(cur.fetchone()))
conn.close()

# New connection inherits the raised global limit
conn2 = pymysql.connect(
    host=a.AURORA_HOST, port=a.AURORA_PORT, user=a.AURORA_USER,
    password=a.AURORA_PASSWORD, database=a.AURORA_DATABASE,
    connect_timeout=8, autocommit=True,
)
with conn2.cursor() as cur:
    cur.execute("SELECT @@max_allowed_packet")
    print('new_connection=' + str(cur.fetchone()))
conn2.close()
