import pymysql, csv
import pymysql.cursors
import os
from dotenv import load_dotenv
load_dotenv(".env")
db_config=dict(
    host="127.0.0.1",
    user="root",
    password=os.getenv("DB_PASSWORD"),
    #测试的时候把数据库的密码写进环境变量（在同级目录创建一个文件叫".env"也就是只有后缀没有名字
    #然后在里面写(DB_PASSWORD=你的数据库密码)
    database="ledger",
    charset="utf8mb4",
    cursorclass=pymysql.cursors.DictCursor,
)
def get_conn():
    return pymysql.connect(**db_config)
def insert_transaction(conn,amount,merchant,
                       category,occurred_at,source,raw_input=None,
                        confidence=1.0,status="confirmed"):
    #这里没有传进currency，感觉货币类型默认为人名币够用了，以后有空再传进去
    """
       插入一笔交易，返回新记录的自增 id。

       参数（与 transactions 表字段一一对应，LLM/OCR 抽取后直接传）：
         amount      金额，单位元，如 32.50
         merchant    商户名，如 "滴滴"
         category    类别，如 "餐饮"（必须是约定类别之一）
         occurred_at 消费发生时间，"YYYY-MM-DD HH:MM:SS" 或 "YYYY-MM-DD HH:MM"
         source      来源：text / voice / ocr / csv
         raw_input   原始输入（用户原话/OCR原文），便于追溯，可空
         confidence  抽取置信度 0.00-1.00，默认 1.0
         status      pending 待确认 / confirmed 已确认，默认 confirmed
       """
    with conn.cursor() as cur:
        cur.execute(
            """insert into transactions
                           (amount, merchant, category, occurred_at, source, raw_input, confidence, status)
                           values (%s,%s,%s,%s,%s,%s,%s,%s)""",
            (amount, merchant, category, occurred_at, source,
             raw_input, confidence, status))
        tid = cur.lastrowid
        conn.commit()
        return tid
def update_transaction(conn,tid,**fields): #用来更新数据
    """
       按 id 更新指定字段（OCR 确认页修正后调用）。
       用法：update_transaction(conn, 5, amount=30, category="交通")
       注意：字段名必须是 transactions 表真实存在的列名。
       """
    sets=",".join(f"{k}=%s"for k in fields.keys())
    with conn.cursor() as cur:
        cur.execute(
            f"update  transactions set {sets} where id=%s",
            (*fields.values(), tid))
    conn.commit()
def import_csv(conn,path="sample.csv"):
    """
        逐行读取 CSV 并入库。
        CSV 表头需含：amount, merchant, category, occurred_at
        固定标记 source="csv"、raw_input="csv import"，便于区分来源。
        """
    with open(path,encoding="utf-8") as f:
        for row in csv.DictReader(f):
            insert_transaction(conn,
            amount = float(row['amount']),
            merchant = row['merchant'],
            category = row['category'],
            occurred_at = row['occurred_at'],
            source = "csv",
            raw_input = "csv import")
# 接下来是数据库查询
def sum_by_category(conn,month,category):
    """
       某月某类别一共花了多少（元）。
       month 格式 "YYYY-MM"，如 "2026-10"。
       只统计 status='confirmed' 的（pending 待确认的不算）。
       coalesce(...,0) 保证没数据时返回 0 而不是 None。如果要查询的这个月没有消费就返回0块钱
       """
    with conn.cursor() as cur:
        cur.execute(
            """select coalesce(sum(amount),0) s from transactions
            where category=%s and occurred_at like %s and status='confirmed'""",
            (category, f"{month}%")
            )
        row = cur.fetchone()
        return row["s"] if row else 0
#下面的函数是算出一个月消费最多的类别
def top_category(conn, month):
    """
    某月花钱最多的类别，返回 {"category": ..., "s": 金额} 或 None。
    按类别分组求和，降序取第一条。
    """
    with conn.cursor() as cur:
        cur.execute(
            "select category, sum(amount) s from transactions "
            "where occurred_at like %s and status='confirmed' "
            "group by category order by s desc limit 1",
            (f"{month}%",))
        return cur.fetchone()

def spent_vs_budget(conn, month, category):
    """
    对比某月某类别"已花 vs 预算"，用于超支提醒。
    返回 dict：
      {spent, budget, ratio, alert}
      - 没有预算记录时：budget=None、ratio=None
      - alert=True 表示已花 >= 预算 × 阈值（默认 80%）
    """
    spent = sum_by_category(conn, month, category)
    with conn.cursor() as cur:
        cur.execute("select amount, threshold from budgets "
                    "where category=%s and period=%s", (category, month))
        row = cur.fetchone()
    if not row:   # 该类别该月没设预算
        return {"spent": spent, "budget": None, "ratio": None}
    # decimal 类型转 float 才能直接参与除法
    budget = float(row["amount"])
    ratio = float(spent) / budget
    return {"spent": spent, "budget": budget,
            "ratio": ratio,
            "alert": ratio >= float(row["threshold"])}#alter=True说明超出阈值反之就说明还没达到











