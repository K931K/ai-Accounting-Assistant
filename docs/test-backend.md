# MySQL 测试总结报告（transactions 表）
## 一、索引测试（前面explain）
测试SQL：`select * from transactions where category='xxx';`
- 索引 `ix_trans_category_occur` 生效，走索引，没有全表扫描
- 索引组合：`category + occurred_at`，适合按分类+时间筛选账单场景

## 二、非空约束 NOT NULL 验证
1. `amount` 传 null → `ERROR 1048`，拦截成功
2. `source` 传 null → `ERROR 1048`，拦截成功
✅ 结论：这两个字段底层约束生效，数据库层面禁止存入空值。

## 三、外键缺失风险测试
执行：
```sql
insert into transactions(amount, currency, category, occurred_at, source) values (200, 'CNY', 'xxx', '2026-11-27 10:30:00', 'test');
```
✅ 现象：**成功插入**
⚠️ 风险：category 没有外键关联分类字典表，数据库不校验分类是否合法，脏数据只能靠业务代码拦截。

## 四、事务重点测试（高频面试坑）
### 测试1：表不存在报错（DDL类错误）
```sql
start transaction;
insert ... (50那条记录);
update budget ...（budget表不存在）;
commit;
```
- 报错：`Table doesn't exist`
- 结果：**50 的数据成功入库**
> 核心知识点：
> MySQL InnoDB **不是所有报错都会自动回滚事务**。语法错误、表不存在这类语句级错误，前面已经执行成功的DML会保留，commit直接持久化。
> 业务代码必须捕获异常，手动 `rollback`，不能等数据库自动回滚。

### 测试2：手动rollback（正常原子性演示）
```sql
start transaction;
insert ... (80那条记录);
rollback;
```
查询 `amount=80` → Empty set
✅ 结论：手动rollback可以撤销本次事务内所有修改，原子性正常。

## 五、汇总清单
| 测试项 | 结果 | 备注 |
| ---- | ---- | ---- |
| 联合索引 ix_trans_category_occur | 生效 | category+occurred_at 查询走索引 |
| amount NOT NULL | 生效 | 禁止null |
| source NOT NULL | 生效 | 禁止null |
| category 外键约束 | 无 | 可插入不存在的分类，存在脏数据风险 |
| 事务-约束冲突类错误 | 自动回滚 | 比如主键重复、非空冲突 |
| 事务-表不存在/语法错误 | 不会自动回滚 | 前面DML会保留，业务必须手动rollback |
| 手动rollback | 正常 | 事务内修改全部撤销 |

