"""数据库连接（MySQL），供 main_web.py 和 memory.py 共用"""

import os
import pymysql


class _DB:
    """包装 pymysql 连接，兼容 sqlite3 的 execute() 接口"""
    def __init__(self, conn):
        self.conn = conn
    def execute(self, sql, params=None):
        cur = self.conn.cursor()
        cur.execute(sql, params or ())
        return cur
    def commit(self):
        self.conn.commit()
    def close(self):
        self.conn.close()


def get_db():
    conn = pymysql.connect(
        host=os.getenv("MYSQL_HOST", "127.0.0.1"),
        port=int(os.getenv("MYSQL_PORT", "3306")),
        user=os.getenv("MYSQL_USER", "root"),
        password=os.getenv("MYSQL_PASSWORD", "root"),
        database=os.getenv("MYSQL_DATABASE", "zhugece"),
        cursorclass=pymysql.cursors.DictCursor,
        charset="utf8mb4",
        autocommit=False,
    )
    return _DB(conn)
