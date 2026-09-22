r"""
一键启动：启动服务 + 交互式问答，一个终端搞定。

用法：
    cd d:\Projects\ai_agent\agent_data_v1.0.0\mysql_agent_v2
    python scripts\run.py
"""
import sys
import os
import threading
import time
import uvicorn
import requests

# 将项目根目录加入 Python 路径，确保能 import app
_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)


def start_server():
    """在后台线程启动 FastAPI 服务"""
    uvicorn.run("app.main:app", host="127.0.0.1", port=8000, log_level="warning")


def wait_for_server(timeout: int = 15):
    """等待服务就绪"""
    print("正在启动服务...", end="", flush=True)
    for _ in range(timeout * 10):
        try:
            r = requests.get("http://127.0.0.1:8000/api/health", timeout=2)
            if r.status_code == 200:
                d = r.json()
                print(f" 就绪！({d['tables_loaded']} 张表, {d['metrics_loaded']} 个指标)")
                return True
        except requests.ConnectionError:
            pass
        time.sleep(0.1)
        print(".", end="", flush=True)
    print("\n[错误] 服务启动超时")
    return False


# ═══════════════════════════════════════════
# 交互式问答（和 chat.py 逻辑相同）
# ═══════════════════════════════════════════

BASE = "http://127.0.0.1:8000"
SESSION_ID = "interactive-chat"
USER_ID = "admin"
ROLE = "exec"


def ask(question: str):
    try:
        r = requests.post(f"{BASE}/api/query", json={
            "question": question,
            "session_id": SESSION_ID,
            "user_id": USER_ID,
            "role": ROLE,
        }, timeout=30)
        r.raise_for_status()
        d = r.json()
    except Exception as e:
        print(f"\n[错误] {e}")
        return

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


def chat_loop():
    print("\n══════════════════════════════════════════╗")
    print("║   MySQL Agent 交互式问答终端             ║")
    print("║   输入问题，回车发送。输入 /quit 退出    ║")
    print("╚══════════════════════════════════════════╝\n")

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
        if question.lower() == "/help":
            print("命令: /quit 退出, /help 帮助")
            print("示例:")
            print("  - 2026年6月各部门的收入是多少？")
            print("  - 张三的入职日期是什么时候？")
            print("  - 你能查哪些数据？")
            continue

        ask(question)


def main():
    # 后台启动服务
    server_thread = threading.Thread(target=start_server, daemon=True)
    server_thread.start()

    # 等待服务就绪
    if not wait_for_server():
        return

    # 进入交互问答
    chat_loop()


if __name__ == "__main__":
    main()
