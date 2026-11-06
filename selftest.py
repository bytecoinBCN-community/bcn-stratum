#!/usr/bin/env python3
"""Checks bcnhelper against real network blocks: our PoW hashing blob + CryptoNight
must give a hash that satisfies each block's recorded difficulty, and the root nonce
must sit at byte 39 where ASICs expect it."""
import json
import sys
import urllib.request
import ssl

sys.path.insert(0, sys.argv[1] if len(sys.argv) > 1 else ".")
import bcnlib  # noqa: E402

LOCAL = "http://127.0.0.1:8081/json_rpc"
REMOTE = "https://node01.bytecoin.org:8091/json_rpc"


def rpc(url, method, params=None):
    body = json.dumps({"jsonrpc": "2.0", "id": "0", "method": method, "params": params or {}}).encode()
    req = urllib.request.Request(url, body, {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60, context=ssl.create_default_context()) as r:
        resp = json.loads(r.read().decode())
    if "error" in resp:
        raise RuntimeError(resp["error"])
    return resp["result"]


lib = bcnlib.Helper()
cid = bytes.fromhex(rpc(LOCAL, "get_currency_id")["currency_id_blob"])
top = rpc(REMOTE, "get_status")["top_block_height"]
ok = 0
for h in (top - 5, top - 500, top - 50000, 1800000):
    blk = rpc(REMOTE, "get_raw_block", {"height_or_depth": h})["block"]
    hb = lib.hashing_blob_from_json(json.dumps(blk["raw_header"]), cid)
    pow_hash = lib.cn_slow_hash(hb)
    diff = blk["header"]["difficulty"]
    good = lib.check_hash(pow_hash, diff)
    nonce_ok = hb[39:43] == bytes.fromhex(blk["header"]["binary_nonce"])
    print(f"height={h} v{blk['header']['major_version']} diff={diff} blob_len={len(hb)} "
          f"pow_ok={good} nonce_at_39={nonce_ok}")
    ok += good and nonce_ok
print("SELFTEST", "PASSED" if ok == 4 else "FAILED")
sys.exit(0 if ok == 4 else 1)
