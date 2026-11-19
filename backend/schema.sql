-- 记账后端的建表脚本：可以直接在 MySQL 里执行，应用启动时也会读取本文件建表。
-- 应用侧跳过 create database / use 两句，库名由环境变量 DB_NAME 决定；
-- 手动执行时用脚本里的 ledger，与缺省配置一致。

create database if not exists ledger default charset utf8mb4;
use ledger;

-- 交易流水：一笔消费一行，occurred_at 存 UTC，接口进出参也是 UTC。
create table if not exists transactions(
  id int auto_increment primary key,
  amount decimal(10,2) not null,
  currency varchar(8) default 'CNY',
  merchant varchar(30),
  category varchar(30),
  occurred_at datetime not null,
  created_at datetime default current_timestamp,
  source varchar(16) not null,
  raw_input text,
  confidence decimal(4,2) default 1.00,
  status varchar(16) default 'confirmed',
  note varchar(200) null,
  key ix_transactions_occurred (occurred_at),
  key ix_transactions_category_occurred (category, occurred_at)
) engine=InnoDB default charset=utf8mb4;

-- 消费类别：name 与 API 的 7 个中文枚举一致，keywords 供规则兜底使用。
create table if not exists categories(
  id int auto_increment primary key,
  name varchar(30) not null,
  parent_id int,
  keywords varchar(200)
) engine=InnoDB default charset=utf8mb4;

-- 预算：月份 + 类别唯一，period 格式 YYYY-MM；
-- category 为空串表示整月总预算，threshold 是提醒阈值（0.8 即用掉 80% 提醒）。
create table if not exists budgets(
  id int auto_increment primary key,
  category varchar(32) not null,
  period varchar(7) not null,
  amount decimal(10,2) not null,
  threshold decimal(4,2) default 0.80,
  unique key uq_budgets_period_category (period, category)
) engine=InnoDB default charset=utf8mb4;

-- 解析草稿：一句话或小票解析出的待确认字段；
-- 保存账目时读取 source 后删除该行，所以需要落库而不是放在进程内存里。
create table if not exists transaction_drafts(
  draft_id char(32) primary key,
  source varchar(16) not null,
  amount decimal(10,2) null,
  merchant varchar(30) null,
  occurred_at datetime null,
  category varchar(30) null,
  confidence decimal(4,2) not null default 0.00,
  missing_fields json not null,
  raw_text text null,
  created_at datetime not null
) engine=InnoDB default charset=utf8mb4;
