#!/usr/bin/env python3
"""Сборщик живых цифр «Добро» для штаба.

Раз в полчаса (launchd com.shtab.stats) забирает из ЦРМ waterapp сводку дня
(заказы, доставки, выручка, бутыли, новые клиенты) и публикует stats.json
в репо штаба. Адрес ЦРМ с секретом лежит в ~/штаб/.waterapp-url (вне гита).
"""

import datetime
import json
import os
import subprocess
import urllib.request

BASE = os.path.expanduser("~/штаб")
URL = open(os.path.join(BASE, ".waterapp-url"), encoding="utf-8").read().strip()
SESSION = {"id": None}
_rid = [0]


def post(payload):
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if SESSION["id"]:
        headers["Mcp-Session-Id"] = SESSION["id"]
    req = urllib.request.Request(URL, data=json.dumps(payload).encode(), headers=headers)
    with urllib.request.urlopen(req, timeout=90) as r:
        sid = r.headers.get("Mcp-Session-Id")
        if sid:
            SESSION["id"] = sid
        body = r.read().decode("utf-8", "replace")
        ctype = r.headers.get("Content-Type", "")
    if "text/event-stream" in ctype:
        for line in body.splitlines():
            if line.startswith("data:"):
                data = line[5:].strip()
                if data:
                    try:
                        obj = json.loads(data)
                        if obj.get("id") is not None:
                            return obj
                    except ValueError:
                        pass
        return None
    if body.strip():
        return json.loads(body)
    return None


def rpc(method, params=None, notify=False):
    if notify:
        post({"jsonrpc": "2.0", "method": method, "params": params or {}})
        return None
    _rid[0] += 1
    resp = post({"jsonrpc": "2.0", "id": _rid[0], "method": method, "params": params or {}})
    if resp is None:
        raise RuntimeError("нет ответа на " + method)
    if "error" in resp:
        raise RuntimeError(str(resp["error"])[:300])
    return resp["result"]


def tool(name, args):
    res = rpc("tools/call", {"name": name, "arguments": args})
    for c in res.get("content", []):
        if c.get("type") == "text":
            return json.loads(c["text"])
    if "structuredContent" in res:
        return res["structuredContent"]
    raise RuntimeError("пустой ответ инструмента " + name)


def day_stats(day):
    ds = day.strftime("%d.%m.%Y")
    orders, total, page = [], None, 1
    while True:
        r = tool("list_orders", {"start": ds, "stop": ds, "count": 100, "page": page})
        chunk = r.get("orders", [])
        orders += chunk
        total = r.get("count", len(orders))
        if len(orders) >= total or not chunk or page > 10:
            break
        page += 1
    delivered = sum(1 for o in orders if o.get("status") == "Доставлен")
    revenue = sum(o.get("total_sum") or 0 for o in orders)
    bottles = 0
    for o in orders:
        for it in o.get("items") or []:
            if "19" in (it.get("title") or ""):
                bottles += it.get("quantity") or 0
    return {"orders": total, "delivered": delivered, "revenue": revenue, "bottles": bottles}


def main():
    rpc("initialize", {
        "protocolVersion": "2025-03-26",
        "capabilities": {},
        "clientInfo": {"name": "shtab-stats", "version": "1.0"},
    })
    rpc("notifications/initialized", {}, notify=True)

    today = datetime.date.today()
    yesterday = today - datetime.timedelta(days=1)
    week_ago = today - datetime.timedelta(days=7)

    clients = tool("count_clients", {"created_at_from": week_ago.strftime("%d.%m.%Y")})
    if isinstance(clients, dict):
        new_clients = clients.get("count", clients.get("total"))
    else:
        new_clients = clients

    stats = {
        "updated": datetime.datetime.now().strftime("%d.%m %H:%M"),
        "today": day_stats(today),
        "yesterday": day_stats(yesterday),
        "new_clients_7d": new_clients,
    }

    with open(os.path.join(BASE, "stats.json"), "w", encoding="utf-8") as f:
        json.dump(stats, f, ensure_ascii=False, indent=1)

    def git(*args):
        subprocess.run(["git", "-C", BASE] + list(args), capture_output=True, timeout=120)

    git("add", "stats.json")
    git("commit", "-m", "Живые цифры Добро")
    git("pull", "--rebase", "origin", "main")
    git("push", "origin", "main")


if __name__ == "__main__":
    main()
