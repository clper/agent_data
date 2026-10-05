"""
生成完整的 company_ops 数据库初始化 SQL。

包含：
- 12 张表（8 张原有 + 4 张新增）
- 22 名员工、10 个事业部、15 个客户、12 个项目
- 考勤 ~2600 行、绩效 132 行、收入 72 行、成本 144 行
- 调薪记录 15 行、晋升记录 8 行、会议记录 30 行
"""
import random
import os
from datetime import date, timedelta

random.seed(42)  # 可重复生成

OUTPUT_PATH = os.path.join(os.path.dirname(__file__), "init_demo.sql")

# ═══════════════════════════════════════════
# 基础数据定义
# ═══════════════════════════════════════════

BUSINESS_UNITS = [
    (10, "国内业务事业部"),
    (20, "国际业务事业部"),
    (30, "创新业务事业部"),
]

DEPARTMENTS = [
    # (dept_id, dept_name, bu_id)
    (1, "销售部", 10),
    (2, "研发部", 10),
    (3, "市场部", 20),
    (4, "人力资源部", 10),
    (5, "财务部", 10),
    (6, "客服部", 20),
    (7, "数据部", 30),
    (8, "运维部", 30),
]

EMPLOYEES = [
    # (emp_id, name, dept_id, hire_date, salary, id_card)
    (101, "张三", 1, "2022-03-01", 12000, "110101199001011234"),
    (102, "李四", 1, "2021-07-15", 15000, "110101199202022345"),
    (103, "王五", 2, "2020-01-10", 18000, "110101198911113456"),
    (104, "赵六", 2, "2023-06-20", 14000, "110101199305054567"),
    (105, "钱七", 3, "2021-11-08", 13000, "110101199106065678"),
    (106, "孙八", 3, "2024-02-14", 11000, "110101199407076789"),
    (107, "周九", 4, "2019-09-01", 16000, "110101198808087890"),
    (108, "吴十", 4, "2022-12-05", 12500, "110101199509098901"),
    (109, "郑十一", 5, "2020-05-18", 17000, "110101198710109012"),
    (110, "冯十二", 5, "2023-08-22", 13500, "110101199611110123"),
    (111, "陈十三", 6, "2021-04-12", 11500, "110101199212121234"),
    (112, "褚十四", 6, "2024-01-30", 10000, "110101199703132345"),
    (113, "卫十五", 7, "2020-10-08", 19000, "110101198604143456"),
    (114, "蒋十六", 7, "2022-07-25", 15500, "110101199305154567"),
    (115, "沈十七", 8, "2021-02-17", 14500, "110101199006165678"),
    (116, "韩十八", 8, "2023-11-11", 12000, "110101199407176789"),
    (117, "杨十九", 1, "2024-05-06", 10500, "110101199808187890"),
    (118, "朱二十", 2, "2020-08-19", 20000, "110101198509198901"),
    (119, "秦二一", 3, "2022-09-14", 13000, "110101199110209012"),
    (120, "许二二", 6, "2023-03-28", 11000, "110101199511210123"),
    (121, "何二四", 1, "2019-04-15", 16000, "110101198812221234"),
    (122, "吕二五", 2, "2024-08-01", 11000, "110101199901232345"),
]

CUSTOMERS = [
    # (cust_id, cust_name, owner_emp_id, level, industry)
    (1, "客户甲", 101, "A", "互联网"),
    (2, "客户乙", 102, "B", "制造"),
    (3, "客户丙", 105, "C", "教育"),
    (4, "华腾科技", 101, "A", "互联网"),
    (5, "鼎盛集团", 102, "A", "金融"),
    (6, "万通实业", 105, "B", "制造"),
    (7, "新纪元教育", 106, "B", "教育"),
    (8, "康健医疗", 111, "A", "医疗"),
    (9, "乐享零售", 112, "C", "零售"),
    (10, "绿源能源", 115, "B", "能源"),
    (11, "天合物流", 121, "A", "物流"),
    (12, "锐思咨询", 105, "B", "咨询"),
    (13, "博远地产", 101, "C", "地产"),
    (14, "星辰传媒", 119, "B", "传媒"),
    (15, "恒达电子", 114, "A", "电子"),
]

