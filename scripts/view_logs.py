"""
审计日志查看器：把 logs/audit.jsonl 格式化为可读输出。

用法：
    python scripts/view_logs.py          # 查看全部
    python scripts/view_logs.py -n 5     # 看最近 5 条
    python scripts/view_logs.py --sql    # 只看 SQL 相关
    python scripts/view_logs.py --json   # 原始 JSON 输出
"""
import json
import sys
from pathlib import Path

LOG_PATH = Path(__file__).resolve().parent.parent / "logs" / "audit.jsonl"


def load_logs(n: int | None = None) -> list[dict]:
    """加载日志，n 为 None 表示全部"""
    if not LOG_PATH.exists():
        print(f"日志文件不存在: {LOG_PATH}")
        return []
    lines = LOG_PATH.read_text(encoding="utf-8").strip().split("\n")
    logs = [json.loads(line) for line in lines if line.strip()]
    if n:
        logs = logs[-n:]
    return logs


def print_summary(logs: list[dict]):
    """打印摘要表格"""
    if not logs:
        print("没有日志记录")
        return

    print(f"\n{'='*90}")
    print(f" 共 {len(logs)} 条记录")
    print(f"{'='*90}")

    for i, log in enumerate(logs, 1):
        ts = log.get("timestamp", "")[:19]
        q = log.get("question", "")[:40]
        sql = log.get("generated_sql", "")[:60]
        tables = ", ".join(log.get("tables_accessed", [])) or "-"
        rows = log.get("row_count", 0)
        ms = log.get("execution_ms", 0)
        status = log.get("status", "?")
        role = log.get("role", "?")

        print(f"\n[{i}] {ts}  角色:{role}  状态:{status}")
        print(f"    问题: {q}")
        print(f"    SQL:  {sql}")
        print(f"    表: {tables}  行数: {rows}  耗时: {ms:.0f}ms")

        # 打印回答（截断）
        answer = log.get("answer", "")
        if answer:
            preview = answer[:120].replace("\n", " ")
            print(f"    回答: {preview}{'...' if len(answer) > 120 else ''}")


def print_detail(logs: list[dict]):
    """打印详细信息"""
    if not logs:
        return

    for i, log in enumerate(logs, 1):
        print(f"\n{'═'*70}")
        print(f" 记录 #{i}  |  {log.get('timestamp', '')[:19]}  |  {log.get('request_id', '')}")
        print(f"{'═'*70}")
        print(f" 用户: {log.get('user_id', '')}  角色: {log.get('role', '')}  会话: {log.get('session_id', '')}")
        print(f"\n 问题: {log.get('question', '')}")
        print(f" 改写: {log.get('rewritten_question', '')}")
        print(f"\n SQL:")
        print(f"   {log.get('generated_sql', '')}")
        print(f"\n 候选表: {', '.join(log.get('candidate_tables', []))}")
        print(f" 使用表: {', '.join(log.get('tables_accessed', []))}")
        print(f" 使用列: {', '.join(log.get('columns_accessed', []))}")
        print(f"\n 回答:")
        for line in log.get("answer", "").split("\n"):
            print(f"   {line}")
        print(f"\n 行数: {log.get('row_count', 0)}  耗时: {log.get('execution_ms', 0):.0f}ms  状态: {log.get('status', '')}")
        if log.get("error"):
            print(f" 错误: {log.get('error', '')}")


def main():
    args = sys.argv[1:]
    n = None
    detail = False
    raw_json = False

    i = 0
    while i < len(args):
        if args[i] == "-n" and i + 1 < len(args):
            n = int(args[i + 1])
            i += 2
        elif args[i] in ("--detail", "-d"):
            detail = True
            i += 1
        elif args[i] == "--json":
            raw_json = True
            i += 1
        else:
            i += 1

    logs = load_logs(n)

    if raw_json:
        for log in logs:
            print(json.dumps(log, ensure_ascii=False, indent=2))
    elif detail:
        print_detail(logs)
    else:
        print_summary(logs)


if __name__ == "__main__":
    main()
