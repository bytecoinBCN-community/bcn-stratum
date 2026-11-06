#!/usr/bin/env python3
"""End-to-end test without a synced node.

fake bytecoind: serves a REAL v4 mainnet block (from node01) as the block template, with tiny
difficulty, and validates every submit_block by recomputing PoW from the submitted blob.
test miner: speaks the same Monero-style stratum as the X3 (login/job/submit), puts the nonce
at byte 39 of the blob and grinds CryptoNight on CPU.
"""
import json
import socket
import ssl
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

import bcnlib

NET_DIFF, SHARE_DIFF, FAKE_PORT, STRATUM_PORT = 80, 40, 18081, 13333
lib = bcnlib.Helper()


def remote(method, params=None):
    body = json.dumps({"jsonrpc": "2.0", "id": "0", "method": method, "params": params or {}}).encode()
    with urllib.request.urlopen(urllib.request.Request("https://node01.bytecoin.org:8091/json_rpc", body),
                                timeout=60, context=ssl.create_default_context()) as r:
        return json.loads(r.read().decode())["result"]


def local(method):
    body = json.dumps({"jsonrpc": "2.0", "id": "0", "method": method, "params": {}}).encode()
    with urllib.request.urlopen(urllib.request.Request("http://127.0.0.1:8081/json_rpc", body), timeout=30) as r:
        return json.loads(r.read().decode())["result"]


CID = local("get_currency_id")["currency_id_blob"]
top = remote("get_status")["top_block_height"]
blk = remote("get_raw_block", {"height_or_depth": top})["block"]
TEMPLATE = lib.template_from_json(json.dumps(blk["raw_header"]))
submitted = []


class Fake(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        req = json.loads(self.rfile.read(int(self.headers["Content-Length"])).decode())
        m, p = req["method"], req.get("params", {})
        if m == "get_currency_id":
            res = {"currency_id_blob": CID}
        elif m == "get_status":
            res = {"top_block_hash": "aa" * 32, "top_block_height": top, "top_known_block_height": top,
                   "outgoing_peer_count": 8}
        elif m == "get_block_template":
            res = {"difficulty": NET_DIFF, "height": top + 1, "reserved_offset": 0,
                   "blocktemplate_blob": TEMPLATE.hex()}
        elif m == "submit_block":
            blob = bytes.fromhex(p["blocktemplate_blob"])
            pow_hash = lib.cn_slow_hash(lib.hashing_blob(blob, bytes.fromhex(CID)))
            ok = lib.check_hash(pow_hash, NET_DIFF)
            submitted.append(ok)
            res = {"block_header": {"height": top + 1, "hash": pow_hash.hex(), "reward": 1}, "orphan_status": False}
            if not ok:
                return self.reply({"jsonrpc": "2.0", "id": req["id"], "error": {"code": -7, "message": "bad pow"}})
        else:
            res = {}
        self.reply({"jsonrpc": "2.0", "id": req["id"], "result": res})

    def reply(self, obj):
        data = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


threading.Thread(target=HTTPServer(("127.0.0.1", FAKE_PORT), Fake).serve_forever, daemon=True).start()
bridge = subprocess.Popen([sys.executable, "bcn_stratum.py", "--wallet-address", "test",
                          "--bytecoind", "http://127.0.0.1:%d/json_rpc" % FAKE_PORT,
                          "--listen", "127.0.0.1:%d" % STRATUM_PORT, "--share-diff", "1000",
                          "--min-diff", "1", "--refresh", "3600", "--verify-ratio", "1"],
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
time.sleep(3)

s = socket.create_connection(("127.0.0.1", STRATUM_PORT))
f = s.makefile("rw")


job = blob = target = None
jobs_seen = 0


def set_job(j):
    global job, blob, target, jobs_seen
    job, blob = j, bytearray.fromhex(j["blob"])
    target = int.from_bytes(bytes.fromhex(j["target"]), "little")
    jobs_seen += 1


def call(obj):
    f.write(json.dumps(obj) + "\n")
    f.flush()
    while True:
        msg = json.loads(f.readline())
        if msg.get("method") == "job":  # pushed after a found block, like a real pool
            set_job(msg["params"])
            continue
        return msg


login = call({"id": 1, "jsonrpc": "2.0", "method": "login",
              "params": {"login": "x3-test", "pass": "d=%d" % SHARE_DIFF, "agent": "e2e"}})
set_job(login["result"]["job"])
print("login ok, blob_len=%d target=%s height=%s" % (len(blob), job["target"], job["height"]))
assert job["target"] == ((2 ** 32 - 1) // SHARE_DIFF).to_bytes(4, "little").hex()

accepted = rejected = 0
dup = {}
nonce = 0
submitted_before = 0
t0 = time.time()
while (accepted < 3 or not submitted) and time.time() - t0 < 300:
    nonce += 1
    nb = nonce.to_bytes(4, "little")
    blob[39:43] = nb
    h = lib.cn_slow_hash(bytes(blob))
    if int.from_bytes(h[28:32], "little") <= target:  # standard X3/xmrig share test
        r = call({"id": 2, "jsonrpc": "2.0", "method": "submit",
                  "params": {"id": login["result"]["id"], "job_id": job["job_id"], "nonce": nb.hex(),
                             "result": h.hex()}})
        if r.get("error"):
            rejected += 1
            print("share rejected:", r["error"])
        else:
            accepted += 1
            print("share accepted nonce=%s" % nb.hex())
            if accepted == 1 and submitted_before == len(submitted):
                dup = call({"id": 4, "jsonrpc": "2.0", "method": "submit",
                            "params": {"job_id": job["job_id"], "nonce": nb.hex(), "result": h.hex()}})
                print("duplicate ->", dup.get("error"))
            time.sleep(0.5)
            if submitted_before != len(submitted):  # found a block -> the bridge pushes a clean job
                msg = json.loads(f.readline())
                assert msg.get("method") == "job", msg
                set_job(msg["params"])
                print("new job after block: %s" % job["job_id"])
        submitted_before = len(submitted)
# a wrong hash and a duplicate must both be refused
bad = call({"id": 3, "jsonrpc": "2.0", "method": "submit",
            "params": {"job_id": job["job_id"], "nonce": "00000001", "result": "00" * 32}})
time.sleep(2)
bridge.terminate()
out = bridge.communicate()[0].decode()
print("--- bridge log ---\n" + out)
print("accepted=%d rejected=%d bad_refused=%s dup_refused=%s blocks_submitted=%s jobs_seen=%d" % (
    accepted, rejected, bool(bad.get("error")), bool(dup.get("error")), submitted, jobs_seen))
ok = accepted >= 3 and rejected == 0 and bad.get("error") and dup.get("error") and submitted and all(submitted)
print("E2E", "PASSED" if ok else "FAILED")
sys.exit(0 if ok else 1)