PROJECTS = [
    # (project_id, project_name, dept_id, budget, status)
    (1, "智能客服系统", 2, 500000, "active"),
    (2, "数据中台建设", 7, 800000, "active"),
    (3, "年度营销推广", 3, 200000, "closed"),
    (4, "ERP 系统升级", 2, 600000, "active"),
    (5, "新员工培训体系", 4, 100000, "active"),
    (6, "财务自动化", 5, 350000, "active"),
    (7, "客户满意度提升", 6, 150000, "active"),
    (8, "运维监控平台", 8, 400000, "active"),
    (9, "海外市场拓展", 3, 700000, "active"),
    (10, "AI 数据分析", 7, 550000, "active"),
    (11, "销售 CRM 优化", 1, 250000, "closed"),
    (12, "办公网络改造", 8, 180000, "closed"),
]

# 月份范围
MONTHS_12 = [f"2025-{m:02d}" for m in range(10, 13)] + [f"2026-{m:02d}" for m in range(1, 10)]
MONTHS_6 = [f"2026-{m:02d}" for m in range(4, 10)]


# ═══════════════════════════════════════════
# 数据生成函数
# ═══════════════════════════════════════════

def gen_attendance() -> list[tuple]:
    """生成考勤数据：6个月 × 22人 × ~22工作日"""
    rows = []
    att_id = 1
    start = date(2026, 4, 1)
    end = date(2026, 9, 30)

    # 每个员工的考勤模式
    patterns = {}
    for emp in EMPLOYEES:
        emp_id = emp[0]
        # 随机生成该员工的迟到概率和缺勤概率
        late_prob = random.choice([0.02, 0.05, 0.08, 0.12, 0.15])
        absent_prob = random.choice([0.01, 0.02, 0.03])
        leave_prob = random.choice([0.01, 0.02, 0.03, 0.05])
        patterns[emp_id] = (late_prob, absent_prob, leave_prob)

    current = start
    while current <= end:
        if current.weekday() < 5:  # 只算工作日
            for emp in EMPLOYEES:
                emp_id = emp[0]
                late_p, absent_p, leave_p = patterns[emp_id]
                r = random.random()
                if r < absent_p:
                    status = "absent"
                elif r < absent_p + late_p:
                    status = "late"
                elif r < absent_p + late_p + leave_p:
                    status = "leave"
                else:
                    status = "normal"
                rows.append((att_id, emp_id, current.isoformat(), status))
                att_id += 1
        current += timedelta(days=1)

    return rows


def gen_performance() -> list[tuple]:
    """生成绩效数据：6个月 × 22人"""
    rows = []
    perf_id = 1
    for month in MONTHS_6:
        for emp in EMPLOYEES:
            emp_id = emp[0]
            base_score = random.gauss(85, 8)
            score = round(max(50, min(100, base_score)), 1)
            bonus = round(score * 30 + random.uniform(-500, 500), 2)
            bonus = max(0, bonus)
            rows.append((perf_id, emp_id, month, score, bonus))
            perf_id += 1
    return rows


def gen_revenue() -> list[tuple]:
    """生成收入数据：12个月 × 8部门（部分部门有收入）"""
    rows = []
    rev_id = 1
    # 销售部、市场部、国际业务有收入
    revenue_depts = {1: (300000, 80000), 3: (200000, 60000), 6: (100000, 30000)}
    for month in MONTHS_12:
        for dept_id, (base, variance) in revenue_depts.items():
            amount = round(base + random.uniform(-variance, variance), 2)
            amount = max(50000, amount)
            rows.append((rev_id, month, dept_id, amount))
            rev_id += 1
    return rows


def gen_cost() -> list[tuple]:
    """生成成本数据：12个月 × 8部门 × 4科目"""
    rows = []
    cost_id = 1
    items = ["人力", "差旅", "推广", "办公费"]
    # 各部门各科目基础成本
    base_costs = {
        "人力": {1: 200000, 2: 300000, 3: 150000, 4: 120000, 5: 130000, 6: 100000, 7: 250000, 8: 180000},
        "差旅": {1: 30000, 2: 15000, 3: 40000, 4: 10000, 5: 8000, 6: 20000, 7: 12000, 8: 25000},
        "推广": {1: 50000, 2: 10000, 3: 80000, 4: 5000, 5: 3000, 6: 15000, 7: 8000, 8: 5000},
        "办公费": {1: 15000, 2: 20000, 3: 12000, 4: 10000, 5: 8000, 6: 10000, 7: 18000, 8: 15000},
    }
    for month in MONTHS_12:
        for item in items:
            for dept_id in range(1, 9):
                base = base_costs[item][dept_id]
                amount = round(base + random.uniform(-base * 0.2, base * 0.2), 2)
                rows.append((cost_id, month, dept_id, item, amount))
                cost_id += 1
    return rows


