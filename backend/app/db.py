"""MySQL 连接与建表。

不再使用 SQLAlchemy：这里直接用 pymysql 建连接，用原生 SQL 建库建表。
- create_conn()  新建一条连接（DictCursor，关闭自动提交）
- get_conn()     按请求创建连接，请求结束或抛异常时回滚并关闭
- ConnDep        路由参数类型别名，等价于「注入一条连接」
- init_db()      应用启动时建库、建表，并补齐老库里缺失的列和索引

连接参数全部来自环境变量（见 backend/.env.example），代码里不写账号密码。
"""

import os
from collections.abc import Iterator
from pathlib import Path
from typing import Annotated

import pymysql
from dotenv import load_dotenv
from fastapi import Depends
from pymysql.connections import Connection
from pymysql.cursors import DictCursor

# 读 backend/.env：路径按文件位置推算，不受启动目录影响。
_DOTENV_PATH = Path(__file__).resolve().parents[1] / ".env"
load_dotenv(_DOTENV_PATH)

# 环境变量缺省值与本机开发环境一致；生产部署用环境变量覆盖。
DB_NAME = os.getenv("DB_NAME", "ledger")
_DB_CONFIG = {
    "host": os.getenv("DB_HOST", "127.0.0.1"),
    "port": int(os.getenv("DB_PORT", "3306")),
    "user": os.getenv("DB_USER", "root"),
    "password": os.getenv("DB_PASSWORD", ""),
    "charset": "utf8mb4",
}

# 建表语句与 backend/schema.sql 保持一致；CREATE TABLE IF NOT EXISTS 可重复执行。
# transaction_drafts 是「解析出草稿 → 用户确认后保存」流程用的中间表，
# 保存时读取草稿的 source 并删除草稿，所以需要落库而不是放在内存里。
_TABLE_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS transactions (
        id INT AUTO_INCREMENT PRIMARY KEY,
        amount DECIMAL(10, 2) NOT NULL,
        currency VARCHAR(8) DEFAULT 'CNY',
        merchant VARCHAR(30),
        category VARCHAR(30),
        occurred_at DATETIME NOT NULL,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        source VARCHAR(16) NOT NULL,
        raw_input TEXT,
        confidence DECIMAL(4, 2) DEFAULT 1.00,
        status VARCHAR(16) DEFAULT 'confirmed',
        note VARCHAR(200) NULL,
        KEY ix_transactions_occurred (occurred_at),
        KEY ix_transactions_category_occurred (category, occurred_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS categories (
        id INT AUTO_INCREMENT PRIMARY KEY,
        name VARCHAR(30) NOT NULL,
        parent_id INT,
        keywords VARCHAR(200)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS budgets (
        id INT AUTO_INCREMENT PRIMARY KEY,
        category VARCHAR(32) NOT NULL,
        period VARCHAR(7) NOT NULL,
        amount DECIMAL(10, 2) NOT NULL,
        threshold DECIMAL(4, 2) DEFAULT 0.80,
        UNIQUE KEY uq_budgets_period_category (period, category)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS transaction_drafts (
        draft_id CHAR(32) PRIMARY KEY,
        source VARCHAR(16) NOT NULL,
        amount DECIMAL(10, 2) NULL,
        merchant VARCHAR(30) NULL,
        occurred_at DATETIME NULL,
        category VARCHAR(30) NULL,
        confidence DECIMAL(4, 2) NOT NULL DEFAULT 0.00,
        missing_fields JSON NOT NULL,
        raw_text TEXT NULL,
        created_at DATETIME NOT NULL
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
)


def create_conn(*, with_database: bool = True) -> Connection:
    """新建一条 MySQL 连接。

    参数:
        with_database: 为 False 时不带库名，用于建库前的连接。

    返回:
        pymysql Connection；行数据以 dict 返回（DictCursor），autocommit 关闭。

    异常:
        连不上时抛 RuntimeError，并把 host / port / 用户带在消息里，方便定位配置问题。
    """
    config = dict(_DB_CONFIG)
    if with_database:
        config["database"] = DB_NAME
    try:
        return pymysql.connect(cursorclass=DictCursor, autocommit=False, **config)
    except pymysql.MySQLError as exc:
        raise RuntimeError(
            f"MySQL 连接失败：{config['host']}:{config['port']}，"
            f"用户 {config['user']}，库 {DB_NAME}；原始错误：{exc}"
        ) from exc


def get_conn() -> Iterator[Connection]:
    """为单个请求创建连接，请求结束后关闭。

    路由函数处理中抛异常时先回滚，避免半截写入留在事务里；
    正常结束时由各 store 函数自己 commit。
    """
    conn = create_conn()
    try:
        yield conn
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# 路由参数写 conn: ConnDep，FastAPI 会调用 get_conn() 注入请求级连接。
ConnDep = Annotated[Connection, Depends(get_conn)]


def _column_exists(cur: DictCursor, table: str, column: str) -> bool:
    """查询 information_schema，判断表里是否已有该列。"""
    cur.execute(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_schema = DATABASE() AND table_name = %s AND column_name = %s",
        (table, column),
    )
    return cur.fetchone() is not None


def _index_exists(cur: DictCursor, table: str, index: str) -> bool:
    """查询 information_schema，判断表里是否已有该索引。"""
    cur.execute(
        "SELECT 1 FROM information_schema.statistics "
        "WHERE table_schema = DATABASE() AND table_name = %s AND index_name = %s",
        (table, index),
    )
    return cur.fetchone() is not None


def _upgrade_schema(cur: DictCursor) -> None:
    """给按 schema.sql 建好的老库补齐新增的列与索引。

    只做加法：已有列和数据都不动；merchant 等字段长度以 schema.sql 为准。
    """
    if not _column_exists(cur, "transactions", "note"):
        cur.execute("ALTER TABLE transactions ADD COLUMN note VARCHAR(200) NULL AFTER status")
    if not _index_exists(cur, "transactions", "ix_transactions_occurred"):
        cur.execute("ALTER TABLE transactions ADD INDEX ix_transactions_occurred (occurred_at)")
    if not _index_exists(cur, "transactions", "ix_transactions_category_occurred"):
        cur.execute(
            "ALTER TABLE transactions "
            "ADD INDEX ix_transactions_category_occurred (category, occurred_at)"
        )
    if not _index_exists(cur, "budgets", "uq_budgets_period_category"):
        cur.execute(
            "ALTER TABLE budgets "
            "ADD UNIQUE KEY uq_budgets_period_category (period, category)"
        )


def init_db() -> None:
    """应用启动时确保库和表存在。

    先不带库名连接执行 CREATE DATABASE，再连库执行建表与补列；
    已存在的表和列不会重建，字段类型变更需要单独的迁移脚本。
    """
    bootstrap = create_conn(with_database=False)
    try:
        with bootstrap.cursor() as cur:
            cur.execute(f"CREATE DATABASE IF NOT EXISTS `{DB_NAME}` DEFAULT CHARSET utf8mb4")
        bootstrap.commit()
    finally:
        bootstrap.close()

    conn = create_conn()
    try:
        with conn.cursor() as cur:
            for statement in _TABLE_STATEMENTS:
                cur.execute(statement)
            _upgrade_schema(cur)
        conn.commit()
    finally:
        conn.close()
