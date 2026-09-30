"""快速测试脚本：发送问题给 Agent 并打印结果"""
import json
import requests

BASE = "http://127.0.0.1:8000"

def ask(question: str, role: str = "exec", user_id: str = "admin"):
    print(f"\n{'='*60}")
    print(f"问题: {question}")
    print(f"角色: {role}")
    print(f"{'='*60}")
    
    r = requests.post(f"{BASE}/api/query", json={
        "question": question,
        "session_id": "test-script",
        "user_id": user_id,
        "role": role,
    })
    d = r.json()
    
    print(f"\n回答: {d['answer']}")
    print(f"\nSQL: {d['sql']}")
    print(f"表: {d['tables_used']}")
    print(f"行数: {d['row_count']}, 耗时: {d['execution_time_ms']:.0f}ms")
    if d.get("error"):
        print(f"错误: {d['error']}")
    if d.get("needs_clarification"):
        print(f"追问: {d['clarification_question']}")

# 测试 1: 简单数据查询
ask("2026年6月各部门的收入是多少？")

# 测试 2: 员工信息查询
ask("张三的入职日期是什么时候？")

# 测试 3: 权限测试 - 普通员工查薪资
ask("查看我的薪资", role="employee", user_id="101")

# 测试 4: 超范围问题
ask("今天天气怎么样？")

# 测试 5: 元问题
ask("你能查哪些数据？")