def gen_salary_history() -> list[tuple]:
    """生成调薪记录"""
    records = [
        (1, 101, "2023-04-01", 10000, 12000, "年度调薪"),
        (2, 102, "2023-04-01", 13000, 15000, "年度调薪"),
        (3, 103, "2022-01-10", 15000, 18000, "晋升调薪"),
        (4, 104, "2024-07-01", 12000, 14000, "年度调薪"),
        (5, 105, "2023-12-01", 11000, 13000, "年度调薪"),
        (6, 107, "2022-09-01", 14000, 16000, "晋升调薪"),
        (7, 109, "2023-05-01", 15000, 17000, "年度调薪"),
        (8, 113, "2023-10-01", 16000, 19000, "晋升调薪"),
        (9, 118, "2023-08-01", 17000, 20000, "年度调薪"),
        (10, 121, "2022-04-01", 14000, 16000, "年度调薪"),
        (11, 101, "2025-04-01", 12000, 12000, "年度调薪（未调整）"),
        (12, 114, "2025-07-01", 13500, 15500, "年度调薪"),
        (13, 115, "2024-02-01", 12500, 14500, "年度调薪"),
        (14, 106, "2025-03-01", 9500, 11000, "年度调薪"),
        (15, 111, "2024-04-01", 10000, 11500, "年度调薪"),
    ]
    return records


def gen_promotion() -> list[tuple]:
    """生成晋升记录"""
    records = [
        (1, 103, "2022-01-10", "高级工程师", "技术专家", 2, 2),
        (2, 107, "2022-09-01", "HR专员", "HR主管", 4, 4),
        (3, 113, "2023-10-01", "数据分析师", "高级数据分析师", 7, 7),
        (4, 118, "2023-08-01", "工程师", "高级工程师", 2, 2),
        (5, 121, "2022-04-15", "销售专员", "销售主管", 1, 1),
        (6, 101, "2025-01-15", "销售专员", "高级销售专员", 1, 1),
        (7, 109, "2024-06-01", "会计", "财务主管", 5, 5),
        (8, 115, "2024-03-01", "运维工程师", "高级运维工程师", 8, 8),
    ]
    return records


def gen_meetings() -> list[tuple]:
    """生成会议记录"""
    rows = []
    mid = 1
    titles = [
        "周例会", "月度复盘", "项目评审", "需求对齐", "季度总结",
        "客户沟通会", "技术方案讨论", "跨部门协调会", "新人培训", "OKR 对齐",
    ]
    for month_idx, month in enumerate(MONTHS_6):
        # 每月每个部门至少 1 次会议
        for dept_id in range(1, 9):
            n_meetings = random.randint(1, 3)
            for _ in range(n_meetings):
                day = random.randint(1, 28)
                meeting_date = f"{month}-{day:02d}"
                title = random.choice(titles)
                duration = random.choice([30, 45, 60, 90, 120])
                organizer = random.choice([e[0] for e in EMPLOYEES if e[2] == dept_id])
                rows.append((mid, title, meeting_date, duration, dept_id, organizer))
                mid += 1
    return rows


# ═══════════════════════════════════════════
# SQL 生成
# ═══════════════════════════════════════════

