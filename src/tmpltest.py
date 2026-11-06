#!/usr/bin/env python3
"""Fetches a fresh template from local bytecoind, prepares it and checks the nonce plumbing:
patching the nonce into the template must reproduce the hashing blob bcnhelper derives."""
import json
import sys
import urllib.request

sys.path.insert(0, sys.argv[2] if len(sys.argv) > 2 else ".")
import bcnlib  # noqa: E402


def rpc(method, params=None):
    body = json.dumps({"jsonrpc": "2.0", "id": "0", "method": method, "params": params or {}}).encode()
    with urllib.request.urlopen(urllib.request.Request("http://127.0.0.1:8081/json_rpc", body), timeout=30) as r:
        resp = json.loads(r.read().decode())
    if "error" in resp:
        raise RuntimeError(resp["error"])
    return resp["result"]


lib = bcnlib.Helper()
cid = bytes.fromhex(rpc("get_currency_id")["currency_id_blob"])
r = rpc("get_block_template", {"wallet_address": sys.argv[1], "reserve_size": 0})
raw = bytes.fromhex(r["blocktemplate_blob"])
tmpl, hb, toff, hoff = lib.prepare_template(raw, cid)
print(f"height={r['height']} diff={r['difficulty']} tmpl={len(raw)}->{len(tmpl)} hashing={len(hb)} "
      f"tmpl_nonce_off={toff} hash_nonce_off={hoff}")
nonce = bytes.fromhex("deadbeef")
t2 = tmpl[:toff] + nonce + tmpl[toff + 4:]
_, hb2, _, _ = lib.prepare_template(t2, cid)  # re-prepare resets nonce to 0
h_patched = hb[:hoff] + nonce + hb[hoff + 4:]
print("hashing blob prefix:", hb[:43].hex())
print("prepare is idempotent:", hb2 == hb)
print("hash of patched blob:", lib.cn_slow_hash(h_patched).hex())
