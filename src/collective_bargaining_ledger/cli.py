"""命令行接口：工会端与企业端各自调用，看到自己的待办、分歧与共同承诺。

常用环境变量：
    CB_DB           SQLite 账本路径（默认 ./bargaining.db）
    CB_STAFF_TOKEN  园区工会人员凭据
    CB_MANDATE      当前代表授权凭据

结构化入参（--content/--clauses/--evidence/--periods）可以直接给 JSON 字符串，
也可以用 @路径 从文件读取，或用 - 从标准输入读取。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from .errors import LedgerError
from .service import BargainingService


def _load_json(value: str | None) -> Any:
    if value is None:
        return {}
    if value == "-":
        return json.loads(sys.stdin.read())
    if value.startswith("@"):
        return json.loads(Path(value[1:]).read_text(encoding="utf-8"))
    return json.loads(value)


def _print(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def _service(args: argparse.Namespace) -> BargainingService:
    return BargainingService(
        args.db,
        staff_token=args.staff_token or os.environ.get("CB_STAFF_TOKEN", ""))


def _mandate(args: argparse.Namespace) -> str:
    return args.mandate or os.environ.get("CB_MANDATE", "")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="collective-bargaining",
        description="集体协商与履约账本服务")
    parser.add_argument("--db", default=os.environ.get("CB_DB", "bargaining.db"),
                        help="账本数据库路径")
    parser.add_argument("--staff-token", default=None,
                        help="园区工会人员凭据（或 CB_STAFF_TOKEN）")
    parser.add_argument("--mandate", default=None,
                        help="代表授权凭据（或 CB_MANDATE）")

    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("register-rep", help="登记/更换一方代表（工会人员）")
    p.add_argument("--side", choices=("worker", "company"), required=True)
    p.add_argument("--rep-id", required=True)
    p.add_argument("--name", required=True)
    p.add_argument("--mandate-id")
    p.set_defaults(func=cmd_register_rep)

    p = sub.add_parser("list-mandates", help="列出授权记录（工会人员）")
    p.add_argument("--side", choices=("worker", "company"))
    p.set_defaults(func=cmd_list_mandates)

    p = sub.add_parser("open", help="发起一轮集体协商")
    p.add_argument("--title", required=True)
    p.add_argument("--business-no")
    p.set_defaults(func=cmd_open)

    p = sub.add_parser("abandon", help="终止协商（工会人员）")
    p.add_argument("negotiation_id")
    p.add_argument("--reason", default="")
    p.set_defaults(func=cmd_abandon)

    p = sub.add_parser("submit", help="提交诉求/经营假设/测算口径/反建议/个人陈述")
    p.add_argument("negotiation_id")
    p.add_argument("--kind", required=True,
                   choices=("demands", "assumptions", "calculation_bases",
                            "counter_proposal", "personal_statement"))
    p.add_argument("--content", required=True, help="JSON 字符串 / @文件 / -")
    p.add_argument("--sensitive", action="store_true")
    p.add_argument("--business-no")
    p.set_defaults(func=cmd_submit)

    p = sub.add_parser("accept", help="接受对方的一条意见")
    p.add_argument("statement_id", type=int)
    p.add_argument("--business-no")
    p.set_defaults(func=cmd_accept)

    p = sub.add_parser("statements", help="查看协商陈述（按角色脱敏）")
    p.add_argument("negotiation_id")
    p.set_defaults(func=cmd_statements)

    p = sub.add_parser("propose", help="提交逐字条款包（工资/工时/福利整体）")
    p.add_argument("negotiation_id")
    p.add_argument("--clauses", required=True, help="JSON，含 wages/hours/benefits")
    p.add_argument("--assumptions", default="{}")
    p.add_argument("--bases", default="{}")
    p.add_argument("--note", default="")
    p.add_argument("--business-no")
    p.set_defaults(func=cmd_propose)

    p = sub.add_parser("confirm", help="确认逐字版本（必须双方各自确认）")
    p.add_argument("negotiation_id")
    p.add_argument("version_hash")
    p.add_argument("--business-no")
    p.set_defaults(func=cmd_confirm)

    p = sub.add_parser("vote", help="对已确认版本表决")
    p.add_argument("negotiation_id")
    p.add_argument("version_hash")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--yes", dest="yes", action="store_true")
    g.add_argument("--no", dest="yes", action="store_false")
    p.add_argument("--business-no")
    p.set_defaults(func=cmd_vote)

    p = sub.add_parser("sign", help="签署；双方齐备时原子生效")
    p.add_argument("negotiation_id")
    p.add_argument("version_hash")
    p.add_argument("--effective-from")
    p.add_argument("--business-no")
    p.set_defaults(func=cmd_sign)

    p = sub.add_parser("reconsider", help="凭新证据发起重新审议（不改原条款）")
    p.add_argument("agreement_id")
    p.add_argument("--evidence", required=True)
    p.add_argument("--title")
    p.add_argument("--business-no")
    p.set_defaults(func=cmd_reconsider)

    p = sub.add_parser("performance", help="按周期追加履约事实")
    p.add_argument("agreement_id")
    p.add_argument("--period", required=True)
    p.add_argument("--kind", required=True,
                   choices=("full", "partial", "dispute"))
    p.add_argument("--content", required=True)
    p.add_argument("--business-no")
    p.set_defaults(func=cmd_performance)

    p = sub.add_parser("supplement", help="追加补充约定提议（须双方同意）")
    p.add_argument("agreement_id")
    p.add_argument("--period", required=True)
    p.add_argument("--content", required=True)
    p.add_argument("--business-no")
    p.set_defaults(func=cmd_supplement)

    p = sub.add_parser("consent", help="同意一条补充约定")
    p.add_argument("entry_id", type=int)
    p.add_argument("--business-no")
    p.set_defaults(func=cmd_consent)

    p = sub.add_parser("agreements", help="列出协议")
    p.set_defaults(func=cmd_agreements)

    p = sub.add_parser("agreement", help="查看单份协议")
    p.add_argument("agreement_id")
    p.set_defaults(func=cmd_agreement)

    p = sub.add_parser("ledger", help="查看协议履约账本")
    p.add_argument("agreement_id")
    p.set_defaults(func=cmd_ledger)

    p = sub.add_parser("dashboard", help="本方待办、未解决分歧、共同有效承诺")
    p.set_defaults(func=cmd_dashboard)

    p = sub.add_parser("check-start", help="发起周期履约核查（工会人员）")
    p.add_argument("agreement_id")
    p.add_argument("--periods", required=True, help="JSON 数组或逗号分隔")
    p.add_argument("--run-id")
    p.set_defaults(func=cmd_check_start)

    p = sub.add_parser("check-resume", help="从检查点恢复核查（工会人员）")
    p.add_argument("run_id")
    p.set_defaults(func=cmd_check_resume)

    p = sub.add_parser("check-status", help="查看核查进度（工会人员）")
    p.add_argument("run_id")
    p.set_defaults(func=cmd_check_status)

    return parser


def cmd_register_rep(svc, args):
    return svc.register_representative(
        args.staff_token or os.environ.get("CB_STAFF_TOKEN", ""),
        args.side, args.rep_id, args.name, mandate_id=args.mandate_id)


def cmd_list_mandates(svc, args):
    return svc.list_mandates(
        args.staff_token or os.environ.get("CB_STAFF_TOKEN", ""), args.side)


def cmd_open(svc, args):
    return svc.open_negotiation(_mandate(args), args.title,
                                business_no=args.business_no)


def cmd_abandon(svc, args):
    return svc.abandon_negotiation(
        args.staff_token or os.environ.get("CB_STAFF_TOKEN", ""),
        args.negotiation_id, args.reason)


def cmd_submit(svc, args):
    return svc.submit_statement(
        _mandate(args), args.negotiation_id, args.kind,
        _load_json(args.content), sensitive=args.sensitive,
        business_no=args.business_no)


def cmd_accept(svc, args):
    return svc.accept_statement(_mandate(args), args.statement_id,
                                business_no=args.business_no)


def cmd_statements(svc, args):
    return svc.list_statements(_mandate(args), args.negotiation_id)


def cmd_propose(svc, args):
    return svc.propose_package(
        _mandate(args), args.negotiation_id, _load_json(args.clauses),
        assumptions=_load_json(args.assumptions),
        bases=_load_json(args.bases), note=args.note,
        business_no=args.business_no)


def cmd_confirm(svc, args):
    return svc.confirm_version(_mandate(args), args.negotiation_id,
                               args.version_hash, business_no=args.business_no)


def cmd_vote(svc, args):
    return svc.cast_vote(_mandate(args), args.negotiation_id,
                         args.version_hash, args.yes,
                         business_no=args.business_no)


def cmd_sign(svc, args):
    return svc.sign_agreement(_mandate(args), args.negotiation_id,
                              args.version_hash,
                              effective_from=args.effective_from,
                              business_no=args.business_no)


def cmd_reconsider(svc, args):
    return svc.request_reconsideration(
        _mandate(args), args.agreement_id, _load_json(args.evidence),
        title=args.title, business_no=args.business_no)


def cmd_performance(svc, args):
    return svc.append_performance(
        _mandate(args), args.agreement_id, args.period, args.kind,
        _load_json(args.content), business_no=args.business_no)


def cmd_supplement(svc, args):
    return svc.append_supplement(
        _mandate(args), args.agreement_id, args.period,
        _load_json(args.content), business_no=args.business_no)


def cmd_consent(svc, args):
    return svc.consent_supplement(_mandate(args), args.entry_id,
                                  business_no=args.business_no)


def cmd_agreements(svc, args):
    return svc.list_agreements(_mandate(args))


def cmd_agreement(svc, args):
    return svc.get_agreement(_mandate(args), args.agreement_id)


def cmd_ledger(svc, args):
    return svc.list_ledger(_mandate(args), args.agreement_id)


def cmd_dashboard(svc, args):
    return svc.dashboard(_mandate(args))


def _parse_periods(value: str) -> list[str]:
    if value.startswith(("[", "@", "-")):
        parsed = _load_json(value)
        if not isinstance(parsed, list):
            raise ValueError("周期清单必须是 JSON 数组")
        return [str(v) for v in parsed]
    return [v.strip() for v in value.split(",") if v.strip()]


def cmd_check_start(svc, args):
    return svc.start_performance_check(
        args.staff_token or os.environ.get("CB_STAFF_TOKEN", ""),
        args.agreement_id, _parse_periods(args.periods), run_id=args.run_id)


def cmd_check_resume(svc, args):
    return svc.resume_performance_check(
        args.staff_token or os.environ.get("CB_STAFF_TOKEN", ""), args.run_id)


def cmd_check_status(svc, args):
    return svc.check_status(
        args.staff_token or os.environ.get("CB_STAFF_TOKEN", ""), args.run_id)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        svc = _service(args)
        result = args.func(svc, args)
    except LedgerError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    except (json.JSONDecodeError, ValueError) as exc:
        print(f"输入错误：{exc}", file=sys.stderr)
        return 2
    _print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
