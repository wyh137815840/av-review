"""Lead 独立复核：Pan115.list_children 的 aps 分页终止条件。

模拟 115 aps 文档描述的行为（p115client fs_files_aps 的 caution）：
- 每种排序最多可见前 1200+n 条（n 用 0 近似）；
- offset >= 1200+n 时服务端等价于 offset=0；
- 响应里带 count 与 offset。

保护：handler 请求超过 200 次就抛 LoopDetected，避免真的无限循环。

跑法：python3 /tmp/verify_aps_paging.py
"""

import sys

import httpx

import os
PLUGIN_DIR = os.environ.get("MP115_PLUGIN_DIR", "/workspace/mp-av/plugins.v2/av115scraper")
sys.path.insert(0, PLUGIN_DIR)


class LoopDetected(RuntimeError):
    pass


def make_rows(n):
    rows = []
    for i in range(n):
        if i % 5 == 0:
            rows.append({"cid": str(10000 + i), "n": f"folder-{i}", "fc": "0"})
        else:
            rows.append({"fid": str(20000 + i), "cid": "1", "n": f"file-{i}.mp4", "fc": "1", "s": 1000, "pc": "p" * 17})
    return rows


def run(mode, total):
    from av115_pan import Pan115

    rows = make_rows(total)
    state = {"calls": 0, "offsets": []}

    def handler(request: httpx.Request) -> httpx.Response:
        state["calls"] += 1
        if state["calls"] > 200:
            raise LoopDetected("list_children 仍在翻页（>200 次请求）")
        offset = int(request.url.params.get("offset") or 0)
        limit = int(request.url.params.get("limit") or 500)
        state["offsets"].append(offset)
        if mode == "clamp":
            if offset >= 1200:
                return httpx.Response(200, json={"state": True, "count": total, "offset": 0, "data": rows[:limit]})
            page = rows[offset:min(offset + limit, 1200)]
            return httpx.Response(200, json={"state": True, "count": total, "offset": offset, "data": page})
        # wrap：1200 以内满页；超过就从头部回绕，且仍是满页（文档描述的等价 offset=0）
        if offset >= 1200:
            return httpx.Response(200, json={"state": True, "count": total, "offset": 0, "data": rows[:limit]})
        return httpx.Response(200, json={"state": True, "count": total, "offset": offset, "data": rows[offset:offset + limit]})

    pan = Pan115("UID=1_A1_1; CID=x; SEID=y; KID=z")
    pan.http.close()
    pan.http = httpx.Client(transport=httpx.MockTransport(handler), timeout=5)
    try:
        items = pan.list_children("1")
        print(f"mode={mode} total={total}: returned={len(items)} calls={state['calls']} offsets={state['offsets'][:10]}")
    except LoopDetected as exc:
        print(f"mode={mode} total={total}: LOOP DETECTED - {exc}; offsets={state['offsets'][:10]}")
    finally:
        pan.http.close()


if __name__ == "__main__":
    run("clamp", 2000)
    run("wrap", 1300)
