#!/usr/bin/env python3
"""Solo stratum bridge: Antminer X3 (CryptoNight v0, Monero-style stratum) <-> bytecoind v3.5 (Amethyst, block v4).

Each miner connection gets its own block template (bytecoind puts a fresh random coinbase key into
every template), so several ASICs never search the same nonce space. Block parsing, PoW hashing blob
and CryptoNight all come from bytecoin's own code via libbcnhelper.so.

Block limiter: one X3 can have several times the hashrate of the rest of the network. If all its blocks
reached the chain, difficulty would follow it up and an X3 power cut would stall the network for days. The bridge
therefore submits only a share of the blocks it finds (the rest are dropped), so the X3 counts on chain
as ratio * its real hashrate. The limit file (re-read every minute) sets either a fixed ratio or a
target share of the network that the bridge holds by tuning the ratio as other miners come and go.
"""
import argparse
import asyncio
import concurrent.futures
import json
import logging
import os
import random
import time
import urllib.request

import bcnlib

log = logging.getLogger("bcn-stratum")
JOBS_KEPT = 4  # per connection; older job ids are rejected as unknown
BLOCK_TARGET = 120  # seconds; bytecoind DIFFICULTY_TARGET
DIFFICULTY_SPAN = 24 * 3600  # bytecoind difficulty window: 720 blocks of 2 minutes


