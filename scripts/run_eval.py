"""
独立运行 Golden Dataset 评测（不依赖 pytest）。

用法：
    python scripts/run_eval.py
"""
import sys
import os

# 将项目根目录加入 Python 路径
_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from tests.test_golden_dataset import GoldenDatasetEvaluator


def main():
    print("=" * 80)
    print("MySQL Agent Golden Dataset 评测")
    print("=" * 80)
    print()

    evaluator = GoldenDatasetEvaluator()
    report = evaluator.run_all()

    # 打印摘要
    print("\n" + "=" * 80)
    print("评测报告摘要")
    print("=" * 80)
    print(f"总体状态: {report['summary']['overall_status']}")
    print(f"通过率:   {report['summary']['pass_rate']}%")
    print(f"通过:     {report['summary']['passed']}")
    print(f"失败:     {report['summary']['failed']}")
    print(f"错误:     {report['summary']['errors']}")
    print(f"总耗时:   {report['metadata']['elapsed_ms']} ms")
    print()

    print("\n平均得分:")
    for metric, score in report["avg_scores"].items():
        status = "[OK]" if score >= 0.8 else "[WARN]" if score >= 0.5 else "[FAIL]"
        print(f"  {status} {metric}: {score}")

    print()
    print("分层统计:")
    print(f"{'层级':<20} {'总数':>6} {'通过':>6} {'失败':>6} {'通过率':>8}")
    print("-" * 80)
    for layer, stats in sorted(report["layer_stats"].items()):
        pass_rate = stats["passed"] / stats["total"] * 100 if stats["total"] > 0 else 0
        print(f"{layer:<20} {stats['total']:>6} {stats['passed']:>6} {stats['failed']:>6} {pass_rate:>7.1f}%")

    print()
    print("\n失败用例详情:")
    failed_cases = [r for r in report["details"] if r["status"] != "pass"]
    if failed_cases:
        for r in failed_cases:
            print(f"  - {r['case_id']}: {r['question'][:50]}...")
            print(f"    状态: {r['status']}, 错误: {r.get('error', 'N/A')}")
            print(f"    得分: {r['scores']}")
    else:
        print("  [PASS] 全部通过！")

    print()
    print("=" * 80)
    print(f"详细报告已保存至 reports/ 目录")
    print("=" * 80)

    # 返回退出码（用于 CI/CD）
    return 0 if report["summary"]["overall_status"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