def generate_sql():
    lines = []
    w = lines.append

    w("-- ═══════════════════════════════════════════════════════")
    w("-- company_ops 数据库初始化脚本（扩充版）")
    w("-- 12 张表 + 完整演示数据")
    w("-- 生成时间: 自动")
    w("-- ═══════════════════════════════════════════════════════")
    w("")
    w("SET NAMES utf8mb4;")
    w("")

    # ── 建表 ──
    w("-- ═══ 1. 事业部表（新增） ═══")
    w("CREATE TABLE IF NOT EXISTS business_unit (")
    w("  bu_id BIGINT PRIMARY KEY,")
    w("  bu_name VARCHAR(64) NOT NULL COMMENT '事业部名称'")
    w(") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='事业部表';")
    w("")

    w("-- ═══ 2. 部门表 ═══")
    w("CREATE TABLE IF NOT EXISTS department (")
    w("  dept_id BIGINT PRIMARY KEY,")
    w("  dept_name VARCHAR(64) NOT NULL COMMENT '部门名称',")
    w("  bu_id BIGINT NOT NULL COMMENT '所属事业部ID'")
    w(") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='部门表';")
    w("")

    w("-- ═══ 3. 员工表 ═══")
    w("CREATE TABLE IF NOT EXISTS employee (")
    w("  emp_id BIGINT PRIMARY KEY,")
    w("  name VARCHAR(64) NOT NULL COMMENT '姓名',")
    w("  dept_id BIGINT NOT NULL COMMENT '部门ID',")
    w("  hire_date DATE COMMENT '入职日期',")
    w("  salary DECIMAL(12,2) COMMENT '月薪',")
    w("  id_card CHAR(18) COMMENT '身份证号'")
    w(") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='员工表';")
    w("")

    w("-- ═══ 4. 绩效表 ═══")
    w("CREATE TABLE IF NOT EXISTS performance (")
    w("  perf_id BIGINT PRIMARY KEY,")
    w("  emp_id BIGINT NOT NULL,")
    w("  month CHAR(7) NOT NULL,")
    w("  score DECIMAL(5,2),")
    w("  bonus DECIMAL(12,2)")
    w(") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='绩效表';")
    w("")

    w("-- ═══ 5. 考勤表 ═══")
    w("CREATE TABLE IF NOT EXISTS attendance (")
    w("  att_id BIGINT PRIMARY KEY,")
    w("  emp_id BIGINT NOT NULL,")
    w("  work_date DATE NOT NULL,")
    w("  status VARCHAR(16) NOT NULL")
    w(") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='考勤表';")
    w("")

    w("-- ═══ 6. 项目表 ═══")
    w("CREATE TABLE IF NOT EXISTS project (")
    w("  project_id BIGINT PRIMARY KEY,")
    w("  project_name VARCHAR(128),")
    w("  dept_id BIGINT,")
    w("  budget DECIMAL(14,2),")
    w("  status VARCHAR(16)")
    w(") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='项目表';")
    w("")

    w("-- ═══ 7. 客户表 ═══")
    w("CREATE TABLE IF NOT EXISTS customer (")
    w("  cust_id BIGINT PRIMARY KEY,")
    w("  cust_name VARCHAR(128),")
    w("  owner_emp_id BIGINT,")
    w("  level CHAR(1),")
    w("  industry VARCHAR(64)")
    w(") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='客户表';")
    w("")

    w("-- ═══ 8. 收入表 ═══")
    w("CREATE TABLE IF NOT EXISTS revenue (")
    w("  rev_id BIGINT PRIMARY KEY,")
    w("  month CHAR(7) NOT NULL,")
    w("  dept_id BIGINT NOT NULL,")
    w("  amount DECIMAL(14,2)")
    w(") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='收入表';")
    w("")

    w("-- ═══ 9. 成本表 ═══")
    w("CREATE TABLE IF NOT EXISTS cost (")
    w("  cost_id BIGINT PRIMARY KEY,")
    w("  month CHAR(7) NOT NULL,")
    w("  dept_id BIGINT NOT NULL,")
    w("  item VARCHAR(64),")
    w("  amount DECIMAL(14,2)")
    w(") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='成本表';")
    w("")

    w("-- ═══ 10. 调薪记录表（新增） ═══")
    w("CREATE TABLE IF NOT EXISTS salary_history (")
    w("  record_id BIGINT PRIMARY KEY,")
    w("  emp_id BIGINT NOT NULL COMMENT '员工ID',")
    w("  effective_date DATE NOT NULL COMMENT '生效日期',")
    w("  old_salary DECIMAL(12,2) COMMENT '调前薪资',")
    w("  new_salary DECIMAL(12,2) COMMENT '调后薪资',")
    w("  reason VARCHAR(128) COMMENT '调薪原因'")
    w(") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='调薪记录表';")
    w("")

    w("-- ═══ 11. 晋升记录表（新增） ═══")
    w("CREATE TABLE IF NOT EXISTS promotion (")
    w("  promo_id BIGINT PRIMARY KEY,")
    w("  emp_id BIGINT NOT NULL COMMENT '员工ID',")
    w("  promo_date DATE NOT NULL COMMENT '晋升日期',")
    w("  old_position VARCHAR(64) COMMENT '原职位',")
    w("  new_position VARCHAR(64) COMMENT '新职位',")
    w("  old_dept_id BIGINT COMMENT '原部门ID',")
    w("  new_dept_id BIGINT COMMENT '新部门ID'")
    w(") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='晋升记录表';")
    w("")

    w("-- ═══ 12. 会议记录表（新增） ═══")
    w("CREATE TABLE IF NOT EXISTS meeting (")
    w("  meeting_id BIGINT PRIMARY KEY,")
    w("  title VARCHAR(128) NOT NULL COMMENT '会议主题',")
    w("  meeting_date DATE NOT NULL COMMENT '会议日期',")
    w("  duration_minutes INT COMMENT '时长(分钟)',")
    w("  dept_id BIGINT COMMENT '组织部门ID',")
    w("  organizer_emp_id BIGINT COMMENT '组织者员工ID'")
    w(") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='会议记录表';")
    w("")

    # ── 清空旧数据（按依赖顺序） ──
    w("-- ═══ 清空旧数据 ═══")
    for t in ["meeting", "promotion", "salary_history", "cost", "revenue",
              "customer", "project", "attendance", "performance",
              "employee", "department", "business_unit"]:
        w(f"DELETE FROM {t};")
    w("")

    # ── 插入数据 ──
    def insert_batch(table, columns, rows, batch_size=50):
        cols = ", ".join(columns)
        for i in range(0, len(rows), batch_size):
            batch = rows[i:i + batch_size]
            w(f"INSERT INTO {table} ({cols}) VALUES")
            vals = []
            for row in batch:
                formatted = []
                for v in row:
                    if isinstance(v, str):
                        formatted.append(f"'{v}'")
                    elif v is None:
                        formatted.append("NULL")
                    else:
                        formatted.append(str(v))
                vals.append(f"  ({', '.join(formatted)})")
            w(",\n".join(vals) + ";")
            w("")

    w("-- ═══ 插入数据 ═══")
    w("")

    w("-- business_unit")
    insert_batch("business_unit", ["bu_id", "bu_name"], BUSINESS_UNITS)

    w("-- department")
    insert_batch("department", ["dept_id", "dept_name", "bu_id"], DEPARTMENTS)

    w("-- employee")
    insert_batch("employee", ["emp_id", "name", "dept_id", "hire_date", "salary", "id_card"], EMPLOYEES)

    w("-- customer")
    insert_batch("customer", ["cust_id", "cust_name", "owner_emp_id", "level", "industry"], CUSTOMERS)

    w("-- project")
    insert_batch("project", ["project_id", "project_name", "dept_id", "budget", "status"], PROJECTS)

    w("-- salary_history")
    insert_batch("salary_history", ["record_id", "emp_id", "effective_date", "old_salary", "new_salary", "reason"], gen_salary_history())

    w("-- promotion")
    insert_batch("promotion", ["promo_id", "emp_id", "promo_date", "old_position", "new_position", "old_dept_id", "new_dept_id"], gen_promotion())

    w("-- performance")
    insert_batch("performance", ["perf_id", "emp_id", "month", "score", "bonus"], gen_performance())

    w("-- revenue")
    insert_batch("revenue", ["rev_id", "month", "dept_id", "amount"], gen_revenue())

    w("-- cost")
    insert_batch("cost", ["cost_id", "month", "dept_id", "item", "amount"], gen_cost())

    w("-- attendance (大批量)")
    insert_batch("attendance", ["att_id", "emp_id", "work_date", "status"], gen_attendance(), batch_size=100)

    w("-- meeting")
    insert_batch("meeting", ["meeting_id", "title", "meeting_date", "duration_minutes", "dept_id", "organizer_emp_id"], gen_meetings())

    sql = "\n".join(lines)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        f.write(sql)

    # 统计
    print(f"SQL 文件已生成: {OUTPUT_PATH}")
    print(f"文件大小: {os.path.getsize(OUTPUT_PATH) / 1024:.1f} KB")
    print(f"总行数: {len(lines)}")

    # 数据统计
    att = gen_attendance()
    perf = gen_performance()
    rev = gen_revenue()
    cost_rows = gen_cost()
    meet = gen_meetings()
    print(f"\n数据统计:")
    print(f"  business_unit: {len(BUSINESS_UNITS)}")
    print(f"  department: {len(DEPARTMENTS)}")
    print(f"  employee: {len(EMPLOYEES)}")
    print(f"  customer: {len(CUSTOMERS)}")
    print(f"  project: {len(PROJECTS)}")
    print(f"  performance: {len(perf)}")
    print(f"  revenue: {len(rev)}")
    print(f"  cost: {len(cost_rows)}")
    print(f"  attendance: {len(att)}")
    print(f"  salary_history: {len(gen_salary_history())}")
    print(f"  promotion: {len(gen_promotion())}")
    print(f"  meeting: {len(meet)}")
    total = (len(BUSINESS_UNITS) + len(DEPARTMENTS) + len(EMPLOYEES) + len(CUSTOMERS) +
             len(PROJECTS) + len(perf) + len(rev) + len(cost_rows) + len(att) +
             len(gen_salary_history()) + len(gen_promotion()) + len(meet))
    print(f"\n  总计: 12 张表, {total} 行数据")


if __name__ == "__main__":
    generate_sql()
