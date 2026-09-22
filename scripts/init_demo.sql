-- 演示业务库 company_ops 初始化（含 agent_ro 只读账号与审计表）
-- 使用方法：mysql -uroot -p < scripts/init_demo.sql

CREATE DATABASE IF NOT EXISTS company_ops DEFAULT CHARACTER SET utf8mb4;
USE company_ops;

CREATE TABLE IF NOT EXISTS department (
  dept_id BIGINT PRIMARY KEY,
  dept_name VARCHAR(64) NOT NULL,
  bu_id BIGINT NOT NULL
);

CREATE TABLE IF NOT EXISTS employee (
  emp_id BIGINT PRIMARY KEY,
  name VARCHAR(64) NOT NULL,
  dept_id BIGINT NOT NULL,
  hire_date DATE,
  salary DECIMAL(12,2),
  id_card CHAR(18)
);

CREATE TABLE IF NOT EXISTS performance (
  perf_id BIGINT PRIMARY KEY,
  emp_id BIGINT NOT NULL,
  month CHAR(7) NOT NULL,
  score DECIMAL(5,2),
  bonus DECIMAL(12,2)
);

CREATE TABLE IF NOT EXISTS attendance (
  att_id BIGINT PRIMARY KEY,
  emp_id BIGINT NOT NULL,
  work_date DATE NOT NULL,
  status VARCHAR(16) NOT NULL
);

CREATE TABLE IF NOT EXISTS project (
  project_id BIGINT PRIMARY KEY,
  project_name VARCHAR(128) NOT NULL,
  dept_id BIGINT NOT NULL,
  budget DECIMAL(14,2),
  status VARCHAR(16)
);

CREATE TABLE IF NOT EXISTS customer (
  cust_id BIGINT PRIMARY KEY,
  cust_name VARCHAR(128) NOT NULL,
  owner_emp_id BIGINT,
  level CHAR(1),
  industry VARCHAR(64)
);

CREATE TABLE IF NOT EXISTS revenue (
  rev_id BIGINT PRIMARY KEY,
  month CHAR(7) NOT NULL,
  dept_id BIGINT NOT NULL,
  amount DECIMAL(14,2)
);

CREATE TABLE IF NOT EXISTS cost (
  cost_id BIGINT PRIMARY KEY,
  month CHAR(7) NOT NULL,
  dept_id BIGINT NOT NULL,
  item VARCHAR(64),
  amount DECIMAL(14,2)
);

INSERT IGNORE INTO department VALUES
 (1,'销售部',10),(2,'研发部',10),(3,'市场部',20);
INSERT IGNORE INTO employee VALUES
 (101,'张三',1,'2022-03-01',12000.00,'110101199001011234'),
 (102,'李四',1,'2021-07-15',15000.00,'110101199202022345'),
 (103,'王五',2,'2020-01-10',18000.00,'110101198911113456'),
 (104,'赵六',3,'2023-05-20',10000.00,'110101199505054567');
INSERT IGNORE INTO performance VALUES
 (1,101,'2026-06',88.50,2000.00),(2,102,'2026-06',92.00,3000.00),
 (3,103,'2026-06',85.00,1500.00),(4,104,'2026-06',78.00,800.00),
 (5,101,'2026-08',90.00,2200.00);
INSERT IGNORE INTO attendance VALUES
 (1,101,'2026-06-01','normal'),(2,101,'2026-06-02','late'),
 (3,102,'2026-06-01','normal'),(4,103,'2026-06-01','absent');
INSERT IGNORE INTO project VALUES
 (1,'A项目',1,500000.00,'active'),(2,'B项目',2,800000.00,'active'),(3,'C项目',1,200000.00,'closed');
INSERT IGNORE INTO customer VALUES
 (1,'客户甲',101,'A','互联网'),(2,'客户乙',102,'B','制造'),(3,'客户丙',103,'C','教育');
INSERT IGNORE INTO revenue VALUES
 (1,'2026-05',1,300000.00),(2,'2026-05',2,500000.00),(3,'2026-06',1,420000.00),
 (4,'2026-06',2,460000.00),(5,'2026-06',3,150000.00),
 (6,'2026-08',1,400000.00),(7,'2026-08',2,480000.00);
INSERT IGNORE INTO cost VALUES
 (1,'2026-06',1,'人力',200000.00),(2,'2026-06',1,'差旅',20000.00),
 (3,'2026-06',2,'人力',300000.00),(4,'2026-06',3,'推广',80000.00);

-- ============================================================
-- 关键安全配置：Agent 专用只读账号（禁止 root/应用账号连库）
-- 密码请改成强密码，通过环境变量 AGENT_DB_URL 注入
-- ============================================================
CREATE USER IF NOT EXISTS 'agent_ro'@'%' IDENTIFIED BY 'CHANGE_ME';
GRANT SELECT ON company_ops.* TO 'agent_ro'@'%';
FLUSH PRIVILEGES;

-- 审计日志表（AUDIT_MODE=db 时使用）
CREATE TABLE IF NOT EXISTS company_ops.agent_audit_log (
  request_id VARCHAR(64) PRIMARY KEY,
  user_id VARCHAR(64), role VARCHAR(32), session_id VARCHAR(64),
  question TEXT, rewritten_question TEXT, generated_sql TEXT,
  candidate_tables TEXT, tables_accessed TEXT, columns_accessed TEXT,
  row_count INT, execution_ms INT, status VARCHAR(32),
  ts DATETIME DEFAULT CURRENT_TIMESTAMP
);