class Limiter:
    """Decides which found blocks go to the network.

    Limit file, "key = value" lines, '#' comments:
      submit_ratio  share of found blocks submitted, 0 < r <= 1 (fixed mode; start value in auto mode)
      target_share  0 < s < 1 turns on auto mode: keep our on-chain hashrate at s of the network
      min_ratio, max_ratio   bounds for auto mode
      adjust_hours  how often auto mode may change the ratio, at most x1.25 per step
    No file means ratio 1 (every block submitted).
    """
    DEFAULTS = {"submit_ratio": 1.0, "target_share": 0.0, "min_ratio": 0.005, "max_ratio": 1.0, "adjust_hours": 6.0}
    STEP = 1.25
    # Our raw hashrate is measured from block candidates (each one stands for net_diff hashes on
    # average) once there are enough of them: an X3 ignores the share target and sends only its own
    # much harder shares, so share work undercounts it ~30x. Share work is the fallback for slow miners.
    MIN_CANDIDATES = 10

    def __init__(self, path):
        self.path, self.mtime, self.cfg = path, None, dict(self.DEFAULTS)
        self.ratio = 1.0
        self.credit = 0.0
        self.history = []  # (time, share work, candidate work, candidates, ratio, seconds) per report period
        self.last_adjust = time.time()
        self.started = time.time()
        self.found = self.submitted = 0
        self.reload(force=True)

    def reload(self, force=False):
        if not self.path:
            return
        try:
            mtime = os.path.getmtime(self.path)
        except OSError:
            if force:
                log.warning("limit file %s not found, every block is submitted", self.path)
            return
        if mtime == self.mtime and not force:
            return
        cfg = dict(self.DEFAULTS)
        try:
            with open(self.path) as f:
                for line in f:
                    line = line.split("#", 1)[0].strip()
                    if "=" in line:
                        k, v = (x.strip() for x in line.split("=", 1))
                        if k in cfg:
                            cfg[k] = float(v)
                        else:
                            log.warning("limit file: unknown key %s", k)
        except (OSError, ValueError) as ex:
            log.error("limit file %s unreadable, keeping previous settings: %s", self.path, ex)
            return
        cfg["submit_ratio"] = min(max(cfg["submit_ratio"], 0.0001), 1.0)
        cfg["min_ratio"] = min(max(cfg["min_ratio"], 0.0001), 1.0)
        cfg["max_ratio"] = min(max(cfg["max_ratio"], cfg["min_ratio"]), 1.0)
        changed_ratio = self.mtime is None or cfg["submit_ratio"] != self.cfg["submit_ratio"]
        self.mtime, self.cfg = mtime, cfg
        if not self.auto or changed_ratio:  # an edited ratio is applied right away, also as auto start
            self.ratio = cfg["submit_ratio"]
            self.last_adjust = time.time()
        log.info("limit settings: %s, ratio now %.4f", ", ".join("%s=%g" % kv for kv in sorted(cfg.items())),
                 self.ratio)

    @property
    def auto(self):
        return 0 < self.cfg["target_share"] < 1

    def should_submit(self):
        """Deterministic: over any run of blocks exactly `ratio` of them are submitted."""
        self.found += 1
        self.credit += self.ratio
        if self.credit >= 1:
            self.credit -= 1
            self.submitted += 1
            return True
        return False

    def record(self, share_work, cand_work, cands, seconds):
        now = time.time()
        self.history.append((now, share_work, cand_work, cands, self.ratio, seconds))
        self.history = [h for h in self.history if now - h[0] <= DIFFICULTY_SPAN]

    def _work(self, entries):
        """-> per-entry raw work, from candidates if there are enough of them in `entries`."""
        by_cands = sum(h[3] for h in entries) >= self.MIN_CANDIDATES
        return [h[2] if by_cands else h[1] for h in entries]

    def estimate(self, net_diff):
        """-> (our raw H/s, our on-chain H/s averaged over the difficulty window, others H/s) or None."""
        span = sum(h[5] for h in self.history)
        recent = [h for h in self.history if time.time() - h[0] <= 3600]
        recent_span = sum(h[5] for h in recent)
        if span <= 0 or recent_span <= 0 or not net_diff:
            return None
        raw_now = sum(self._work(recent)) / recent_span
        onchain = sum(w * h[4] for w, h in zip(self._work(self.history), self.history)) / span
        others = max(net_diff / BLOCK_TARGET - onchain, 0.0)
        return raw_now, onchain, others

    def adjust(self, net_diff):
        if not self.auto:
            return
        now = time.time()
        if now - self.started < DIFFICULTY_SPAN or now - self.last_adjust < self.cfg["adjust_hours"] * 3600:
            return  # difficulty has not caught up with our current ratio yet
        est = self.estimate(net_diff)
        if not est or est[0] <= 0:
            return
        raw_now, _, others = est
        s = self.cfg["target_share"]
        want = s / (1 - s) * others / raw_now
        new = min(max(want, self.ratio / self.STEP, self.cfg["min_ratio"]), self.ratio * self.STEP,
                  self.cfg["max_ratio"])
        self.last_adjust = now
        if abs(new - self.ratio) > 1e-9:
            log.info("auto limit: others~%.1f KH/s, ours raw~%.1f KH/s, ratio %.4f -> %.4f (wanted %.4f)",
                     others / 1000, raw_now / 1000, self.ratio, new, want)
            self.ratio = new


