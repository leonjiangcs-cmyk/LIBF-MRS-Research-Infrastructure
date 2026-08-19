from __future__ import annotations

import json
import socket
import sys
from datetime import date

HOST = "public-api.baostock.com"
PORT = 10030
TIMEOUT_SECONDS = 30


def tcp_probe() -> dict:
    result = {"host": HOST, "port": PORT}
    try:
        ip = socket.gethostbyname(HOST)
        result["resolved_ip"] = ip
        with socket.create_connection((HOST, PORT), timeout=10):
            pass
        result["tcp"] = "PASS"
    except Exception as exc:
        result["tcp"] = "FAIL"
        result["tcp_error"] = repr(exc)
    return result


def patch_baostock_endpoint() -> dict:
    import baostock.common.contants as constants
    import baostock.login.loginout as loginout

    changed = {}
    for module_name, module in (("contants", constants), ("loginout", loginout)):
        if hasattr(module, "BAOSTOCK_SERVER_IP"):
            old = getattr(module, "BAOSTOCK_SERVER_IP")
            setattr(module, "BAOSTOCK_SERVER_IP", HOST)
            changed[f"{module_name}.BAOSTOCK_SERVER_IP"] = {"old": old, "new": HOST}
        if hasattr(module, "BAOSTOCK_SERVER_PORT"):
            old = getattr(module, "BAOSTOCK_SERVER_PORT")
            setattr(module, "BAOSTOCK_SERVER_PORT", PORT)
            changed[f"{module_name}.BAOSTOCK_SERVER_PORT"] = {"old": old, "new": PORT}
    return changed


def baostock_probe() -> dict:
    socket.setdefaulttimeout(TIMEOUT_SECONDS)
    import baostock as bs

    result = {"endpoint_patch": patch_baostock_endpoint()}
    login = bs.login()
    result["login_error_code"] = login.error_code
    result["login_error_msg"] = login.error_msg
    if login.error_code != "0":
        result["status"] = "FAIL_LOGIN"
        return result

    try:
        rs = bs.query_history_k_data_plus(
            "sh.600000",
            "date,code,close,tradestatus,pctChg,isST",
            start_date="2023-12-20",
            end_date="2023-12-29",
            frequency="d",
            adjustflag="3",
        )
        result["query_error_code"] = rs.error_code
        result["query_error_msg"] = rs.error_msg
        rows = []
        if rs.error_code == "0":
            while rs.next():
                rows.append(rs.get_row_data())
        result["row_count"] = len(rows)
        result["fields"] = list(rs.fields) if getattr(rs, "fields", None) else []
        result["max_date"] = max((row[0] for row in rows), default=None)
        result["is_st_values"] = sorted({row[-1] for row in rows}) if rows else []
        result["status"] = "PASS" if rs.error_code == "0" and rows else "FAIL_QUERY"
        return result
    finally:
        bs.logout()


def main() -> int:
    output = {
        "probe_date": date.today().isoformat(),
        "python": sys.version,
        "tcp_probe": tcp_probe(),
    }
    if output["tcp_probe"].get("tcp") != "PASS":
        output["status"] = "FAIL_TCP"
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return 2

    try:
        output["baostock"] = baostock_probe()
    except Exception as exc:
        output["baostock"] = {"status": "EXCEPTION", "error": repr(exc)}
    output["status"] = output["baostock"].get("status", "UNKNOWN")
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0 if output["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
