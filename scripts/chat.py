"""
交互式问答终端：在命令行里逐条提问，实时看 Agent 回答。

用法：
    cd d:\Projects\ai_agent\agent_data_v1.0.0\mysql_agent_v2
    python scripts\chat.py
"""
import json
import requests

BASE = "http://127.0.0.1:8000"

# 全局会话 ID（同一会话内多轮对话有上下文）
SESSION_ID = "interactive-chat"
USER_ID = "admin"
ROLE = "exec"


def ask(question: str):
    """发送问题并打印结果"""
    try:
        r = requests.post(f"{BASE}/api/query", json={
            "question": question,
            "session_id": SESSION_ID,
            "user_id": USER_ID,
            "role": ROLE,
        }, timeout=30)
        r.raise_for_status()
        d = r.json()
    except requests.ConnectionError:
        print("\n[错误] 无法连接到服务，请先启动：python -m uvicorn app.main:app --host 127.0.0.1 --port 8000")
        return
    except requests.Timeout:
        print("\n[错误] 请求超时")
        return
    except Exception as e:
        print(f"\n[错误] {e}")
        return

    # 打印回答
    print(f"\n{'─'*60}")
    print(f"回答: {d['answer']}")
    print(f"{'─'*60}")
    print(f"SQL:  {d['sql']}")
    print(f"表:   {', '.join(d['tables_used']) if d['tables_used'] else '(无)'}")
    print(f"行数: {d['row_count']}  耗时: {d['execution_time_ms']:.0f}ms")
    if d.get("error"):
        print(f"[!] 错误: {d['error']}")
    if d.get("needs_clarification"):
        print(f"[?] 追问: {d['clarification_question']}")
    print()


def main():
    print("╔══════════════════════════════════════════╗")
    print("║   MySQL Agent 交互式问答终端             ║")
    print("║   输入问题，回车发送。输入 /quit 退出    ║")
    print("╚══════════════════════════════════════════╝")
    print()

    while True:
        try:
            question = input("你> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n再见！")
            break

        if not question:
            continue
        if question.lower() in ("/quit", "/exit", "/q"):
            print("再见！")
            break
        if question.lower() == "/role":
            print(f"当前角色: {ROLE}，用户: {USER_ID}")
            continue
        if question.lower() == "/help":
            print("命令: /quit 退出, /role 查看角色, /help 帮助")
            print("示例问题:")
            print("  - 2026年6月各部门的收入是多少？")
            print("  - 张三的入职日期是什么时候？")
            print("  - 你能查哪些数据？")
            print("  - 今天天气怎么样？")
            continue

        ask(question)


if __name__ == "__main__":
    main()