class Daemon:
    def __init__(self, url):
        self.url = url

    def call(self, method, params=None):
        body = json.dumps({"jsonrpc": "2.0", "id": "0", "method": method, "params": params or {}}).encode()
        req = urllib.request.Request(self.url, body, {"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            resp = json.loads(r.read().decode())
        if "error" in resp:
            raise RuntimeError("%s: %s" % (method, resp["error"].get("message", resp["error"])))
        return resp["result"]


def target_hex(difficulty):
    """Compact 32-bit stratum target, little-endian hex, as Monero-style miners expect."""
    return ((2 ** 32 - 1) // difficulty).to_bytes(4, "little").hex()


class Job:
    __slots__ = ("job_id", "height", "net_diff", "template", "hashing", "toff", "hoff", "seen")

    def __init__(self, job_id, height, net_diff, template, hashing, toff, hoff):
        self.job_id, self.height, self.net_diff = job_id, height, net_diff
        self.template, self.hashing, self.toff, self.hoff = template, hashing, toff, hoff
        self.seen = set()


class Bridge:
    def __init__(self, args):
        self.args = args
        self.daemon = Daemon(args.bytecoind)
        self.helper = bcnlib.Helper()
        self.limiter = Limiter(args.limit_file)
        self.net_diff = 0
        self.loop = asyncio.new_event_loop()  # Python 3.14 no longer creates one implicitly
        asyncio.set_event_loop(self.loop)
        self.rpc_pool = concurrent.futures.ThreadPoolExecutor(max_workers=4)
        self.hash_pool = concurrent.futures.ThreadPoolExecutor(max_workers=args.hash_threads)
        self.currency_id = None
        self.top_hash = None
        self.height = 0
        self.synced = False
        self.sessions = set()
        self.job_seq = 0
        self.stats = {"accepted": 0, "rejected": 0, "work": 0, "cand_work": 0, "cands": 0, "blocks": 0,
                      "since": time.time()}

    async def rpc(self, method, params=None):
        return await self.loop.run_in_executor(self.rpc_pool, self.daemon.call, method, params)

    async def new_job(self):
        r = await self.rpc("get_block_template", {"wallet_address": self.args.wallet_address, "reserve_size": 0})
        tmpl, hashing, toff, hoff = self.helper.prepare_template(bytes.fromhex(r["blocktemplate_blob"]),
                                                                 self.currency_id)
        if len(hashing) < hoff + 4 or hoff != 39:
            log.warning("unexpected nonce offset %d in hashing blob (ASICs expect 39)", hoff)
        self.job_seq += 1
        self.net_diff = r["difficulty"]
        return Job("%x" % self.job_seq, r["height"], r["difficulty"], tmpl, hashing, toff, hoff)

    async def watch_daemon(self):
        """Polls bytecoind; on a new top block (or every refresh interval) pushes fresh jobs."""
        last_refresh = 0.0
        while True:
            try:
                if self.currency_id is None:
                    self.currency_id = bytes.fromhex((await self.rpc("get_currency_id"))["currency_id_blob"])
                st = await self.rpc("get_status")
                synced = st["top_block_height"] + 1 >= st["top_known_block_height"] and st["outgoing_peer_count"] > 0
                if synced != self.synced:
                    log.info("bytecoind %s (height %d / %d)", "synced" if synced else "NOT synced, jobs paused",
                             st["top_block_height"], st["top_known_block_height"])
                    self.synced = synced
                changed = st["top_block_hash"] != self.top_hash
                if changed:
                    self.top_hash, self.height = st["top_block_hash"], st["top_block_height"]
                if self.synced and (changed or time.time() - last_refresh >= self.args.refresh):
                    last_refresh = time.time()
                    for s in list(self.sessions):
                        await s.push_job(clean=changed)
            except Exception as ex:  # keep serving; bytecoind restarts are normal
                log.error("daemon poll failed: %s", ex)
                await asyncio.sleep(5)
            await asyncio.sleep(self.args.poll)

    async def report(self):
        while True:
            await asyncio.sleep(60)
            s, lim = self.stats, self.limiter
            dt = max(time.time() - s["since"], 1)
            lim.reload()
            if self.sessions:
                lim.record(s["work"], s["cand_work"], s["cands"], dt)
                lim.adjust(self.net_diff)
            est = lim.estimate(self.net_diff)
            log.info("miners=%d accepted=%d rejected=%d hashrate~%.1f KH/s (by shares) blocks=%d height=%d "
                     "limit[%s ratio=%.4f found=%d submitted=%d raw~%s on-chain~%s others~%s]",
                     len(self.sessions), s["accepted"], s["rejected"], s["work"] / dt / 1000, s["blocks"],
                     self.height, "auto %.0f%%" % (lim.cfg["target_share"] * 100) if lim.auto else "fixed",
                     lim.ratio, lim.found, lim.submitted,
                     *("%.1fKH/s" % (e / 1000) if est else "?" for e in (est or (0, 0, 0))))
            s.update(accepted=0, rejected=0, work=0, cand_work=0, cands=0, since=time.time())

    async def check_share(self, job, nonce):
        blob = job.hashing[:job.hoff] + nonce + job.hashing[job.hoff + 4:]
        return await self.loop.run_in_executor(self.hash_pool, self.helper.cn_slow_hash, blob)

    async def submit_block(self, job, nonce, worker):
        tmpl = job.template[:job.toff] + nonce + job.template[job.toff + 4:]
        try:
            r = await self.rpc("submit_block", {"blocktemplate_blob": tmpl.hex()})
            hdr = r.get("block_header", {})
            self.stats["blocks"] += 1
            log.info("BLOCK ACCEPTED height=%s hash=%s reward=%s orphan=%s worker=%s", hdr.get("height"),
                     hdr.get("hash"), hdr.get("reward"), r.get("orphan_status"), worker)
        except Exception as ex:
            log.error("block REJECTED by bytecoind at height %d: %s", job.height, ex)
        self.top_hash = None  # force immediate refresh


class Session:
    def __init__(self, bridge, reader, writer):
        self.b, self.reader, self.writer = bridge, reader, writer
        self.peer = "%s:%s" % writer.get_extra_info("peername")[:2]
        self.worker = "?"
        self.diff = bridge.args.share_diff
        self.jobs = []
        self.logged_in = False

    def send(self, obj):
        self.writer.write((json.dumps(obj) + "\n").encode())

    def job_params(self, job):
        return {"blob": job.hashing.hex(), "job_id": job.job_id, "target": target_hex(self.diff),
                "height": job.height, "algo": "cn/0"}

    async def push_job(self, clean=False):
        if not self.logged_in:
            return
        try:
            job = await self.b.new_job()
        except Exception as ex:
            log.error("get_block_template failed: %s", ex)
            return
        if clean:
            self.jobs.clear()
        self.jobs = (self.jobs + [job])[-JOBS_KEPT:]
        self.send({"jsonrpc": "2.0", "method": "job", "params": self.job_params(job)})

    async def on_login(self, rid, params):
        if not self.b.synced or self.b.currency_id is None:
            return self.send({"id": rid, "jsonrpc": "2.0", "error": {"code": -1, "message": "node not synced"}})
        self.worker = str(params.get("login", "?"))[:64]
        for part in str(params.get("pass", "")).split(","):  # optional "d=NNNN" sets share difficulty
            if part.startswith("d=") and part[2:].isdigit():
                self.diff = max(self.b.args.min_diff, int(part[2:]))
        job = await self.b.new_job()
        self.jobs = [job]
        self.logged_in = True
        log.info("login %s worker=%s agent=%s diff=%d", self.peer, self.worker, params.get("agent", ""), self.diff)
        self.send({"id": rid, "jsonrpc": "2.0", "error": None,
                   "result": {"id": self.peer, "job": self.job_params(job), "status": "OK", "extensions": []}})

    async def on_submit(self, rid, params):
        def reply(err=None):
            if err:
                self.b.stats["rejected"] += 1
                log.warning("share rejected worker=%s: %s", self.worker, err)
                self.send({"id": rid, "jsonrpc": "2.0", "error": {"code": -1, "message": err}})
            else:
                self.send({"id": rid, "jsonrpc": "2.0", "error": None, "result": {"status": "OK"}})

        job = next((j for j in self.jobs if j.job_id == params.get("job_id")), None)
        if job is None:
            return reply("unknown job")
        if job.height <= self.b.height:  # a block at this height already exists
            return reply("stale job")
        try:
            nonce = bytes.fromhex(params.get("nonce", ""))
        except ValueError:
            nonce = b""
        if len(nonce) != 4:
            return reply("bad nonce")
        if nonce in job.seen:
            return reply("duplicate share")
        job.seen.add(nonce)
        # CryptoNight costs ~0.1 s on this VM, so the miner's own result is trusted for ordinary
        # shares; a random sample and every block candidate are recomputed.
        try:
            claimed = bytes.fromhex(params.get("result") or "")
        except ValueError:
            claimed = b""
        if len(claimed) != 32 or random.random() < self.b.args.verify_ratio or \
                self.b.helper.check_hash(claimed, job.net_diff):
            h = await self.b.check_share(job, nonce)
            if claimed and claimed != h:
                return reply("wrong hash (miner/algorithm mismatch?)")
        else:
            h = claimed
        if not self.b.helper.check_hash(h, self.diff):
            return reply("low difficulty share")
        self.b.stats["accepted"] += 1
        self.b.stats["work"] += self.diff
        reply()
        if self.b.helper.check_hash(h, job.net_diff):
            self.b.stats["cand_work"] += job.net_diff
            self.b.stats["cands"] += 1
            if self.b.limiter.should_submit():
                log.info("block candidate from %s at height %d, submitting", self.worker, job.height)
                await self.b.submit_block(job, nonce, self.worker)
            else:
                log.info("block candidate from %s at height %d withheld by limit (ratio %.4f)", self.worker,
                         job.height, self.b.limiter.ratio)

    async def run(self):
        self.b.sessions.add(self)
        log.info("connect %s", self.peer)
        try:
            while True:
                line = await asyncio.wait_for(self.reader.readline(), timeout=self.b.args.idle_timeout)
                if not line:
                    break
                try:
                    msg = json.loads(line.decode())
                    rid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
                except (ValueError, AttributeError):
                    log.warning("bad json from %s", self.peer)
                    break
                if method == "login":
                    await self.on_login(rid, params)
                elif method == "submit" and self.logged_in:
                    await self.on_submit(rid, params)
                elif method == "getjob" and self.logged_in:
                    self.send({"id": rid, "jsonrpc": "2.0", "error": None, "result": self.job_params(self.jobs[-1])})
                elif method == "keepalived":
                    self.send({"id": rid, "jsonrpc": "2.0", "error": None, "result": {"status": "KEEPALIVED"}})
                else:
                    self.send({"id": rid, "jsonrpc": "2.0", "error": {"code": -1, "message": "unsupported"}})
                await self.writer.drain()
        except (asyncio.TimeoutError, ConnectionError):
            pass
        except Exception:
            log.exception("session %s crashed", self.peer)
        finally:
            self.b.sessions.discard(self)
            self.writer.close()
            log.info("disconnect %s worker=%s", self.peer, self.worker)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--wallet-address", default=os.environ.get("BCN_WALLET_ADDRESS"), help="address for block rewards")
    p.add_argument("--bytecoind", default="http://127.0.0.1:8081/json_rpc")
    p.add_argument("--listen", default="0.0.0.0:3333")
    p.add_argument("--share-diff", type=int, default=60000, help="X3 at ~220 KH/s gives ~3.7 shares/s at 60000")
    p.add_argument("--refresh", type=float, default=30, help="seconds between template refreshes (new txs)")
    p.add_argument("--poll", type=float, default=1, help="seconds between bytecoind status polls")
    p.add_argument("--min-diff", type=int, default=1000, help="lowest share difficulty a miner may request")
    p.add_argument("--verify-ratio", type=float, default=0.05, help="share of ordinary shares recomputed")
    p.add_argument("--hash-threads", type=int, default=1)
    p.add_argument("--idle-timeout", type=float, default=600)
    p.add_argument("--limit-file", default=os.environ.get("BCN_LIMIT_FILE"),
                   help="block limiter settings, re-read every minute (see Limiter)")
    args = p.parse_args()
    if not args.wallet_address:
        p.error("--wallet-address (or BCN_WALLET_ADDRESS) is required")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    bridge = Bridge(args)
    host, port = args.listen.rsplit(":", 1)
    loop = bridge.loop
    server = loop.run_until_complete(
        asyncio.start_server(lambda r, w: Session(bridge, r, w).run(), host, int(port)))
    log.info("stratum listening on %s, bytecoind %s, share diff %d", args.listen, args.bytecoind, args.share_diff)
    loop.create_task(bridge.watch_daemon())
    loop.create_task(bridge.report())
    try:
        loop.run_forever()
    finally:
        server.close()


if __name__ == "__main__":
    main()
